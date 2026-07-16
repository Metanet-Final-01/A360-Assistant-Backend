"""Slack 알림의 전이·쿨다운·fail-open (RPA-189).

이 모듈의 본질은 "슬랙에 POST한다"가 아니라 **"같은 사유로 도배하지 않는다"**이다.
/health가 30초마다 degraded면 하루 2,880개다 — 그러면 아무도 안 본다.

⚠️ 상태가 **프로세스 밖**에 있는지가 핵심이다. 모듈 전역이면 재시작·멀티워커에서 각자
   "처음"이라 중복 발송한다. 여기선 그 계약만 본다 — 저장소는 인메모리 dict로 대체한다.

   **진짜 DB를 쓰면 안 된다**: 유닛 테스트는 DB 없이 돌아야 한다. CI의 postgres 서비스는
   `postgres`(유지보수 DB)만 만들고 `a360`은 없어서(통합 테스트가 `a360_test`를 따로 만든다)
   `database "a360" does not exist`로 깨진다 — 실제로 그렇게 CI를 깨뜨렸다(로컬엔 docker가
   만들어놔서 안 보였다). 실 DB 경로는 **live 증명**으로 확인했다(실제 Neon + 실제 Slack).
"""

from datetime import datetime, timedelta, timezone

import pytest

import app.services.alerts as alerts

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
HOOK = "https://hooks.slack.com/services/T000/B000/fake"


@pytest.fixture
def sent(monkeypatch):
    """웹훅을 켜고, 실제 HTTP 대신 발송 횟수를 센다.

    ⚠️ `_post`를 대신 세는 게 아니라 **`notify`가 `_post`를 부르는지**를 센다 — 도배 방지가
       듣는지는 "몇 번 보냈나"로만 증명된다.
    """
    monkeypatch.setenv("SLACK_WEBHOOK_URL", HOOK)
    monkeypatch.delenv("ALERT_COOLDOWN_MINUTES", raising=False)
    calls: list[alerts.Alert] = []
    monkeypatch.setattr(alerts, "_post", lambda a: (calls.append(a), True)[1])
    return calls


@pytest.fixture(autouse=True)
def state(monkeypatch):
    """alert_state 저장소를 **인메모리 dict로 대체**하고, 매 테스트를 빈 상태로 시작한다.

    ⚠️ 처음엔 진짜 DB(`_obs_session` → 앱 SessionLocal 폴백 → 로컬 `a360`)를 지웠다.
       **내 로컬에서만 됐다** — CI의 postgres 서비스는 `postgres`(유지보수 DB)만 만들고
       `a360`은 없다(통합 테스트가 `a360_test`를 따로 만든다). `database "a360" does not exist`로
       CI가 깨졌다. 유닛 테스트는 DB 없이 돌아야 한다(conftest 상단 주석의 전제).

    이 가짜로도 검증 대상은 그대로다 — 여기서 볼 건 "**상태가 프로세스 밖에 있나**"지
    "Postgres가 되나"가 아니다. 진짜 DB 경로는 **live 증명**으로 확인했다(실제 Neon +
    실제 Slack 2통, RPA-189 PR 본문).
    """
    store: dict = {}

    class _DB:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, model, key): return store.get(key)
        def add(self, row): store[row.key] = row
        def commit(self): pass

    monkeypatch.setattr(alerts, "_obs_session", lambda: _DB())
    return store


def _alert(key="test:thing") -> alerts.Alert:
    return alerts.Alert(key=key, title="테스트", text="본문", severity="warning")


# --- 미설정 = 비활성 (회귀 없음) ---

def test_disabled_when_webhook_unset(monkeypatch):
    """`SLACK_WEBHOOK_URL`이 없으면 아무것도 안 한다 — 기존 동작 그대로.

    로컬 개발·CI는 웹훅을 안 넣는다. 여기서 뭐라도 하면 전원의 스위트가 느려지거나 깨진다.
    """
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    calls = []
    monkeypatch.setattr(alerts, "_post", lambda a: calls.append(a))

    assert alerts.notify(_alert()) is False
    assert calls == [], "비활성인데 발송을 시도했다"


def test_webhook_is_read_at_call_time(monkeypatch):
    """웹훅을 **참조 시점에** 읽는다 — import 시점에 굳으면 테스트도 운영도 못 바꾼다.

    이 프로젝트에서 import 시점 고정 때문에 여러 번 당했다(app/db.py engine, RAG config 상수).
    """
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    assert alerts.enabled() is False

    monkeypatch.setenv("SLACK_WEBHOOK_URL", HOOK)
    assert alerts.enabled() is True, "env를 켰는데 반영이 안 된다 — 값이 import 시점에 굳었다"


# --- 전이: 상태가 바뀔 때만 ---

def test_first_firing_notifies(sent):
    assert alerts.notify(_alert(), alerts.FIRING, NOW) is True
    assert len(sent) == 1


def test_repeated_firing_within_cooldown_is_silent(sent):
    """같은 firing이 이어지면 쿨다운 내엔 **한 번만**. 이게 이 모듈의 존재 이유다."""
    alerts.notify(_alert(), alerts.FIRING, NOW)
    for i in range(1, 20):  # 30초 간격 헬스체크 흉내
        alerts.notify(_alert(), alerts.FIRING, NOW + timedelta(seconds=30 * i))

    assert len(sent) == 1, f"쿨다운(기본 60분) 안에서 {len(sent)}번 보냈다 — 도배다"


def test_still_firing_after_cooldown_notifies_again(sent):
    """쿨다운이 지나면 재알림 — "아직도 터져 있다"를 알아야 한다.

    (전이만 알리면 첫 알림을 놓친 사람은 영영 모른다.)
    """
    alerts.notify(_alert(), alerts.FIRING, NOW)
    alerts.notify(_alert(), alerts.FIRING, NOW + timedelta(minutes=61))

    assert len(sent) == 2


def test_recovery_notifies_once(sent):
    """복구(firing→ok)도 알린다 — 사람이 "끝났다"를 알아야 한다. 단 한 번만."""
    alerts.notify(_alert(), alerts.FIRING, NOW)
    assert alerts.notify(_alert(), alerts.OK, NOW + timedelta(minutes=5)) is True
    assert len(sent) == 2

    # 정상이 이어지는 건 뉴스가 아니다
    for i in range(5):
        alerts.notify(_alert(), alerts.OK, NOW + timedelta(minutes=10 + i))
    assert len(sent) == 2, "정상 상태가 이어지는데 알림을 보냈다"


def test_ok_without_prior_firing_is_silent(sent):
    """처음부터 정상이면 아무 말 안 한다 — 기동할 때마다 "정상입니다" 슬랙은 소음이다."""
    alerts.notify(_alert(), alerts.OK, NOW)
    assert sent == []


def test_keys_are_independent(sent):
    """다른 key는 서로의 쿨다운에 영향받지 않는다 — 예산 알림이 헬스 알림을 막으면 안 된다."""
    alerts.notify(alerts.Alert("budget:global:daily", "예산", "초과"), alerts.FIRING, NOW)
    alerts.notify(alerts.Alert("health:degraded", "헬스", "degraded"), alerts.FIRING, NOW)

    assert len(sent) == 2


def test_cooldown_zero_means_transition_only(sent, monkeypatch):
    """ALERT_COOLDOWN_MINUTES=0이면 재알림 없이 전이 때만."""
    monkeypatch.setenv("ALERT_COOLDOWN_MINUTES", "0")
    alerts.notify(_alert(), alerts.FIRING, NOW)
    alerts.notify(_alert(), alerts.FIRING, NOW + timedelta(days=1))

    assert len(sent) == 1


# --- 상태가 DB에 있나 (인메모리면 재시작에 샌다) ---

def test_state_survives_module_reload(sent, state):
    """상태가 **모듈 밖**(DB)에 있어야 한다 — 모듈 전역이면 재시작·워커마다 재알림이다.

    importlib.reload로 "새 프로세스"를 흉내낸다. `alerts` 안에 dict를 뒀다면 여기서 초기화돼
    두 번째 알림이 나간다.

    ⚠️ reload는 monkeypatch를 **전부 날린다**(모듈을 새로 실행하므로). `_post`뿐 아니라
       `_obs_session`도 다시 심어야 한다 — 안 그러면 reload된 모듈이 **진짜 DB**를 찾아
       CI에서 깨진다(로컬엔 a360이 있어서 안 보였다).
    """
    import importlib

    alerts.notify(_alert(), alerts.FIRING, NOW)
    assert len(sent) == 1

    importlib.reload(alerts)
    # reload가 심어둔 것들을 날렸으므로 다시 심는다. store(state)는 모듈 **밖**에 있으므로
    # 살아남는다 — 그게 이 테스트의 요점이다.
    class _DB:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, model, key): return state.get(key)
        def add(self, row): state[row.key] = row
        def commit(self): pass

    alerts._obs_session = lambda: _DB()
    alerts._post = lambda a: (sent.append(a), True)[1]
    alerts.notify(_alert(), alerts.FIRING, NOW + timedelta(minutes=1))

    assert len(sent) == 1, "재시작 후 같은 사유로 또 보냈다 — 상태가 프로세스 안에 있다"


# --- 배치 임계 알림 (롤업 직후) ---

@pytest.fixture
def agg(monkeypatch, state):
    """usage_daily·metrics_daily의 '오늘' 값을 원하는 대로 세운다.

    실제 집계 테이블에 쓰지 않고 조회만 가로챈다 — 팀 관측 DB에 테스트 행을 남기지 않기 위해서.

    ⚠️ `_obs_session`은 집계 조회(check_daily_thresholds)**와** 알림 상태(_should_notify·_record)
       **양쪽이** 쓴다. 조회만 흉내내면 상태 쪽이 터지고 — notify의 fail-open이 그걸 **삼켜서**
       "알림이 안 갔다"로만 보인다(실제로 그렇게 헛다리를 짚었다). 그래서 완전한 가짜를 준다.
    """
    def _set(cost: float = 0.0, e5: int = 0):
        class _R:
            def __init__(self, v): self._v = v
            def scalar(self): return self._v

        class _DB:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, stmt, params=None):
                return _R(cost if "usage_daily" in str(stmt).lower() else e5)
            # 알림 상태 저장소 — dict 하나로 전이·쿨다운이 실제로 동작하게 한다
            def get(self, model, key): return state.get(key)
            def add(self, row): state[row.key] = row
            def commit(self): pass

        monkeypatch.setattr(alerts, "_obs_session", lambda: _DB())
    return _set


def test_cost_over_threshold_alerts(sent, agg, monkeypatch):
    """오늘 누적 비용이 임계를 넘으면 알린다 — 차단($45)보다 **일찍**($15) 안다."""
    monkeypatch.setenv("ALERT_GLOBAL_DAILY_USD", "15")
    agg(cost=17.81)  # 실측 최대(07-15)

    keys = alerts.check_daily_thresholds(NOW)

    assert "alert:cost:daily" in keys
    assert "$17.81" in sent[0].text and "$15.00" in sent[0].text


def test_cost_under_threshold_is_silent(sent, agg, monkeypatch):
    """임계 아래면 조용하다 — 평범한 날($1~5)에 울리면 아무도 안 본다."""
    monkeypatch.setenv("ALERT_GLOBAL_DAILY_USD", "15")
    agg(cost=4.40)  # 실측 평범한 날

    assert alerts.check_daily_thresholds(NOW) == []
    assert sent == []


def test_5xx_over_threshold_alerts(sent, agg, monkeypatch):
    """5xx는 실측 기준선이 6일간 총 1건 — 몇 건만 나도 이상이다."""
    monkeypatch.setenv("ALERT_5XX_DAILY", "3")
    agg(e5=9)

    assert "alert:5xx:daily" in alerts.check_daily_thresholds(NOW)
    assert sent[0].severity == "critical"


def test_thresholds_unset_means_disabled(sent, agg, monkeypatch):
    """임계 미설정이면 아무것도 안 한다 — 기존 배포 동작 그대로."""
    monkeypatch.delenv("ALERT_GLOBAL_DAILY_USD", raising=False)
    monkeypatch.delenv("ALERT_5XX_DAILY", raising=False)
    agg(cost=999.0, e5=999)

    assert alerts.check_daily_thresholds(NOW) == []
    assert sent == []


@pytest.mark.parametrize("bad", ["nan", "inf", "-5", "0", "abc"])
def test_bad_threshold_disables_that_alert(sent, agg, monkeypatch, bad):
    """비정상 임계는 그 알림만 끈다 — 특히 nan/inf.

    nan은 **모든 비교가 False**라 음수·양수 검사로 못 거른다(파이썬↔Postgres도 반대다).
    거르지 않으면 '임계가 있는데 절대 안 울리는' 상태가 되고, 그건 없는 것보다 나쁘다 —
    켜뒀다고 믿게 되니까. (같은 이유로 app/schemas/budget.py가 isfinite를 먼저 본다.)
    """
    monkeypatch.setenv("ALERT_GLOBAL_DAILY_USD", bad)
    monkeypatch.delenv("ALERT_5XX_DAILY", raising=False)
    agg(cost=999.0)

    assert alerts.check_daily_thresholds(NOW) == []
    assert sent == []


def test_recovery_below_threshold_notifies(sent, agg, monkeypatch):
    """넘었다가 내려오면 '복귀'를 알린다 — 사람이 끝난 걸 알아야 한다."""
    monkeypatch.setenv("ALERT_GLOBAL_DAILY_USD", "15")
    agg(cost=20.0)
    alerts.check_daily_thresholds(NOW)          # firing
    agg(cost=4.0)
    alerts.check_daily_thresholds(NOW + timedelta(minutes=1))  # ok

    assert len(sent) == 2
    assert "복귀" in sent[1].title


# --- 헬스 전이 알림 ---

@pytest.fixture
def health(monkeypatch):
    """`/health` 판정 결과를 원하는 대로 세운다 (의존성을 실제로 찌르지 않는다)."""
    def _set(status: str, **checks):
        import app.main as main_mod
        monkeypatch.setattr(main_mod, "compute_health",
                            lambda: {"status": status, "checks": checks or {}})
    return _set


def test_health_degraded_alerts(sent, health):
    """degraded로 **전이**하면 알린다 — 지금은 뒤집혀도 아무도 슬랙을 못 받는다."""
    health("degraded", database="ok", observability_database="fail", opensearch="ok")

    assert alerts.check_health(NOW) is True
    assert "degraded" in sent[0].title
    assert "observability_database" in sent[0].text, "어느 의존성이 죽었는지 없으면 대응을 못 한다"


def test_health_unhealthy_is_critical(sent, health):
    """앱 DB가 죽으면 critical — 서비스가 사실상 동작 불가다(503)."""
    health("unhealthy", database="fail", observability_database="ok", opensearch="ok")

    alerts.check_health(NOW)
    assert sent[0].severity == "critical"


def test_health_degraded_does_not_spam(sent, health):
    """5분마다 도는데 degraded가 이어지면 하루 288개다 — 전이 때만 보낸다."""
    health("degraded", database="ok", observability_database="fail", opensearch="ok")
    for i in range(12):  # 1시간치
        alerts.check_health(NOW + timedelta(minutes=5 * i))

    assert len(sent) == 1, f"{len(sent)}번 보냈다 — 도배다"


def test_health_degraded_to_unhealthy_alerts_immediately(sent, health):
    """🔴 **악화(degraded → unhealthy)는 쿨다운을 뚫고 즉시 알린다** (CodeRabbit #263).

    처음엔 둘 다 FIRING으로 저장해서 `_should_notify`가 "같은 상태"로 보고 쿨다운에 걸렸다 —
    **degraded 중에 앱 DB가 죽어도 critical 알림이 안 갔다.** 가장 알려야 할 순간을 놓친 것이다.
    status를 그대로 전이 토큰으로 쓰면 값이 달라져 전이로 잡힌다.
    """
    health("degraded", database="ok", observability_database="fail", opensearch="ok")
    alerts.check_health(NOW)
    assert len(sent) == 1

    # 쿨다운(60분) 한참 안, 겨우 1분 뒤 — 그런데 상태가 악화됐다
    health("unhealthy", database="fail", observability_database="fail", opensearch="ok")
    assert alerts.check_health(NOW + timedelta(minutes=1)) is True, (
        "degraded→unhealthy 악화가 쿨다운에 묻혔다 — 앱 DB가 죽었는데 아무도 모른다")
    assert len(sent) == 2
    assert sent[1].severity == "critical"


def test_health_same_degraded_still_throttled(sent, health):
    """단, **같은** degraded가 이어지는 건 여전히 쿨다운에 막힌다 — 악화 감지가 도배를 열면 안 된다."""
    health("degraded", database="ok", observability_database="fail", opensearch="ok")
    for i in range(12):
        alerts.check_health(NOW + timedelta(minutes=5 * i))

    assert len(sent) == 1


def test_health_recovery_notifies(sent, health):
    """복귀도 알린다 — 사람이 끝난 걸 알아야 한다."""
    health("degraded", database="ok", observability_database="fail", opensearch="ok")
    alerts.check_health(NOW)
    health("healthy", database="ok", observability_database="ok", opensearch="ok")
    alerts.check_health(NOW + timedelta(minutes=5))

    assert len(sent) == 2 and "복귀" in sent[1].title


def test_health_healthy_from_start_is_silent(sent, health):
    """처음부터 정상이면 조용 — 기동할 때마다 "정상입니다"는 소음이다."""
    health("healthy", database="ok", observability_database="ok", opensearch="ok")

    assert alerts.check_health(NOW) is False
    assert sent == []


def test_alert_and_endpoint_share_one_judge(sent, monkeypatch):
    """알림과 `/health` 엔드포인트가 **같은 판정 함수**를 쓰는지 — 재구현 금지.

    복사하면 반드시 갈린다 — 한쪽에 체크를 추가하고 다른 쪽을 잊으면 "/health는 degraded인데
    알림은 조용"이 된다. 이 프로젝트에서 그 계열 버그를 여러 번 냈다(CONVENTIONS §9).

    ⚠️ 처음엔 `inspect.getsource(...)`에서 "compute_health" **문자열**을 찾았다. 장식이었다 —
       **docstring에 그 단어가 있어서** 판정 코드를 통째로 지워도 통과했다(prove_teeth.py가
       잡았다). 언급이 아니라 **호출**을 봐야 한다. 그래서 하나를 가짜로 바꾸고 **양쪽이 다
       그 가짜를 따라오는지** 본다.
    """
    from fastapi.testclient import TestClient

    import app.main as main_mod

    calls = []

    def _fake_judge():
        calls.append(1)
        return {"status": "degraded", "checks": {"opensearch": "fail"}, "observability_shared": False}

    monkeypatch.setattr(main_mod, "compute_health", _fake_judge)

    # ① 알림이 그 판정을 따르나
    assert alerts.check_health(NOW) is True, "알림이 가짜 판정을 안 따랐다 — 재구현했다"
    assert calls, "check_health가 compute_health를 안 불렀다"
    assert "opensearch" in sent[0].text

    # ② 엔드포인트도 같은 판정을 따르나
    before = len(calls)
    with TestClient(main_mod.app) as c:
        r = c.get("/health")
    assert r.json()["status"] == "degraded", "/health가 가짜 판정을 안 따랐다 — 판정이 둘이다"
    assert len(calls) > before


# --- fail-open: 알림이 서비스를 죽이면 안 된다 ---

def test_slack_failure_does_not_raise(monkeypatch):
    """슬랙이 죽어도 예외가 새면 안 된다 — 관측이 본체를 망가뜨리는 것이다."""
    monkeypatch.setenv("SLACK_WEBHOOK_URL", HOOK)

    def _boom(a):
        raise RuntimeError("slack down")

    monkeypatch.setattr(alerts, "_post", _boom)

    assert alerts.notify(_alert(), alerts.FIRING, NOW) is False  # 예외가 아니라 False


def test_state_db_failure_still_sends(sent, monkeypatch):
    """🔴 상태 DB가 죽어도 **발송은 한다** (CodeRabbit #263).

    처음엔 정반대로 짰다 — 상태 조회가 실패하면 `notify`가 False를 반환했고, 그걸
    `test_state_db_failure_does_not_raise`가 **정답으로 못 박고 있었다**.

    자기모순이다: 관측 DB가 죽으면 → 상태 조회 실패 → 침묵. **하필 그때가 "관측 DB가
    죽었다"를 알려야 할 때다.** 도배 방지(상태)는 부가 기능이고 **통지가 본질**이다.
    DB 장애 시엔 프로세스 로컬 폴백 스로틀로 최소한만 막고 보낸다.
    """
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(alerts, "_obs_session", _boom)
    alerts._fallback_state.clear()

    assert alerts.notify(_alert(), alerts.FIRING, NOW) is True, (
        "상태 DB가 죽었다고 알림을 접었다 — 그때가 알려야 할 때다")
    assert len(sent) == 1


def test_state_db_failure_still_throttles(sent, monkeypatch):
    """단, 폴백에서도 도배는 막는다 — DB가 죽었다고 5분마다 288개를 쏘면 안 된다."""
    monkeypatch.setattr(alerts, "_obs_session", lambda: (_ for _ in ()).throw(RuntimeError("db")))
    alerts._fallback_state.clear()

    for i in range(12):
        alerts.notify(_alert(), alerts.FIRING, NOW + timedelta(minutes=5 * i))

    assert len(sent) == 1, f"폴백 스로틀이 안 듣는다 — {len(sent)}번 보냈다"


def test_fallback_new_outage_after_recovery_alerts(sent, monkeypatch):
    """🔴 폴백에서도 **복구 후 재발은 새 사건**이다 (CodeRabbit #263 2차).

    `(key, status)`별 시각만 저장하던 첫 구현은 FIRING→OK→FIRING이 쿨다운 안에 오면
    두 번째 FIRING이 첫 번째의 타임스탬프에 걸려 **새 장애를 침묵**시켰다.
    복구됐다가 다시 터진 건 반복이 아니라 새 장애다 — DB 쪽 전이 판정과 같아야 한다.
    """
    monkeypatch.setattr(alerts, "_obs_session", lambda: (_ for _ in ()).throw(RuntimeError("db")))
    alerts._fallback_state.clear()

    assert alerts.notify(_alert(), alerts.FIRING, NOW) is True
    assert alerts.notify(_alert(), alerts.OK, NOW + timedelta(minutes=5)) is True
    assert alerts.notify(_alert(), alerts.FIRING, NOW + timedelta(minutes=10)) is True, (
        "복구 후 10분 만에 다시 터졌는데 쿨다운으로 삼켰다 — 새 장애가 묻힌다")
    assert len(sent) == 3


def test_fallback_first_ok_is_silent(sent, monkeypatch):
    """폴백에서도 처음 본 OK는 침묵 — DB 경로와 같은 규칙."""
    monkeypatch.setattr(alerts, "_obs_session", lambda: (_ for _ in ()).throw(RuntimeError("db")))
    alerts._fallback_state.clear()

    assert alerts.notify(_alert(), alerts.OK, NOW) is False
    assert sent == []


# --- SSRF: 웹훅 목적지 허용목록 ---

@pytest.mark.parametrize("bad", [
    "https://evil.example.com/services/T0/B0/x",   # 다른 호스트 — 알림 본문이 임의 목적지로
    "http://hooks.slack.com/services/T0/B0/x",     # 평문 — 토큰이 그대로 노출
    "https://hooks.slack.com.evil.com/x",          # 접미사 위장
    "not-a-url",
])
def test_webhook_rejects_non_slack_destinations(monkeypatch, bad):
    """웹훅이 Slack 형식이 아니면 **미설정과 동일**하게 비활성 (CodeRabbit #263 2차, SSRF).

    이 URL로 알림 본문(비용·사용자 라벨·의존성 상태)을 POST하므로, env가 오염되면
    내부 정보가 임의 목적지로 나간다. https + hooks.slack.com만 허용한다.
    """
    monkeypatch.setenv("SLACK_WEBHOOK_URL", bad)
    alerts._bad_url_warned = False

    assert alerts.enabled() is False, f"허용 안 된 목적지를 통과시켰다: {bad[:40]}"


def test_webhook_accepts_real_slack_form(monkeypatch):
    """정상 형식은 통과 — 허용목록이 기능을 죽이면 안 된다."""
    monkeypatch.setenv("SLACK_WEBHOOK_URL", HOOK)

    assert alerts.enabled() is True


def test_state_db_failure_does_not_raise(monkeypatch):
    """어느 경우든 예외가 새면 안 된다 — 알림이 서비스를 죽이면 안 된다."""
    monkeypatch.setenv("SLACK_WEBHOOK_URL", HOOK)
    monkeypatch.setattr(alerts, "_obs_session", lambda: (_ for _ in ()).throw(RuntimeError("db")))
    monkeypatch.setattr(alerts, "_post", lambda a: (_ for _ in ()).throw(RuntimeError("slack")))
    alerts._fallback_state.clear()

    # 상태 DB도 슬랙도 죽었다 — 그래도 예외는 안 난다
    alerts.notify(_alert(), alerts.FIRING, NOW)
