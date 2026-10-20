import logging
import runpy
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import crawl
import weibo_im.crawler as crawler_module


def _response(data, url="https://api.weibo.com/webim/test.json"):
    resp = MagicMock()
    resp.url = url
    resp.json.return_value = data
    resp.raise_for_status = MagicMock()
    return resp


def test_contacts_raises_for_explicit_cookie_error_21301():
    session = MagicMock()
    resp = _response({
        "error_code": 21301,
        "error": "Auth failed, Cookie expires or invalid.",
    })
    with patch.object(crawler_module, "_request_with_retry", return_value=resp):
        with pytest.raises(crawler_module.CookieExpiredError):
            crawler_module.fetch_contacts(session)


def test_contacts_raises_for_explicit_login_redirect():
    session = MagicMock()
    resp = _response(
        {},
        url="https://login.sina.com.cn/sso/login.php",
    )
    with patch.object(crawler_module, "_request_with_retry", return_value=resp):
        with pytest.raises(crawler_module.CookieExpiredError):
            crawler_module.fetch_contacts(session)


@pytest.mark.parametrize("data", [
    {"contacts": []},
    {"error_code": 10012, "error": "服务异常"},
])
def test_contacts_empty_or_service_error_is_not_cookie_expiry(data):
    session = MagicMock()
    with patch.object(
        crawler_module,
        "_request_with_retry",
        return_value=_response(data),
    ):
        assert crawler_module.fetch_contacts(session) == []


def test_messages_raises_for_explicit_cookie_error_21301():
    session = MagicMock()
    resp = _response({
        "error_code": 21301,
        "error": "Auth failed, Cookie expires or invalid.",
    })
    with patch.object(crawler_module, "_request_with_retry", return_value=resp):
        with pytest.raises(crawler_module.CookieExpiredError):
            crawler_module.fetch_messages(session, gid=123)


def test_messages_result_false_is_not_cookie_expiry(caplog):
    session = MagicMock()
    with patch.object(
        crawler_module,
        "_request_with_retry",
        return_value=_response({"result": False}),
    ), caplog.at_level(logging.WARNING):
        assert crawler_module.fetch_messages(session, gid=123) == []
    assert "cookie" not in caplog.text.lower()


def test_crawl_all_does_not_swallow_cookie_expiry():
    crawler = object.__new__(crawler_module.Crawler)
    crawler.sync_groups = MagicMock(return_value=[{"gid": 123, "name": "群"}])
    crawler.crawl_group = MagicMock(
        side_effect=crawler_module.CookieExpiredError("expired")
    )
    with patch.object(crawler_module, "_jitter_sleep"):
        with pytest.raises(crawler_module.CookieExpiredError):
            crawler.crawl_all()


def test_script_entrypoint_reports_renew_only_for_cookie_expiry(caplog):
    class ExpiredCrawler:
        def __init__(self, db_path, cookie="", browser=None):
            pass

        def sync_groups(self):
            raise crawler_module.CookieExpiredError("expired")

    script = Path(__file__).parents[1] / "crawl.py"
    tmpdir = tempfile.mkdtemp(prefix="wb_entry_")
    db_path = Path(tmpdir) / "test.db"
    with patch.object(crawler_module, "Crawler", ExpiredCrawler), \
         patch.object(
             sys,
             "argv",
             ["crawl.py", "--db", str(db_path), "--group-only"],
         ), caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit) as exc:
            runpy.run_path(str(script), run_name="__main__")

    assert exc.value.code == 2
    assert "uv run crawl.py --renew-cookie" in caplog.text


def test_script_entrypoint_does_not_report_renew_for_unrelated_error(caplog):
    class BrokenCrawler:
        def __init__(self, db_path, cookie="", browser=None):
            pass

        def sync_groups(self):
            raise RuntimeError("network error")

    script = Path(__file__).parents[1] / "crawl.py"
    tmpdir = tempfile.mkdtemp(prefix="wb_entry2_")
    db_path = Path(tmpdir) / "test.db"
    with patch.object(crawler_module, "Crawler", BrokenCrawler), \
         patch.object(
             sys,
             "argv",
             ["crawl.py", "--db", str(db_path), "--group-only"],
         ), caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError):
            runpy.run_path(str(script), run_name="__main__")

    assert "--renew-cookie" not in caplog.text


def test_module_keeps_main_as_the_only_entrypoint():
    assert not hasattr(crawl, "cli")


def test_renew_cookie_rejects_missing_sub_via_inspection():
    """_renew_cookie 在 set_cookie 前应有 SUB 兜底校验（对齐 weiboblog）。

    通过源码检查验证：扫码后若未提取到 SUB，应报错退出而非把残缺 cookie 写库。
    """
    source = (Path(__file__).parents[1] / "crawl.py").read_text(encoding="utf-8")
    # SUB 校验应在 set_cookie 之前
    sub_check = 'if "SUB" not in deduped'
    set_cookie_call = "set_cookie(cookie_str)"
    assert sub_check in source, "crawl.py 应有 SUB 兜底校验"
    assert set_cookie_call in source, "crawl.py 应调用 set_cookie"
    assert source.index(sub_check) < source.index(set_cookie_call), \
        "SUB 校验应在 set_cookie 之前"
