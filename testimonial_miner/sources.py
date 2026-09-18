"""Message sources: Gmail over IMAP (app password) and a directory of .eml files."""

from __future__ import annotations

import email
import imaplib
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from email import policy
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from .config import Account

HEADER_FIELDS = (
    "FROM TO SUBJECT DATE MESSAGE-ID LIST-UNSUBSCRIBE LIST-ID PRECEDENCE AUTO-SUBMITTED "
    "X-AUTOREPLY X-AUTORESPOND IN-REPLY-TO CONTENT-TYPE"
)
HEADER_BATCH = 200
BODY_BATCH = 25


@dataclass
class MessageHead:
    """Headers plus identifiers; the body is fetched later only for survivors."""

    id: str          # stable id: Gmail X-GM-MSGID, or Message-ID / filename for .eml
    account: str
    uid: int | None
    thread_id: str | None
    from_name: str
    from_email: str
    subject: str
    date: str        # ISO 8601 or ""
    headers: dict[str, str] = field(default_factory=dict)
    _message: Message | None = None

    @property
    def message(self) -> Message | None:
        return self._message


def _headers_dict(msg: Message) -> dict[str, str]:
    out: dict[str, str] = {}
    for k in msg.keys():
        try:
            out[k.lower()] = str(msg.get(k, ""))
        except Exception:  # noqa: BLE001 - undecodable header
            out[k.lower()] = ""
    return out


def _head_from_message(msg: Message, *, id: str, account: str, uid: int | None,
                       thread_id: str | None) -> MessageHead:
    name, addr = parseaddr(str(msg.get("From", "")))
    date_iso = ""
    try:
        d = parsedate_to_datetime(str(msg.get("Date", "")))
        date_iso = d.isoformat() if d else ""
    except Exception:  # noqa: BLE001
        pass
    return MessageHead(
        id=id, account=account, uid=uid, thread_id=thread_id,
        from_name=name.strip(), from_email=addr.strip().lower(),
        subject=str(msg.get("Subject", "")).strip(), date=date_iso,
        headers=_headers_dict(msg),
    )


# --- .eml directory ------------------------------------------------------------------------

def iter_eml_dir(path: Path, account: str = "eml") -> Iterator[MessageHead]:
    for file in sorted(path.glob("*.eml")):
        msg = email.message_from_bytes(file.read_bytes(), policy=policy.default)
        mid = str(msg.get("Message-ID", "")).strip() or file.name
        head = _head_from_message(msg, id=mid, account=account, uid=None, thread_id=None)
        head._message = msg
        yield head


# --- Gmail IMAP ----------------------------------------------------------------------------

_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?:"(?P<delim>[^"]*)"|NIL)\s+(?P<name>.+)$')
_ATTR_RE = {
    "uid": re.compile(r"\bUID (\d+)"),
    "msgid": re.compile(r"\bX-GM-MSGID (\d+)"),
    "thrid": re.compile(r"\bX-GM-THRID (\d+)"),
}


class ImapError(RuntimeError):
    """Login, folder, search or fetch failure on one account."""


class GmailImap:
    """Read-only access to one Gmail account's All Mail folder over IMAP."""

    def __init__(self, account: Account):
        self.account = account
        self.conn: imaplib.IMAP4_SSL | None = None
        self.folder = '"[Gmail]/All Mail"'
        self.uidvalidity: str | None = None

    def __enter__(self) -> GmailImap:
        try:
            self.conn = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=60)
            self.conn.login(self.account.email, self.account.app_password)
            self.folder = self._find_all_mail()
            typ, data = self.conn.select(self.folder, readonly=True)
            if typ != "OK":
                raise ImapError(f"Could not open {self.folder} on {self.account.email}: {data}")
            typ, data = self.conn.response("UIDVALIDITY")
            if data and data[0]:
                self.uidvalidity = (data[0].decode() if isinstance(data[0], bytes)
                                    else str(data[0]))
            if not self.uidvalidity:
                raise ImapError(
                    f"Gmail did not report UIDVALIDITY for {self.account.email}; refusing to "
                    "use an unsafe incremental cursor"
                )
            return self
        except (imaplib.IMAP4.error, OSError) as e:
            self._close()
            raise ImapError(
                f"IMAP login/setup failed for {self.account.email}: {e}\n"
                "  Use a 16-character Google *app password* (requires 2-Step Verification), "
                "not the account password. For a Workspace account also check that IMAP is "
                "enabled and app passwords are allowed by the admin. See README."
            ) from e
        except Exception:
            self._close()
            raise

    def __exit__(self, *exc) -> None:
        self._close()

    def _close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.logout()
            except Exception:  # noqa: BLE001
                pass
            finally:
                self.conn = None

    def _uid(self, command: str, *args):
        if self.conn is None:
            raise ImapError(f"IMAP connection is not open for {self.account.email}")
        try:
            return self.conn.uid(command, *args)
        except (imaplib.IMAP4.error, OSError) as e:
            raise ImapError(f"IMAP {command.lower()} failed for {self.account.email}: {e}") from e

    def _find_all_mail(self) -> str:
        typ, lines = self.conn.list()
        for line in lines or []:
            if not isinstance(line, bytes):
                continue
            m = _LIST_RE.match(line)
            if m and rb"\All" in m.group("flags"):
                return m.group("name").decode("utf-8", "replace")
        return '"[Gmail]/All Mail"'

    def search(self, since_iso: str, after_uid: int | None = None) -> list[int]:
        """UIDs of messages received after `since_iso` (YYYY-MM-DD), newest first."""
        y, mo, d = since_iso.split("-")
        query = f'"after:{y}/{mo}/{d}"'
        args: list[str] = []
        if after_uid:
            args += ["UID", f"{after_uid + 1}:*"]
        args += ["X-GM-RAW", query]
        typ, data = self._uid("SEARCH", *args)
        if typ != "OK":
            raise ImapError(f"IMAP search failed: {data}")
        uids = [int(x) for x in (data[0] or b"").split()]
        # "N:*" also returns UID N itself when nothing newer exists; drop it.
        if after_uid:
            uids = [u for u in uids if u > after_uid]
        return sorted(uids, reverse=True)

    @staticmethod
    def _iter_fetch(data) -> Iterator[tuple[str, bytes]]:
        for item in data or []:
            if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], bytes):
                yield item[0].decode("latin-1", "replace"), item[1]

    def fetch_heads(self, uids: list[int]) -> list[MessageHead]:
        out: list[MessageHead] = []
        fetched_uids: set[int] = set()
        for i in range(0, len(uids), HEADER_BATCH):
            chunk = uids[i:i + HEADER_BATCH]
            spec = f"(UID X-GM-MSGID X-GM-THRID BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])"
            typ, data = self._uid("FETCH", ",".join(map(str, chunk)), spec)
            if typ != "OK":
                raise ImapError(f"IMAP header fetch failed: {data}")
            for attrs, raw in self._iter_fetch(data):
                uid = _ATTR_RE["uid"].search(attrs)
                msgid = _ATTR_RE["msgid"].search(attrs)
                thrid = _ATTR_RE["thrid"].search(attrs)
                if not uid:
                    continue
                fetched_uid = int(uid.group(1))
                fetched_uids.add(fetched_uid)
                msg = email.message_from_bytes(raw, policy=policy.default)
                out.append(_head_from_message(
                    msg, id=msgid.group(1) if msgid else f"uid:{uid.group(1)}",
                    account=self.account.email, uid=fetched_uid,
                    thread_id=thrid.group(1) if thrid else None,
                ))
        missing = sorted(set(uids) - fetched_uids)
        if missing:
            sample = ", ".join(map(str, missing[:5]))
            raise ImapError(
                f"Gmail returned no headers for {len(missing)} requested message(s) in "
                f"{self.account.email} (UIDs: {sample}); cursor was not advanced"
            )
        return out

    def fetch_bodies(self, heads: list[MessageHead]) -> None:
        """Attach the full parsed message to each head (in place)."""
        by_uid = {h.uid: h for h in heads if h.uid is not None}
        uids = list(by_uid)
        for i in range(0, len(uids), BODY_BATCH):
            chunk = uids[i:i + BODY_BATCH]
            typ, data = self._uid("FETCH", ",".join(map(str, chunk)), "(UID BODY.PEEK[])")
            if typ != "OK":
                raise ImapError(f"IMAP body fetch failed: {data}")
            for attrs, raw in self._iter_fetch(data):
                uid = _ATTR_RE["uid"].search(attrs)
                if uid and int(uid.group(1)) in by_uid:
                    by_uid[int(uid.group(1))]._message = email.message_from_bytes(
                        raw, policy=policy.default)
        missing = sorted(uid for uid, head in by_uid.items() if head.message is None)
        if missing:
            sample = ", ".join(map(str, missing[:5]))
            raise ImapError(
                f"Gmail returned no body for {len(missing)} requested message(s) in "
                f"{self.account.email} (UIDs: {sample}); cursor was not advanced"
            )
