"""On-disk state: an append-only log of every message seen, the testimonials JSON
database you review, and per-account IMAP cursors for incremental runs."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _write_json_atomic(path: Path, value: object, *, ensure_ascii: bool = True) -> None:
    """Write private JSON and atomically replace the destination."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(value, f, ensure_ascii=ensure_ascii, indent=2)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)
    path.chmod(0o600)


class Store:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        created = not self.dir.exists()
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if created:
            self.dir.chmod(0o700)
        self.log_path = self.dir / "classified.jsonl"
        self.db_path = self.dir / "testimonials.json"
        self.cursor_path = self.dir / "cursors.json"
        self._db: dict | None = None

    # --- log -------------------------------------------------------------------------
    def iter_log(self):
        if not self.log_path.exists():
            return
        with self.log_path.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def processed_ids(self) -> set[str]:
        return {r["id"] for r in self.iter_log() if r.get("status") != "error"}

    def append_log(self, record: dict) -> None:
        fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.log_path.chmod(0o600)

    # --- testimonials db --------------------------------------------------------------
    def db(self) -> dict:
        if self._db is None:
            if self.db_path.exists():
                with self.db_path.open() as f:
                    self._db = json.load(f)
            else:
                self._db = {"version": 1, "updated_at": None, "items": []}
        return self._db

    def upsert(self, item: dict) -> None:
        items = self.db()["items"]
        for i, existing in enumerate(items):
            if existing["id"] == item["id"]:
                # Keep what you edited by hand.
                item["review"] = existing.get("review", item["review"])
                item["notes"] = existing.get("notes", item["notes"])
                items[i] = item
                return
        items.append(item)

    def save_db(self) -> None:
        db = self.db()
        db["items"].sort(key=lambda x: x.get("date") or "", reverse=True)
        db["updated_at"] = now_iso()
        _write_json_atomic(self.db_path, db, ensure_ascii=False)

    # --- cursors ----------------------------------------------------------------------
    def cursors(self) -> dict:
        if self.cursor_path.exists():
            with self.cursor_path.open() as f:
                return json.load(f)
        return {}

    def set_cursor(self, account: str, uidvalidity: str, last_uid: int, since: str, *,
                   oldest_uid_checked: int | None = None,
                   oldest_message_date: str | None = None,
                   newest_message_date: str | None = None,
                   messages_checked: int | None = None) -> None:
        c = self.cursors()
        cursor = {"uidvalidity": uidvalidity, "last_uid": last_uid, "since": since,
                  "updated_at": now_iso()}
        if oldest_uid_checked is not None:
            cursor["oldest_uid_checked"] = oldest_uid_checked
        if oldest_message_date is not None:
            cursor["oldest_message_date"] = oldest_message_date
        if newest_message_date is not None:
            cursor["newest_message_date"] = newest_message_date
        if messages_checked is not None:
            cursor["messages_checked"] = messages_checked
        c[account] = cursor
        _write_json_atomic(self.cursor_path, c)
