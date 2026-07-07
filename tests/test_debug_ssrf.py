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
    assert r.json()["detail"]["code"] == "BLOCKED_TARGET"  # {code, message} 포맷


def test_debug_router_gated_in_production(monkeypatch):
    """프로덕션에서는 디버그 라우터 전체가 차단된다 (RAG 디버그 엔드포인트 포함)."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("DEBUG_ENDPOINTS_ENABLED", raising=False)
    with TestClient(app) as client:
        r = client.get("/api/rag/debug/status")  # http-request가 아닌 다른 디버그 라우트
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "DEBUG_DISABLED"


def test_debug_gate_forced_on_passes(monkeypatch):
    """프로덕션이라도 DEBUG_ENDPOINTS_ENABLED=true면 게이트를 통과한다.

    (엔드포인트 본문은 무거운 외부 의존이 있어 게이트 함수만 직접 검증)
    """
    from app.api.debug import require_debug_enabled

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEBUG_ENDPOINTS_ENABLED", "true")
    require_debug_enabled()  # 예외가 나지 않으면 통과


def test_debug_gate_allows_local(monkeypatch):
    """로컬/개발(APP_ENV 미설정=development)에서는 게이트가 열려 있다."""
    from app.api.debug import require_debug_enabled

    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("DEBUG_ENDPOINTS_ENABLED", raising=False)
    require_debug_enabled()  # 예외 없음


def test_pin_url_to_ip_preserves_host_and_port():
    pinned, host = _pin_url_to_ip("https://example.com:8443/path?q=1", "93.184.216.34")
    assert pinned == "https://93.184.216.34:8443/path?q=1"
    assert host == "example.com:8443"


def test_pin_url_to_ip_ipv6_brackets():
    pinned, _ = _pin_url_to_ip("http://example.com/x", "2606:2800:220:1:248:1893:25c8:1946")
    assert "[2606:2800:220:1:248:1893:25c8:1946]" in pinned
