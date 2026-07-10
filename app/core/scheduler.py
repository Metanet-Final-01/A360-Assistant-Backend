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
        _scheduler = BackgroundScheduler(daemon=True)
        _scheduler.add_job(_rollup_job, "interval", minutes=max(1, interval), id="metrics_rollup")
        # 시작 캐치업 — 서버가 꺼져 있던 날들의 집계 공백을 메운다 (별도 스레드, 기동 안 막음)
        _scheduler.add_job(_catchup_job, id="rollup_catchup")
        _scheduler.start()
        logger.info("롤업 스케줄러 시작 (매 %d분, 시작 캐치업 7일)", interval)
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
