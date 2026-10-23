"""Top-level workflows: `login` (sign in once, save the session) and `extract` (collect links)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date

from playwright.sync_api import BrowserContext, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from agent.compliance import AuditLog, RateLimiter, RobotsPolicy
from agent.config import Settings
from agent.models import Conference, fill_missing, is_upcoming, starts_within

from .auth import Authenticator
from .extractor import ConferenceListAgent

log = logging.getLogger(__name__)


@dataclass
class ExtractionRequest:
    tabs: list[str] = field(default_factory=lambda: ["all"])
    search: str = ""
    max_pages: int | None = None
    upcoming: bool = False
    date_from: date | None = None
    date_to: date | None = None

    @property
    def has_date_window(self) -> bool:
        return self.date_from is not None or self.date_to is not None

    @property
    def by_time(self) -> bool:
        return self.upcoming or self.has_date_window

    def wants(self, conference: Conference, today: date) -> bool:
        if self.upcoming and not is_upcoming(conference, today):
            return False
        return not self.has_date_window or starts_within(conference, self.date_from, self.date_to)

    def stop_paging_before(self, today: date) -> date | None:
        if self.date_from is not None and self.date_from >= today:
            return self.date_from
        return today if self.upcoming else None


@dataclass
class ExtractionResult:
    conferences: list[Conference]
    robots_status: str
    audit_path: str


@contextmanager
def open_browser(settings: Settings) -> Iterator[BrowserContext]:
    """A Chromium context that starts from the saved session, when there is one."""
    log.info("Starting the browser (this can take 10-20 seconds)...")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=settings.headless)
        try:
            options = {"viewport": {"width": 1366, "height": 900}}
            context = None
            if settings.state_path.exists():
                try:
                    context = browser.new_context(storage_state=str(settings.state_path), **options)
                except (PlaywrightError, ValueError, OSError) as exc:
                    log.warning("Ignoring unreadable session file %s (%s).", settings.state_path, exc)
            if context is None:
                context = browser.new_context(**options)
            context.set_default_timeout(settings.action_timeout * 1000)
            context.set_default_navigation_timeout(settings.action_timeout * 1000)
            yield context
        finally:
            try:
                browser.close()
            except (PlaywrightError, KeyboardInterrupt, OSError):
                pass


def keep_window_open(context: BrowserContext, settings: Settings) -> None:
    """When the browser is visible, leave it open until you close it (or press Ctrl+C here)."""
    if settings.headless or not settings.keep_open:
        return
    print("\nDone. The browser stays open: close its window (or press Ctrl+C here) to finish.", flush=True)
    try:
        while open_pages := [p for p in context.pages if not p.is_closed()]:
            open_pages[0].wait_for_event("close", timeout=0)
    except (PlaywrightError, KeyboardInterrupt):
        pass


def check_robots(context: BrowserContext, settings: Settings, audit: AuditLog) -> RobotsPolicy:
    robots = RobotsPolicy.fetch(context.request, settings.base_url)
    log.info("robots.txt: %s.", robots.status)
    audit.record("robots", status=robots.status)
    robots.require(settings.conference_list_url)
    return robots


def run_login(settings: Settings) -> None:
    audit = AuditLog(settings.log_dir)
    with open_browser(settings) as context:
        check_robots(context, settings, audit)
        page = context.new_page()
        auth = Authenticator(settings, audit, RateLimiter(settings.min_delay, settings.max_delay))
        if not auth.ensure_session(context, page):
            auth.save(context)
        keep_window_open(context, settings)


def run_extraction(
    settings: Settings,
    request: ExtractionRequest,
    on_done: Callable[[ExtractionResult], None] | None = None,
) -> ExtractionResult:
    """Collect the conferences. `on_done` runs before the window is left open (e.g. to save the files)."""
    audit = AuditLog(settings.log_dir)
    rate = RateLimiter(settings.min_delay, settings.max_delay)
    audit.record("run_start", base_url=settings.base_url, **asdict(request))
    today = date.today()
    found: dict[str, Conference] = {}

    with open_browser(settings) as context:
        robots = check_robots(context, settings, audit)
        page = context.new_page()
        lister = ConferenceListAgent(page, settings, rate, audit, search=request.search)
        auth = Authenticator(settings, audit, rate)
        auth.ensure_session(context, page)
        lister.open_list()
        for tab in request.tabs:
            lister.select_tab(tab)
            lister.apply_filter()
            lister.load_list(max_pages=request.max_pages, stop_before=request.stop_paging_before(today))
            for conference in lister.collect(tab):
                existing = found.get(conference.key)
                if existing is None:
                    found[conference.key] = conference
                else:
                    fill_missing(existing, conference)
                    existing.lists = f"{existing.lists}+{tab}"
        auth.save(context, quiet=True)

        conferences = [c for c in found.values() if request.wants(c, today)]
        if request.by_time:
            conferences.sort(key=lambda c: (c.start_date, c.name.lower()))
        audit.record("run_end", conferences=len(conferences))
        result = ExtractionResult(conferences=conferences, robots_status=robots.status, audit_path=str(audit.path))
        if on_done is not None:
            on_done(result)
        keep_window_open(context, settings)
    return result
