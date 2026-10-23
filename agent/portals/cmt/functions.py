"""Reusable functions — the actual operations the CMT tool performs.

Tools call these; so can scripts, tests, or an API layer.
"""

from __future__ import annotations

from collections.abc import Callable

from agent.tools import ExtractResult


def login(settings) -> str:
    """Sign into CMT and save the browser session."""
    from .browser import run_login
    run_login(settings)
    return f"Logged in. Session saved to {settings.state_path}."


def extract(settings, *, tabs: list[str] | None = None, search: str = "",
            max_pages: int | None = None, upcoming: bool = False,
            date_from=None, date_to=None,
            on_complete: Callable[[ExtractResult], None] | None = None) -> ExtractResult:
    """Extract conferences from CMT's conference list."""
    from .browser import ExtractionRequest, run_extraction

    request = ExtractionRequest(
        tabs=tabs or ["all"], search=search, max_pages=max_pages,
        upcoming=upcoming, date_from=date_from, date_to=date_to,
    )
    holder: list[ExtractResult] = []

    def on_done(agent_result):
        result = ExtractResult(
            conferences=agent_result.conferences,
            metadata={
                "robots_status": agent_result.robots_status,
                "audit_path": agent_result.audit_path,
            },
        )
        holder.append(result)
        if on_complete:
            on_complete(result)

    run_extraction(settings, request, on_done=on_done)
    return holder[0]


def logout(settings) -> str:
    """Delete the saved CMT session."""
    if settings.state_path.exists():
        settings.state_path.unlink()
        return f"Deleted the saved session ({settings.state_path})."
    return "There is no saved session."
