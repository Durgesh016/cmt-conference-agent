"""Scrape the public EasyChair Smart CFP page (https://easychair.org/cfp).

No login needed — the page is public. Opens Chromium, clicks 'show all'
to load every entry, reads the table, and returns Conference objects.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from playwright.sync_api import sync_playwright
from playwright.sync_api import Error as PlaywrightError

from agent.config import Settings
from agent.models import Conference

log = logging.getLogger(__name__)

CFP_URL = "https://easychair.org/cfp"

# ---- JS that reads the CFP data table --------------------------------------

_READ_TABLE_JS = """
() => {
    const clean = s => (s || "").replace(/\\s+/g, " ").trim();

    // Find the big data table (header row contains Acronym + Name + SubmissionDeadline)
    let target = null;
    for (const t of document.querySelectorAll("table")) {
        const txt = t.textContent;
        if (txt.includes("Acronym") && txt.includes("SubmissionDeadline") && t.querySelectorAll("tr").length > 10) {
            target = t;
        }
    }
    if (!target) return null;

    // Locate the header row
    const allRows = Array.from(target.querySelectorAll("tr"));
    let hdrIdx = -1;
    for (let i = 0; i < allRows.length; i++) {
        const cells = Array.from(allRows[i].querySelectorAll("th, td")).map(c => clean(c.textContent));
        if (cells.includes("Acronym") && cells.includes("Name")) { hdrIdx = i; break; }
    }
    if (hdrIdx < 0) return null;

    // Read every data row after the header
    const out = [];
    for (let i = hdrIdx + 1; i < allRows.length; i++) {
        const tds = allRows[i].querySelectorAll("td");
        if (tds.length < 5) continue;
        const cells = Array.from(tds).map(c => clean(c.textContent));
        if (!cells[0] && !cells[1]) continue;          // skip empty / nav rows
        const link = tds[0].querySelector("a[href]");
        out.push({
            acronym:    cells[0] || "",
            name:       cells[1] || "",
            location:   cells[2] || "",
            deadline:   cells[3] || "",
            start_date: cells[4] || "",
            topics:     cells[5] || "",
            url:        link ? link.href : "",
        });
    }
    return out.length ? JSON.stringify(out) : null;
}
"""

# ---- date helpers -----------------------------------------------------------

_DATE_FMTS = ["%b %d, %Y", "%B %d, %Y", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"]


def _parse_date(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return raw


# ---- public entry point -----------------------------------------------------

def scrape_cfp(settings: Settings, search: str = "") -> list[Conference]:
    """Open easychair.org/cfp, load all entries, and return a list of Conferences."""

    log.info("Opening %s ...", CFP_URL)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=settings.headless)
        try:
            page = browser.new_page(viewport={"width": 1366, "height": 900})
            page.set_default_timeout(settings.action_timeout * 1000)
            page.goto(CFP_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            log.info("Page loaded: %s — %s", page.url, page.title())

            # Click "all" in the pagination bar to load every row
            show_all = page.locator("a:text-is('all')").first
            if show_all.count() and show_all.is_visible():
                log.info("Clicking 'show all' to load every entry...")
                show_all.click()
                page.wait_for_timeout(8000)
            else:
                log.warning("'show all' link not found; extracting visible rows only.")

            # Read the table
            raw = page.evaluate(_READ_TABLE_JS)
            if not raw:
                log.warning("No data found on the page.")
                return []

            items = json.loads(raw)
            log.info("Parsed %d rows from the table.", len(items))

            conferences: list[Conference] = []
            seen: set[str] = set()

            for item in items:
                name = item.get("name", "")
                acronym = item.get("acronym", "")
                if not name and not acronym:
                    continue
                if search and search.lower() not in (name + " " + acronym).lower():
                    continue
                key = (acronym or name).strip().lower()
                if key in seen:
                    continue
                seen.add(key)

                cfp_url = item.get("url", "")
                conferences.append(Conference(
                    name=name,
                    short_name=acronym,
                    cmt_url=cfp_url,
                    external_url=cfp_url,
                    start_date=_parse_date(item.get("start_date", "")),
                    location=item.get("location", ""),
                    contact_url=item.get("deadline", ""),
                    lists="easychair-cfp",
                ))

            log.info("Extracted %d unique conference(s).", len(conferences))
            return conferences

        finally:
            try:
                browser.close()
            except (PlaywrightError, OSError):
                pass
