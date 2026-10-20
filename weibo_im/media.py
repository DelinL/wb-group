"""媒体文件下载 — 从 upload API 下载图片/视频"""
from __future__ import annotations

import os
import time
import json
import hashlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

from .db import (
    update_media_status, update_message_media_local_path,
    update_media_results, get_pending_media,
)

log = logging.getLogger("weibo_im.media")

# 下载根目录 — 相对于本文件: <project>/media/
MEDIA_ROOT = Path(__file__).resolve().parent.parent / "media"

# 单个媒体文件的下载上限（字节）。4 路并发且不限长时，一个异常大文件
# 就能写满磁盘；512MB 足够覆盖群内常见视频与文档。
MAX_MEDIA_BYTES = 512 * 1024 * 1024

# User-Agent 与 API 一致
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": "https://web.im.weibo.com",
    "Referer": "https://web.im.weibo.com/",
}


def _file_ext_from_url(url: str, media_type: int) -> tuple[str, str]:
    """根据 media_type 和 URL 确定文件类型和扩展名"""
    if media_type in (10, 13):
        return ("video", ".mp4")
    if media_type == 1:
        return ("image", ".jpg")
    elif media_type == 16:
        return ("redpacket", ".png")
    elif media_type == 5:
        return ("file", ".bin")  # 待响应时根据 Content-Type 修正
    else:
        return ("unknown", ".bin")


def _patch_ext_from_response(local_path: str, content_type: str,
                              content_disposition: str, fid: str = "") -> str:
    """根据响应头修正文件扩展名，优先使用 Content-Disposition 中的原始文件名"""
    import re
    from urllib.parse import unquote
    save_dir = os.path.dirname(local_path)
    # 1. 从 Content-Disposition 提取原始文件名
    if content_disposition:
        m = re.search(r'filename\*?=(?:UTF-8\'\')?\"?([^\"\n;]+)', content_disposition)
        if m:
            name = unquote(m.group(1))
            # 响应头属不可信输入：只取 basename，再剥掉反斜杠/NUL，
            # 防止 "../../x" 或绝对路径把文件写到 media 目录之外
            name = os.path.basename(name.replace("\\", "/"))
            name = name.replace("\x00", "").strip()
            if name and name not in (".", ".."):
                new_path = os.path.join(save_dir, name)
                if not os.path.isfile(new_path) and os.path.isfile(local_path):
                    os.rename(local_path, new_path)
                    return new_path
                # 冲突: {文件名}.{fid}.{ext}
                stem, ext = os.path.splitext(name)
                fallback = os.path.join(save_dir, f"{stem}.{fid}{ext}")
                if not os.path.isfile(fallback) and os.path.isfile(local_path):
                    os.rename(local_path, fallback)
                    return fallback
                # fallback 也冲突？极小概率，放弃改名
                return local_path
    # 2. 降级：从 Content-Type 判断扩展名
    ct_map = {
        "application/pdf": ".pdf",
        "application/zip": ".zip",
        "application/x-rar-compressed": ".rar",
        "application/x-7z-compressed": ".7z",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.ms-excel": ".xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/vnd.android.package-archive": ".apk",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "video/mp4": ".mp4",
    }
    ct = content_type.split(";")[0].strip().lower()
    if ct in ct_map:
        ext = ct_map[ct]
        new_path = os.path.splitext(local_path)[0] + ext
        if not os.path.isfile(new_path) and os.path.isfile(local_path):
            os.rename(local_path, new_path)
        return new_path
    return local_path


_COOKIE: str = ""


def get_cookie_or_default() -> str:
    """返回已设置的 cookie，未设置时从 DB 读取"""
    global _COOKIE
    if not _COOKIE:
        from .db import get_cookie
        _COOKIE = get_cookie()
    return _COOKIE


def set_cookie(c: str):
    global _COOKIE
    _COOKIE = c


# 每线程一个 Session：复用 TCP/TLS 连接（图片服务器同域，keep-alive 可省掉
# 每个文件的握手），同时避免多线程共享 Session 的线程安全问题。
_tls = threading.local()

# 默认并发下载数。媒体是独立 HTTPS 域名，4 路足够提速又不容易触发限流。
DEFAULT_WORKERS = 4


def _get_session() -> requests.Session:
    session = getattr(_tls, "session", None)
    if session is None:
        session = requests.Session()
        # 保持默认 TLS 证书校验（原 verify=False 已移除，2026-10-20）
        session.headers.update(HEADERS)
        adapter = HTTPAdapter(pool_connections=DEFAULT_WORKERS,
                              pool_maxsize=DEFAULT_WORKERS * 4)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _tls.session = session
    return session


def download_file(fid: str, url: str, media_type: int,
                  timeout: int = 60) -> dict:
    """下载单个媒体文件，返回 {status, local_path, file_size, md5}"""
    # 确定文件类型和扩展名
    file_type, ext = _file_ext_from_url(url, media_type)

    # 目标路径
    sub = "files" if file_type == "file" else ("videos" if file_type == "video" else "images")
    save_dir = MEDIA_ROOT / sub
    save_dir.mkdir(parents=True, exist_ok=True)
    local_path = str(save_dir / f"{fid}{ext}")

    # 如果已存在则跳过
    if os.path.isfile(local_path) and os.path.getsize(local_path) > 0:
        file_size = os.path.getsize(local_path)
        md5 = _calc_md5(local_path)
        log.debug("已存在: %s (%d bytes)", local_path, file_size)
        return {"status": "done", "local_path": local_path,
                "file_size": file_size, "md5": md5}

    if file_type == "redpacket":
        with open(local_path, "w") as f:
            f.write(f"redpacket fid={fid}")
        return {"status": "done", "local_path": local_path, "file_size": 0, "md5": ""}

    try:
        log.info("下载: %s", url)
        h = dict(HEADERS)
        ck = get_cookie_or_default()
        if ck:
            h["Cookie"] = ck
        resp = _get_session().get(
            url,
            headers=h,
            timeout=timeout,
            stream=True,
        )
        try:
            if resp.status_code != 200:
                log.warning("下载失败 [HTTP %d]: %s", resp.status_code, url)
                return {"status": "failed", "local_path": "", "file_size": 0, "md5": ""}

            # 写入文件（64KB 一块，减少 syscall）
            file_size = 0
            with open(local_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        file_size += len(chunk)
                        if file_size > MAX_MEDIA_BYTES:
                            raise ValueError(
                                f"文件超过大小上限 {MAX_MEDIA_BYTES} 字节"
                            )
        finally:
            resp.close()

        # 根据响应头修正扩展名
        ct = resp.headers.get("Content-Type", "")
        cd = resp.headers.get("Content-Disposition", "")
        corrected_path = _patch_ext_from_response(local_path, ct, cd, fid)
        if corrected_path != local_path:
            local_path = corrected_path

        md5 = _calc_md5(local_path)
        log.info("下载完成: %s (%d bytes)", local_path, file_size)
        return {"status": "done", "local_path": local_path,
                "file_size": file_size, "md5": md5}

    except Exception as e:
        log.error("下载异常: %s — %s", url, e)
        # 清掉残缺文件，避免下次因「本地已存在」被当成下载完成
        try:
            if os.path.isfile(local_path):
                os.remove(local_path)
        except OSError:
            pass
        return {"status": "failed", "local_path": "", "file_size": 0, "md5": ""}


def download_pending(limit: int = 20, cookie: str = "", workers: int | None = None):
    """下载 pending 状态的媒体文件

    多线程并发下载（纯网络 IO），下载结果回到主线程一次性批量写库，
    避免多线程争用同一个 SQLite 连接。
    """
    if cookie:
        set_cookie(cookie)
    get_cookie_or_default()  # 确保 cookie 被加载
    files = get_pending_media(limit)
    if not files:
        return 0

    workers = DEFAULT_WORKERS if workers is None else max(1, int(workers))
    tasks = [(f["fid"], f["orig_url"], f["media_type"]) for f in files]
    if workers > 1 and len(tasks) > 1:
        with ThreadPoolExecutor(max_workers=min(workers, len(tasks))) as pool:
            results = list(pool.map(lambda t: download_file(*t), tasks))
    else:
        results = [download_file(*t) for t in tasks]

    update_media_results([
        {
            "fid": f["fid"],
            "mid": f["mid"],
            "status": r["status"],
            "local_path": r["local_path"],
            "file_size": r["file_size"],
            "md5": r["md5"],
        }
        for f, r in zip(files, results)
    ])
    return len(files)


def _calc_md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
