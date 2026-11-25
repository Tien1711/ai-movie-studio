# -*- coding: utf-8 -*-
"""
test_ssrf_audit_2026_11_25.py — Behaviour tests for audit #1/#2 (SSRF), group A.

ĐỎ trên main 4d8a563 (lỗ hổng còn đó), XANH sau khi vá. Không gọi mạng thật:
- DNS được giả lập bằng monkeypatch socket.getaddrinfo.
- Kết nối TCP được giả lập bằng monkeypatch socket.create_connection +
  socket giả (_FakeSocket) phục vụ response HTTP đã soạn sẵn cho cả
  http.client (code mới) lẫn httpcore/httpx (code cũ).

Nhóm kiểm chứng:
  #1 affiliate redirect tới 127.0.0.1 / 169.254.169.254 bị chặn TRƯỚC khi
     tải body (server nội bộ chưa từng bị chạm tới).
  #2a CGNAT 100.64.0.0/10 bị chặn (is_global).
  #2b DNS-rebinding bị chặn bằng IP pinning (chỉ phân giải 1 lần, kết nối
     tới IP đã kiểm tra).
  #2c nhánh http_get inject vẫn phải kiểm tra DNS.
  Control: URL công khai hợp lệ vẫn tải được.
"""
import io
import socket

import pytest

import affiliate_engine
import story_engine
from story_engine import _is_blocked_ip, _validate_public_url

PUBLIC_IP = "93.184.216.34"  # example.com — IP công khai dùng cho test


# ── Socket giả phục vụ 1 response HTTP đã soạn sẵn ──────────────────────────

class _FakeSocket:
    """Socket giả: http.client đọc qua makefile, httpcore đọc qua recv()."""

    def __init__(self, raw: bytes):
        self._bio = io.BytesIO(raw)
        self.sent = b""

    def makefile(self, mode, *args, **kwargs):
        return self._bio

    def sendall(self, data):
        self.sent += data

    def send(self, data):
        self.sent += data
        return len(data)

    def recv(self, n):
        return self._bio.read(n)

    def recv_into(self, buf):
        data = self._bio.read(len(buf))
        buf[:len(data)] = data
        return len(data)

    def close(self):
        pass

    def settimeout(self, timeout):
        pass

    def fileno(self):
        # -1: httpcore coi như "readable" mà không gọi select() lên fd giả.
        return -1

    def gettimeout(self):
        return None

    def setblocking(self, flag):
        pass

    def setsockopt(self, *args, **kwargs):
        pass

    def getsockopt(self, *args, **kwargs):
        return 0

    def getpeername(self):
        return (PUBLIC_IP, 80)


def _http_response(status: int, headers, body: bytes = b"") -> bytes:
    lines = [f"HTTP/1.1 {status} X".encode()]
    for key, value in headers.items():
        lines.append(f"{key}: {value}".encode())
    return b"\r\n".join(lines) + b"\r\n\r\n" + body


class _FakeResp:
    """Response tối giản cho http_get inject (đủ cho extract_text_from_url)."""

    def __init__(self, text: str, status_code: int = 200):
        self.status_code = status_code
        self.text = text


# ── #1: affiliate redirect tới host nội bộ ──────────────────────────────────

@pytest.mark.parametrize("internal_url", [
    "http://127.0.0.1:9/secret",
    "http://169.254.169.254/latest/meta-data/",
])
def test_affiliate_redirect_to_internal_blocked_before_body(monkeypatch, internal_url):
    """#1: redirect tới IP nội bộ bị chặn TRƯỚC khi tải body.

    Hop đầu là host công khai (public.test) trả 302 về IP nội bộ. Code cũ
    (follow_redirects=True) tải luôn body bí mật rồi mới kiểm final_host.
    Code mới phải raise ValueError và KHÔNG BAO GIỜ kết nối tới IP nội bộ.
    """
    secret = b"TOP-SECRET-INTERNAL"
    redirect_resp = _http_response(302, {
        "Location": internal_url, "Content-Length": "0",
    })
    secret_resp = _http_response(200, {
        "Content-Type": "text/plain", "Content-Length": str(len(secret)),
    }, secret)
    targets = []

    def fake_getaddrinfo(host, port, *args, **kwargs):
        ip = PUBLIC_IP if host == "public.test" else host
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    def fake_create_connection(address, timeout=None, source_address=None):
        targets.append(address)
        if address[0] == PUBLIC_IP:
            return _FakeSocket(redirect_resp)
        return _FakeSocket(secret_resp)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", fake_create_connection)

    with pytest.raises(ValueError, match="SSRF|nội bộ"):
        affiliate_engine._default_link_http_get("http://public.test/go", timeout=5)
    # Chỉ kết nối tới IP công khai của hop đầu; hop nội bộ chưa từng bị chạm.
    assert targets == [(PUBLIC_IP, 80)]


def test_affiliate_redirect_chain_public_hosts_still_works(monkeypatch):
    """#1 control: redirect giữa các host công khai vẫn theo được tới cuối."""
    hop1 = _http_response(302, {
        "Location": "http://cdn.public.test/file", "Content-Length": "0",
    })
    body = b"<html><body>ok</body></html>"
    hop2 = _http_response(200, {
        "Content-Type": "text/html", "Content-Length": str(len(body)),
    }, body)

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port))]

    _seen = {"n": 0}

    def counting_create_connection(address, timeout=None, source_address=None):
        _seen["n"] += 1
        return _FakeSocket(hop1 if _seen["n"] == 1 else hop2)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", counting_create_connection)

    resp = affiliate_engine._default_link_http_get("http://public.test/go", timeout=5)
    assert resp.status_code == 200
    assert resp.url == "http://cdn.public.test/file"
    assert b"ok" in resp.content


# ── #2a: CGNAT 100.64.0.0/10 ────────────────────────────────────────────────

@pytest.mark.parametrize("ip", ["100.64.0.1", "100.64.0.0", "100.127.255.255"])
def test_story_cgnat_ip_literal_blocked(ip):
    """#2a: IP literal dải CGNAT 100.64.0.0/10 bị chặn (not ip.is_global)."""
    assert _is_blocked_ip(ip) is True
    with pytest.raises(ValueError, match="SSRF|nội bộ"):
        _validate_public_url(f"http://{ip}/x")
    with pytest.raises(ValueError, match="SSRF|nội bộ"):
        story_engine.extract_text_from_url(f"http://{ip}/x", http_get=lambda u: None)


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34"])
def test_story_public_ip_literal_allowed(ip):
    """#2a control: IP công khai thật vẫn đi qua được."""
    assert _is_blocked_ip(ip) is False
    assert _validate_public_url(f"http://{ip}/x") is not None


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1",
    "169.254.169.254", "0.0.0.0", "::1", "fe80::1",
])
def test_story_blocked_ip_ranges_still_blocked(ip):
    """#2a control: các dải nội bộ cũ vẫn bị chặn (không regression)."""
    assert _is_blocked_ip(ip) is True
    host = f"[{ip}]" if ":" in ip else ip  # IPv6 literal cần ngoặc vuông trong URL
    with pytest.raises(ValueError, match="SSRF|nội bộ"):
        _validate_public_url(f"http://{host}/x")


# ── #2b: DNS-rebinding TOCTOU — IP pinning ──────────────────────────────────

def test_story_dns_rebinding_blocked_by_ip_pinning(monkeypatch):
    """#2b: domain trả IP công khai lúc kiểm tra, IP nội bộ lúc kết nối thật.

    getaddrinfo lần 1 -> IP công khai (qua kiểm tra), các lần sau ->
    127.0.0.1 (nội bộ). Code pin IP: chỉ phân giải 1 lần rồi kết nối tới IP
    đã kiểm tra. Code cũ (httpx tự phân giải lại lúc kết nối) sẽ chạm tới
    127.0.0.1 — test này bắt đúng hành vi đó qua địa chỉ kết nối ghi lại.
    """
    calls = {"n": 0}
    targets = []

    def fake_getaddrinfo(host, port, *args, **kwargs):
        calls["n"] += 1
        ip = PUBLIC_IP if calls["n"] == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    def fake_create_connection(address, timeout=None, source_address=None):
        targets.append(address)
        raise ConnectionRefusedError("fake: không kết nối thật trong test")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", fake_create_connection)

    with pytest.raises(ConnectionRefusedError):
        story_engine._safe_story_url_get("http://rebind.test/truyen")
    # Chỉ phân giải 1 lần và chỉ kết nối tới IP công khai đã kiểm tra.
    assert calls["n"] == 1
    assert targets == [(PUBLIC_IP, 80)]


# ── #2c: nhánh http_get inject vẫn phải kiểm tra DNS ────────────────────────

def test_extract_injected_http_get_still_validates_dns(monkeypatch):
    """#2c: http_get inject không được bỏ kiểm tra DNS.

    Domain trỏ về IP nội bộ (10.0.0.5) phải bị chặn ngay cả khi http_get
    được inject. Code cũ bỏ qua DNS ở nhánh này (resolve_dns=False).
    """
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError, match="SSRF|nội bộ"):
        story_engine.extract_text_from_url(
            "https://evil.example.com/x", http_get=lambda u: _FakeResp("x"))


def test_extract_injected_http_get_public_host_ok(monkeypatch):
    """#2c control: host công khai + http_get inject vẫn trích được."""
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    html = ("<html><body><article><p>" + "Nội dung dài. " * 60
            + "</p></article></body></html>")
    text = story_engine.extract_text_from_url(
        "https://public.test/x", http_get=lambda u: _FakeResp(html))
    assert "Nội dung dài" in text


# ── Control: URL công khai hợp lệ vẫn tải được ──────────────────────────────

def test_public_url_still_fetches(monkeypatch):
    """URL công khai hợp lệ vẫn tải được qua IP đã pin (story + affiliate)."""
    html = ("<html><body><article><p>Ngay xua co mot chang trai ngheo.</p>"
            "</article></body></html>").encode()
    resp = _http_response(200, {
        "Content-Type": "text/html; charset=utf-8",
        "Content-Length": str(len(html)),
    }, html)
    targets = []

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port))]

    def fake_create_connection(address, timeout=None, source_address=None):
        targets.append(address)
        return _FakeSocket(resp)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", fake_create_connection)

    res = story_engine._safe_story_url_get("http://public.test/truyen")
    assert res.status_code == 200
    assert "chang trai" in res.text
    # Code mới pin IP: kết nối tới IP đã kiểm tra, không phải hostname.
    # (Trên code cũ assertion này đỏ vì httpx tự phân giải lại lúc kết nối.)
    assert targets == [(PUBLIC_IP, 80)]

    link = affiliate_engine._default_link_http_get("http://public.test/go", timeout=5)
    assert link.status_code == 200
    assert link.url == "http://public.test/go"
