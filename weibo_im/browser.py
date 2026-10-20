"""浏览器传输层 — 用持久化 profile 承载微博登录态

为什么需要这一层
----------------
微博 Web IM 的私有接口要求有效的 SUB cookie。传统做法（HTTP 模式）是把
扫码得到的 cookie 字符串存进 SQLite，再用 ``requests.Session`` 携带。但那
个字符串是**静态快照**，实测约 6 天失效（见 crawler._RENEW_THRESHOLD_SEC
附近的注释），到期必须重新扫码。

这里换一种承载方式：

    launch_persistent_context(user_data_dir=...)   登录态落到磁盘目录
    ctx.request.get(...)                           借该身份发请求，不渲染页面

请求始终在同一个浏览器会话内发出，微博的 SSO 续期链（响应里的 Set-Cookie）
由浏览器自动接收并写回 profile，因此不需要人工搬运，也不需要定期续期。

与 HTTP 模式的关系
------------------
本模块提供一个 ``requests.Session`` 的鸭子类型（``.request`` / ``.cookies``），
``Crawler`` 只需替换 ``self.session``，上层的 ``fetch_contacts`` /
``fetch_messages`` 以及退避重试逻辑全部原样复用。

安全注意
--------
profile 目录里存的是**完整登录态**（比数据库里的明文快照更敏感）。
目录权限固定 700，且必须排除在版本控制与云同步之外。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import requests

from .crawler import HEADERS as API_HEADERS

log = logging.getLogger("weibo_im.browser")

DEFAULT_PROFILE_DIR = Path.home() / ".weibo-profile"

CHAT_URL = "https://api.weibo.com/chat"
CONTACTS_URL = "https://api.weibo.com/webim/2/direct_messages/contacts.json"
SOURCE = "209678993"

# 无头模式下 Chromium 的默认 UA 会带 "HeadlessChrome"，与真实浏览器差异明显。
# 统一成常见桌面 Chrome UA，与 HTTP 模式的 HEADERS 保持一致口径。
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_active: "BrowserSession | None" = None


class BrowserResponse:
    """把 Playwright 的 APIResponse 包装成 requests.Response 的常用子集。

    只实现上层实际用到的属性：status_code / url / headers / text / json() /
    raise_for_status()。其中 raise_for_status 抛的是 requests.HTTPError，
    以便复用 _request_with_retry 里现成的 4xx/5xx 分支。
    """

    __slots__ = ("_resp", "status_code", "url", "headers")

    def __init__(self, resp):
        self._resp = resp
        self.status_code = resp.status
        self.url = resp.url
        self.headers = resp.headers

    @property
    def text(self) -> str:
        return self._resp.text()

    def json(self):
        return self._resp.json()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} Error for url: {self.url}", response=self
            )


class _CookieJar:
    """context.cookies() 的轻量只读代理，兼容 ``for c in jar`` / ``jar.get(name)``。

    浏览器模式下不会走续期链，这个代理只为兼容可能的外部调用方（例如
    _serialize_cookies）而存在。
    """

    def __init__(self, ctx):
        self._ctx = ctx

    def _items(self):
        try:
            return self._ctx.cookies()
        except Exception:
            return []

    def get(self, name: str, default: str | None = None) -> str | None:
        for c in self._items():
            if c.get("name") == name:
                return c.get("value")
        return default

    def __iter__(self):
        for c in self._items():
            yield type("Cookie", (), {"name": c.get("name"),
                                      "value": c.get("value")})()


class BrowserSession:
    """伪装成 requests.Session 的持久化浏览器上下文。

    生命周期由 close_active() 统一收口（见 crawl.py 的 main 入口）。
    """

    def __init__(self, profile_dir: str | Path | None = None, *,
                 headless: bool = True, channel: str = "",
                 timeout_ms: int = 30000):
        global _active

        self.profile_dir = Path(profile_dir or DEFAULT_PROFILE_DIR).expanduser()
        self.headless = headless
        self.channel = channel
        self._timeout_ms = timeout_ms
        self._closed = False

        self._ensure_private_dir()

        from playwright.sync_api import (
            sync_playwright, Error as PWError, TimeoutError as PWTimeout,
        )
        self._pw_error = PWError
        self._pw_timeout = PWTimeout

        log.info("启动持久化浏览器 profile: %s", self.profile_dir)
        self._pw = sync_playwright().start()

        launch_kwargs = {
            "user_data_dir": str(self.profile_dir),
            "headless": headless,
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "viewport": {"width": 1440, "height": 900},
            "ignore_https_errors": True,
            "user_agent": _UA,
            "args": [
                # 去掉 navigator.webdriver 这一显式自动化标记，避免无谓的特征差异。
                # 目标只是让 SSO 续期链正常工作，不做任何对抗性绕过。
                "--disable-blink-features=AutomationControlled",
                "--no-default-browser-check",
                "--no-first-run",
            ],
        }
        if channel:
            launch_kwargs["channel"] = channel

        try:
            self._ctx = self._pw.chromium.launch_persistent_context(**launch_kwargs)
        except Exception:
            self._pw.stop()
            raise

        self._ctx.set_default_timeout(timeout_ms)
        self.cookies = _CookieJar(self._ctx)
        _active = self

    # ── requests.Session 兼容层 ──────────────────────────────

    def request(self, method: str, url: str, **kwargs) -> BrowserResponse:
        """转发到 ctx.request.fetch，并把参数/异常翻译成 requests 口径。"""
        pw_kwargs = {}

        if kwargs.get("params") is not None:
            pw_kwargs["params"] = kwargs["params"]

        # ctx.request 不会自动带 Origin/Referer，缺了会直接 10012 服务异常。
        # 默认套用 HTTP 模式那套请求头，调用方额外传入的再覆盖上去。
        merged_headers = dict(API_HEADERS)
        if kwargs.get("headers"):
            merged_headers.update(kwargs["headers"])
        pw_kwargs["headers"] = merged_headers

        if kwargs.get("data") is not None:
            pw_kwargs["data"] = kwargs["data"]
        if kwargs.get("json") is not None:
            pw_kwargs["json"] = kwargs["json"]

        # requests 的 timeout 单位是秒，Playwright 是毫秒
        timeout = kwargs.get("timeout")
        pw_kwargs["timeout"] = float(timeout) * 1000 if timeout else self._timeout_ms

        try:
            resp = self._ctx.request.fetch(url, method=method, **pw_kwargs)
        except self._pw_timeout as e:
            raise requests.Timeout(str(e)) from e
        except self._pw_error as e:
            raise requests.ConnectionError(str(e)) from e

        return BrowserResponse(resp)

    def get(self, url: str, **kwargs) -> BrowserResponse:
        return self.request("GET", url, **kwargs)

    def close(self):
        global _active
        if self._closed:
            return
        self._closed = True
        try:
            self._ctx.close()
        except Exception as e:
            log.debug("关闭 context 失败: %s", e)
        try:
            self._pw.stop()
        except Exception as e:
            log.debug("停止 playwright 失败: %s", e)
        if _active is self:
            _active = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ── 登录态 ───────────────────────────────────────────────

    def probe_login(self) -> bool:
        """用一个最轻的接口探测登录态，失败返回 False 而不是抛异常。"""
        try:
            resp = self._ctx.request.get(
                CONTACTS_URL,
                params={
                    "special_source": "3",
                    "add_virtual_user": "3,4",
                    "is_include_group": "0",
                    "need_back": "0,0",
                    "is_include_folder": "1",
                    "count": "50",
                    "source": SOURCE,
                    "t": str(int(time.time() * 1000)),
                },
                headers=API_HEADERS,
                timeout=self._timeout_ms,
                fail_on_status_code=False,
            )
        except Exception as e:
            log.warning("登录态探测请求失败: %s", e)
            return False

        if resp.status >= 400:
            return False

        url = (resp.url or "").lower()
        if "login.sina.com.cn" in url or "passport.weibo" in url:
            return False

        try:
            data = resp.json()
        except Exception:
            return False

        if str(data.get("error_code", "")) == "21301":
            return False

        return isinstance(data.get("contacts"), list)

    def login_interactive(self, qr_path: str | Path = "qrcode.png",
                          timeout: int = 180) -> bool:
        """有头模式下走一次扫码，登录态由浏览器直接写进 profile。

        注意：这里不提取 cookie 字符串，也不写数据库 —— 这正是与
        --renew-cookie 的本质区别。
        """
        if self.probe_login():
            log.info("profile 里已有有效登录态，无需扫码")
            return True

        log.info("打开 %s ...", CHAT_URL)
        page = self._ctx.new_page()
        try:
            page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)

            if self.probe_login():
                log.info("已登录，登录态已在 profile 中")
                return True

            page.screenshot(path=str(qr_path))
            log.info("=" * 60)
            log.info("二维码已截图 -> %s", qr_path)
            log.info("请用微博 App 扫码（最多等待 %d 秒）", timeout)
            log.info("=" * 60)

            deadline = time.time() + timeout
            while time.time() < deadline:
                time.sleep(2)
                if self.probe_login():
                    log.info("登录成功，登录态已写入 profile: %s", self.profile_dir)
                    # 留一点时间让 cookie 落盘
                    time.sleep(2)
                    return True

            log.error("扫码超时（%d 秒），请重试", timeout)
            return False
        finally:
            try:
                page.close()
            except Exception:
                pass

    # ── 内部 ─────────────────────────────────────────────────

    def _ensure_private_dir(self):
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.profile_dir.chmod(0o700)
        except OSError as e:
            log.warning("无法设置 profile 目录权限: %s", e)


def close_active():
    """关闭当前进程里打开的 BrowserSession（幂等，供进程退出兜底）。"""
    if _active is not None:
        _active.close()
