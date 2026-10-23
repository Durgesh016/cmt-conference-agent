"""Tool interface and registry.

Each conference source (CMT, EasyChair, HotCRP, ...) implements ConferenceTool.
To add a new source: subclass ConferenceTool, register() it — done.

The single entry point is run(source, command, settings=..., **kwargs).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .models import Conference

log = logging.getLogger(__name__)


@dataclass
class ExtractResult:
    """What every extraction tool returns."""
    conferences: list[Conference]
    metadata: dict[str, Any] = field(default_factory=dict)


class ConferenceTool:
    """Base class every conference-source tool implements."""
    name: str = ""
    description: str = ""

    def login(self, settings) -> str:
        """Authenticate and save the session. Returns a status message."""
        raise NotImplementedError

    def extract(self, settings, *,
                on_complete: Callable[[ExtractResult], None] | None = None,
                **params) -> ExtractResult:
        """Extract conferences. Calls on_complete(result) before cleanup."""
        raise NotImplementedError

    def logout(self, settings) -> str:
        """Delete the saved session. Returns a status message."""
        raise NotImplementedError


# -- registry ----------------------------------------------------------------

_TOOLS: dict[str, ConferenceTool] = {}


def register(tool_instance: ConferenceTool) -> ConferenceTool:
    """Add a tool to the registry (call once at import time)."""
    _TOOLS[tool_instance.name] = tool_instance
    return tool_instance


def get_tool(name: str) -> ConferenceTool:
    if name not in _TOOLS:
        raise KeyError(f"Unknown tool {name!r}. Available: {', '.join(_TOOLS)}")
    return _TOOLS[name]


def available_tools() -> list[str]:
    return list(_TOOLS)


# -- single dispatch entry point ---------------------------------------------

def run(source: str, command: str, *, settings, **kwargs) -> Any:
    """Look up the tool for *source* and run *command* on it.

    Every call in the codebase goes through here.  To add a new conference
    source later, register one ConferenceTool subclass and call
    run("newsource", "extract", settings=..., ...).
    """
    tool = get_tool(source)
    handler = getattr(tool, command, None)
    if handler is None:
        raise ValueError(f"Tool {source!r} has no command {command!r}")
    log.info("Running %s.%s", source, command)
    return handler(settings, **kwargs)
