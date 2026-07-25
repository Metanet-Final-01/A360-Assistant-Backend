"""디버그 HTTP 프록시의 SSRF 가드 테스트 (RPA-20).

방어 3겹: ① 명시적 opt-in만 허용, ② 대상 IP 검증(사설망/메타데이터 차단) +
검증된 IP로 연결 고정(DNS 리바인딩 차단), ③ 리다이렉트 미추적.
"""

import asyncio

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.debug import _pin_url_to_ip, _resolve_validated_ip, require_debug_enabled
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


def test_endpoint_403_when_http_client_disabled(monkeypatch):
    """라우터 게이트는 열되(플래그 on), http 프록시 하위 게이트만 꺼서 403을 검증한다.

    라우터 게이트를 안 열면 그게 먼저 403을 내 http-client 하위 게이트가 검증되지 않는다
    (가짜 초록). 그래서 DEBUG_ENDPOINTS_ENABLED=true로 라우터를 연 뒤 하위 게이트만 본다.
    """
    monkeypatch.setenv("DEBUG_ENDPOINTS_ENABLED", "true")
    monkeypatch.delenv("DEBUG_HTTP_CLIENT_ENABLED", raising=False)
    with TestClient(app) as client:
        r = client.post("/api/debug/http-request", json={"url": "https://api.github.com"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "DEBUG_HTTP_DISABLED"  # 라우터가 아닌 http 하위 게이트


def test_endpoint_blocks_metadata_when_enabled(monkeypatch):
    monkeypatch.setenv("DEBUG_ENDPOINTS_ENABLED", "true")  # 라우터 게이트 개방
    monkeypatch.setenv("DEBUG_HTTP_CLIENT_ENABLED", "true")
    with TestClient(app) as client:
        r = client.post("/api/debug/http-request", json={"url": "http://169.254.169.254/"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "BLOCKED_TARGET"  # {code, message} 포맷


def test_debug_router_closed_without_flag(monkeypatch):
    """플래그 미설정이면 디버그 라우터 전체가 차단된다 — APP_ENV 무관, fail-closed (RPA-290).

    env 미설정은 '로컬'과 '배포 오설정'을 구분 못 하므로, 미설정 형태 그대로 차단됨을 본다.
    """
    monkeypatch.delenv("DEBUG_ENDPOINTS_ENABLED", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    with TestClient(app) as client:
        r = client.get("/api/rag/debug/status")  # http-request가 아닌 다른 디버그 라우트
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "DEBUG_DISABLED"


def test_debug_gate_closed_in_staging(monkeypatch):
    """staging 등 production이 아닌 배포에서도 플래그 없으면 닫힌다 (과거 fail-open 지점, RPA-290)."""
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.delenv("DEBUG_ENDPOINTS_ENABLED", raising=False)
    with pytest.raises(HTTPException) as exc:
        require_debug_enabled()
    assert exc.value.detail["code"] == "DEBUG_DISABLED"


def test_debug_gate_closed_by_default(monkeypatch):
    """기본값(플래그·APP_ENV 모두 미설정)에서 게이트는 닫혀 있다 — fail-closed (RPA-290).

    예전엔 여기서 '열려 있다'를 단언했다(fail-open의 근원 계약). 이제 뒤집는다.
    """
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("DEBUG_ENDPOINTS_ENABLED", raising=False)
    with pytest.raises(HTTPException) as exc:
        require_debug_enabled()
    assert exc.value.detail["code"] == "DEBUG_DISABLED"


def test_debug_gate_forced_on_passes(monkeypatch):
    """DEBUG_ENDPOINTS_ENABLED=true면 게이트를 통과한다 (APP_ENV 무관 — production이라도).

    (엔드포인트 본문은 무거운 외부 의존이 있어 게이트 함수만 직접 검증)
    """
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEBUG_ENDPOINTS_ENABLED", "true")
    require_debug_enabled()  # 예외가 나지 않으면 통과


def test_pin_url_to_ip_preserves_host_and_port():
    pinned, host = _pin_url_to_ip("https://example.com:8443/path?q=1", "93.184.216.34")
    assert pinned == "https://93.184.216.34:8443/path?q=1"
    assert host == "example.com:8443"


def test_pin_url_to_ip_ipv6_brackets():
    pinned, _ = _pin_url_to_ip("http://example.com/x", "2606:2800:220:1:248:1893:25c8:1946")
    assert "[2606:2800:220:1:248:1893:25c8:1946]" in pinned
