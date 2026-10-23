"""Guard-rails that keep the agent's traffic polite, transparent and accountable.

* RateLimiter  - spaces out every action that makes CMT do work, like a person clicking.
* RobotsPolicy - honours robots.txt if the CMT host publishes one.
* AuditLog     - append-only JSON-lines record of every page opened and action taken.
"""

from __future__ import annotations

import json
import logging
import random
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.robotparser import RobotFileParser

log = logging.getLogger(__name__)

AGENT_NAME = "CMTConferenceAgent"

USAGE_NOTICE = """\
CMT Conference Agent - responsible-use notice
  * Signs in only with YOUR OWN CMT account and reads only pages that account can already see.
  * Never bypasses CAPTCHAs, email verification or other checks; those are handed to you in the browser.
  * Paces its clicks ({min_delay:g}-{max_delay:g}s apart), honours robots.txt and logs every action to {audit}.
  * You remain responsible for following the Microsoft CMT Terms of Use and for how you use the data.
"""


class ComplianceError(RuntimeError):
    """The requested action is not permitted by the site's published rules."""


class RateLimiter:
    """Waits a random, human-like interval between server-hitting actions."""

    def __init__(self, min_delay: float, max_delay: float) -> None:
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._last_action: float | None = None

    def wait(self, reason: str = "") -> float:
        """Sleep until the next action is allowed; returns the seconds slept."""
        now = time.monotonic()
        slept = 0.0
        if self._last_action is not None:
            target_gap = random.uniform(self.min_delay, self.max_delay)
            slept = max(0.0, target_gap - (now - self._last_action))
            if slept > 0:
                log.debug("Pausing %.1fs before %s", slept, reason or "next action")
                time.sleep(slept)
        self._last_action = time.monotonic()
        return slept


class RobotsPolicy:
    """robots.txt rules for the CMT host. No published file means no extra restrictions."""

    def __init__(self, parser: RobotFileParser | None, status: str) -> None:
        self._parser = parser
        self.status = status

    @classmethod
    def fetch(cls, request_context, base_url: str) -> RobotsPolicy:
        """Fetch robots.txt with the browser's own request context (same session, same identity)."""
        url = f"{base_url}/robots.txt"
        try:
            # Don't follow redirects: CMT answers unknown paths with a redirect to its login page.
            response = request_context.get(url, max_redirects=0, timeout=15_000)
        except Exception as exc:  # network trouble shouldn't be mistaken for permission
            return cls(None, f"could not be fetched ({exc.__class__.__name__}); no rules applied")
        content_type = response.headers.get("content-type", "")
        if response.status == 200 and "text/plain" in content_type:
            parser = RobotFileParser(url)
            parser.parse(response.text().splitlines())
            return cls(parser, "published; rules applied")
        return cls(None, f"not published (HTTP {response.status}); no rules applied")

    def allows(self, url: str) -> bool:
        return self._parser is None or self._parser.can_fetch(AGENT_NAME, url)

    def require(self, url: str) -> None:
        if not self.allows(url):
            raise ComplianceError(f"robots.txt disallows automated access to {url}; stopping.")


class AuditLog:
    """Append-only JSON-lines log. Never contains passwords or cookies."""

    def __init__(self, log_dir: Path) -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = uuid.uuid4().hex[:8]
        self.path = log_dir / f"audit-{datetime.now():%Y%m%d}.jsonl"

    def record(self, event: str, **details) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "run": self.run_id,
            "event": event,
            **details,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def open_page(page, url: str, rate: RateLimiter, audit: AuditLog) -> None:
    """Open `url` the way every page load must happen: paced like a person, and recorded."""
    rate.wait(f"opening {url}")
    audit.record("navigate", url=url)
    page.goto(url, wait_until="domcontentloaded")


def mask_email(email: str | None) -> str:
    """'someone@example.com' -> 's*****e@example.com' for logs and console output."""
    if not email or "@" not in email:
        return "<unknown>"
    user, _, domain = email.partition("@")
    if len(user) <= 2:
        return f"{user[:1]}*@{domain}"
    return f"{user[0]}{'*' * (len(user) - 2)}{user[-1]}@{domain}"
