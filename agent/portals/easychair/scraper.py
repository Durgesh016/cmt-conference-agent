"""Scrape the public EasyChair Smart CFP page (https://easychair.org/cfp).

No login needed — the page is public. Opens Chromium, clicks 'show all'
to load every entry, reads the table, then visits each conference's
detail page to grab the real conference website URL.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from agent.config import Settings
from agent.keywords import extract_keywords
from agent.models import Conference

log = logging.getLogger(__name__)

CFP_URL = "https://easychair.org/cfp"

# ---- JS: read the main CFP table -------------------------------------------

_READ_TABLE_JS = """
() => {
    const clean = s => (s || "").replace(/\\s+/g, " ").trim();

    let target = null;
    for (const t of document.querySelectorAll("table")) {
        const txt = t.textContent;
        if (txt.includes("Acronym") && txt.includes("SubmissionDeadline") && t.querySelectorAll("tr").length > 10)
            target = t;
    }
    if (!target) return null;

    const allRows = Array.from(target.querySelectorAll("tr"));
    let hdrIdx = -1;
    for (let i = 0; i < allRows.length; i++) {
        const cells = Array.from(allRows[i].querySelectorAll("th, td")).map(c => clean(c.textContent));
        if (cells.includes("Acronym") && cells.includes("Name")) { hdrIdx = i; break; }
    }
    if (hdrIdx < 0) return null;

    const out = [];
    for (let i = hdrIdx + 1; i < allRows.length; i++) {
        const tds = allRows[i].querySelectorAll("td");
        if (tds.length < 5) continue;
        const cells = Array.from(tds).map(c => clean(c.textContent));
        if (!cells[0] && !cells[1]) continue;
        const link = tds[0].querySelector("a[href]");
        out.push({
            acronym:    cells[0] || "",
            name:       cells[1] || "",
            location:   cells[2] || "",
            deadline:   cells[3] || "",
            start_date: cells[4] || "",
            topics:     cells[5] || "",
            cfp_path:   link ? link.getAttribute("href") : "",
        });
    }
    return out.length ? JSON.stringify(out) : null;
}
"""

# ---- JS: grab the conference website and description from a detail page -----

_READ_DETAIL_JS = """
() => {
    const clean = s => (s || "").replace(/\\s+/g, " ").trim();

    // External website URL (first non-EasyChair link)
    let website = null;
    for (const a of document.querySelectorAll("a[href]")) {
        const h = a.href;
        if (h && !h.includes("easychair.org") && h.startsWith("http")) { website = h; break; }
    }

    // Collect description / topics text from the detail page
    const parts = [];
    // Table rows often contain labelled fields like "Topics", "Abstract", etc.
    for (const tr of document.querySelectorAll("table tr")) {
        const t = clean(tr.textContent);
        if (t.length > 20) parts.push(t);
        if (parts.length >= 20) break;
    }
    // Explicit description / topics sections
    for (const sel of ['.cfp-section', '.topics', '#topics', '.description', '#description',
                        'main p', 'article p', '.cfp-text p']) {
        for (const el of document.querySelectorAll(sel)) {
            const t = clean(el.textContent);
            if (t.length > 20) parts.push(t);
            if (parts.length >= 30) break;
        }
        if (parts.length >= 30) break;
    }
    // Body-text fallback when the page has no structured sections
    if (!parts.length && document.body)
        parts.push(clean(document.body.innerText).substring(0, 3000));

    return { website, description: parts.join(" ").substring(0, 4000) };
}
"""

# ---- helpers ----------------------------------------------------------------

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


def _fetch_detail(page: Page, cfp_path: str) -> tuple[str, str]:
    """Visit a conference's CFP detail page; return (external_website_url, description_text)."""
    if not cfp_path:
        return ("", "")
    url = f"https://easychair.org{cfp_path}" if cfp_path.startswith("/") else cfp_path
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=15_000)
        page.wait_for_timeout(1000)
        result = page.evaluate(_READ_DETAIL_JS)
        if isinstance(result, dict):
            return (result.get("website") or "", result.get("description") or "")
        return ("", "")
    except PlaywrightError:
        return ("", "")


# ---- public entry point -----------------------------------------------------

def scrape_cfp(settings: Settings, search: str = "") -> list[Conference]:
    """Open easychair.org/cfp, load all entries, fetch each website, return Conferences."""

    log.info("Opening %s ...", CFP_URL)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=settings.headless)
        try:
            page = browser.new_page(viewport={"width": 1366, "height": 900})
            page.set_default_timeout(settings.action_timeout * 1000)

            # Step 1: Load the main CFP table
            page.goto(CFP_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            log.info("Page loaded: %s", page.title())

            show_all = page.locator("a:text-is('all')").first
            if show_all.count() and show_all.is_visible():
                log.info("Clicking 'show all' to load every entry...")
                show_all.click()
                page.wait_for_timeout(8000)

            raw = page.evaluate(_READ_TABLE_JS)
            if not raw:
                log.warning("No data found on the page.")
                return []

            items = json.loads(raw)
            log.info("Found %d rows in the table.", len(items))

            # Deduplicate and filter
            entries: list[dict] = []
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
                entries.append(item)

            log.info("Processing %d unique conference(s) — fetching website links...", len(entries))

            # Step 2: Visit each detail page to get the real conference website + description
            conferences: list[Conference] = []
            for i, item in enumerate(entries):
                website, detail_text = _fetch_detail(page, item.get("cfp_path", ""))
                cfp_url = f"https://easychair.org{item['cfp_path']}" if item.get("cfp_path") else ""

                # Combine the table's topics column with the richer detail-page text
                combined_topics = " ".join(filter(None, [item.get("topics", ""), detail_text]))
                kw = extract_keywords(item.get("name", ""), combined_topics)

                conferences.append(Conference(
                    name=item.get("name", ""),
                    short_name=item.get("acronym", ""),
                    cmt_url=cfp_url,
                    external_url=website,
                    start_date=_parse_date(item.get("start_date", "")),
                    location=item.get("location", ""),
                    contact_url=item.get("deadline", ""),
                    lists="easychair-cfp",
                    keywords=kw,
                ))

                if (i + 1) % 50 == 0:
                    log.info("  Progress: %d / %d conferences done.", i + 1, len(entries))

            log.info("Extracted %d conference(s) with website links.", len(conferences))
            return conferences

        finally:
            try:
                browser.close()
            except (PlaywrightError, OSError):
                pass
