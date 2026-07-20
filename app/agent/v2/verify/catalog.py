"""카탈로그 조회 인터페이스 — (package, action) → 구조 스펙.

구조 스펙은 백엔드가 JAR `package.json`에서 정규화해 `rag_documents.metadata.schema`에
저장해 둔 것과 같은 형태다(jar_parser 산출: name/label/type/required/options/default).
검수 하네스(checker)는 RAG 청크가 아니라 이 구조 스펙으로 검증한다 — 렌더 문자열은
파라미터 기계명·enum이 손실돼 있어 골드셋 표기 검사에 못 쓴다.

에이전트는 DB에 직접 접근하지 않으므로(INTERFACES 소유권), 실제 조회는 백엔드
서비스(`app.services.catalog.BackendCatalog`)에 위임한다 — `search_actions`(RPA-9)와
같은 경계. 테스트는 인프라 없이 돌아야 하므로 `tests/conftest.py`의 autouse fixture가
`_make_catalog`를 인메모리 스텁(tests/agent_stubs.FakeCatalog)으로 주입한다.

스펙 dict 형태:
    {
      "package": "Excel_MS", "action": "GoToCell", "label": "셀로 이동",
      "return_type": None,
      "parameters": [
        {"name": "cellOption", "label": "셀 옵션", "type": "RADIO", "required": True,
         "options": [{"label": "특정 셀", "value": "specific"}, ...]},
        {"name": "session", "label": "세션 이름", "type": "SESSION", "required": True},
      ],
    }
"""

from typing import Protocol


class CatalogLookup(Protocol):
    """구조 스펙 조회 계약. 없는 액션이면 None."""

    def get_action_schema(self, package: str, action: str) -> dict | None: ...


def _make_catalog() -> CatalogLookup:
    """실제 백엔드 카탈로그 조회기를 만든다. 테스트는 conftest가 이 함수를 스텁으로 patch한다.

    지연 임포트 — 카탈로그를 실제로 쓸 때만 백엔드 서비스(→ DB)에 의존하게 한다.
    get_catalog가 이 모듈 전역을 호출하므로, 사용처의 from-import 참조를 건드리지 않고
    이 함수만 갈아끼우면 된다. get_backend_catalog는 프로세스당 1개 인스턴스를
    재사용(내부 인덱스 lazy 캐싱)하므로 여기서 별도 캐싱은 두지 않는다.
    """
    from app.services.catalog import get_backend_catalog

    return get_backend_catalog()


def get_catalog() -> CatalogLookup:
    """checker·shortlist·harness가 쓰는 실제 카탈로그 조회기를 반환한다."""
    return _make_catalog()
