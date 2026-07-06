"""디버그 HTTP 프록시의 SSRF 가드 테스트 (RPA-20)."""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import _blocked_target_reason, app


def test_blocks_ec2_metadata_ip():
    assert _blocked_target_reason("http://169.254.169.254/latest/meta-data/") is not None


def test_blocks_localhost():
    assert _blocked_target_reason("http://localhost:8000/") is not None
    assert _blocked_target_reason("http://127.0.0.1/") is not None


def test_blocks_private_network():
    assert _blocked_target_reason("http://10.0.0.5/") is not None
    assert _blocked_target_reason("http://192.168.1.1/admin") is not None


def test_blocks_unresolvable_host():
    assert _blocked_target_reason("http://nonexistent.invalid/") is not None


def test_allows_public_host():
    # 공인 IP로 해석되는 호스트는 통과 (실제 요청은 보내지 않음)
    assert _blocked_target_reason("https://api.github.com/") is None


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
