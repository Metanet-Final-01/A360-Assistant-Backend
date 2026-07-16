"""Slack 알림 (RPA-189) — 관측의 마지막 한 걸음.

`meter → enforce → alert`에서 alert가 비어 있었다. 수집(llm_usage·request_metrics·rag_events
·turn_events·audit_logs)도 통제(예산 가드레일이 429로 차단)도 있는데, **아무도 안 알렸다.**
예산을 넘겨 사용자를 막아도 대시보드를 열어봐야만 알았다.

**미설정=비활성.** `SLACK_WEBHOOK_URL`이 없으면 이 모듈은 아무것도 하지 않는다 —
로컬 개발·CI는 기존 동작 그대로다. 켜는 건 배포 인스턴스(또는 원하는 개발자)뿐.

**fail-open.** 슬랙이 죽어도 요청은 살아야 한다. 알림 실패로 /turn이 500나면 관측이 서비스를
망가뜨리는 것이다 — 관측은 곁다리지 본체가 아니다.

**전이 + 쿨다운.** 같은 사유로 계속 쏘면 아무도 안 본다. `/health`가 30초마다 degraded면
하루 2,880개다. 상태가 바뀔 때(ok↔firing) 알리고, 계속 터져 있으면 쿨다운 주기로만 재알림한다.
그 상태는 **DB**(alert_state)에 둔다 — 인메모리면 재시작마다·워커마다 중복 발송이다.
"""

import json
import logging
import math
import os
import sys as _sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

FIRING = "firing"
OK = "ok"

_DEFAULT_COOLDOWN_MIN = 60


def webhook_url() -> str:
    """참조 시점에 읽는다 — 테스트가 monkeypatch로 켜고 끌 수 있게(import 시점 고정 금지)."""
    return os.getenv("SLACK_WEBHOOK_URL", "").strip()


def enabled() -> bool:
    return bool(webhook_url())


def _cooldown() -> timedelta:
    """같은 사유가 이어질 때 재알림 주기. 0이면 재알림 없음(전이 때만)."""
    try:
        m = int(os.getenv("ALERT_COOLDOWN_MINUTES", str(_DEFAULT_COOLDOWN_MIN)))
    except ValueError:
        m = _DEFAULT_COOLDOWN_MIN
    return timedelta(minutes=max(0, m))


@dataclass(frozen=True)
class Alert:
    """보낼 알림 하나.

    key: 알림 종류 식별자 — 이 값 단위로 전이·쿨다운을 판단한다.
         예: "budget:global:daily" · "budget:subject:<uuid>:daily" · "health:degraded"
    """

    key: str
    title: str
    text: str
    severity: str = "warning"  # info | warning | critical


def budget_exceeded(verdict, subject_label: str) -> Alert:
    """예산 초과 429를 알림으로 — enforce가 실제로 사용자를 막은 순간.

    **key에 주체를 넣는다.** `budget:subject:<id>:daily`처럼 분리하지 않으면 한 사용자의
    쿨다운이 다른 사용자의 알림을 삼킨다. 전역은 주체가 없으므로 `budget:global:daily` 하나다
    (전원이 같은 사유로 막히니 알림도 하나가 맞다).

    ⚠️ 429 응답(`budget.exceeded_detail`)은 전역 초과 때 사용량을 **가린다** — 남의 합계가
    새어나가기 때문이다. 여기 슬랙은 **내부 운영 채널**이라 수치를 담는다. 그게 chargeback의
    요점이다("누가 얼마 썼나"). 채널을 외부에 공유하면 이 전제가 깨진다.
    """
    scope, period = verdict.scope or "?", verdict.period or "?"
    if scope == "global":
        key = f"budget:global:{period}"
        who = "서비스 전체"
    else:
        key = f"budget:subject:{subject_label}:{period}"
        who = subject_label

    spent = f"${verdict.spent_usd:.2f}" if verdict.spent_usd is not None else "?"
    limit = f"${verdict.limit_usd:.2f}" if verdict.limit_usd is not None else "?"
    text = (f"• 주체: {who}\n"
            f"• 사용: {spent} / 상한 {limit} ({period})\n"
            f"• 해제: {verdict.resets_at or '?'}\n"
            f"→ 해당 요청은 **429로 차단**됐습니다.")
    return Alert(
        key=key,
        title=f"LLM 예산 초과 — {who} ({period})",
        text=text,
        severity="critical" if scope == "global" else "warning",
    )


def _threshold(name: str) -> float | None:
    """알림 임계 — 미설정/비정상이면 None(그 알림 비활성)."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
    except ValueError:
        logger.warning("%s가 숫자가 아니라 무시한다: %r", name, raw)
        return None
    if not math.isfinite(v) or v <= 0:
        # nan/inf는 비교가 전부 False라 '임계 없음'과 구분이 안 된다 — 명시적으로 끈다.
        # (app/schemas/budget.py가 같은 이유로 isfinite를 양수 검사보다 먼저 한다.)
        logger.warning("%s가 비정상(%r)이라 무시한다", name, raw)
        return None
    return v


def check_daily_thresholds(now: datetime | None = None) -> list[str]:
    """집계 테이블을 보고 임계 초과를 알린다. 보낸 알림의 key 목록을 돌려준다.

    **롤업 직후에 부른다** — metrics_daily·usage_daily가 방금 갱신된 그 자리다. 별도 스케줄러를
    두면 집계와 알림이 어긋나(집계 전 값으로 판정) 있지도 않은 급등을 알린다.

    ⚠️ **오늘(당일)을 본다.** 완결일이 아니라 '지금까지 누적'이다 — 알림은 **지금 대응하라**는
    신호라서 하루가 끝나길 기다리면 늦다. (반대로 **임계를 산출**할 땐 완결일만 써야 한다.
    당일을 포함해 산출했다가 권장값이 실측 최대보다 낮아진 적이 있다 — RPA-171 → #258.)

    ⚠️ 4xx는 보지 않는다. 실측 2.2~23%로 튄다(테스트 401/404) — 켜면 상시 울려 무시당한다.
       5xx는 6일간 총 1건(0.1%)이라 몇 건만 나도 이상 신호다.
    """
    from sqlalchemy import text

    now = now or datetime.now(timezone.utc)
    today = now.date()
    sent: list[str] = []

    cost_limit = _threshold("ALERT_GLOBAL_DAILY_USD")
    err_limit = _threshold("ALERT_5XX_DAILY")
    if not enabled() or (cost_limit is None and err_limit is None):
        return sent  # 미설정=비활성

    try:
        with _obs_session() as db:
            if cost_limit is not None:
                spent = db.execute(text(
                    "select coalesce(sum(cost_usd), 0) from usage_daily where day = :d"
                ), {"d": today}).scalar() or 0.0
                firing = float(spent) > cost_limit
                a = Alert(
                    key="alert:cost:daily",
                    title=("LLM 일 비용 임계 초과" if firing else "LLM 일 비용 정상 복귀"),
                    text=(f"• 오늘({today}) 누적: ${float(spent):.2f} / 임계 ${cost_limit:.2f}\n"
                          f"→ 차단은 BUDGET_GLOBAL_DAILY_USD에서 별도로 겁니다(알림은 일찍, 차단은 늦게)."),
                    severity="warning",
                )
                if notify(a, FIRING if firing else OK, now):
                    sent.append(a.key)

            if err_limit is not None:
                e5 = db.execute(text(
                    "select coalesce(sum(err_5xx), 0) from metrics_daily where day = :d"
                ), {"d": today}).scalar() or 0
                firing = int(e5) > err_limit
                a = Alert(
                    key="alert:5xx:daily",
                    title=("5xx 급증" if firing else "5xx 정상 복귀"),
                    text=(f"• 오늘({today}) 5xx: {int(e5)}건 / 임계 {err_limit:.0f}건\n"
                          f"→ 실측 기준선은 6일간 총 1건입니다 — 몇 건만 나도 이상입니다."),
                    severity="critical",
                )
                if notify(a, FIRING if firing else OK, now):
                    sent.append(a.key)
    except Exception:  # noqa: BLE001 — 알림 판정 실패가 롤업을 죽이면 안 된다
        # exc_info=True 금지 — 스택트레이스가 웹훅 URL(토큰)을 싣는다(실측 확인).
        logger.warning("임계 알림 판정 실패 — error=%s", type(_sys.exc_info()[1]).__name__)
    return sent


def check_health(now: datetime | None = None) -> bool:
    """`/health`가 정상이 아니면 알린다. 보냈으면 True.

    ⚠️ **판정을 재구현하지 않는다** — `app.main.compute_health()`를 그대로 부른다. `/health`
    엔드포인트가 쓰는 바로 그 함수다. 복사하면 반드시 갈린다(한쪽에 체크를 추가하고 다른 쪽을
    잊으면 "/health는 degraded인데 알림은 조용"). 가드가 읽는 것 == 동작이 읽는 것.

    ⚠️ **전이만 알린다.** 5분마다 도는데 degraded가 이어지면 하루 288개다 — notify()의
    전이·쿨다운이 그걸 막는다.

    🔴 **한계: 앱이 죽으면 이 잡도 죽는다.** 프로세스 다운·OOM·네트워크 단절은 **자기가 못
    알린다**. 그건 앱 밖에서 봐야 한다(백오피스 프로브 — RPA-189 코멘트 참고). 여기가 잡는 건
    "앱은 살아 있는데 의존성이 죽은" degraded/unhealthy다.
    """
    if not enabled():
        return False
    try:
        from app.main import compute_health

        result = compute_health()
        status = result.get("status")
        failed = [k for k, v in (result.get("checks") or {}).items() if v != "ok"]
        firing = status != "healthy"

        if firing:
            title = f"백엔드 {status}"
            text = ("• 실패한 의존성: " + ", ".join(failed) + "\n"
                    + ("→ **앱 DB가 죽었습니다** — 서비스가 사실상 동작 불가입니다(503)."
                       if status == "unhealthy" else
                       "→ 본 기능은 살아 있으나 반쯤 죽은 상태입니다"
                       "(관측 유실 또는 BM25 없이 dense 검색만)."))
        else:
            title, text = "백엔드 정상 복귀", "• 모든 의존성 ok"

        # 🔴 status를 **그대로** 전이 토큰으로 쓴다 (CodeRabbit #263).
        # degraded/unhealthy를 둘 다 FIRING으로 뭉개면 `_should_notify`가 "같은 상태"로 보고
        # 쿨다운에 걸린다 → **degraded 중에 앱 DB가 죽어 unhealthy가 돼도 알림이 안 간다.**
        # 가장 알려야 할 악화가 묻히는 것이다. healthy/degraded/unhealthy를 구분하면
        # degraded→unhealthy가 전이로 잡혀 즉시 critical이 나간다.
        # (OK 상수는 "정상"의 의미로 계속 쓴다 — healthy가 그 값이어야 복구 판정이 맞는다.)
        token = OK if status == "healthy" else str(status)
        return notify(
            Alert(key="health:deps", title=title, text=text,
                  severity="critical" if status == "unhealthy" else "warning"),
            token, now,
        )
    except Exception:  # noqa: BLE001 — 헬스 알림 실패가 스케줄러를 죽이면 안 된다
        # exc_info=True 금지 — 스택트레이스가 웹훅 URL(토큰)을 싣는다(실측 확인).
        logger.warning("헬스 알림 판정 실패 — error=%s", type(_sys.exc_info()[1]).__name__)
        return False


def _obs_session():
    """관측 DB 세션 — 미설정이면 앱 DB로 폴백(observability_db가 알아서 한다)."""
    from app.core.observability_db import observability_sessionmaker

    return observability_sessionmaker()()


def _should_notify(key: str, status: str, now: datetime) -> bool:
    """이 알림을 지금 보낼까? **전이면 보내고, 이어지면 쿨다운 주기로만 보낸다.**

    한 프리미티브로 두 가지를 판단한다:
      - **상태가 바뀜** → 항상 보낸다. 사람이 "터졌다"/"악화됐다"/"끝났다"를 알아야 한다.
      - 같은 비정상이 이어짐 → 쿨다운이 지났을 때만("아직도 터져 있다"). 안 그러면 도배.
      - 같은 OK가 이어짐 → 안 보낸다. 정상은 뉴스가 아니다.

    ⚠️ status는 FIRING/OK만이 아니다 — 호출부가 **의미 있는 토큰**을 넘길 수 있고, 그래야
       악화가 잡힌다. `check_health`는 "degraded"/"unhealthy"를 그대로 넘긴다: 둘 다 FIRING으로
       뭉개면 degraded→unhealthy가 "같은 상태"가 돼 **쿨다운에 묻힌다**(CodeRabbit #263).
       여기선 OK가 아닌 값이면 전부 "비정상"으로 다루고, 값이 다르면 전이로 본다.

    ⚠️ 상태를 **DB에서** 읽는다. 인메모리면 재시작·멀티워커에서 각자 "처음"이라 중복 발송한다.
       "스로틀했다"는 주장은 그 저장소를 모든 발신자가 공유할 때만 참이다.
    """
    from app import models

    with _obs_session() as db:
        row = db.get(models.AlertState, key)
        prev_status = row.status if row else None
        last_sent = row.last_sent_at if row else None

        if prev_status is None:
            # 이 key를 처음 본다. firing이면 알리고, **ok면 침묵** — 기록이 없다는 건 터진 적이
            # 없다는 뜻이고, 기동할 때마다 "정상입니다" 슬랙이 오면 아무도 안 본다.
            return status != OK

        if prev_status == status:
            if status == OK:
                return False  # 정상이 이어짐 — 알릴 것 없음
            cd = _cooldown()
            if not cd:
                return False  # 재알림 끔 — 전이 때만
            if last_sent is not None and (now - _aware(last_sent)) < cd:
                return False  # 쿨다운 중
        return True


_fallback_last: dict[tuple[str, str], datetime] = {}
_fallback_lock = threading.Lock()


def _fallback_should_notify(key: str, status: str, now: datetime) -> bool:
    """**상태 DB가 죽었을 때만** 쓰는 프로세스 로컬 스로틀.

    평소엔 alert_state(DB)가 전이·쿨다운을 판단한다. 그게 죽으면 통지 자체를 접는 게 아니라
    — 하필 그때가 "관측 DB가 죽었다"를 알려야 할 때다 — **최소한의 도배 방지만** 하고 보낸다.

    ⚠️ 인메모리라 한계가 명확하다: 재시작하면 초기화되고, 워커 N개면 최대 N배 발송된다.
       그래서 **평소 경로가 아니다.** DB 장애라는 예외 상황에서 "중복 몇 통"과 "침묵" 중
       중복을 택한 것이다 — 침묵은 장애를 숨기지만 중복은 시끄러울 뿐이다.
    """
    cd = _cooldown() or timedelta(minutes=_DEFAULT_COOLDOWN_MIN)
    with _fallback_lock:
        last = _fallback_last.get((key, status))
        if last is not None and (now - last) < cd:
            return False
        _fallback_last[(key, status)] = now
    return True


def _aware(dt: datetime) -> datetime:
    """naive로 돌아온 값(드라이버·DB 설정에 따라)을 UTC로 간주해 비교 가능하게."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _record(key: str, status: str, detail: str, now: datetime, sent: bool) -> None:
    """상태를 갱신한다. 보내지 않았어도 status는 기록해야 다음 전이를 판단할 수 있다."""
    from app import models

    with _obs_session() as db:
        row = db.get(models.AlertState, key)
        if row is None:
            row = models.AlertState(key=key, status=status)
            db.add(row)
        row.status = status
        row.detail = detail[:4000]
        if sent:
            row.last_sent_at = now
        db.commit()


def _post(alert: Alert) -> bool:
    """Slack Incoming Webhook 발송. 실패는 삼키고 로깅만(fail-open).

    🔴 **예외 객체를 로그에 넣지 않는다** (CodeRabbit #263). `httpx.HTTPStatusError`의 문자열은
    요청 URL을 통째로 담는다 — 실측:
        "Client error '404 Not Found' for url 'https://hooks.slack.com/services/T0/B0/<토큰>'"
    즉 `logger.warning(..., e)` 한 줄로 **웹훅 토큰이 로그에 영구 기록**된다. 그 URL을 아는
    사람은 누구나 그 채널에 글을 쓸 수 있다. 상태 코드와 예외 **타입만** 남긴다.
    """
    import httpx

    icon = {"critical": "🔴", "warning": "🟠", "info": "🔵"}.get(alert.severity, "🟠")
    payload = {"text": f"{icon} *{alert.title}*\n{alert.text}"}
    try:
        r = httpx.post(webhook_url(), json=payload, timeout=5.0)
        r.raise_for_status()
        return True
    except httpx.HTTPStatusError as e:  # noqa: PERF203 — 상태 코드만(URL·본문에 토큰이 있다)
        logger.warning("Slack 알림 발송 실패 — key=%s status=%s (서비스는 계속)",
                       alert.key, e.response.status_code)
        return False
    except Exception as e:  # noqa: BLE001 — 알림 실패가 서비스를 죽이면 안 된다(fail-open)
        # 타입만. 네트워크 예외(ConnectError 등)도 메시지에 URL을 담을 수 있다.
        logger.warning("Slack 알림 발송 실패 — key=%s error=%s (서비스는 계속)",
                       alert.key, type(e).__name__)
        return False


def notify(alert: Alert, status: str = FIRING, now: datetime | None = None) -> bool:
    """알림을 보낸다(보낼 만하면). 보냈으면 True.

    호출부는 "이 조건이 지금 firing인가"만 판단해 넘기면 된다 — 도배 방지는 여기가 한다.
    status=OK로 부르면 "복구됐다"는 뜻이고, 직전이 firing이었을 때만 실제로 발송된다.
    """
    if not enabled():
        return False  # 미설정=비활성 — 기존 동작 그대로

    now = now or datetime.now(timezone.utc)

    # 🔴 상태 DB가 죽어도 **발송은 해야 한다** (CodeRabbit #263).
    # 상태 조회 실패로 알림을 접으면, 하필 "관측 DB가 죽었다"를 알려야 할 때 침묵한다 —
    # 자기모순이다. 도배 방지(상태)는 **부가 기능**이고 통지가 본질이다.
    # 그래서 DB 장애 시엔 프로세스 로컬 폴백 스로틀로 최소한의 도배만 막고 **보낸다**.
    try:
        should = _should_notify(alert.key, status, now)
        state_ok = True
    except Exception as e:  # noqa: BLE001 — 상태 DB 장애
        logger.warning("알림 상태 조회 실패 — key=%s error=%s (폴백 스로틀로 발송 시도)",
                       alert.key, type(e).__name__)
        should, state_ok = _fallback_should_notify(alert.key, status, now), False

    if not should:
        return False

    # _post는 자체 fail-open이지만 여기서도 감싼다 — 그 구현에 기대면, 누가 _post를 바꾸는
    # 순간 알림이 서비스를 죽인다. "관측 실패는 서비스를 죽이지 않는다"는 notify 전체의 계약이다.
    # (sessions.py에서 빌더가 try 밖이라 429가 500이 된 것과 같은 교훈 — CodeRabbit #258.)
    try:
        sent = _post(alert)
    except Exception as e:  # noqa: BLE001
        logger.warning("알림 발송 실패 — key=%s error=%s", alert.key, type(e).__name__)
        sent = False

    if state_ok:
        try:
            _record(alert.key, status, json.dumps(
                {"title": alert.title, "text": alert.text}, ensure_ascii=False), now, sent)
        except Exception as e:  # noqa: BLE001 — 기록 실패가 발송을 무르지 않는다
            logger.warning("알림 상태 기록 실패 — key=%s error=%s", alert.key, type(e).__name__)
    return sent
