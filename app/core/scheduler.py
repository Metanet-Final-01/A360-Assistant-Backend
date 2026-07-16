"""APScheduler 배치 (RPA-104) — 일별 롤업·retention을 주기 실행한다.

lifespan에서 start_scheduler()로 켜고 종료 시 stop_scheduler(). 설계:
- 주기(기본 60분)마다 run_rollup(오늘+어제 멱등 재집계 + retention) — 대시보드가
  "오늘" 데이터도 신선하게 보게. 멱등이라 몇 번 돌아도 안전.
- 시작 직후 1회 캐치업(최근 7일) — 서버가 꺼져 있던 날의 공백을 메운다.
- METRICS_ROLLUP_ENABLED=false면 아예 안 켠다 (테스트는 conftest가 끔).
- 스케줄러/잡 실패가 앱을 죽이면 안 된다 — 전부 best-effort.
"""

import logging
import os

logger = logging.getLogger(__name__)

_scheduler = None


def _rollup_job() -> None:
    from app.services.rollup import run_rollup

    run_rollup(days_back=1)  # 오늘+어제 (멱등)


def _catchup_job() -> None:
    from app.services.rollup import run_rollup

    run_rollup(days_back=7)  # 시작 캐치업 — 꺼져 있던 날들의 공백 메움


def _health_job() -> None:
    """의존성 헬스를 찔러 전이 시 알린다 (RPA-189).

    롤업(60분)에 얹지 않고 별도 잡을 둔 이유: **의존성이 죽은 걸 1시간 뒤에 알면 늦다.**
    반대로 너무 잦으면 DB·OpenSearch를 쓸데없이 찌른다 — 기본 5분.
    전이·쿨다운은 alerts가 하므로 여기서 도배 걱정은 없다.
    """
    from app.services import alerts

    alerts.check_health()


def start_scheduler() -> bool:
    """롤업 스케줄러를 켠다. 비활성/실패 시 False (앱 기동은 계속)."""
    global _scheduler
    if os.getenv("METRICS_ROLLUP_ENABLED", "true").strip().lower() in ("false", "0", "no"):
        logger.info("롤업 스케줄러 비활성 (METRICS_ROLLUP_ENABLED)")
        return False
    try:
        from apscheduler.schedulers.background import BackgroundScheduler

        try:
            interval = int(os.getenv("ROLLUP_INTERVAL_MINUTES", "60"))
        except ValueError:
            interval = 60
        from apscheduler.executors.pool import ThreadPoolExecutor

        # 단일 워커 + max_instances=1 — 캐치업과 주기 잡(또는 밀린 주기 잡끼리)이 같은
        # 날짜를 동시에 DELETE→INSERT 하는 프로세스 내 경쟁을 원천 차단(CodeRabbit #164).
        # 프로세스 간(팀원 여러 명) 경쟁은 단일 트랜잭션 + PK가 한 승자를 보장하고,
        # 패자는 다음 주기에 멱등 재집계되므로 안전.
        _scheduler = BackgroundScheduler(
            daemon=True,
            executors={"default": ThreadPoolExecutor(1)},
            job_defaults={"max_instances": 1, "coalesce": True},
        )
        _scheduler.add_job(_rollup_job, "interval", minutes=max(1, interval), id="metrics_rollup")
        # 시작 캐치업 — 서버가 꺼져 있던 날들의 집계 공백을 메운다 (별도 스레드, 기동 안 막음)
        _scheduler.add_job(_catchup_job, id="rollup_catchup")

        # 헬스 감시 (RPA-189) — SLACK_WEBHOOK_URL 미설정이면 alerts가 no-op이라 기존 배포 무변화.
        # 롤업(60분)과 분리한 이유는 _health_job docstring 참고(1시간 뒤에 알면 늦다).
        try:
            health_min = int(os.getenv("ALERT_HEALTH_INTERVAL_MINUTES", "5"))
        except ValueError:
            health_min = 5
        _scheduler.add_job(
            _health_job, "interval", minutes=max(1, health_min), id="health_watch")

        _scheduler.start()
        logger.info("스케줄러 시작 (롤업 매 %d분 + 시작 캐치업 7일, 헬스 감시 매 %d분)",
                    interval, health_min)
        return True
    except Exception:  # noqa: BLE001 — 스케줄러 실패가 앱 기동을 막으면 안 됨
        logger.warning("롤업 스케줄러 시작 실패 (앱은 계속)", exc_info=True)
        return False


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass
        _scheduler = None
