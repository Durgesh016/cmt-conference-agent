"""Command-line interface and the single main() entry point.

main() parses arguments, picks the right tool, and connects it to export.
To support a new conference source, register a new tool and add its name
to SOURCE_CHOICES.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

from . import __version__
from .compliance import USAGE_NOTICE, ComplianceError
from .config import DEFAULT_BASE_URL, Settings
from .errors import ExtractionError, LoginError, short_error
from .exporters import FORMATS, export
from .models import parse_timeline_date
from .tools import ExtractResult, get_tool

log = logging.getLogger("cmt_agent")

# -- register all tools (one import per source) ------------------------------
import agent.portals.cmt  # noqa: F401  — registers the "cmt" tool
import agent.portals.easychair  # noqa: F401  — registers the "easychair" tool
# Future: import cmt_agent.hotcrp_tool

TAB_CHOICES = {"all": ["all"], "my": ["my"], "both": ["my", "all"]}
SOURCE_CHOICES = ("cmt", "easychair")
COMMANDS = ("login", "extract", "logout", "ask", "chat")
EXAMPLES = """\
examples:
  python main.py                 log in if needed and extract every conference (same as: extract)
  python main.py --source easychair   extract from EasyChair Smart CFP
  python main.py --upcoming      only conferences that haven't ended yet
  python main.py --from 2027-01 --to 2027-03
                                 only conferences starting January-March 2027
  python main.py login           just log in and save the session
  python main.py login --source easychair   log in to EasyChair
  python main.py extract --help  all extraction options
  python main.py ask "get upcoming AI conferences from CMT"
  python main.py chat            interactive agent mode
"""


# -- arg parsing -------------------------------------------------------------

def _timeline_date(end: bool):
    def parse(text: str):
        try:
            return parse_timeline_date(text, end=end)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from None
    return parse


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="show debug logging")
    common.add_argument("--base-url", default=argparse.SUPPRESS, help=f"CMT site to use (default: {DEFAULT_BASE_URL})")
    common.add_argument("--source", choices=SOURCE_CHOICES, default="cmt",
                         help="conference portal: cmt (default) or easychair (Smart CFP)")

    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="Extract conference links from Microsoft CMT, signed in with your own CMT account. "
                    "Without a command it runs `extract`.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="{login,extract,logout}")

    login_p = sub.add_parser("login", parents=[common], help="sign in with your CMT account and save the session")
    login_p.add_argument("--headless", action="store_true", help="no browser window (needs CMT_EMAIL/CMT_PASSWORD in .env)")
    login_p.add_argument("--close", action="store_true",
                         help="close the browser when done (default: it stays open until you close it)")

    ext = sub.add_parser("extract", parents=[common], help="extract conference links from CMT's conference list")
    ext.add_argument("--tab", choices=list(TAB_CHOICES), default="all",
                     help="'all' = All Conferences (default), 'my' = My Conferences, 'both'")
    ext.add_argument("-s", "--search", default="",
                     help="text typed into CMT's 'type to filter...' box")
    ext.add_argument("--max-pages", type=int, metavar="N",
                     help="press 'Show More' at most N times instead of loading the whole list")
    ext.add_argument("--upcoming", action="store_true",
                     help="keep only conferences ending today or later")
    ext.add_argument("--from", dest="date_from", type=_timeline_date(end=False), metavar="DATE",
                     help="only conferences starting on or after DATE")
    ext.add_argument("--to", dest="date_to", type=_timeline_date(end=True), metavar="DATE",
                     help="only conferences starting on or before DATE")
    ext.add_argument("--headless", action="store_true",
                     help="no browser window (needs a saved session or .env credentials)")
    ext.add_argument("--close", action="store_true",
                     help="close the browser when done (default: it stays open until you close it)")
    ext.add_argument("--format", nargs="+", choices=FORMATS, default=["docx"], metavar="FMT",
                     help="files to write: csv, json and/or docx (default: docx)")
    ext.add_argument("--out", type=Path, help=f"output folder (default: {Settings.output_dir})")
    ext.add_argument("--min-delay", type=float, help=f"min seconds between actions (default: {Settings.min_delay:g})")
    ext.add_argument("--max-delay", type=float, help=f"max seconds between actions (default: {Settings.max_delay:g})")

    sub.add_parser("logout", parents=[common], help="delete the saved session file")

    ask_p = sub.add_parser("ask", parents=[common],
                           help="send a natural-language request to the LLM agent")
    ask_p.add_argument("request", nargs="+", help="what you want the agent to do")
    ask_p.add_argument("--headless", action="store_true")

    sub.add_parser("chat", parents=[common], help="interactive agent mode (type requests, get answers)")

    return parser


def _default_command(argv: list[str]) -> list[str]:
    """No command means `extract`."""
    i = 0
    while i < len(argv) and (argv[i] in ("-v", "--verbose") or argv[i].startswith("--base-url")):
        i += 2 if argv[i] == "--base-url" else 1
    if i < len(argv) and argv[i] in (*COMMANDS, "-h", "--help", "--version"):
        return argv
    return [*argv[:i], "extract", *argv[i:]]


# -- the one main function ---------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments → pick the tool → run it → export results."""
    parser = _build_parser()
    args = parser.parse_args(_default_command(list(sys.argv[1:] if argv is None else argv)))

    if getattr(args, "max_pages", None) is not None and args.max_pages < 0:
        parser.error("--max-pages must be 0 or more")
    if getattr(args, "date_from", None) and getattr(args, "date_to", None) and args.date_from > args.date_to:
        parser.error(f"--from ({args.date_from}) is after --to ({args.date_to})")

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        settings = Settings.from_env(
            base_url=getattr(args, "base_url", None),
            headless=True if getattr(args, "headless", False) else None,
            keep_open=False if getattr(args, "close", False) else None,
            output_dir=getattr(args, "out", None),
            min_delay=getattr(args, "min_delay", None),
            max_delay=getattr(args, "max_delay", None),
        )
    except ValueError as exc:
        parser.error(str(exc))

    try:
        # ---- ask / chat (LLM agent) ---------------------------------------
        if args.command in ("ask", "chat"):
            from .agent import ConferenceAgent
            agent = ConferenceAgent(settings)
            if args.command == "chat":
                agent.chat()
            else:
                print(agent.run(" ".join(args.request)))
            return 0

        tool = get_tool(args.source)

        # ---- logout --------------------------------------------------------
        if args.command == "logout":
            print(tool.logout(settings))
            return 0

        print(USAGE_NOTICE.format(min_delay=settings.min_delay, max_delay=settings.max_delay,
                                  audit=settings.log_dir), flush=True)

        # ---- login ---------------------------------------------------------
        if args.command == "login":
            print(f"\n{tool.login(settings)}")
            return 0

        # ---- extract -------------------------------------------------------
        formats = set(args.format)
        saved: list[Path] = []

        def on_complete(result: ExtractResult) -> None:
            """Export files while the browser is still open."""
            stamp = datetime.now().astimezone()
            meta = {
                "source": settings.base_url,
                "extracted_at": stamp.isoformat(timespec="seconds"),
                "tab": args.tab,
                "search": args.search.strip(),
                "max_pages": args.max_pages,
                "upcoming": args.upcoming,
                "from": args.date_from.isoformat() if args.date_from else None,
                "to": args.date_to.isoformat() if args.date_to else None,
                "count": len(result.conferences),
                "robots_txt": result.metadata.get("robots_status", ""),
                "tool": f"cmt-conference-agent {__version__}",
            }
            span = ""
            if args.date_from or args.date_to:
                span = f"_{args.date_from or 'start'}_to_{args.date_to or 'end'}"
            stem = f"cmt_conferences_{args.tab}{span}_{stamp:%Y%m%d-%H%M%S}"
            saved.extend(export(result.conferences, settings.output_dir, stem, formats, meta))

        result = tool.extract(
            settings,
            on_complete=on_complete,
            tabs=TAB_CHOICES[args.tab],
            search=args.search.strip(),
            max_pages=args.max_pages,
            upcoming=args.upcoming,
            date_from=args.date_from,
            date_to=args.date_to,
        )
        _print_summary(result, saved)
        return 0 if len(saved) == len(formats) else 1

    except LoginError as exc:
        log.error("%s", exc)
        return 2
    except ComplianceError as exc:
        log.error("%s", exc)
        return 3
    except ExtractionError as exc:
        log.error("%s", exc)
        return 4
    except PlaywrightError as exc:
        log.error("Browser error: %s", short_error(exc))
        log.debug("Full browser error: %s", exc)
        if "Executable doesn't exist" in str(exc):
            log.error("Chromium isn't installed. Run:  python -m playwright install chromium")
        return 1
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130


def _print_summary(result: ExtractResult, paths: list[Path]) -> None:
    conferences = result.conferences
    print(f"\nExtracted {len(conferences)} conference(s).")
    for p in paths:
        print(f"  saved:     {p}")
    audit = result.metadata.get("audit_path")
    if audit:
        print(f"  audit log: {audit}")
    for c in conferences[:10]:
        website = f"  ->  {c.external_url}" if c.external_url else ""
        print(f"  - {(c.short_name or c.name)[:24]:<24} {c.start_date:<10}  {c.cmt_url}{website}")
    if len(conferences) > 10:
        print(f"  ... and {len(conferences) - 10} more in the files above.")
