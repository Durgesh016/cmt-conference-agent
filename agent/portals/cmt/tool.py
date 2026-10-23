"""CMT tool — connects the ConferenceTool interface to the CMT functions."""

from __future__ import annotations

from collections.abc import Callable

from agent.tools import ConferenceTool, ExtractResult, register

from .functions import extract, login, logout


class CMTTool(ConferenceTool):
    name = "cmt"
    description = "Extract conferences from Microsoft CMT (cmt3.research.microsoft.com)"

    def login(self, settings) -> str:
        return login(settings)

    def extract(self, settings, *,
                on_complete: Callable[[ExtractResult], None] | None = None,
                **params) -> ExtractResult:
        return extract(settings, on_complete=on_complete, **params)

    def logout(self, settings) -> str:
        return logout(settings)


register(CMTTool())
