"""Runtime settings, read from environment variables (or a local .env file) and CLI flags."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is optional; plain environment variables still work
    load_dotenv = None

DEFAULT_BASE_URL = "https://cmt3.research.microsoft.com"
LOGIN_PATH = "/User/Login"
CONFERENCE_LIST_PATH = "/Conference/Recent"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env(name: str) -> str | None:
    """The variable's value, or None when it is unset or blank."""
    value = (os.getenv(name) or "").strip()
    return value or None


def _env_bool(name: str) -> bool | None:
    value = _env(name)
    return None if value is None else value.lower() in {"1", "true", "yes", "on"}


def _env_float(name: str) -> float | None:
    value = _env(name)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {value!r}") from exc


def _env_path(name: str) -> Path | None:
    value = _env(name)
    return None if value is None else Path(value)


@dataclass
class Settings:
    base_url: str = DEFAULT_BASE_URL
    email: str | None = None
    password: str | None = field(default=None, repr=False)  # never printed
    state_path: Path = PROJECT_ROOT / ".auth" / "cmt_state.json"
    easychair_email: str | None = None
    easychair_password: str | None = field(default=None, repr=False)
    easychair_state_path: Path = PROJECT_ROOT / ".auth" / "easychair_state.json"
    ollama_model: str | None = None
    ollama_base_url: str | None = None
    output_dir: Path = PROJECT_ROOT / "output"
    log_dir: Path = PROJECT_ROOT / "logs"
    headless: bool = False
    keep_open: bool = True  # leave the browser window open when done, until you close it
    # Politeness: random pause between actions that make CMT do work (page loads, list requests).
    min_delay: float = 2.0
    max_delay: float = 5.0
    login_timeout: float = 300.0  # how long to wait for you to finish a manual login
    action_timeout: float = 60.0  # page loads, clicks and API responses
    render_timeout: float = 300.0  # "Show All" on CMT can render 10,000+ rows
    settle_time: float = 1.5  # the list counts as loaded once rows stop changing for this long

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        address = urlparse(self.base_url)
        if address.scheme not in ("http", "https") or not address.netloc:
            raise ValueError(f"The CMT address must start with http:// or https:// (got {self.base_url!r}).")
        if self.min_delay < 0 or self.max_delay < self.min_delay:
            raise ValueError("Delays must satisfy 0 <= min_delay <= max_delay.")

    @property
    def conference_list_url(self) -> str:
        return f"{self.base_url}{CONFERENCE_LIST_PATH}"

    @property
    def has_credentials(self) -> bool:
        return bool(self.email and self.password)

    @classmethod
    def from_env(cls, env_file: Path | None = None, **overrides) -> Settings:
        """Settings from the environment / .env, with non-None `overrides` (e.g. CLI flags) on top.

        Anything given neither way keeps the default declared on the class above.
        """
        if load_dotenv is not None:
            # Real environment variables win over values in the .env file.
            load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
        from_environment = {
            "base_url": _env("CMT_BASE_URL"),
            "email": _env("CMT_EMAIL"),
            "password": os.getenv("CMT_PASSWORD") or None,  # not stripped: spaces may be part of it
            "headless": _env_bool("CMT_HEADLESS"),
            "keep_open": _env_bool("CMT_KEEP_OPEN"),
            "min_delay": _env_float("CMT_MIN_DELAY"),
            "max_delay": _env_float("CMT_MAX_DELAY"),
            "state_path": _env_path("CMT_STATE_PATH"),
            "easychair_email": _env("EASYCHAIR_EMAIL"),
            "easychair_password": os.getenv("EASYCHAIR_PASSWORD") or None,
            "easychair_state_path": _env_path("EASYCHAIR_STATE_PATH"),
            "ollama_model": _env("OLLAMA_MODEL"),
            "ollama_base_url": _env("OLLAMA_BASE_URL"),
            "output_dir": _env_path("CMT_OUTPUT_DIR"),
            "log_dir": _env_path("CMT_LOG_DIR"),
        }
        values = {key: value for key, value in from_environment.items() if value is not None}
        values.update({key: value for key, value in overrides.items() if value is not None})
        # When only one delay limit is given (e.g. --min-delay 8), the other one moves along with it.
        if "max_delay" not in values:
            values["max_delay"] = max(values.get("min_delay", cls.min_delay), cls.max_delay)
        if "min_delay" not in values:
            values["min_delay"] = min(values["max_delay"], cls.min_delay)
        return cls(**values)
