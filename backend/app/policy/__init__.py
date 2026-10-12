"""Versioned server policy. Values come from docs/decisions.md, access-tiers.md and voice-and-domain-controls.md."""

from pydantic import BaseModel, ConfigDict


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TokenPolicy(_Frozen):
    max_user_message_tokens: int = 2_000
    classifier_input_tokens: int = 2_048
    classifier_output_tokens: int = 64
    planner_call_input_tokens: int = 12_000
    planner_call_output_tokens: int = 1_200
    run_input_tokens: int = 40_000
    run_output_tokens: int = 4_000
    run_model_calls: int = 6
    run_tool_calls: int = 12
    run_escalations: int = 1
    visible_reply_tokens: int = 450
    spoken_reply_tokens: int = 160
    run_deadline_seconds: int = 20


class DailyTokenCeiling(_Frozen):
    input_tokens: int
    output_tokens: int


class Allowance(_Frozen):
    user_turns: int
    activated_trips: int
    period_days: int | None  # None = lifetime


class EntitlementPolicy(_Frozen):
    version: str
    guest: Allowance = Allowance(user_turns=50, activated_trips=10, period_days=None)
    registered_free: Allowance = Allowance(user_turns=100, activated_trips=10, period_days=30)
    guest_daily: DailyTokenCeiling = DailyTokenCeiling(input_tokens=60_000, output_tokens=6_000)
    registered_daily: DailyTokenCeiling = DailyTokenCeiling(input_tokens=120_000, output_tokens=12_000)
    guest_workspace_retention_days: int = 7
    notice_quiet_meter_ratio: float = 0.7
    notice_benefit_ratio: float = 0.9
    tokens: TokenPolicy = TokenPolicy()


OUT_OF_SCOPE_REPLY = (
    "I can help plan trips and outings. Tell me where you want to go, "
    "what you want to do, or when you need to arrive or return."
)

CURRENT_POLICY = EntitlementPolicy(version="2026-10-11.1")
