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
import os
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


def _obs_session():
    """관측 DB 세션 — 미설정이면 앱 DB로 폴백(observability_db가 알아서 한다)."""
    from app.core.observability_db import observability_sessionmaker

    return observability_sessionmaker()()


def _should_notify(key: str, status: str, now: datetime) -> bool:
    """이 알림을 지금 보낼까? **전이면 보내고, 이어지면 쿨다운 주기로만 보낸다.**

    한 프리미티브로 두 가지를 판단한다:
      - 상태 전이(ok→firing, firing→ok) → 항상 보낸다. 사람이 "터졌다"/"끝났다"를 알아야 한다.
      - 같은 firing이 이어짐 → 쿨다운이 지났을 때만("아직도 터져 있다"). 안 그러면 도배.
      - 같은 ok가 이어짐 → 안 보낸다. 정상은 뉴스가 아니다.

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
    """Slack Incoming Webhook 발송. 실패는 삼키고 로깅만(fail-open)."""
    import httpx

    icon = {"critical": "🔴", "warning": "🟠", "info": "🔵"}.get(alert.severity, "🟠")
    payload = {
        "text": f"{icon} *{alert.title}*\n{alert.text}",
    }
    try:
        r = httpx.post(webhook_url(), json=payload, timeout=5.0)
        r.raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001 — 알림 실패가 서비스를 죽이면 안 된다(fail-open)
        logger.warning("Slack 알림 발송 실패 (서비스는 계속): %s — %s", alert.key, e)
        return False


def notify(alert: Alert, status: str = FIRING, now: datetime | None = None) -> bool:
    """알림을 보낸다(보낼 만하면). 보냈으면 True.

    호출부는 "이 조건이 지금 firing인가"만 판단해 넘기면 된다 — 도배 방지는 여기가 한다.
    status=OK로 부르면 "복구됐다"는 뜻이고, 직전이 firing이었을 때만 실제로 발송된다.
    """
    if not enabled():
        return False  # 미설정=비활성 — 기존 동작 그대로

    now = now or datetime.now(timezone.utc)
    try:
        if not _should_notify(alert.key, status, now):
            return False
        sent = _post(alert)
        _record(alert.key, status, json.dumps(
            {"title": alert.title, "text": alert.text}, ensure_ascii=False), now, sent)
        return sent
    except Exception as e:  # noqa: BLE001 — 상태 DB 장애도 서비스를 죽이면 안 된다
        logger.warning("알림 처리 실패 (서비스는 계속): %s — %s", alert.key, e)
        return False
