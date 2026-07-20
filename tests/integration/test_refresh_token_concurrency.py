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
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app import models
from app.main import app

_CONCURRENCY = 8


def _wait_for_refresh_token_lock_wait(integration_engine, *, timeout: float = 30) -> None:
    """경쟁 요청이 PostgreSQL lock에서 실제 대기 중일 때만 반환한다.

    ⚠️ **잠금 구현에 종속되지 않게 판정한다.** 예전엔 `query ilike '%refresh_tokens%'`로만
    봤는데, 그건 행 잠금(`SELECT ... FOR UPDATE on refresh_tokens`)일 때만 맞는 대리 지표다.
    계열 잠금으로 바꾸자 대기 쿼리가 `select pg_advisory_xact_lock(...)`이 되어 같은 대기를
    **탐지하지 못했다** — 테스트가 구현을 검증한 게 아니라 구현의 문자열을 검증하고 있었다.
    지금은 대기 종류(`Lock`/`advisory`)를 함께 보고, 쿼리는 둘 중 하나면 인정한다.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with integration_engine.connect() as connection:
            waiting = connection.scalar(text("""
                select exists (
                    select 1
                    from pg_stat_activity
                    where datname = current_database()
                      and wait_event_type in ('Lock', 'LWLock')
                      and (query ilike '%refresh_tokens%'
                           or query ilike '%advisory%')
                )
            """))
        if waiting:
            return
        time.sleep(0.02)
    raise AssertionError("경쟁 요청이 잠금 대기에 진입하지 않았습니다")


@pytest.fixture()
def client():
    """스레드가 **공유**하는 단일 클라이언트.

    스레드마다 TestClient를 만들면 각자 이벤트 루프를 띄워 앱의 커넥션 풀과 충돌한다
    ("attached to a different loop"). 하나만 두고 공유해도 동시성은 확보된다 —
    인증 라우트가 동기 함수(`def`)라 FastAPI가 워커 스레드풀에서 **병렬로** 실행한다.

    🔴 **쿠키는 끈다** (RPA-216). 이 파일의 시나리오는 "회전된 **옛** 토큰으로 로그아웃" 처럼
    특정 토큰을 바디로 지정해 경합을 만든다. 갱신 토큰 쿠키가 함께 실리면 서버가 쿠키를
    우선하므로 **옛 토큰 시나리오가 성립하지 않고**, 테스트는 통과하면서 실제로는 다른 것을
    재게 된다. 지금은 SECURE_COOKIES 기본값(true)+http라 쿠키가 우연히 전송되지 않을 뿐이라,
    그 우연에 기대지 않고 명시적으로 비운다.

    ⚠️ 한 번만 비우면 안 된다 — register 응답이 곧바로 쿠키를 다시 심는다. 응답 훅으로
    **매번** 비워야 한다(훅이 실제로 jar를 비우는지는 별도 확인했다).
    """
    with TestClient(app) as c:
        c.event_hooks["response"].append(lambda _resp: c.cookies.clear())
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


def test_refresh_commit_then_logout_revokes_the_new_child(
    client, issued_refresh_token, integration_engine, monkeypatch
):
    """refresh가 lock을 먼저 잡아도 logout은 commit을 기다린 뒤 새 후손까지 폐기한다."""
    from app.api import auth as auth_mod

    refresh_inside_lock = threading.Event()
    allow_refresh_commit = threading.Event()
    logout_started = threading.Event()
    real_issue = auth_mod._issue_tokens
    outcome: dict[str, int] = {}

    def paused_issue(*args, **kwargs):
        refresh_inside_lock.set()
        assert allow_refresh_commit.wait(timeout=30)
        return real_issue(*args, **kwargs)

    monkeypatch.setattr(auth_mod, "_issue_tokens", paused_issue)

    def do_refresh() -> None:
        outcome["refresh"] = client.post(
            "/api/auth/refresh", json={"refresh_token": issued_refresh_token}
        ).status_code

    def do_logout() -> None:
        logout_started.set()
        outcome["logout"] = client.post(
            "/api/auth/logout", json={"refresh_token": issued_refresh_token}
        ).status_code

    refresh_thread = threading.Thread(target=do_refresh)
    refresh_thread.start()
    assert refresh_inside_lock.wait(timeout=30)
    logout_thread = threading.Thread(target=do_logout)
    logout_thread.start()
    assert logout_started.wait(timeout=30)
    _wait_for_refresh_token_lock_wait(integration_engine)
    allow_refresh_commit.set()
    refresh_thread.join(timeout=60)
    logout_thread.join(timeout=60)

    assert outcome == {"refresh": 200, "logout": 204}
    _assert_no_live_tokens(integration_engine)


def test_logout_commit_then_refresh_cannot_create_a_child(
    client, issued_refresh_token, integration_engine, monkeypatch
):
    """logout이 lock을 먼저 잡으면 refresh는 폐기 상태를 보고 401로 끝난다."""
    from app.api import auth as auth_mod

    logout_inside_lock = threading.Event()
    allow_logout_commit = threading.Event()
    refresh_started = threading.Event()
    real_revoke = auth_mod._revoke_family
    outcome: dict[str, int] = {}

    def paused_revoke(*args, **kwargs):
        logout_inside_lock.set()
        assert allow_logout_commit.wait(timeout=30)
        return real_revoke(*args, **kwargs)

    monkeypatch.setattr(auth_mod, "_revoke_family", paused_revoke)

    def do_logout() -> None:
        outcome["logout"] = client.post(
            "/api/auth/logout", json={"refresh_token": issued_refresh_token}
        ).status_code

    def do_refresh() -> None:
        refresh_started.set()
        outcome["refresh"] = client.post(
            "/api/auth/refresh", json={"refresh_token": issued_refresh_token}
        ).status_code

    logout_thread = threading.Thread(target=do_logout)
    logout_thread.start()
    assert logout_inside_lock.wait(timeout=30)
    refresh_thread = threading.Thread(target=do_refresh)
    refresh_thread.start()
    assert refresh_started.wait(timeout=30)
    _wait_for_refresh_token_lock_wait(integration_engine)
    allow_logout_commit.set()
    logout_thread.join(timeout=60)
    refresh_thread.join(timeout=60)

    assert outcome == {"logout": 204, "refresh": 401}
    _assert_no_live_tokens(integration_engine)


def _assert_no_live_tokens(integration_engine) -> None:
    from sqlalchemy.orm import Session

    with Session(integration_engine) as session:
        assert session.scalars(
            select(models.RefreshToken).where(models.RefreshToken.revoked_at.is_(None))
        ).all() == []

def test_logout_with_stale_token_still_kills_session(client, integration_engine):
    """🔴 **로그아웃이 회전된 옛 토큰으로 들어와도** 세션이 끊겨야 한다.

    멀티탭·모바일 캐시·네트워크 재시도에서 흔하다: A가 t2로 갱신하는 사이 B는 아직
    t1(회전 전)을 들고 로그아웃을 누른다.

    ⚠️ **잠금 단위가 갈리는 지점이다.** 잠금을 '제시된 토큰의 행'에 걸면 t1과 t2는 서로 다른
    행이라 잠금이 갈린다 — 로그아웃의 계열 UPDATE는 t2에서 잠깐 막히지만, 풀린 뒤에도 그 사이
    INSERT된 t3는 **자기 스냅샷에 없어 폐기하지 못한다**(실측: 블록됐던 UPDATE는 그동안
    삽입된 행을 보지 못한다). 그래서 잠금은 **계열 단위**여야 한다.

    사용자가 로그아웃을 눌렀으면, 어느 토큰을 냈든 그 로그인은 끝나야 한다.
    """
    import threading

    from sqlalchemy.orm import Session

    from app.api import auth as auth_mod

    t1 = client.post(
        "/api/auth/register",
        json={"email": "stale@integration.example.com", "password": "pw12345678"},
    ).json()["refresh_token"]
    t2 = client.post("/api/auth/refresh", json={"refresh_token": t1}).json()["refresh_token"]
    # t1은 폐기됨, t2가 살아 있다. B 탭은 아직 t1을 들고 있다.

    claimed, release = threading.Event(), threading.Event()
    real_issue = auth_mod._issue_tokens

    def paused_issue(user_id, db, **kw):
        claimed.set()
        release.wait(timeout=30)
        return real_issue(user_id, db, **kw)

    auth_mod._issue_tokens = paused_issue
    try:
        tr = threading.Thread(
            target=lambda: client.post("/api/auth/refresh", json={"refresh_token": t2}))
        tr.start()
        assert claimed.wait(timeout=30), "갱신이 선점 단계까지 오지 못했다"

        logout_done = threading.Event()

        def do_logout():
            client.post("/api/auth/logout", json={"refresh_token": t1})  # ← 회전된 옛 토큰
            logout_done.set()

        tl = threading.Thread(target=do_logout)
        tl.start()
        # ⚠️ **실제 잠금 대기를 확인한 뒤에** 갱신을 풀어준다 (CodeRabbit #278).
        #    처음엔 `logout_done.wait(3)`으로 뒀는데, 그건 로그아웃이 계열 UPDATE에
        #    **도달했다는 보장이 없다** — 스레드 스케줄링이 늦으면 잘못된 행 단위 잠금
        #    구현도 통과한다. pg_stat_activity로 진짜 대기를 확인해야 경합이 재현된다.
        _wait_for_refresh_token_lock_wait(integration_engine)
        release.set()
        tr.join(timeout=30)
        tl.join(timeout=30)
    finally:
        auth_mod._issue_tokens = real_issue
        release.set()

    with Session(integration_engine) as s:
        alive = s.scalars(
            select(models.RefreshToken).where(models.RefreshToken.revoked_at.is_(None))
        ).all()
    assert alive == [], (
        f"옛 토큰으로 로그아웃했더니 유효 토큰이 {len(alive)}개 남았다 — "
        f"잠금이 계열이 아니라 행 단위면 여기서 새 토큰이 살아남는다"
    )
