"""디버그 HTTP 프록시의 SSRF 가드 테스트 (RPA-20).

방어 3겹: ① 명시적 opt-in만 허용, ② 대상 IP 검증(사설망/메타데이터 차단) +
검증된 IP로 연결 고정(DNS 리바인딩 차단), ③ 리다이렉트 미추적.
"""

import asyncio

from fastapi.testclient import TestClient

from app.api.debug import _pin_url_to_ip, _resolve_validated_ip
from app.main import app


def _resolve(host):
    return asyncio.run(_resolve_validated_ip(host))


def test_blocks_ec2_metadata_ip():
    ip, reason = _resolve("169.254.169.254")
    assert ip is None and reason is not None


def test_blocks_localhost():
    assert _resolve("localhost")[1] is not None
    assert _resolve("127.0.0.1")[1] is not None


def test_blocks_private_network():
    assert _resolve("10.0.0.5")[1] is not None
    assert _resolve("192.168.1.1")[1] is not None


def test_blocks_unresolvable_host():
    ip, reason = _resolve("nonexistent.invalid")
    assert ip is None and reason is not None


def test_allows_public_host_returns_pinned_ip():
    ip, reason = _resolve("api.github.com")
    assert reason is None and ip is not None  # 연결에 쓸 검증된 IP를 반환


def test_endpoint_403_when_disabled(monkeypatch):
    monkeypatch.delenv("DEBUG_HTTP_CLIENT_ENABLED", raising=False)
    monkeypatch.setenv("APP_ENV", "development")  # 예전엔 이것만으로 열렸음 — 이제 막혀야 함
    with TestClient(app) as client:
        r = client.post("/api/debug/http-request", json={"url": "https://api.github.com"})
    assert r.status_code == 403


def test_endpoint_blocks_metadata_when_enabled(monkeypatch):
    monkeypatch.setenv("DEBUG_HTTP_CLIENT_ENABLED", "true")
    with TestClient(app) as client:
        r = client.post("/api/debug/http-request", json={"url": "http://169.254.169.254/"})
    assert r.status_code == 400


def test_pin_url_to_ip_preserves_host_and_port():
    pinned, host = _pin_url_to_ip("https://example.com:8443/path?q=1", "93.184.216.34")
    assert pinned == "https://93.184.216.34:8443/path?q=1"
    assert host == "example.com:8443"


def test_pin_url_to_ip_ipv6_brackets():
    pinned, _ = _pin_url_to_ip("http://example.com/x", "2606:2800:220:1:248:1893:25c8:1946")
    assert "[2606:2800:220:1:248:1893:25c8:1946]" in pinned
