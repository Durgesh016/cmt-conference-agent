"""Reads CMT's "Conference List" page the way you would: pick a tab, type in the filter box,
press "Show More" / "Show All", then read the table.

Two views of the same list are combined:
  1. the table the page renders (what you see), and
  2. the JSON the page itself downloads from /api/odata/(My)Conferences while you click.
The agent never calls that API on its own; it only observes the responses the page requested.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Locator, Page, Request, Response
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from agent.compliance import AuditLog, RateLimiter, open_page
from agent.config import CONFERENCE_LIST_PATH, Settings
from agent.errors import ExtractionError, LoginError
from agent.keywords import extract_keywords
from agent.models import (
    Conference,
    as_date,
    clean_text,
    conference_from_api,
    matches_filter,
    merge_sources,
    normalize_external_url,
    parse_display_date,
    short_name_from_url,
)

from .auth import is_login_url

log = logging.getLogger(__name__)

# Only visit individual conference pages for keyword fallback when the result set is small
_FETCH_DESC_THRESHOLD = 200

# JS to scrape a conference's own page for description / topics text
_READ_CONF_PAGE_JS = """
() => {
    const clean = s => (s || "").replace(/\\s+/g, " ").trim();
    const parts = [];
    const meta = document.querySelector('meta[name="description"]');
    if (meta) { const c = meta.getAttribute('content'); if (c) parts.push(clean(c)); }
    for (const sel of ['[class*=descript]', '[class*=topic]', '[class*=about]',
                        'main p', 'article p', '.col-md-8 p', '.content p']) {
        for (const el of document.querySelectorAll(sel)) {
            const t = clean(el.textContent);
            if (t.length > 40) { parts.push(t.substring(0, 600)); if (parts.length >= 5) break; }
        }
        if (parts.length >= 5) break;
    }
    if (!parts.length && document.body)
        parts.push(clean(document.body.innerText).substring(0, 2000));
    return parts.join(" ").substring(0, 3000);
}
"""

TABS = {"my": "My Conferences", "all": "All Conferences"}
ENTITY_SETS = {"my": "MyConferences", "all": "Conferences"}
TAB_TEXT = {
    "my": re.compile(r"^\s*My Conferences\s*(\(\d+\))?\s*$", re.I),
    "all": re.compile(r"^\s*All Conferences\s*$", re.I),
}
API_PATH = re.compile(r"/api/odata/(MyConferences|Conferences)(?=[/(]|$)", re.I)
FILTER_PLACEHOLDER = re.compile(r"type to filter", re.I)
PAGER_TEXT = {"more": re.compile(r"\bmore\b", re.I), "all": re.compile(r"\ball\b", re.I)}
PAGER_TEXT_ANYWHERE = {"more": re.compile(r"\bshow\b.*\bmore\b", re.I), "all": re.compile(r"\bshow\b.*\ball\b", re.I)}
HEADER_FIELDS = (
    ("external", "external_url"),
    ("start", "start_date"),
    ("end", "end_date"),
    ("location", "location"),
    ("contact", "contact_url"),
    ("name", "name"),
)

READ_TABLE_JS = """
(countOnly) => {
  const clean = (s) => (s || "").replace(/\\s+/g, " ").trim();
  const shown = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const visibleCells = (cells) => Array.from(cells).filter(shown);
  const headersOf = (table) => {
    let cells = table.querySelectorAll("thead th");
    if (!cells.length && table.rows.length) cells = table.rows[0].cells;
    return visibleCells(cells).map((c) => clean(c.textContent).toLowerCase());
  };
  const table = Array.from(document.querySelectorAll("table")).find((t) => {
    const h = headersOf(t);
    return shown(t) && h.some((x) => x.startsWith("name")) &&
      h.some((x) => x.includes("external") || x.includes("start") || x.includes("location"));
  });
  if (!table) return countOnly ? -1 : null;
  const headers = headersOf(table);
  const bodyRows = table.tBodies.length
    ? Array.from(table.tBodies).flatMap((b) => Array.from(b.rows))
    : Array.from(table.rows).slice(1);
  const minCells = Math.min(2, headers.length);
  const isDataRow = (tr) => tr.cells.length >= minCells && tr.querySelector("td");
  if (countOnly) return bodyRows.filter(isDataRow).length;
  const rows = bodyRows.filter((tr) => isDataRow(tr) && shown(tr)).map((tr) => visibleCells(tr.cells).map((td) => {
    const link = td.querySelector("a[href]");
    return { text: clean(td.textContent), href: link ? link.href : "", raw: link ? link.getAttribute("href") || "" : "" };
  }));
  return JSON.stringify({ headers, rows });
}
"""


class ConferenceListAgent:
    """Drives CMT's /Conference/Recent page."""

    def __init__(self, page: Page, settings: Settings, rate: RateLimiter, audit: AuditLog, search: str = "") -> None:
        self.page = page
        self.settings = settings
        self.rate = rate
        self.audit = audit
        self.search = search.strip()
        self._loaded: dict[str, dict[str, Conference]] = {tab: {} for tab in TABS}
        self._api_totals: dict[str, int] = {}
        self._inflight: dict[Request, float] = {}
        self._descriptions: dict[str, str] = {}  # conference key → description text from API
        self._active_tab = "my"
        page.on("request", self._on_request)
        page.on("requestfinished", self._on_request_done)
        page.on("requestfailed", self._on_request_done)
        page.on("response", self._on_response)

    @staticmethod
    def _tab_of(url: str) -> str | None:
        match = API_PATH.search(urlparse(url).path)
        if not match:
            return None
        return "my" if match.group(1).lower() == "myconferences" else "all"

    def _on_request(self, request: Request) -> None:
        if self._tab_of(request.url):
            self._inflight[request] = time.monotonic()

    def _on_request_done(self, request: Request) -> None:
        self._inflight.pop(request, None)

    def _on_response(self, response: Response) -> None:
        tab = self._tab_of(response.url)
        if not tab or response.request.method != "GET" or not response.ok:
            return
        try:
            payload = response.json()
        except Exception as exc:
            log.debug("Ignoring unreadable list response %s: %s", response.url, exc)
            return
        items = payload.get("value") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return
        store = self._loaded[tab]
        for item in items:
            if isinstance(item, dict):
                conference = conference_from_api(item, self.settings.base_url)
                if conference.key:
                    store.setdefault(conference.key, conference)
                    # Capture any description/topics the API returns (field names vary)
                    lowered = {str(k).lower(): v for k, v in item.items()}
                    for df in ("description", "topics", "topicsofinterest", "abstract", "about", "callforpapers"):
                        val = clean_text(lowered.get(df, ""))
                        if val:
                            self._descriptions.setdefault(conference.key, val)
                            break
        total = payload.get("@odata.count")
        if isinstance(total, int):
            self._api_totals[tab] = total
        self.audit.record("list_data", entity=ENTITY_SETS[tab], items=len(items), total=total)

    def _busy(self) -> bool:
        cutoff = time.monotonic() - self.settings.render_timeout
        return any(started > cutoff for started in self._inflight.values())

    def _row_count(self) -> int:
        try:
            return self.page.evaluate(READ_TABLE_JS, True)
        except PlaywrightError:
            return -2

    def _wait_until_settled(self, grace: float = 0.5) -> int:
        self.page.wait_for_timeout(grace * 1000)
        deadline = time.monotonic() + self.settings.render_timeout
        last, stable_since = None, time.monotonic()
        while time.monotonic() < deadline:
            count = self._row_count()
            now = time.monotonic()
            if self._busy() or count != last:
                last, stable_since = count, now
            elif now - stable_since >= self.settings.settle_time:
                return count
            self.page.wait_for_timeout(250)
        log.warning("The list was still changing after %gs; continuing with what has loaded.", self.settings.render_timeout)
        return last if last is not None else -1

    def open_list(self) -> None:
        if urlparse(self.page.url).path.rstrip("/").lower() != CONFERENCE_LIST_PATH.lower():
            open_page(self.page, self.settings.conference_list_url, self.rate, self.audit)
        if is_login_url(self.page.url):
            raise LoginError("CMT sent the browser back to the login page; the saved session has expired.")
        try:
            self._tab("all").wait_for(state="visible")
        except PlaywrightTimeoutError as exc:
            folder = self.save_debug_snapshot("conference-list-not-found")
            raise ExtractionError(
                f"The CMT conference list did not appear at {self.page.url}. "
                f"A screenshot and the page HTML were saved in {folder} to help adjust the selectors."
            ) from exc
        self._wait_until_settled()

    def _tab(self, tab: str) -> Locator:
        return self.page.get_by_text(TAB_TEXT[tab]).locator("visible=true").first

    @staticmethod
    def _first_visible(candidates: Locator) -> Locator | None:
        match = candidates.locator("visible=true").first
        return match if match.count() else None

    def select_tab(self, tab: str) -> None:
        if tab == self._active_tab:
            return
        log.info("Opening the '%s' tab.", TABS[tab])
        self.rate.wait(f"opening {TABS[tab]}")
        self.audit.record("click", target=TABS[tab])
        self._tab(tab).click()
        self._active_tab = tab
        self._wait_until_settled()

    def apply_filter(self) -> None:
        if not self.search:
            return
        box = self._first_visible(self.page.get_by_placeholder(FILTER_PLACEHOLDER)) or self._first_visible(
            self.page.locator("input[type='search']"))
        if box is None:
            log.warning("No filter box found; the search text will be applied to the extracted rows instead.")
            return
        if box.input_value().strip().lower() == self.search.lower():
            return
        log.info("Filtering the list for %r.", self.search)
        self.rate.wait("filtering the list")
        self.audit.record("filter", text=self.search)
        box.click()
        box.fill("")
        box.press_sequentially(self.search, delay=150)
        box.blur()
        self._wait_until_settled(grace=1.0)

    def _pager_button(self, kind: str) -> Locator | None:
        widget = self.page.locator("#showMore")
        if widget.count():
            return self._first_visible(widget.locator("a, button").filter(has_text=PAGER_TEXT[kind]))
        return self._first_visible(self.page.locator("a, button").filter(has_text=PAGER_TEXT_ANYWHERE[kind]))

    def load_list(self, max_pages: int | None = None, stop_before: date | None = None) -> int:
        incremental = max_pages is not None or stop_before is not None
        pages = stalls = 0
        rows = self._row_count()
        while True:
            more, everything = self._pager_button("more"), self._pager_button("all")
            if more is None and everything is None:
                break
            if max_pages is not None and pages >= max_pages:
                log.info("Stopping after %d extra page(s), as requested.", pages)
                break
            if stop_before is not None:
                oldest = self._oldest_loaded_end_date()
                if oldest is not None and oldest < stop_before:
                    log.info("Reached conferences that ended on %s; not loading older ones.", oldest)
                    break
            if incremental:
                button, label = (more, "Show More") if more is not None else (everything, "Show All")
            else:
                button, label = (everything, "Show All") if everything is not None else (more, "Show More")
            self.rate.wait(f"pressing {label}")
            self.audit.record("click", target=label, rows_before=rows)
            log.info("Pressing '%s' (%d rows loaded so far)...", label, max(rows, 0))
            button.click()
            pages += 1
            new_rows = self._wait_until_settled()
            stalls = stalls + 1 if new_rows <= rows else 0
            rows = new_rows
            if stalls >= 2:
                log.warning("'%s' stopped adding rows; continuing with %d rows.", label, rows)
                break
        total = self._api_totals.get(self._active_tab)
        if not incremental and not self.search and total is not None and 0 <= rows < total:
            folder = self.save_debug_snapshot("list-incomplete")
            log.warning("CMT reports %d conferences but only %d rows loaded (the Show More / Show All controls "
                        "may have changed). Page snapshot saved in %s.", total, rows, folder)
        return rows

    def _api_conferences(self, tab: str) -> list[Conference]:
        return [c for c in self._loaded[tab].values() if matches_filter(c, self.search)]

    def _oldest_loaded_end_date(self) -> date | None:
        dates = (as_date(c.end_date) for c in self._api_conferences(self._active_tab))
        return min((d for d in dates if d), default=None)

    def read_table(self) -> list[Conference]:
        raw = self.page.evaluate(READ_TABLE_JS, False)
        if not raw:
            log.warning("Could not find the conference table on the page; using the list data only.")
            return []
        data = json.loads(raw)
        columns = [self._field_for_header(header) for header in data["headers"]]
        conferences = []
        for cells in data["rows"]:
            by_field = {field: cell for field, cell in zip(columns, cells) if field}
            conference = self._row_to_conference(by_field)
            if conference is not None:
                conferences.append(conference)
        return conferences

    @staticmethod
    def _field_for_header(header: str) -> str | None:
        return next((field for keyword, field in HEADER_FIELDS if keyword in header), None)

    def _row_to_conference(self, cells: dict) -> Conference | None:
        def cell(field: str, key: str = "text") -> str:
            return cells[field][key] if field in cells else ""

        name = cell("name")
        if not name:
            return None
        cmt_url = cell("name", "href")
        external = cell("external_url", "raw") or cell("external_url")
        return Conference(
            name=name,
            short_name=short_name_from_url(cmt_url, self.settings.base_url),
            cmt_url=cmt_url,
            external_url=normalize_external_url(external),
            start_date=parse_display_date(cell("start_date")),
            end_date=parse_display_date(cell("end_date")),
            location=cell("location"),
            contact_url=cell("contact_url", "href"),
        )

    def collect(self, tab: str) -> list[Conference]:
        rows = self.read_table() if tab == self._active_tab else []
        merged = merge_sources(rows, self._api_conferences(tab))
        filtered = [c for c in merged if matches_filter(c, self.search)]

        # Only visit individual conference pages as a fallback when the result set is small
        do_page_fallback = len(filtered) <= _FETCH_DESC_THRESHOLD

        conferences = []
        for c in filtered:
            desc = self._descriptions.get(c.key, "")
            kw = extract_keywords(c.name, desc)
            if not kw and do_page_fallback and c.cmt_url:
                log.debug("No keywords from name for %r — fetching conference page.", c.name)
                page_desc = self._fetch_description(c.cmt_url)
                kw = extract_keywords(c.name, page_desc)
            conferences.append(replace(c, lists=tab, keywords=kw))

        log.info("%s: %d conference(s) (%d from the table).", TABS[tab], len(conferences), len(rows))
        return conferences

    def _fetch_description(self, url: str) -> str:
        """Open a new tab, navigate to the conference page, and return its description text."""
        try:
            extra = self.page.context.new_page()
            try:
                self.rate.wait("fetching conference description")
                extra.goto(url, wait_until="domcontentloaded", timeout=15_000)
                extra.wait_for_timeout(800)
                return extra.evaluate(_READ_CONF_PAGE_JS) or ""
            finally:
                extra.close()
        except Exception as exc:
            log.debug("Could not fetch description from %s: %s", url, exc)
            return ""

    def save_debug_snapshot(self, reason: str) -> Path:
        folder = self.settings.log_dir / "debug"
        folder.mkdir(parents=True, exist_ok=True)
        stem = folder / f"{datetime.now():%Y%m%d-%H%M%S}-{reason}"
        try:
            self.page.screenshot(path=f"{stem}.png")
            Path(f"{stem}.html").write_text(self.page.content(), encoding="utf-8")
        except PlaywrightError as exc:
            log.debug("Could not save a debug snapshot: %s", exc)
        return folder
