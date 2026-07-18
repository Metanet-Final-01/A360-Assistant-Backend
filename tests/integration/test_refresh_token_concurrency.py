# -*- coding: utf-8 -*-
"""갱신 토큰 회전의 **실제 동시성** 회귀 테스트 (RPA-200, CodeRabbit #273).

왜 통합 테스트인가 — 이 결함은 **DB가 직렬화해 주는지**의 문제라 진짜 커넥션·진짜 커밋이
아니면 재현되지 않는다. 순차 시뮬레이션은 "진 쪽이 401을 받는다"는 결과만 확인할 뿐,
조회 후 갱신(TOCTOU)으로 되돌려도 그대로 통과한다 — 실측으로 확인했다.

지키는 불변식: **같은 리프레시 토큰으로 N개가 동시에 갱신을 시도해도 성공은 정확히 1건.**
깨지면 하나의 토큰에서 여러 세션이 갈라져 나오고(회전 무력화), 둘 다 폐기 전 상태로 보였으므로
재사용 탐지도 걸리지 않는다.
"""

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models
from app.main import app

_CONCURRENCY = 8


@pytest.fixture()
def client():
    """스레드가 **공유**하는 단일 클라이언트.

    스레드마다 TestClient를 만들면 각자 이벤트 루프를 띄워 앱의 커넥션 풀과 충돌한다
    ("attached to a different loop"). 하나만 두고 공유해도 동시성은 확보된다 —
    인증 라우트가 동기 함수(`def`)라 FastAPI가 워커 스레드풀에서 **병렬로** 실행한다.
    """
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def issued_refresh_token(client):
    """실 DB에 사용자와 토큰 쌍을 만들고 리프레시 토큰 원문을 준다."""
    r = client.post(
        "/api/auth/register",
        json={"email": "race@integration.example.com", "password": "pw12345678"},
    )
    assert r.status_code == 201, r.text
    return r.json()["refresh_token"]


def test_claim_is_atomic_under_real_concurrency(issued_refresh_token, integration_engine):
    """🔴 **핵심 회귀** — N개 스레드가 각자 커넥션으로 같은 토큰을 선점해도 성공은 정확히 1건.

    원자성이 사는 지점은 선점 프리미티브라 여기를 직접 겨냥한다. HTTP 계층으로만 재현하려 하면
    요청이 사실상 직렬화돼 조회 후 갱신(TOCTOU)으로 되돌려도 통과한다 — 실측으로 확인했다.

    barrier로 전원을 같은 순간에 풀어 조회~갱신 구간을 겹친다. 조건부 UPDATE면 DB가 승자를
    하나로 만들고, TOCTOU면 여러 스레드가 전부 '아직 유효'를 보고 각자 True를 받는다.
    """
    from sqlalchemy.orm import Session

    from app.api import auth as auth_mod
    from app.core.security import hash_token

    token_hash = hash_token(issued_refresh_token)
    barrier = threading.Barrier(_CONCURRENCY)
    wins: list[bool] = []
    lock = threading.Lock()

    def claim() -> None:
        with Session(integration_engine) as s:  # 스레드마다 독립 커넥션
            barrier.wait()
            got = auth_mod._claim_refresh_token(token_hash, s)
            # 프로덕션은 선점과 발급을 **함께 커밋**한다(_issue_tokens) — 여기서도 같게 닫는다.
            # 커밋하지 않으면 세션 종료 시 롤백돼 폐기가 되돌아가고, 다음 스레드가 또 선점해
            # 8건 전부 성공한다(경합이 아니라 순차 재시도가 된다).
            s.commit() if got else s.rollback()
        with lock:
            wins.append(got)

    threads = [threading.Thread(target=claim) for _ in range(_CONCURRENCY)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert len(wins) == _CONCURRENCY, "모든 스레드가 결과를 내야 한다"
    assert wins.count(True) == 1, (
        f"선점 성공이 {wins.count(True)}건 — 정확히 1건이어야 한다. "
        f"1건보다 많으면 하나의 토큰에서 세션이 갈라진다(회전 무력화)."
    )


def test_concurrent_refresh_grants_exactly_one_new_pair(client, issued_refresh_token, integration_engine):
    """🔴 동시 갱신 N건 중 **성공은 정확히 1건**이어야 한다.

    barrier로 모든 스레드를 같은 순간에 풀어 조회~갱신 구간을 최대한 겹친다.
    조건부 UPDATE면 DB가 승자를 하나로 만들고, 조회 후 갱신이면 여러 스레드가
    전부 '아직 유효'를 보고 각자 새 쌍을 발급받는다.
    """
    barrier = threading.Barrier(_CONCURRENCY)
    results: list[int] = []
    lock = threading.Lock()

    def attempt() -> None:
        barrier.wait()  # 전원 동시 출발
        r = client.post("/api/auth/refresh", json={"refresh_token": issued_refresh_token})
        with lock:
            results.append(r.status_code)

    threads = [threading.Thread(target=attempt) for _ in range(_CONCURRENCY)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert len(results) == _CONCURRENCY, "모든 스레드가 응답을 받아야 한다"
    assert results.count(200) == 1, (
        f"동시 갱신 성공이 {results.count(200)}건 — 정확히 1건이어야 한다. "
        f"1건보다 많으면 하나의 토큰에서 세션이 갈라진다(회전 무력화). 응답: {sorted(results)}"
    )
    assert results.count(401) == _CONCURRENCY - 1

    # 발급된 유효 토큰도 정확히 1개여야 한다 — 응답만 보고 판단하지 않고 DB 상태로 재확인한다.
    from sqlalchemy.orm import Session

    with Session(integration_engine) as s:
        alive = s.scalars(
            select(models.RefreshToken).where(models.RefreshToken.revoked_at.is_(None))
        ).all()
        assert len(alive) == 1, f"유효 토큰이 {len(alive)}개 — 1개여야 한다"


def test_concurrent_logout_and_refresh_leaves_no_live_token(client, issued_refresh_token, integration_engine):
    """로그아웃과 갱신이 겹쳐도 **로그아웃한 토큰에서 새 세션이 살아남지 않는다**.

    TOCTOU면 갱신이 로그아웃을 앞질러 새 쌍을 받아 세션이 이어진다 — 사용자는 로그아웃했는데
    서버엔 유효 토큰이 남는다.
    """
    barrier = threading.Barrier(2)
    outcome: dict[str, int] = {}

    def do(kind: str) -> None:
        barrier.wait()
        path = "/api/auth/logout" if kind == "logout" else "/api/auth/refresh"
        r = client.post(path, json={"refresh_token": issued_refresh_token})
        outcome[kind] = r.status_code

    threads = [threading.Thread(target=do, args=(k,)) for k in ("logout", "refresh")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert outcome["logout"] == 204  # 로그아웃은 멱등이라 항상 성공한다
    from sqlalchemy.orm import Session

    # 🔴 **승패와 무관하게** 살아 있는 토큰이 없어야 한다 (CodeRabbit #273 2차 지적).
    # 갱신이 먼저 이겨 새 토큰을 발급했더라도, 로그아웃은 그 토큰의 **계열 전체**를 끊는다.
    # 이게 없으면 사용자가 204를 받고도 같은 토큰을 쥔 쪽의 세션이 유지된다.
    with Session(integration_engine) as s:
        alive = s.scalars(
            select(models.RefreshToken).where(models.RefreshToken.revoked_at.is_(None))
        ).all()
        assert alive == [], (
            f"로그아웃 후 유효 토큰이 {len(alive)}개 남았다 — 갱신(refresh={outcome['refresh']})이 "
            f"경합에서 이겨 발급한 후손이 살아남으면 로그아웃이 사실이 아니라 주장이 된다"
        )


def test_logout_waits_while_refresh_is_mid_transaction(client, issued_refresh_token, integration_engine):
    """🔴 **결정론적 증명** — 갱신이 트랜잭션 중일 때 로그아웃은 기다려야 한다.

    앞의 확률적 테스트들은 타이밍이 맞아야 결함을 잡는다(자문 잠금을 빼도 4회 중 1회만 실패).
    여기서는 순서를 강제해 **직렬화 자체**를 못 박는다:

      1) 갱신이 토큰을 선점한 직후 멈춘다(아직 커밋 전 — 계열 잠금을 쥔 상태)
      2) 그 사이 로그아웃을 부른다 → 잠금이 있으면 **완료되지 못하고 대기**해야 한다
      3) 갱신을 풀어주면 로그아웃이 이어서 끝나고, 살아남은 토큰이 없어야 한다

    잠금이 없으면 (2)에서 로그아웃이 즉시 끝나고 (3)의 갱신이 새 토큰을 남긴다 —
    사용자는 204를 받았는데 세션이 유지되는 그 결함이다.
    """
    import threading

    from sqlalchemy.orm import Session

    from app.api import auth as auth_mod

    claimed = threading.Event()   # 갱신이 선점을 마쳤다
    release = threading.Event()   # 갱신에게 계속 진행하라
    logout_done = threading.Event()
    real_issue = auth_mod._issue_tokens

    def paused_issue(user_id, db, **kw):
        claimed.set()
        release.wait(timeout=30)  # 잠금을 쥔 채 대기
        return real_issue(user_id, db, **kw)

    auth_mod._issue_tokens = paused_issue
    try:
        t_refresh = threading.Thread(
            target=lambda: client.post("/api/auth/refresh",
                                       json={"refresh_token": issued_refresh_token}))
        t_refresh.start()
        assert claimed.wait(timeout=30), "갱신이 선점 단계까지 오지 못했다"

        def do_logout():
            client.post("/api/auth/logout", json={"refresh_token": issued_refresh_token})
            logout_done.set()

        t_logout = threading.Thread(target=do_logout)
        t_logout.start()

        # 잠금이 살아 있으면 로그아웃은 여기서 끝나지 못한다.
        assert not logout_done.wait(timeout=3), (
            "갱신이 트랜잭션 중인데 로그아웃이 끝났다 — 계열 직렬화가 깨졌다. "
            "이 상태면 로그아웃의 폐기가 지나간 뒤 갱신이 새 토큰을 남긴다"
        )

        release.set()
        t_refresh.join(timeout=30)
        t_logout.join(timeout=30)
        assert logout_done.is_set(), "잠금 해제 후에도 로그아웃이 끝나지 않았다"
    finally:
        auth_mod._issue_tokens = real_issue
        release.set()

    with Session(integration_engine) as s:
        alive = s.scalars(
            select(models.RefreshToken).where(models.RefreshToken.revoked_at.is_(None))
        ).all()
        assert alive == [], f"로그아웃 후 유효 토큰이 {len(alive)}개 남았다"
