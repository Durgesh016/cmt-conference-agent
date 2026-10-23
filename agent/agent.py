"""LLM-powered conference agent.

Uses a local LLaMA model (via Ollama) with tool-use to dispatch requests
to the right portal tool.

    from agent.agent import ConferenceAgent
    agent = ConferenceAgent(settings)
    print(agent.run("extract upcoming conferences from CMT"))
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from . import __version__
from .config import Settings
from .exporters import FORMATS, export_files
from .models import parse_timeline_date
from .tools import ExtractResult, available_tools, get_tool

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a conference data extraction agent.  You help users extract \
conference information from academic conference portals using browser \
automation (Playwright).

Registered portals: {portals}

Rules:
- When the user names a portal, use it.
- When they don't specify, default to 'cmt'.
- Never ask follow-up questions. Just extract the data immediately.
- After extraction, automatically export the data to CSV, JSON and DOCX.
- Keep answers concise.\
"""

# -- tool definitions (OpenAI function-calling format for Ollama) --------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "extract_conferences",
            "description": "Extract conference data from a portal's conference list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "Portal name (e.g. 'cmt', 'easychair')",
                    },
                    "search": {
                        "type": "string",
                        "description": "Filter text to narrow results",
                    },
                    "upcoming": {
                        "type": "boolean",
                        "description": "Only conferences that haven't ended yet",
                    },
                    "date_from": {
                        "type": "string",
                        "description": "Conferences starting on/after this date (YYYY, YYYY-MM, or YYYY-MM-DD)",
                    },
                    "date_to": {
                        "type": "string",
                        "description": "Conferences starting on/before this date",
                    },
                },
                "required": ["source"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "login_portal",
            "description": "Sign into a conference portal and save the browser session.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Portal name"},
                },
                "required": ["source"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export_data",
            "description": "Export the last extraction to files (CSV, JSON, DOCX).",
            "parameters": {
                "type": "object",
                "properties": {
                    "formats": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["csv", "json", "docx"]},
                        "description": "File formats (default: all three)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "logout_portal",
            "description": "Delete the saved session for a portal.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Portal name"},
                },
                "required": ["source"],
            },
        },
    },
]

OLLAMA_BASE_URL = "http://localhost:11434/v1"
DEFAULT_MODEL = "llama3.2"


# -- the agent ---------------------------------------------------------------

class ConferenceAgent:
    """One function — run() — handles any natural-language request."""

    def __init__(self, settings: Settings, model: str | None = None,
                 base_url: str | None = None):
        from dataclasses import replace
        from openai import OpenAI
        self.settings = replace(settings, keep_open=False)
        self.model = model or settings.ollama_model or DEFAULT_MODEL
        self.client = OpenAI(
            base_url=base_url or settings.ollama_base_url or OLLAMA_BASE_URL,
            api_key="ollama",
        )
        self.last_result: ExtractResult | None = None
        self.last_source: str = ""

    # -- public API ----------------------------------------------------------

    def run(self, request: str) -> str:
        """Handle one natural-language request, return the agent's answer."""
        system = SYSTEM_PROMPT.format(
            portals=", ".join(available_tools()) or "none registered",
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": request},
        ]

        for _ in range(10):
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=TOOLS,
            )

            message = response.choices[0].message

            if not message.tool_calls:
                return message.content or ""

            messages.append(message)

            for tool_call in message.tool_calls:
                try:
                    args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                output = self._handle_tool(tool_call.function.name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": output,
                })

        return "Reached the maximum number of tool calls."

    def chat(self):
        """Interactive loop — type requests, get answers."""
        portals = ", ".join(available_tools()) or "none yet"
        print(f"Conference Agent  (model: {self.model}, portals: {portals})")
        print("Type your request, or 'quit' to exit.\n")
        while True:
            try:
                request = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not request:
                continue
            if request.lower() in ("quit", "exit", "q"):
                break
            print()
            response = self.run(request)
            print(f"{response}\n")

    # -- tool dispatch -------------------------------------------------------

    def _handle_tool(self, name: str, params: dict) -> str:
        source = params.get("source", "")
        try:
            if name == "extract_conferences":
                return self._do_extract(source, params)
            if name == "login_portal":
                return get_tool(source).login(self.settings)
            if name == "export_data":
                return self._do_export(params.get("formats"))
            if name == "logout_portal":
                return get_tool(source).logout(self.settings)
        except KeyError as exc:
            return f"Error: {exc}"
        except Exception as exc:
            log.exception("Tool %s failed", name)
            return f"Error running {name}: {exc}"
        return f"Unknown tool: {name}"

    def _do_extract(self, source: str, params: dict) -> str:
        tool = get_tool(source)
        kwargs: dict = {}
        if params.get("search"):
            kwargs["search"] = params["search"]
        if params.get("upcoming"):
            kwargs["upcoming"] = True
        if params.get("date_from"):
            kwargs["date_from"] = parse_timeline_date(params["date_from"])
        if params.get("date_to"):
            kwargs["date_to"] = parse_timeline_date(params["date_to"], end=True)

        result = tool.extract(self.settings, **kwargs)
        self.last_result = result
        self.last_source = source

        saved = self._do_export(None)

        sample = [
            {"name": c.name, "start_date": c.start_date,
             "location": c.location, "url": c.external_url}
            for c in result.conferences[:5]
        ]
        return json.dumps({"count": len(result.conferences), "sample": sample, "export": saved})

    def _do_export(self, formats: list[str] | None) -> str:
        if self.last_result is None:
            return "No data to export. Run an extraction first."
        fmts = set(formats) if formats else set(FORMATS)
        stamp = datetime.now().astimezone()
        meta = {
            "source": self.last_source,
            "extracted_at": stamp.isoformat(timespec="seconds"),
            "count": len(self.last_result.conferences),
            "tool": f"conference-agent {__version__}",
        }
        stem = f"conferences_{self.last_source}_{stamp:%Y%m%d-%H%M%S}"
        paths = export_files(
            self.last_result.conferences,
            self.settings.output_dir, stem, fmts, meta,
        )
        return json.dumps({"saved": [str(p) for p in paths]})
