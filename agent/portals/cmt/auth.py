"""Signing in to CMT with your own account, and keeping that session between runs."""

from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Page
from playwright.sync_api import Error as PlaywrightError

from agent.compliance import AuditLog, RateLimiter, mask_email, open_page
from agent.config import LOGIN_PATH, Settings
from agent.errors import LoginError, short_error
from agent.models import clean_text, same_site

log = logging.getLogger(__name__)

EMAIL_INPUT = "#emailTextbox, input[type='email']"
PASSWORD_INPUT = "#passwordTextbox, input[type='password']"
SUBMIT_BUTTON = "#loginForm button[type='submit'], form button[type='submit']"
LOGIN_ERROR = ".alert-danger[role='alert']"
FORM_TIMEOUT_MS = 15_000


def is_login_url(url: str) -> bool:
    return urlparse(url).path.lower().startswith(LOGIN_PATH.lower())


def is_account_url(url: str) -> bool:
    """Login, register, verify-email and reset-password pages all live under /User/."""
    return urlparse(url).path.lower().startswith("/user/")


class Authenticator:
    def __init__(self, settings: Settings, audit: AuditLog, rate: RateLimiter) -> None:
        self.settings = settings
        self.audit = audit
        self.rate = rate
        self.last_error = ""

    def ensure_session(self, context: BrowserContext, page: Page) -> bool:
        """Leave `page` logged in (normally on the conference list), signing in first if needed.

        Returns True when a new login happened (the session is then saved right away).
        """
        open_page(page, self.settings.conference_list_url, self.rate, self.audit)
        if not is_login_url(page.url):
            log.info("Reusing the saved CMT session.")
            self.audit.record("session_reused")
            return False

        log.info("CMT is asking for a login.")
        if self.settings.has_credentials and self._login_with_credentials(page):
            self.save(context)
            return True
        if self.settings.headless:
            reason = f" ({self.last_error})" if self.last_error else ""
            raise LoginError(
                f"Login needed but the browser is headless{reason}. Run `python main.py login` once to "
                "sign in through a visible browser window, or check CMT_EMAIL / CMT_PASSWORD in .env."
            )
        self._wait_for_manual_login(page)
        self.save(context)
        return True

    def save(self, context: BrowserContext, quiet: bool = False) -> None:
        path = self.settings.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(path))
        log.log(logging.DEBUG if quiet else logging.INFO,
                "Session saved to %s (keep it private: it acts like a logged-in browser).", path)
        self.audit.record("session_saved", path=str(path))

    def _login_with_credentials(self, page: Page) -> bool:
        log.info("Signing in as %s with the credentials from .env.", mask_email(self.settings.email))
        email_box = page.locator(EMAIL_INPUT).first
        password_box = page.locator(PASSWORD_INPUT).first
        try:
            email_box.wait_for(state="visible", timeout=FORM_TIMEOUT_MS)
            email_box.fill(self.settings.email, timeout=FORM_TIMEOUT_MS)
            password_box.fill(self.settings.password, timeout=FORM_TIMEOUT_MS)
            email_box.dispatch_event("change")
            password_box.dispatch_event("change")
            self.audit.record("login_submit", method="credentials", account=mask_email(self.settings.email))
            page.locator(SUBMIT_BUTTON).first.click(timeout=FORM_TIMEOUT_MS)
        except PlaywrightError as exc:
            return self._login_failed(f"the login form could not be filled in ({short_error(exc)})")

        deadline = time.monotonic() + self.settings.action_timeout
        while time.monotonic() < deadline:
            if not is_login_url(page.url):
                break
            error = self._visible_login_error(page)
            if error:
                return self._login_failed(error)
            page.wait_for_timeout(500)
        else:
            return self._login_failed("the login form did not respond")

        if is_account_url(page.url):
            return self._login_failed(f"CMT redirected to {urlparse(page.url).path} and needs your attention")
        log.info("Logged in.")
        self.audit.record("login_success", method="credentials")
        return True

    def _login_failed(self, reason: str) -> bool:
        self.last_error = reason
        log.warning("Automatic login did not complete: %s", reason)
        self.audit.record("login_failed", method="credentials", reason=reason)
        return False

    @staticmethod
    def _visible_login_error(page: Page) -> str:
        alert = page.locator(LOGIN_ERROR).first
        try:
            if alert.count() and alert.is_visible():
                return clean_text(alert.inner_text()) or "Login failed"
        except PlaywrightError:
            pass
        return ""

    def _wait_for_manual_login(self, page: Page) -> None:
        minutes = self.settings.login_timeout / 60
        print(
            f"\n>>> Sign in to Microsoft CMT in the browser window (waiting up to {minutes:g} min).\n"
            "    Use your own account; complete any CAPTCHA or email verification yourself.\n",
            flush=True,
        )
        self.audit.record("login_manual_wait")
        if self.settings.email:
            try:
                page.locator(EMAIL_INPUT).first.fill(self.settings.email, timeout=5_000)
            except PlaywrightError:
                pass

        deadline = time.monotonic() + self.settings.login_timeout
        while time.monotonic() < deadline:
            try:
                url = page.url
                if same_site(url, self.settings.base_url) and not is_account_url(url):
                    log.info("Login detected.")
                    self.audit.record("login_success", method="manual")
                    return
                page.wait_for_timeout(1_000)
            except PlaywrightError as exc:
                raise LoginError("The browser window was closed before the login finished.") from exc
        raise LoginError(f"Timed out after {minutes:g} min waiting for the login to finish.")
