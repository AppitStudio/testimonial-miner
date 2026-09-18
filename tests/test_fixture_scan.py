"""Offline end-to-end scan of the synthetic emails in fixtures/ with a scripted model.

Exercises header prefiltering, MIME cleaning, quoted-reply removal, sentence splitting,
request construction, the decision policy, the audit log, the database, and deduplication.
The scripted answers below imitate the outcome of the live fixture run recorded in
AGENTS.md; they are not Jev output and say nothing about the model's accuracy.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from testimonial_miner.cli import cmd_scan
from testimonial_miner.config import DEFAULT_APPS, PROJECT_ROOT, Config
from testimonial_miner.store import Store

FIXTURES = PROJECT_ROOT / "fixtures"
OWNER = "appitstudio@gmail.com"

# One script per sender. `quote_terms` marks the sentences the fake model calls quotable.
SCRIPT: dict[str, dict] = {
    "jane.miller@example.com": {"kind": "user_feedback", "praise": 0.97, "quality": 2.8,
                                "quote_terms": ["DockFlow deserves it", "Worth every cent"]},
    "tom@example.org": {"kind": "support_request", "praise": 0.9, "quality": 2.2, "problem": 0.95,
                        "quote_terms": ["I love this app", "cleanest menu bar"]},
    "priya.r@example.net": {"kind": "purchase_or_license", "praise": 0.03, "quality": 0.1,
                            "problem": 0.8},
    "mark@growthagency.example.com": {"kind": "business_outreach", "sender": 0.05, "praise": 0.9,
                                      "quality": 2.0, "quote_terms": ["fantastic product"]},
    "lena.fischer@example.de": {"kind": "user_feedback", "praise": 0.95, "quality": 2.6,
                                "quote_terms": ["better than from companies", "favourite thing"]},
    "oscar@example.com": {"kind": "other", "praise": 0.3, "quality": 0.4},
    "klaus.weber@example.de": {"kind": "user_feedback", "praise": 0.93, "quality": 2.4,
                               "english": 0.03, "quote_terms": ["beste Erweiterung", "sparen mir"]},
    "aiko@example.jp": {"kind": "feature_request", "praise": 0.9, "quality": 2.3,
                        "quote_terms": ["genuinely enjoyable", "typography is beautiful"]},
}


class ScriptedJudge:
    """Stands in for judge.Judge: deterministic answers derived from the request state."""

    calls: list[dict] = []

    def __init__(self, *_args, **_kwargs):
        pass

    def judge(self, state: dict, questions: dict) -> tuple[dict, str, dict]:
        type(self).calls.append(state)
        email = state["email"]
        s = SCRIPT[email["from_email"]]
        sentences = [line.split("| ", 1)[1] for line in email["sentences"]]
        assert {f"quote_{i}" for i in range(len(sentences))} <= set(questions)
        named = state["apps_named_in_text"]
        app = named[0] if len(named) == 1 else "unclear"
        answers = {
            "kind": {"type": "choice", "choice": s["kind"], "confidence": 0.9,
                     "probabilities": {s["kind"]: 0.9}},
            "app": {"type": "choice", "choice": app, "confidence": 0.9 if app != "unclear" else 0.4,
                    "probabilities": {app: 0.9}},
            "sender_is_user": {"type": "noul", "noul": s.get("sender", 0.9)},
            "has_praise": {"type": "noul", "noul": s["praise"]},
            "praise_quality": {"type": "score", "score": s["quality"], "confidence": 0.7,
                               "probabilities": {}},
            "mentions_problem": {"type": "noul", "noul": s.get("problem", 0.05)},
            "is_english": {"type": "noul", "noul": s.get("english", 0.97)},
        }
        for i, text in enumerate(sentences):
            hit = any(term in text for term in s.get("quote_terms", []))
            answers[f"quote_{i}"] = {"type": "noul", "noul": 0.9 if hit else 0.1}
        return answers, "scripted-fixture-model", {"input_tokens": 0, "output_tokens": 0}

    def close(self) -> None:
        pass


def make_config(data_dir: Path) -> Config:
    return Config(api_key="not-a-real-key", model="scripted", accounts=[], pending_accounts=[],
                  own_emails={OWNER}, since="2025-03-01", data_dir=data_dir, apps=DEFAULT_APPS)


def run_scan(data_dir: Path, **overrides) -> tuple[int, str]:
    args = Namespace(data_dir=str(data_dir), since=None, eml_dir=str(FIXTURES), limit=None,
                     account=None, rejudge=False, dry_run=False, full=False, workers=2)
    vars(args).update(overrides)
    out = io.StringIO()
    with (patch("testimonial_miner.cli.load_config", return_value=make_config(data_dir)),
          patch("testimonial_miner.cli.Judge", ScriptedJudge),
          contextlib.redirect_stdout(out)):
        code = cmd_scan(args)
    return code, out.getvalue()


class FixtureScanTests(unittest.TestCase):
    def setUp(self) -> None:
        ScriptedJudge.calls = []

    def test_scripted_scan_reproduces_the_documented_fixture_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            code, _out = run_scan(data_dir)
            self.assertEqual(code, 0)
            store = Store(data_dir)
            log = list(store.iter_log())
            self.assertEqual(len(log), 11)

            skips = {r["from_email"]: r["skip_reason"] for r in log if r.get("skip_reason")}
            self.assertEqual(skips, {
                "digest@macnews.example.com": "automated_sender",  # local part matches before the List-Unsubscribe rule
                "noreply@reviews.example.com": "automated_sender",
                OWNER: "outbound_from_own_address",
            })
            self.assertEqual(len(ScriptedJudge.calls), 8)

            statuses = {r["from_email"]: r["status"] for r in log if "answers" in r}
            self.assertEqual(statuses, {
                "jane.miller@example.com": "candidate",
                "tom@example.org": "candidate",
                "priya.r@example.net": "rejected",
                "mark@growthagency.example.com": "rejected",
                "lena.fischer@example.de": "candidate",
                "oscar@example.com": "rejected",
                "klaus.weber@example.de": "candidate",
                "aiko@example.jp": "candidate",
            })

            items = {i["from_email"]: i for i in store.db()["items"]}
            self.assertEqual(set(items), {e for e, s in statuses.items() if s == "candidate"})
            self.assertEqual({i["app"] for i in items.values()},
                             {"DockFlow", "ExtraBar", "CoolDock", "ExtraDock", "Shiori"})
            self.assertTrue(all(i["review"] == "pending" and i["model"] == "scripted-fixture-model"
                                for i in items.values()))

            jane = items["jane.miller@example.com"]
            self.assertIn("DockFlow deserves it.", jane["quote"])
            self.assertIn(" […] Worth every cent.", jane["quote"])
            self.assertNotIn("Hi Asaf", jane["quote"])
            self.assertNotIn("Product Designer", jane["body"])

            tom = items["tom@example.org"]
            self.assertGreaterEqual(tom["mentions_problem"], 0.5)
            self.assertNotIn("Sent from my iPhone", tom["body"])

            lena = items["lena.fischer@example.de"]
            self.assertNotIn("version 2.0.3", lena["body"])
            self.assertIn("favourite thing on the dock", lena["quote"])

            klaus = items["klaus.weber@example.de"]
            self.assertLess(klaus["is_english"], 0.5)

            oscar = next(r for r in log if r["from_email"] == "oscar@example.com")
            self.assertEqual(oscar["sentences"], ["Got it, thanks!"])

            mark = next(r for r in log if r["from_email"] == "mark@growthagency.example.com")
            self.assertEqual(mark["reasons"], ["kind=business_outreach"])

    def test_second_scan_skips_processed_messages_unless_rejudging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            run_scan(data_dir)
            self.assertEqual(len(ScriptedJudge.calls), 8)

            _code, out = run_scan(data_dir)
            self.assertEqual(len(ScriptedJudge.calls), 8)
            self.assertIn("already_processed                11", out)
            self.assertEqual(len(list(Store(data_dir).iter_log())), 11)

            run_scan(data_dir, rejudge=True)
            self.assertEqual(len(ScriptedJudge.calls), 16)
            self.assertEqual(len(Store(data_dir).db()["items"]), 5)

    def test_dry_run_logs_header_skips_and_never_builds_a_judge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            with patch.object(ScriptedJudge, "__init__", side_effect=AssertionError("no judge")):
                _code, out = run_scan(data_dir, dry_run=True)
            self.assertEqual(out.count("would judge"), 8)
            self.assertEqual(len(list(Store(data_dir).iter_log())), 3)
            self.assertFalse((data_dir / "testimonials.json").exists()
                             and Store(data_dir).db()["items"])


if __name__ == "__main__":
    unittest.main()
