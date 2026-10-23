"""EasyChair Smart CFP tool — registered as 'easychair'."""

from __future__ import annotations

from collections.abc import Callable

from agent.tools import ConferenceTool, ExtractResult, register


class EasyChairTool(ConferenceTool):
    name = "easychair"
    description = "Extract conferences from the public EasyChair Smart CFP (easychair.org/cfp)"

    def login(self, settings) -> str:
        return "EasyChair Smart CFP is public — no login needed."

    def extract(self, settings, *,
                on_complete: Callable[[ExtractResult], None] | None = None,
                **params) -> ExtractResult:
        from .scraper import scrape_cfp

        conferences = scrape_cfp(settings, search=params.get("search", ""))

        result = ExtractResult(
            conferences=conferences,
            metadata={"source": "easychair-cfp", "count": len(conferences)},
        )
        if on_complete:
            on_complete(result)
        return result

    def logout(self, settings) -> str:
        return "EasyChair Smart CFP is public — no session to clear."


register(EasyChairTool())
