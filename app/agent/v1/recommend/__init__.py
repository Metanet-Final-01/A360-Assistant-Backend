"""recommend 서브패키지 — AnalysisResult → Recommendation(A360 액션 트리) 스트림.

INTERFACES §4 ②. 공개 진입점은 recommend() 하나이며 app.agent가 re-export한다.
그래프 구조: plan → Send(단계별 병렬) → step(shortlist→compose→check) → assemble.
"""

from .graph import recommend

__all__ = ["recommend"]
