"""Shared error types used across portals and the CLI."""

from __future__ import annotations


class LoginError(RuntimeError):
    """No logged-in session could be obtained."""


class ExtractionError(RuntimeError):
    """The conference list could not be read."""


def short_error(exc: BaseException) -> str:
    """First line of an error message (Playwright appends a long call log)."""
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__
