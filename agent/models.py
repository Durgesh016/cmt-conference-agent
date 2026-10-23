"""The Conference record plus helpers that normalise what CMT shows into clean values."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, fields
from datetime import date, datetime
from urllib.parse import quote, urlparse


@dataclass
class Conference:
    name: str
    short_name: str = ""  # CMT's URL segment, e.g. "MISTA2026"
    cmt_url: str = ""  # the conference's page on CMT
    external_url: str = ""  # the conference's own website, as entered by its chairs
    start_date: str = ""  # YYYY-MM-DD when CMT provides a parseable date
    end_date: str = ""
    location: str = ""
    city: str = ""
    country: str = ""
    contact_url: str = ""  # CMT "Email Chairs" form (My Conferences only)
    lists: str = ""  # which CMT tab(s) listed it: "all", "my" or "my+all"

    @property
    def key(self) -> str:
        return (self.short_name or self.cmt_url or self.name).strip().lower()


FIELDNAMES = [f.name for f in fields(Conference)]

_WS = re.compile(r"\s+")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)
_DOMAIN_LIKE = re.compile(r"^[\w-]+(\.[\w-]+)+([/:?#].*)?$")


def clean_text(value) -> str:
    return _WS.sub(" ", str(value or "")).strip()


def same_site(url: str, base_url: str) -> bool:
    """True when `url` is on the CMT host of `base_url` (http/https and letter case don't matter)."""
    host = urlparse(url).netloc.lower()
    return bool(host) and host == urlparse(base_url).netloc.lower()


def normalize_external_url(raw: str) -> str:
    """Chairs type this field by hand: add a missing scheme, keep anything unparseable as-is."""
    value = clean_text(raw)
    if not value or _SCHEME.match(value):
        return value
    if value.startswith("//"):
        return "https:" + value
    if " " not in value and _DOMAIN_LIKE.match(value):
        return "http://" + value
    return value


def short_name_from_url(url: str, base_url: str) -> str:
    """https://cmt3.research.microsoft.com/MISTA2026/Submission/Index -> 'MISTA2026'."""
    if not same_site(url, base_url):
        return ""
    segments = [s for s in urlparse(url).path.split("/") if s]
    return segments[0] if segments else ""


def cmt_url_for(short_name: str, base_url: str) -> str:
    return f"{base_url.rstrip('/')}/{quote(short_name)}" if short_name else ""


def parse_display_date(text: str) -> str:
    """CMT renders list dates as M/D/YYYY; return ISO, or the original text if it doesn't parse."""
    value = clean_text(text)
    try:
        return datetime.strptime(value, "%m/%d/%Y").date().isoformat()
    except ValueError:
        return value


def parse_api_date(value: str) -> str:
    """OData timestamp -> local calendar date (the same conversion the CMT page does in your browser)."""
    value = clean_text(value)
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.date().isoformat()


def as_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def conference_from_api(item: dict, base_url: str) -> Conference:
    """Build a record from one item of CMT's /api/odata/(My)Conferences response."""
    lowered = {str(key).lower(): value for key, value in item.items()}  # PascalCase or camelCase keys

    def field(name: str) -> str:
        return clean_text(lowered.get(name.lower()))

    short_name = field("UrlSegmentName")
    city = field("City")
    country = field("Country")
    return Conference(
        name=field("Name"),
        short_name=short_name,
        cmt_url=cmt_url_for(short_name, base_url),
        external_url=normalize_external_url(field("ExternalUrl")),
        start_date=parse_api_date(field("StartDate")),
        end_date=parse_api_date(field("EndDate")),
        location=", ".join(part for part in (city, country) if part),
        city=city,
        country=country,
    )


def fill_missing(primary: Conference, extra: Conference) -> Conference:
    """Copy fields that `primary` lacks from `extra` (values on `primary` always win)."""
    for name in FIELDNAMES:
        if not getattr(primary, name) and getattr(extra, name):
            setattr(primary, name, getattr(extra, name))
    return primary


def merge_sources(table_rows: list[Conference], api_items: list[Conference]) -> list[Conference]:
    """Combine what the page displayed with the structured API data behind it.

    Displayed rows keep their order and values; API data fills gaps (end date, city/country, short name).
    API items that never rendered (e.g. the table was still drawing) are appended so nothing is lost.
    """
    by_short = {c.short_name.lower(): c for c in api_items if c.short_name}
    by_name = {c.name.lower(): c for c in api_items if c.name}
    used: set[int] = set()
    merged: list[Conference] = []
    seen: set[str] = set()
    for row in table_rows:
        # Short names are unique; names aren't, so they are only used when the row's link gave no short name.
        if row.short_name:
            match = by_short.get(row.short_name.lower())
        else:
            match = by_name.get(row.name.lower())
        if match is not None:
            fill_missing(row, match)
            used.add(id(match))
        if row.key not in seen:
            seen.add(row.key)
            merged.append(row)
    for item in api_items:
        if id(item) not in used and item.key not in seen:
            seen.add(item.key)
            merged.append(item)
    return merged


def matches_filter(conference: Conference, text: str) -> bool:
    """CMT's own filter-box rule: substring of name, city, country or short name.

    `location` is searched too: rows read from the table have it instead of city/country.
    """
    needle = text.strip().lower()
    if not needle:
        return True
    haystacks = (conference.name, conference.city, conference.country, conference.short_name, conference.location)
    return any(needle in h.lower() for h in haystacks if h)


def is_upcoming(conference: Conference, today: date) -> bool:
    """True when the conference ends (or, lacking an end date, starts) today or later."""
    when = as_date(conference.end_date) or as_date(conference.start_date)
    return when is not None and when >= today


_TIMELINE_DATE = re.compile(r"^(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?$")


def parse_timeline_date(text: str, *, end: bool = False) -> date:
    """'today', 'YYYY', 'YYYY-MM' or 'YYYY-MM-DD'.

    A bare year or month means its first day, or its last day when `end` is set,
    so "--from 2027-01 --to 2027-03" covers 1 January to 31 March 2027.
    """
    value = text.strip().lower()
    if value == "today":
        return date.today()
    match = _TIMELINE_DATE.match(value)
    if not match:
        raise ValueError(f"{text!r} is not a date; use YYYY, YYYY-MM, YYYY-MM-DD or 'today'")
    year, month, day = (int(part) if part else None for part in match.groups())
    try:
        if day is not None:
            return date(year, month, day)
        if month is not None:
            return date(year, month, calendar.monthrange(year, month)[1] if end else 1)
        return date(year, 12, 31) if end else date(year, 1, 1)
    except ValueError:  # includes calendar.IllegalMonthError
        raise ValueError(f"{text!r} is not a valid date") from None


def starts_within(conference: Conference, first: date | None, last: date | None) -> bool:
    """True when the conference starts between `first` and `last` (inclusive; either may be open)."""
    when = as_date(conference.start_date)
    return when is not None and (first is None or when >= first) and (last is None or when <= last)
