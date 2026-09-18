"""Cheap, deterministic rules that keep obvious non-user mail away from the model.

Every skip is logged with its reason so you can audit what never reached Jev.
"""

from __future__ import annotations

import re

_AUTOMATED_LOCALPART = re.compile(
    r"^(no-?reply|do-?not-?reply|donotreply|mailer-daemon|postmaster|bounce[s]?|"
    r"notification[s]?|alert[s]?|newsletter|digest|auto-?confirm|robot|daemon)([.+_-].*)?$",
    re.IGNORECASE,
)
_AUTOMATED_LOCALPART_PREFIX = re.compile(r"^(noreply|no-reply|donotreply|do-not-reply)", re.IGNORECASE)
_AUTO_SUBJECT = re.compile(
    r"^(automatic reply|auto(matic)?[- ]?reply|out of office|undeliverable|"
    r"delivery status notification|mail delivery failed|delivery failure|"
    r"returned mail|autoreply|abwesenheit|réponse automatique)",
    re.IGNORECASE,
)


def prefilter(headers: dict[str, str], from_email: str, own_emails: set[str],
              own_domains: set[str]) -> str | None:
    """Return a skip reason, or None when the message should be judged."""
    h = {k.lower(): (v or "") for k, v in headers.items()}
    addr = (from_email or "").lower()
    if not addr or "@" not in addr:
        return "no_sender"
    local, domain = addr.split("@", 1)
    if addr in own_emails:
        return "outbound_from_own_address"
    if any(domain == d or domain.endswith("." + d) for d in own_domains):
        return "outbound_from_own_domain"
    if _AUTOMATED_LOCALPART.match(local) or _AUTOMATED_LOCALPART_PREFIX.match(local):
        return "automated_sender"
    if h.get("list-unsubscribe") or h.get("list-id"):
        return "bulk_mail"
    precedence = h.get("precedence", "").strip().lower()
    if precedence in {"bulk", "list", "junk"}:
        return "bulk_mail"
    auto_submitted = h.get("auto-submitted", "").strip().lower()
    if auto_submitted and auto_submitted != "no":
        return "auto_submitted"
    if h.get("x-autoreply") or h.get("x-autorespond"):
        return "auto_reply"
    if _AUTO_SUBJECT.match(h.get("subject", "").strip()):
        return "auto_reply_or_bounce"
    return None
