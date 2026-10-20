"""安全回归测试：Cookie 域名白名单、文件名消毒"""
import os
import shutil
import tempfile

import pytest

from weibo_im import links, media


# ── links.py：Cookie 只发给微博系域名 ──────────────────────

def test_external_url_does_not_carry_cookie(monkeypatch):
    """外链（群友贴的任意 URL）绝不能带登录态"""
    monkeypatch.setattr(links, "get_cookie_or_default", lambda: "SUB=secret")
    h = links._request_headers("https://evil.example.com/a.pdf")
    assert "Cookie" not in h


def test_weibo_urls_carry_cookie(monkeypatch):
    monkeypatch.setattr(links, "get_cookie_or_default", lambda: "SUB=secret")
    for url in (
        "https://weibo.com/123/abc",
        "https://api.weibo.com/2/x",
        "https://wx1.sinaimg.cn/a.jpg",
        "https://t.cn/abc",
    ):
        assert links._request_headers(url).get("Cookie") == "SUB=secret", url


def test_lookalike_domain_does_not_carry_cookie(monkeypatch):
    """weibo.com.evil.com 这类伪装域名必须判为外部"""
    monkeypatch.setattr(links, "get_cookie_or_default", lambda: "SUB=secret")
    assert "Cookie" not in links._request_headers("https://weibo.com.evil.example/a")
    assert "Cookie" not in links._request_headers("https://notweibo.com/a")


# ── media.py：Content-Disposition 文件名消毒 ──────────────

@pytest.fixture
def workdir():
    d = tempfile.mkdtemp(prefix="wb_sec_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_patch_ext_blocks_path_traversal(workdir):
    """../../evil.sh 只能落在下载目录内"""
    local = os.path.join(workdir, "abc.jpg")
    with open(local, "wb") as f:
        f.write(b"x")
    out = media._patch_ext_from_response(
        local, "image/jpeg", 'attachment; filename="../../evil.sh"', "fid1"
    )
    assert os.path.dirname(out) == workdir
    assert not os.path.exists(os.path.join(os.path.dirname(workdir), "evil.sh"))


def test_patch_ext_blocks_absolute_path(workdir):
    local = os.path.join(workdir, "abc.jpg")
    with open(local, "wb") as f:
        f.write(b"x")
    out = media._patch_ext_from_response(
        local, "image/jpeg", 'attachment; filename="/tmp/evil.sh"', "fid1"
    )
    assert os.path.dirname(out) == workdir
