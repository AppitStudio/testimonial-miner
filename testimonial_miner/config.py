"""Configuration: environment, app catalogue, and the thresholds the policy uses."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

PUBLIC_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "yahoo.com", "icloud.com", "me.com", "mac.com", "proton.me", "protonmail.com",
    "aol.com", "gmx.com", "gmx.de", "web.de", "mail.com", "hey.com", "fastmail.com",
}

DEFAULT_APPS: dict[str, dict] = {
    "DockFlow": {"aliases": ["Dock Flow"], "description": ""},
    "ExtraDock": {"aliases": ["Extra Dock"], "description": ""},
    "ExtraBar": {"aliases": ["Extra Bar"], "description": ""},
    "CoolDock": {"aliases": ["Cool Dock"], "description": ""},
    "Shiori": {"aliases": [], "description": ""},
}


@dataclass(frozen=True)
class Account:
    email: str
    app_password: str

    @property
    def label(self) -> str:
        return self.email


@dataclass(frozen=True)
class Thresholds:
    """Decision thresholds. Tune these against your own data; see README."""

    # A message becomes a testimonial *candidate* when all of these hold.
    sender_is_user_min: float = 0.5
    has_praise_min: float = 0.6
    praise_quality_min: float = 1.5  # Score on 0..3 (see judge.PRAISE_LEVELS)

    # Weaker evidence is kept as *borderline* so you can review it.
    borderline_has_praise_min: float = 0.35
    borderline_praise_quality_min: float = 1.0

    # Sentence-level selection of the quote.
    quote_sentence_min: float = 0.6
    quote_fallback_min: float = 0.4  # used only if no sentence clears quote_sentence_min

    # Below this Choice confidence the app is reported as "unclear".
    app_confidence_min: float = 0.5


@dataclass
class Config:
    api_key: str
    model: str
    accounts: list[Account]
    pending_accounts: list[str]   # GMAIL_n_EMAIL set, app password still empty
    own_emails: set[str]
    since: str  # YYYY-MM-DD
    data_dir: Path
    apps: dict[str, dict]
    thresholds: Thresholds = field(default_factory=Thresholds)
    max_body_chars: int = 8000
    max_sentences: int = 40

    @property
    def own_domains(self) -> set[str]:
        domains = {e.split("@", 1)[1].lower() for e in self.own_emails if "@" in e}
        return {d for d in domains if d not in PUBLIC_MAIL_DOMAINS}

    def app_patterns(self) -> dict[str, re.Pattern]:
        """Case-insensitive word-boundary regex per app, matching the name and its aliases."""
        out = {}
        for name, meta in self.apps.items():
            names = [name, *meta.get("aliases", [])]
            alts = "|".join(re.escape(n) for n in names)
            out[name] = re.compile(rf"(?<![A-Za-z0-9])(?:{alts})(?![A-Za-z0-9])", re.IGNORECASE)
        return out


def _load_apps() -> dict[str, dict]:
    path = PROJECT_ROOT / "apps.json"
    if path.exists():
        with path.open() as f:
            data = json.load(f)
        if not isinstance(data, dict) or not data:
            raise SystemExit(f"{path} must be a non-empty JSON object keyed by app name")
        return {k: {"aliases": v.get("aliases", []), "description": v.get("description", "")}
                for k, v in data.items()}
    return DEFAULT_APPS


def _load_accounts() -> tuple[list[Account], list[str]]:
    """Returns (ready accounts, addresses that still lack an app password)."""
    accounts, pending = [], []
    for i in range(1, 10):
        email = os.environ.get(f"GMAIL_{i}_EMAIL", "").strip().lower()
        pw = os.environ.get(f"GMAIL_{i}_APP_PASSWORD", "").replace(" ", "").strip()
        if email and pw:
            accounts.append(Account(email=email, app_password=pw))
        elif email:
            pending.append(email)
        elif pw:
            raise SystemExit(f"GMAIL_{i}_APP_PASSWORD is set but GMAIL_{i}_EMAIL is empty")
    return accounts, pending


def load_config(data_dir: str | None = None, since: str | None = None) -> Config:
    api_key = os.environ.get("TYPESAFE_API_KEY") or os.environ.get("API_KEY", "")
    if not api_key:
        raise SystemExit("Set API_KEY (or TYPESAFE_API_KEY) in .env")
    accounts, pending = _load_accounts()
    own = {a.email for a in accounts} | set(pending)
    own |= {e.strip().lower() for e in os.environ.get("OWN_EMAILS", "").split(",") if e.strip()}
    since_value = since or os.environ.get("SINCE_DATE", "2025-03-01")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since_value):
        raise SystemExit("--since / SINCE_DATE must be YYYY-MM-DD")
    return Config(
        api_key=api_key,
        model=os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        accounts=accounts,
        pending_accounts=pending,
        own_emails=own,
        since=since_value,
        data_dir=Path(data_dir or os.environ.get("DATA_DIR", PROJECT_ROOT / "data")),
        apps=_load_apps(),
    )
