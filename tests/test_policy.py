"""Decision policy, quote assembly, app resolution, and request construction.

Pure unit tests: no network, no mailbox, no model. The answer dictionaries below are
hand-written stand-ins shaped like judge.answers_to_dict output; they are not Jev output.
"""

from __future__ import annotations

import unittest

from testimonial_miner.config import DEFAULT_APPS, Thresholds
from testimonial_miner.judge import build_questions, build_state
from testimonial_miner.policy import decide, resolve_app, select_quote
from testimonial_miner.sources import MessageHead

T = Thresholds()
SENTENCES = [
    "Hi Asaf,",
    "DockFlow has completely changed how I manage my Mac dock.",
    "The multi-monitor support is exactly what I needed.",
    "Could you add a dark mode?",
    "Best regards, Jane",
]


def answers(*, kind="user_feedback", app="DockFlow", app_conf=0.9, sender=0.9, praise=0.95,
            quality=2.4, quotes=(0.1, 0.9, 0.85, 0.05, 0.02)) -> dict:
    out = {
        "kind": {"type": "choice", "choice": kind, "confidence": 0.9, "probabilities": {kind: 0.9}},
        "app": {"type": "choice", "choice": app, "confidence": app_conf, "probabilities": {app: app_conf}},
        "sender_is_user": {"type": "noul", "noul": sender},
        "has_praise": {"type": "noul", "noul": praise},
        "praise_quality": {"type": "score", "score": quality, "confidence": 0.7, "probabilities": {}},
        "mentions_problem": {"type": "noul", "noul": 0.1},
        "is_english": {"type": "noul", "noul": 0.98},
    }
    for i, p in enumerate(quotes):
        out[f"quote_{i}"] = {"type": "noul", "noul": p}
    return out


class DecideTests(unittest.TestCase):
    def test_candidate_quote_is_verbatim_sentences_in_order(self) -> None:
        d = decide(answers(), SENTENCES, ["DockFlow"], T)
        self.assertEqual(d.status, "candidate")
        self.assertEqual(d.app, "DockFlow")
        self.assertEqual(d.quote, f"{SENTENCES[1]} {SENTENCES[2]}")
        self.assertEqual([s["id"] for s in d.quote_sentences], ["S01", "S02"])
        self.assertEqual(d.reasons, [])

    def test_non_contiguous_sentences_are_joined_with_an_ellipsis(self) -> None:
        d = decide(answers(quotes=(0.9, 0.1, 0.9, 0.1, 0.1)), SENTENCES, ["DockFlow"], T)
        self.assertEqual(d.quote, f"{SENTENCES[0]} […] {SENTENCES[2]}")

    def test_borderline_records_which_thresholds_failed(self) -> None:
        d = decide(answers(praise=0.5, quality=1.2), SENTENCES, ["DockFlow"], T)
        self.assertEqual(d.status, "borderline")
        self.assertEqual(d.reasons, ["has_praise=0.50", "praise_quality=1.20"])
        self.assertTrue(d.quote)

    def test_unlikely_user_with_strong_praise_is_borderline(self) -> None:
        d = decide(answers(sender=0.2), SENTENCES, ["DockFlow"], T)
        self.assertEqual(d.status, "borderline")
        self.assertEqual(d.reasons, ["sender_is_user=0.20"])

    def test_business_outreach_is_rejected_even_when_it_praises_the_app(self) -> None:
        d = decide(answers(kind="business_outreach"), SENTENCES, ["DockFlow"], T)
        self.assertEqual(d.status, "rejected")
        self.assertEqual(d.reasons, ["kind=business_outreach"])
        self.assertEqual((d.quote, d.quote_sentences), ("", []))

    def test_automated_mail_is_rejected(self) -> None:
        self.assertEqual(decide(answers(kind="automated"), SENTENCES, [], T).status, "rejected")

    def test_weak_praise_is_rejected_with_both_values_in_reasons(self) -> None:
        d = decide(answers(praise=0.2, quality=0.4), SENTENCES, ["DockFlow"], T)
        self.assertEqual(d.status, "rejected")
        self.assertEqual(d.reasons, ["has_praise=0.20", "praise_quality=0.40"])

    def test_candidate_without_a_quotable_sentence_becomes_borderline(self) -> None:
        d = decide(answers(quotes=(0.1, 0.2, 0.3, 0.1, 0.1)), SENTENCES, ["DockFlow"], T)
        self.assertEqual(d.status, "borderline")
        self.assertIn("no_sentence_selected", d.reasons)
        self.assertEqual(d.quote, "")

    def test_missing_answers_reject_rather_than_crash(self) -> None:
        d = decide({}, SENTENCES, [], T)
        self.assertEqual((d.status, d.app), ("rejected", "unclear"))


class SelectQuoteTests(unittest.TestCase):
    def test_fallback_keeps_at_most_two_sentences_above_the_floor(self) -> None:
        quote, picked = select_quote(answers(quotes=(0.45, 0.5, 0.55, 0.41, 0.1)), SENTENCES, T)
        self.assertEqual([s["id"] for s in picked], ["S01", "S02"])
        self.assertEqual(quote, f"{SENTENCES[1]} {SENTENCES[2]}")

    def test_fallback_ignores_sentences_below_the_floor(self) -> None:
        quote, picked = select_quote(answers(quotes=(0.39, 0.3, 0.2, 0.1, 0.0)), SENTENCES, T)
        self.assertEqual((quote, picked), ("", []))

    def test_picked_sentences_carry_their_probabilities(self) -> None:
        _quote, picked = select_quote(answers(), SENTENCES, T)
        self.assertEqual(picked[0], {"id": "S01", "text": SENTENCES[1], "p": 0.9})


class ResolveAppTests(unittest.TestCase):
    def test_confident_choice_wins_over_names_in_text(self) -> None:
        self.assertEqual(resolve_app(answers(app="Shiori", app_conf=0.8), ["DockFlow"], T),
                         ("Shiori", 0.8))

    def test_unsure_model_falls_back_to_the_single_named_app(self) -> None:
        self.assertEqual(resolve_app(answers(app="ExtraBar", app_conf=0.3), ["Shiori"], T),
                         ("Shiori", 0.3))

    def test_unsure_model_with_several_named_apps_is_unclear(self) -> None:
        app, _conf = resolve_app(answers(app="unclear", app_conf=0.9), ["Shiori", "CoolDock"], T)
        self.assertEqual(app, "unclear")

    def test_confident_multiple_is_kept(self) -> None:
        self.assertEqual(resolve_app(answers(app="multiple", app_conf=0.7), ["Shiori"], T),
                         ("multiple", 0.7))

    def test_missing_answer_is_unclear(self) -> None:
        self.assertEqual(resolve_app({}, [], T), ("unclear", 0.0))


class RequestTests(unittest.TestCase):
    def test_one_quote_question_per_sentence_plus_the_fixed_questions(self) -> None:
        q = build_questions(SENTENCES, DEFAULT_APPS)
        fixed = {"kind", "app", "sender_is_user", "has_praise", "praise_quality",
                 "mentions_problem", "is_english"}
        self.assertEqual(set(q), fixed | {f"quote_{i}" for i in range(len(SENTENCES))})

    def test_state_numbers_sentences_and_keeps_only_what_the_model_needs(self) -> None:
        head = MessageHead(id="m1", account="eml", uid=None, thread_id=None, from_name="Jane",
                           from_email="jane@example.com", subject="Thanks",
                           date="2025-05-06T09:14:00+02:00")
        state = build_state(head, SENTENCES[:2], DEFAULT_APPS, ["DockFlow"])
        self.assertEqual(set(state), {"email", "our_apps", "apps_named_in_text"})
        self.assertEqual(state["email"]["sentences"], ["S00| Hi Asaf,", f"S01| {SENTENCES[1]}"])
        self.assertEqual(state["apps_named_in_text"], ["DockFlow"])
        self.assertNotIn("date", state["email"])
        self.assertEqual(state["our_apps"], list(DEFAULT_APPS))

    def test_app_descriptions_are_passed_when_present(self) -> None:
        apps = {"DockFlow": {"aliases": [], "description": "Dock manager for macOS"},
                "Shiori": {"aliases": [], "description": ""}}
        head = MessageHead(id="m1", account="eml", uid=None, thread_id=None, from_name="",
                           from_email="a@example.com", subject="", date="")
        state = build_state(head, ["Hello."], apps, [])
        self.assertEqual(state["our_apps"], [
            {"name": "DockFlow", "description": "Dock manager for macOS"},
            {"name": "Shiori", "description": "no description"},
        ])


if __name__ == "__main__":
    unittest.main()
