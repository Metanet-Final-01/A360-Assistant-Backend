"""예산 상한 런타임 오버라이드 계약 (RPA-173).

RPA-149(retrieval_params)와 같은 규칙: **부분 갱신 아님 — 전체 스냅샷**. append-only 이력에
"그 시점의 완전한 설정"을 남겨야 감사·롤백이 명확하다(누락 필드가 이전 행에서 암묵 상속되면
어떤 조합으로 돌았는지 추적이 흐려진다). 예산은 서비스를 막는 값이라 이 추적이 특히 중요하다.
"""

from pydantic import BaseModel, Field, model_validator


class BudgetLimitsUpdate(BaseModel):
    """PUT /api/admin/budget-limits 본문. 4개 상한 모두 명시(비활성은 null).

    null = 그 상한 비활성 — .env의 '미설정=비활성'과 같은 의미다.
    """

    subject_daily_usd: float | None = Field(
        None, description="주체별(로그인=user, 익명=session) 일 상한 USD. null이면 비활성")
    subject_monthly_usd: float | None = Field(None, description="주체별 월 상한 USD. null이면 비활성")
    global_daily_usd: float | None = Field(
        None, description="서비스 전체 일 상한 USD (청구서 보호). null이면 비활성")
    global_monthly_usd: float | None = Field(None, description="서비스 전체 월 상한 USD. null이면 비활성")

    @model_validator(mode="after")
    def _sane(self) -> "BudgetLimitsUpdate":
        """0·음수 거부, 월 < 일 거부.

        월이 일보다 작으면 일 상한이 영원히 도달 불가능해져 **일 상한이 조용히 무의미**해진다 —
        설정자가 의도한 바가 아닐 확률이 압도적이라 거부한다(값 자체는 유효해 보이므로 검증이 없으면
        아무도 눈치채지 못한다).
        """
        for name in ("subject_daily_usd", "subject_monthly_usd",
                     "global_daily_usd", "global_monthly_usd"):
            v = getattr(self, name)
            if v is not None and v <= 0:
                raise ValueError(f"{name}는 0보다 커야 합니다 (비활성은 null): {v}")
        for daily, monthly in (("subject_daily_usd", "subject_monthly_usd"),
                               ("global_daily_usd", "global_monthly_usd")):
            d, m = getattr(self, daily), getattr(self, monthly)
            if d is not None and m is not None and m < d:
                raise ValueError(
                    f"{monthly}({m})가 {daily}({d})보다 작습니다 — 일 상한이 무의미해집니다")
        return self
