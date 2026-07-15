"""recommend 서브패키지 — AnalysisResult → Recommendation(A360 액션 트리) 스트림.

INTERFACES §4 ②. 공개 진입점은 recommend() 하나이며 app.agent가 re-export한다.
그래프 구조(에이전트형): compose_agent ⇄ tools(search_kb/get_action_schema) → verify(검수
하네스 게이트) → finalize. 단계 분해로 액션을 1:1 매핑하던 옛 구조(plan→step→assemble)를
폐기하고, 에이전트가 카탈로그를 직접 조사하며 흐름도 전체를 설계한다.
"""

from .graph import recommend

__all__ = ["recommend"]
