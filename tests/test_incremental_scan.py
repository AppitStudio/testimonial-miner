from __future__ import annotations

import json
import stat
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from testimonial_miner.cli import _date_bounds, cmd_scan
from testimonial_miner.config import Account, Config
from testimonial_miner.sources import MessageHead
from testimonial_miner.store import Store


class FakeJudge:
    def __init__(self, *_args, **_kwargs):
        pass

    def close(self) -> None:
        pass


class FakeGmail:
    enter_count = 0
    after_uid: int | None = None

    def __init__(self, account: Account):
        self.account = account
        self.folder = '"[Gmail]/All Mail"'
        self.uidvalidity = "uidv-1"

    def __enter__(self):
        type(self).enter_count += 1
        return self

    def __exit__(self, *_exc) -> None:
        pass

    def search(self, _since: str, after_uid: int | None = None) -> list[int]:
        type(self).after_uid = after_uid
        return [10]

    def fetch_heads(self, uids: list[int]) -> list[MessageHead]:
        return [MessageHead(
            id=f"message-{uid}", account=self.account.email, uid=uid, thread_id=None,
            from_name="User", from_email="user@example.net", subject="Feedback",
            date="2026-09-18T12:00:00+03:00", headers={},
        ) for uid in uids]

    def fetch_bodies(self, _heads: list[MessageHead]) -> None:
        pass


def make_args(data_dir: Path) -> Namespace:
    return Namespace(
        data_dir=str(data_dir), since=None, eml_dir=None, limit=None, account=None,
        rejudge=False, dry_run=False, full=False, workers=1,
    )


def make_config(data_dir: Path) -> Config:
    account = Account("owner@gmail.com", "not-a-real-password")
    return Config(
        api_key="not-a-real-key", model="jev-latest", accounts=[account],
        pending_accounts=[], own_emails={account.email}, since="2025-03-01",
        data_dir=data_dir, apps={"Example": {"aliases": [], "description": ""}},
    )


class IncrementalScanTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeGmail.enter_count = 0
        FakeGmail.after_uid = None

    def test_successful_account_advances_cursor_once_with_audit_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            cfg = make_config(data_dir)

            def successful_process(*_args, **_kwargs) -> None:
                pass

            with (patch("testimonial_miner.cli.load_config", return_value=cfg),
                  patch("testimonial_miner.cli.Judge", FakeJudge),
                  patch("testimonial_miner.cli.GmailImap", FakeGmail),
                  patch("testimonial_miner.cli._process_heads", successful_process)):
                self.assertEqual(cmd_scan(make_args(data_dir)), 0)

            self.assertEqual(FakeGmail.enter_count, 1)
            cursor = json.loads((data_dir / "cursors.json").read_text())[cfg.accounts[0].email]
            self.assertEqual(cursor["last_uid"], 10)
            self.assertEqual(cursor["oldest_uid_checked"], 10)
            self.assertEqual(cursor["oldest_message_date"], "2026-09-18T09:00:00+00:00")
            self.assertEqual(cursor["newest_message_date"], "2026-09-18T09:00:00+00:00")
            self.assertEqual(cursor["messages_checked"], 1)
            self.assertEqual(stat.S_IMODE(data_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((data_dir / "cursors.json").stat().st_mode), 0o600)

    def test_processing_error_keeps_existing_cursor_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            cfg = make_config(data_dir)
            Store(data_dir).set_cursor(cfg.accounts[0].email, "uidv-1", 5, cfg.since)

            def failing_process(_heads, _cfg, store, _judge, _patterns, totals,
                                _workers, _dry_run) -> None:
                totals["error"] += 1
                store.append_log({"id": "message-10", "status": "error"})

            with (patch("testimonial_miner.cli.load_config", return_value=cfg),
                  patch("testimonial_miner.cli.Judge", FakeJudge),
                  patch("testimonial_miner.cli.GmailImap", FakeGmail),
                  patch("testimonial_miner.cli._process_heads", failing_process)):
                self.assertEqual(cmd_scan(make_args(data_dir)), 0)

            cursor = json.loads((data_dir / "cursors.json").read_text())[cfg.accounts[0].email]
            self.assertEqual(FakeGmail.after_uid, 5)
            self.assertEqual(cursor["last_uid"], 5)

    def test_date_bounds_are_normalized_to_utc(self) -> None:
        oldest, newest = _date_bounds([
            "2026-09-18T12:00:00+03:00",
            "2026-09-18T08:30:00+00:00",
            "",
        ])
        self.assertEqual(oldest, "2026-09-18T08:30:00+00:00")
        self.assertEqual(newest, "2026-09-18T09:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
