"""추천 검수 하네스 — 생성된 액션 트리를 카탈로그 메타데이터로 검사한다.

recommend/chat_refine가 공유하는, LLM 없는 결정론적 검증기다. LLM이 지어낸
액션·표기 오류·필수값 누락을 기계가 잡아낸다(우리 팀 차별점).

- catalog: (package, action) → 구조 스펙 조회 인터페이스 (백엔드 get_action_schema를
  감싸는 자리, 아직은 개발용 FakeCatalog).
- checker: R1~R6 정적 체커 (트리 1회 순회).

R7~R8 심볼릭 dryrun(세션·변수 흐름)은 후속(RPA-27b) 범위.
"""

from .catalog import CatalogLookup, FakeCatalog, get_catalog
from .checker import Violation, run_checks

__all__ = ["CatalogLookup", "FakeCatalog", "Violation", "get_catalog", "run_checks"]
