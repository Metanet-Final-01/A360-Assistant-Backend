import os
from pathlib import Path

# 프로젝트 루트의 .env를 있으면 로드 (python-dotenv 없거나 파일 없으면 조용히 통과)
try:
    from dotenv import load_dotenv

    # parents[3] = 리포 루트 (이 파일은 app/agent/v1/config.py — 버전 폴더로 한 단계 깊다).
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
except ImportError:
    pass

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
# 기본은 가벼운 모델. 교체는 코드 수정 없이 .env의 OPENAI_MODEL로 한다.
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")

# 그래프 전체 동시 LLM 호출 상한 (rate limit 방어). orchestrator와 recommend 서브그래프가
# 공유한다 — 값이 갈리면 중첩 실행(orchestrator→recommend)의 동시성 예산이 어긋나므로
# 한 곳에서 관리한다. 배포 환경별 rate limit에 맞춰 .env로 조정 가능(다른 설정과 일관).
MAX_LLM_CONCURRENCY = int(os.getenv("MAX_LLM_CONCURRENCY", "3"))
