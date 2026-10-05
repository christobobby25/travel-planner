"""Give agents today's date so "next week" or "Nov 12" resolve to real, future dates.

Without this the model guesses the year from its training data and searches past dates.
"""
from datetime import date, timedelta

from strands.hooks import BeforeInvocationEvent, HookProvider, HookRegistry


def with_today(prompt: str) -> str:
    today = date.today()
    # Models miscount weekdays ("next Thursday" -> a Friday) when they write dates as prose, so
    # give a lookup table and require ISO dates. Measured with Haiku 4.5 on "4 nights leaving
    # next Thursday": wrong dates 3/5 runs with a plain calendar, 0/5 with this format.
    days = [today + timedelta(days=i) for i in range(1, 15)]
    next_weekday = "; ".join(f"next {d:%A} = {d.isoformat()}" for d in days[:7])
    calendar = ", ".join(f"{d:%a} {d.isoformat()}" for d in days)
    return (
        f"{prompt}\n\nToday is {today:%A} {today.isoformat()}. Look up weekdays here, never "
        f"compute them: {next_weekday}. Calendar: {calendar}. Count nights from the check-in "
        "date in this calendar. Write every date as YYYY-MM-DD, including in requests to other "
        "agents. A date without a year means its next upcoming occurrence; never use past dates."
    )


class TodayInSystemPrompt(HookProvider):
    """Refresh the date before every call. The AgentCore container builds its agents once
    and can live for days, so a date baked in at startup would go stale."""

    def __init__(self, base_prompt: str):
        self.base_prompt = base_prompt

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(BeforeInvocationEvent, self._refresh)

    def _refresh(self, event: BeforeInvocationEvent) -> None:
        event.agent.system_prompt = with_today(self.base_prompt)
