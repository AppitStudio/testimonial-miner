"""The one TypeSafe request made per email.

All judgments the pipeline might need are asked together (speculative fan-out): the
message kind, the app, whether the sender is a user, whether there is praise and how
quotable it is, plus one yes/no question per sentence so code can assemble a verbatim
quote without the model generating text. Code decides afterwards which answers matter.
"""

from __future__ import annotations

from typing import Any

from typesafe_sdk import Choice, Noul, RetryPolicy, Score, TypeSafeClient

from .cleaning import sentence_id
from .sources import MessageHead

KINDS = ("user_feedback", "support_request", "feature_request", "purchase_or_license",
         "business_outreach", "automated", "other")
NON_USER_KINDS = {"business_outreach", "automated"}

PRAISE_LEVELS: list[dict[str, Any]] = [
    {"what": "No positive statement about the app or the support; at most courtesy words",
     "examples": ["Thanks in advance", "Please fix this", "Hope you are well"]},
    {"what": "Brief or generic praise with no detail about what is good",
     "examples": ["Great app, thanks", "Nice work", "Love it"]},
    {"what": "Specific praise that names what the sender likes or how the app helps them",
     "examples": ["The multi-monitor support finally fixed my setup",
                  "Support replied within an hour and solved my problem"]},
    {"what": "Enthusiastic, specific, well-phrased praise that could be published as is",
     "examples": ["This app completely changed how I work on my Mac. I can't imagine going back."]},
]

KIND_CRITERIA: dict[str, Any] = {
    "user_feedback": {
        "what": "A person who uses one of `our_apps` shares their opinion, experience, praise, "
                "thanks, or general feedback, without asking for help",
        "not_for": "Messages that mainly ask for help or report a problem",
        "examples": ["Just wanted to say I love the app", "Been using it for a year, works great"],
    },
    "support_request": {
        "what": "A person asks for help, reports a bug, crash, or problem, or asks how to do "
                "something in one of `our_apps`",
        "not_for": "Pure praise with no problem or question",
        "examples": ["The app crashes when I open settings", "How do I move it to another screen?"],
    },
    "feature_request": {
        "what": "A person asks for a new feature, option, or change in one of `our_apps`",
        "not_for": "Reports that an existing feature is broken",
        "examples": ["Could you add a dark mode?", "It would be great if it supported two monitors"],
    },
    "purchase_or_license": {
        "what": "About buying, paying, license keys, activation, refunds, receipts, discounts, "
                "or upgrades for one of `our_apps`",
        "examples": ["I lost my license key", "Can I get a refund?", "Is there a student discount?"],
    },
    "business_outreach": {
        "what": "A company or professional offers services, partnership, sponsorship, advertising, "
                "press coverage, recruitment, or a sales pitch",
        "examples": ["We can boost your app's SEO", "Would you like to be featured on our site?"],
    },
    "automated": {
        "what": "A system-generated message: notification, receipt, alert, verification code, "
                "newsletter, mailing list, or auto-reply",
        "examples": ["Your payout has been sent", "Your verification code is 123456"],
    },
    "other": {
        "what": "Anything that fits none of the other options, including messages unrelated "
                "to `our_apps`",
    },
}


def detect_apps(text: str, patterns: dict) -> list[str]:
    """Code-side detection of app names (and aliases) mentioned in the text."""
    return [name for name, rx in patterns.items() if rx.search(text)]


def build_state(head: MessageHead, sentences: list[str], apps: dict[str, dict],
                apps_named: list[str]) -> dict[str, Any]:
    described = [{"name": n, "description": m["description"]}
                 for n, m in apps.items() if m.get("description")]
    our_apps: Any = described if len(described) == len(apps) else list(apps)
    if described and len(described) != len(apps):
        our_apps = [{"name": n, "description": m.get("description") or "no description"}
                    for n, m in apps.items()]
    return {
        "email": {
            "from_name": head.from_name,
            "from_email": head.from_email,
            "subject": head.subject,
            "sentences": [f"{sentence_id(i)}| {s}" for i, s in enumerate(sentences)],
        },
        "our_apps": our_apps,
        "apps_named_in_text": apps_named,
    }


def build_questions(sentences: list[str], apps: dict[str, dict]) -> dict[str, Any]:
    app_criteria: dict[str, Any] = {
        name: (meta.get("description") or None) for name, meta in apps.items()
    }
    app_criteria["multiple"] = "Two or more of `our_apps` are discussed substantially"
    app_criteria["unclear"] = "None of `our_apps` is named or clearly described"

    questions: dict[str, Any] = {
        "kind": Choice(
            instructions={
                "question": "What kind of message is `email`, judged by why the sender wrote it?",
                "inspect": ["`email.subject`", "`email.sentences`", "`email.from_email`"],
            },
            criteria=KIND_CRITERIA,
        ),
        "app": Choice(
            instructions={
                "question": "Which app in `our_apps` is `email` mainly about?",
                "inspect": ["`email.subject`", "`email.sentences`", "`apps_named_in_text`"],
                "focus": "Prefer an app that is named. If no app is named but the text clearly "
                         "describes one of `our_apps`, choose that app.",
            },
            criteria=app_criteria,
        ),
        "sender_is_user": Noul(
            instructions={
                "question": "Is the sender of `email` an individual person writing about their "
                            "own use of one of `our_apps`?",
                "inspect": ["`email.from_name`", "`email.from_email`", "`email.sentences`"],
            },
            criteria={
                "true": "A person who has bought, used, tried, or is evaluating the app and "
                        "writes in the first person about it",
                "false": "A company, agency, vendor, recruiter, journalist, automated system, "
                         "or a person who does not describe using the app",
            },
        ),
        "has_praise": Noul(
            instructions={
                "question": "Does `email` contain a positive statement, in the sender's own words, "
                            "about one of `our_apps` (its features, quality, design, reliability, "
                            "or value) or about the support the sender received?",
                "inspect": "`email.sentences`",
                "focus": "Require an opinion or experience, not politeness.",
            },
            criteria={
                "true": {"what": "At least one sentence praises the app or the support",
                         "examples": ["This app has become essential to my workflow",
                                      "Thank you for the fast fix, amazing support"]},
                "false": {"what": "No praise of the app or support; courtesy phrases alone do not count",
                          "examples": ["Thanks in advance", "Hope you are well"]},
            },
        ),
        "praise_quality": Score(
            instructions={
                "question": "How usable is the positive content of `email` as a testimonial quote "
                            "on the app's website?",
                "inspect": "`email.sentences`",
                "focus": "Judge only the praise, not any problems the sender reports.",
            },
            criteria=PRAISE_LEVELS,
        ),
        "mentions_problem": Noul(
            instructions={
                "question": "Does `email` report a bug, crash, problem, complaint, or missing "
                            "feature in one of `our_apps`?",
                "inspect": "`email.sentences`",
            },
            criteria={
                "true": "Describes something broken, missing, confusing, or disappointing in the app",
                "false": "No problem, complaint, or missing feature is described",
            },
        ),
        "is_english": Noul(
            instructions="Is `email` written mainly in English?",
        ),
    }
    for i, text in enumerate(sentences):
        questions[f"quote_{i}"] = Noul(
            instructions={
                "question": f"Is `sentence` (id {sentence_id(i)} in `email.sentences`) a positive "
                            "statement about one of `our_apps` or about the developer's support, "
                            "in the sender's own words, that could be quoted on the app's website?",
                "sentence": text,
                "focus": "Judge only this sentence, using `email` for context.",
            },
            criteria={
                "true": "The sentence expresses the sender's positive opinion of, or good "
                        "experience with, the app or the support",
                "false": "The sentence is a greeting, a question, a problem report, technical "
                         "details such as versions or device names, an offer to send logs or "
                         "information, a request, a courtesy phrase, a sign-off, a signature "
                         "line, or about something other than our apps or the support",
            },
        )
    return questions


def answers_to_dict(response) -> dict[str, dict[str, Any]]:
    """Plain JSON-serializable copy of every answer, for the log and the policy."""
    out: dict[str, dict[str, Any]] = {}
    for qid, a in response.answers.items():
        if a.type == "noul":
            out[qid] = {"type": "noul", "noul": round(a.noul, 4)}
        elif a.type == "choice":
            out[qid] = {"type": "choice", "choice": a.choice, "confidence": round(a.confidence, 4),
                        "probabilities": {k: round(v, 4) for k, v in a.probabilities.items()}}
        elif a.type == "score":
            out[qid] = {"type": "score", "score": round(a.score, 4), "confidence": round(a.confidence, 4),
                        "probabilities": {str(k): round(v, 4) for k, v in a.probabilities.items()}}
    return out


class Judge:
    def __init__(self, api_key: str, model: str):
        self.model = model
        self.client = TypeSafeClient(
            api_key=api_key, model=model, timeout=90.0,
            retry=RetryPolicy(max_retries=5, backoff_initial=1.0, backoff_max=15.0, timeout=90.0),
        )

    def judge(self, state: dict, questions: dict) -> tuple[dict[str, dict], str, dict[str, int]]:
        response = self.client.system_one(state, questions)
        usage = {"input_tokens": response.usage.input_tokens,
                 "output_tokens": response.usage.output_tokens}
        return answers_to_dict(response), response.model, usage

    def close(self) -> None:
        self.client.close()
