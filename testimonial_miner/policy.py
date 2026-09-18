"""Turn raw answers into a decision. All thresholds live in config.Thresholds.

Nothing here calls the model: changing a threshold and re-running `decide` over the
logged answers gives new decisions without new inference.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .cleaning import sentence_id
from .config import Thresholds
from .judge import NON_USER_KINDS


@dataclass
class Decision:
    status: str                 # candidate | borderline | rejected
    reasons: list[str]
    app: str
    app_confidence: float
    quote: str
    quote_sentences: list[dict] = field(default_factory=list)


def _contiguous_groups(indices: list[int]) -> list[list[int]]:
    groups: list[list[int]] = []
    for i in sorted(indices):
        if groups and i == groups[-1][-1] + 1:
            groups[-1].append(i)
        else:
            groups.append([i])
    return groups


def select_quote(answers: dict, sentences: list[str], t: Thresholds) -> tuple[str, list[dict]]:
    probs = [(i, answers.get(f"quote_{i}", {}).get("noul", 0.0)) for i in range(len(sentences))]
    chosen = [i for i, p in probs if p >= t.quote_sentence_min]
    if not chosen:
        best = sorted(probs, key=lambda x: x[1], reverse=True)[:2]
        chosen = [i for i, p in best if p >= t.quote_fallback_min]
    quote = " […] ".join(" ".join(sentences[i] for i in g) for g in _contiguous_groups(chosen))
    picked = [{"id": sentence_id(i), "text": sentences[i], "p": p} for i, p in probs if i in chosen]
    return quote, picked


def resolve_app(answers: dict, apps_named: list[str], t: Thresholds) -> tuple[str, float]:
    a = answers.get("app", {})
    choice, conf = a.get("choice", "unclear"), float(a.get("confidence", 0.0))
    if choice not in ("unclear", "multiple") and conf >= t.app_confidence_min:
        return choice, conf
    if choice == "multiple" and conf >= t.app_confidence_min:
        return "multiple", conf
    if len(apps_named) == 1:            # the model was unsure but exactly one app is named
        return apps_named[0], conf
    return "unclear", conf


def decide(answers: dict, sentences: list[str], apps_named: list[str], t: Thresholds) -> Decision:
    kind = answers.get("kind", {}).get("choice", "other")
    sender = float(answers.get("sender_is_user", {}).get("noul", 0.0))
    praise = float(answers.get("has_praise", {}).get("noul", 0.0))
    quality = float(answers.get("praise_quality", {}).get("score", 0.0))
    reasons: list[str] = []

    app, app_conf = resolve_app(answers, apps_named, t)

    if kind in NON_USER_KINDS:
        reasons.append(f"kind={kind}")
        return Decision("rejected", reasons, app, app_conf, "", [])

    strong = (sender >= t.sender_is_user_min and praise >= t.has_praise_min
              and quality >= t.praise_quality_min)
    weak = (praise >= t.borderline_has_praise_min and quality >= t.borderline_praise_quality_min)

    if strong:
        status = "candidate"
    elif weak:
        status = "borderline"
        if sender < t.sender_is_user_min:
            reasons.append(f"sender_is_user={sender:.2f}")
        if praise < t.has_praise_min:
            reasons.append(f"has_praise={praise:.2f}")
        if quality < t.praise_quality_min:
            reasons.append(f"praise_quality={quality:.2f}")
    else:
        reasons.append(f"has_praise={praise:.2f}")
        reasons.append(f"praise_quality={quality:.2f}")
        return Decision("rejected", reasons, app, app_conf, "", [])

    quote, picked = select_quote(answers, sentences, t)
    if not picked:
        reasons.append("no_sentence_selected")
        if status == "candidate":
            status = "borderline"
    return Decision(status, reasons, app, app_conf, quote, picked)
