import os
from pathlib import Path

# 프로젝트 루트의 .env를 있으면 로드 (python-dotenv 없거나 파일 없으면 조용히 통과)
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except ImportError:
    pass

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
# 기본은 가벼운 모델. 교체는 코드 수정 없이 .env의 OPENAI_MODEL로 한다.
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
