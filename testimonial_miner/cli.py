"""Command line: scan mailboxes, list what was found, show one item, print stats."""

from __future__ import annotations

import argparse
import os
import sys
import textwrap
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .cleaning import message_to_text, split_sentences, strip_quoted_and_signature
from .config import PROJECT_ROOT, Config, load_config
from .judge import Judge, build_questions, build_state, detect_apps
from .policy import decide
from .prefilter import prefilter
from .sources import HEADER_BATCH, GmailImap, ImapError, MessageHead, iter_eml_dir
from .store import Store, now_iso

PRICE_PER_MTOK_USD = 0.042  # Jev 1.13 input price; output tokens are free


def _date_bounds(values: list[str]) -> tuple[str | None, str | None]:
    """UTC bounds for audit metadata; Gmail UIDs, not these dates, drive incrementality."""
    parsed: list[datetime] = []
    for value in values:
        if not value:
            continue
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        parsed.append(dt.astimezone(timezone.utc))
    if not parsed:
        return None, None
    return min(parsed).isoformat(), max(parsed).isoformat()


def _log_base(head: MessageHead) -> dict:
    return {"id": head.id, "account": head.account, "uid": head.uid, "thread_id": head.thread_id,
            "date": head.date, "from_name": head.from_name, "from_email": head.from_email,
            "subject": head.subject, "logged_at": now_iso()}


def _prepare(head: MessageHead, cfg: Config, patterns: dict) -> dict | None:
    """Deterministic part: decode, clean, split, detect app names, build the request."""
    if head.message is None:
        return None
    text = message_to_text(head.message)
    cleaned = strip_quoted_and_signature(text)
    sentences, truncated = split_sentences(cleaned, cfg.max_sentences, cfg.max_body_chars)
    if not sentences:
        return None
    apps_named = detect_apps(f"{head.subject}\n{cleaned}", patterns)
    return {
        "head": head, "cleaned": cleaned, "sentences": sentences, "truncated": truncated,
        "apps_named": apps_named,
        "state": build_state(head, sentences, cfg.apps, apps_named),
        "questions": build_questions(sentences, cfg.apps),
    }


def _item(job: dict, answers: dict, decision, model: str) -> dict:
    head = job["head"]
    a = answers
    return {
        "id": head.id, "account": head.account, "thread_id": head.thread_id, "date": head.date,
        "from_name": head.from_name, "from_email": head.from_email, "subject": head.subject,
        "status": decision.status, "review": "pending", "notes": "",
        "app": decision.app, "app_confidence": decision.app_confidence,
        "app_probabilities": a.get("app", {}).get("probabilities", {}),
        "apps_named_in_text": job["apps_named"],
        "kind": a.get("kind", {}).get("choice"), "kind_confidence": a.get("kind", {}).get("confidence"),
        "sender_is_user": a.get("sender_is_user", {}).get("noul"),
        "has_praise": a.get("has_praise", {}).get("noul"),
        "praise_quality": a.get("praise_quality", {}).get("score"),
        "praise_quality_confidence": a.get("praise_quality", {}).get("confidence"),
        "mentions_problem": a.get("mentions_problem", {}).get("noul"),
        "is_english": a.get("is_english", {}).get("noul"),
        "reasons": decision.reasons,
        "quote": decision.quote, "quote_sentences": decision.quote_sentences,
        "body": job["cleaned"], "body_truncated": job["truncated"],
        "model": model, "judged_at": now_iso(),
    }


def _run_jobs(jobs: list[dict], judge: Judge, cfg: Config, store: Store, totals: Counter,
              workers: int) -> None:
    def run(job):
        try:
            return judge.judge(job["state"], job["questions"])
        except Exception as e:  # noqa: BLE001 - keep scanning, log the failure
            return e

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for job, result in zip(jobs, ex.map(run, jobs)):
            head = job["head"]
            rec = _log_base(head)
            if isinstance(result, Exception):
                rec.update({"status": "error", "error": f"{type(result).__name__}: {result}"})
                store.append_log(rec)
                totals["error"] += 1
                print(f"  ERROR  {head.from_email}  {head.subject[:60]}  ({rec['error'][:80]})")
                continue
            answers, model, usage = result
            decision = decide(answers, job["sentences"], job["apps_named"], cfg.thresholds)
            totals["input_tokens"] += usage["input_tokens"]
            totals[f"status:{decision.status}"] += 1
            totals[f"kind:{answers.get('kind', {}).get('choice')}"] += 1
            rec.update({"status": decision.status, "app": decision.app, "reasons": decision.reasons,
                        "apps_named_in_text": job["apps_named"], "model": model, "usage": usage,
                        "answers": answers, "sentences": job["sentences"],
                        "body": job["cleaned"], "body_truncated": job["truncated"]})
            store.append_log(rec)
            if decision.status in ("candidate", "borderline"):
                store.upsert(_item(job, answers, decision, model))
            q = answers.get("praise_quality", {}).get("score", 0.0)
            print(f"  {decision.status:<10} {decision.app:<10} q={q:.1f}  "
                  f"{head.from_email[:28]:<28} {head.subject[:50]}")


def _process_heads(heads: list[MessageHead], cfg: Config, store: Store, judge: Judge | None,
                   patterns: dict, totals: Counter, workers: int, dry_run: bool) -> None:
    jobs = []
    for head in heads:
        job = _prepare(head, cfg, patterns)
        if job is None:
            rec = _log_base(head)
            rec["skip_reason"] = "empty_body"
            store.append_log(rec)
            totals["skip:empty_body"] += 1
            continue
        jobs.append(job)
    if dry_run:
        for job in jobs:
            h = job["head"]
            print(f"  would judge  {h.from_email[:28]:<28} {h.subject[:40]:<40} "
                  f"apps={job['apps_named']} sentences={len(job['sentences'])}")
            totals["would_judge"] += 1
        return
    if jobs:
        _run_jobs(jobs, judge, cfg, store, totals, workers)


def cmd_scan(args) -> int:
    cfg = load_config(args.data_dir, args.since)
    store = Store(cfg.data_dir)
    processed = set() if args.rejudge else store.processed_ids()
    judge = None if args.dry_run else Judge(cfg.api_key, cfg.model)
    patterns = cfg.app_patterns()
    totals: Counter = Counter()

    def triage(heads: list[MessageHead]) -> list[MessageHead]:
        survivors = []
        for h in heads:
            if h.id in processed:
                totals["already_processed"] += 1
                continue
            reason = prefilter(h.headers, h.from_email, cfg.own_emails, cfg.own_domains)
            if reason:
                rec = _log_base(h)
                rec["skip_reason"] = reason
                store.append_log(rec)
                totals[f"skip:{reason}"] += 1
                continue
            survivors.append(h)
        return survivors

    try:
        if args.eml_dir:
            heads = list(iter_eml_dir(Path(args.eml_dir)))
            if args.limit:
                heads = heads[:args.limit]
            print(f"{args.eml_dir}: {len(heads)} .eml files")
            _process_heads(triage(heads), cfg, store, judge, patterns, totals, args.workers,
                           args.dry_run)
        else:
            accounts = [a for a in cfg.accounts if not args.account or a.email == args.account.lower()]
            for email in cfg.pending_accounts:
                print(f"{email}: no app password in .env yet, skipping")
            if not accounts:
                raise SystemExit("No Gmail account is ready. Paste an app password into "
                                 "GMAIL_n_APP_PASSWORD in .env (see README), then run "
                                 "`check`.")
            for acct in accounts:
                account_errors_before = totals["error"]
                checked_dates: list[str] = []
                try:
                    with GmailImap(acct) as gm:
                        cursor = store.cursors().get(acct.email)
                        after_uid = None
                        if (cursor and not args.full and not args.rejudge
                                and cursor.get("uidvalidity") == gm.uidvalidity
                                and cursor.get("since") == cfg.since):
                            after_uid = cursor["last_uid"]
                        uids = gm.search(cfg.since, after_uid)
                        max_uid = max(uids) if uids else (after_uid or 0)
                        if args.limit:
                            uids = uids[:args.limit]
                        where = f"after UID {after_uid}" if after_uid else f"since {cfg.since}"
                        print(f"{acct.email} ({gm.folder}): {len(uids)} messages {where}")
                        for i in range(0, len(uids), HEADER_BATCH):
                            heads = gm.fetch_heads(uids[i:i + HEADER_BATCH])
                            checked_dates.extend(h.date for h in heads if h.date)
                            survivors = triage(heads)
                            gm.fetch_bodies(survivors)
                            _process_heads(survivors, cfg, store, judge, patterns, totals,
                                           args.workers, args.dry_run)
                            store.save_db()
                        if not args.limit and not args.dry_run and uids:
                            account_error_count = totals["error"] - account_errors_before
                            if account_error_count:
                                totals["cursor_withheld"] += 1
                                print(
                                    f"  WARNING {account_error_count} processing error(s); "
                                    f"cursor for {acct.email} was not advanced so they retry"
                                )
                            else:
                                oldest_date, newest_date = _date_bounds(checked_dates)
                                store.set_cursor(
                                    acct.email, gm.uidvalidity, max_uid, cfg.since,
                                    oldest_uid_checked=min(uids),
                                    oldest_message_date=oldest_date,
                                    newest_message_date=newest_date,
                                    messages_checked=len(uids),
                                )
                except ImapError as e:
                    print(f"ERROR {e}")
                    totals["account_errors"] += 1
                    continue
    finally:
        store.save_db()
        if judge:
            judge.close()

    print("\nSummary")
    for key in sorted(totals):
        if key != "input_tokens":
            print(f"  {key:<32} {totals[key]}")
    if totals["input_tokens"]:
        cost = totals["input_tokens"] / 1e6 * PRICE_PER_MTOK_USD
        print(f"  input tokens                     {totals['input_tokens']:,}  (~${cost:.3f})")
    print(f"\nDatabase: {store.db_path}")
    return 0


def cmd_check(args) -> int:
    """Verify the API key and every mailbox login without judging anything."""
    cfg = load_config(args.data_dir, args.since)
    ok = True
    print("TypeSafe API")
    try:
        j = Judge(cfg.api_key, cfg.model)
        names = [m.name for m in j.client.models.list().models]
        j.close()
        print(f"  ok   key accepted; model {cfg.model}; available: {', '.join(names)}")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"  FAIL {type(e).__name__}: {e}")

    print("Gmail mailboxes")
    for email in cfg.pending_accounts:
        ok = False
        print(f"  TODO {email}: GMAIL_n_APP_PASSWORD is empty in .env")
    for acct in cfg.accounts:
        try:
            with GmailImap(acct) as gm:
                uids = gm.search(cfg.since)
                print(f"  ok   {acct.email}: logged in; folder {gm.folder}; "
                      f"{len(uids):,} messages since {cfg.since}")
        except ImapError as e:
            ok = False
            print(f"  FAIL {e}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"  FAIL {acct.email}: {type(e).__name__}: {e}")
    if not cfg.accounts and not cfg.pending_accounts:
        ok = False
        print("  none configured: set GMAIL_1_EMAIL / GMAIL_1_APP_PASSWORD in .env")

    own = ", ".join(sorted(cfg.own_emails)) or "(none)"
    print(f"Own addresses skipped as outbound: {own}")
    if cfg.own_domains:
        print(f"Own domains skipped as outbound: {', '.join(sorted(cfg.own_domains))}")
    print(f"Apps: {', '.join(cfg.apps)}")
    print("\nReady to scan." if ok else "\nFix the items above, then run `check` again.")
    return 0 if ok else 1


def _matches(item: dict, args) -> bool:
    if args.app and item.get("app", "").lower() != args.app.lower():
        return False
    if args.status and item.get("status") != args.status:
        return False
    if args.review and item.get("review") != args.review:
        return False
    if args.min_quality is not None and (item.get("praise_quality") or 0) < args.min_quality:
        return False
    return True


def cmd_list(args) -> int:
    cfg = load_config(args.data_dir)
    items = [i for i in Store(cfg.data_dir).db()["items"] if _matches(i, args)]
    if not items:
        print("No matching testimonials yet.")
        return 0
    by_app: dict[str, list[dict]] = {}
    for it in items:
        by_app.setdefault(it.get("app", "unclear"), []).append(it)
    order = [*cfg.apps, "multiple", "unclear"]
    for app in sorted(by_app, key=lambda a: order.index(a) if a in order else 99):
        group = sorted(by_app[app], key=lambda x: (x["status"] != "candidate", -(x["praise_quality"] or 0)))
        n_c = sum(1 for g in group if g["status"] == "candidate")
        print(f"\n== {app}  ({n_c} candidates, {len(group) - n_c} borderline) ==")
        for it in group:
            flag = " ⚠ mentions a problem" if (it.get("mentions_problem") or 0) >= 0.5 else ""
            print(f"\n[{it['status']}/{it['review']}] {it['date'][:10]}  {it['from_name'] or '?'} "
                  f"<{it['from_email']}>  quality {it['praise_quality']:.1f}  "
                  f"praise {it['has_praise']:.2f}{flag}")
            print(f"  subject: {it['subject']}")
            quote = it["quote"] or "(no sentence selected; see body)"
            print(textwrap.indent(textwrap.fill(f"“{quote}”", 96), "  "))
            if args.full:
                print(textwrap.indent(it["body"], "  │ "))
            print(f"  id: {it['id']}  kind: {it['kind']}  account: {it['account']}")
    print(f"\n{len(items)} item(s)")
    return 0


def cmd_show(args) -> int:
    cfg = load_config(args.data_dir)
    for it in Store(cfg.data_dir).db()["items"]:
        if it["id"] == args.id:
            import json
            print(json.dumps(it, ensure_ascii=False, indent=2))
            return 0
    print(f"No item with id {args.id}", file=sys.stderr)
    return 1


def cmd_redecide(args) -> int:
    """Re-run the policy over logged answers (no model calls) and rebuild the database.

    Use after changing config.Thresholds: the raw judgments are unchanged, only the
    decisions are recomputed."""
    cfg = load_config(args.data_dir)
    store = Store(cfg.data_dir)
    db = store.db()
    old_items = {i["id"]: i for i in db["items"]}
    db["items"] = []
    latest: dict[str, dict] = {}
    for r in store.iter_log():
        if r.get("answers"):
            latest[r["id"]] = r          # last judgment of a message wins
    counts: Counter = Counter()
    for r in latest.values():
        head = MessageHead(id=r["id"], account=r["account"], uid=r.get("uid"),
                           thread_id=r.get("thread_id"), from_name=r.get("from_name", ""),
                           from_email=r.get("from_email", ""), subject=r.get("subject", ""),
                           date=r.get("date", ""))
        job = {"head": head, "cleaned": r.get("body", ""), "sentences": r["sentences"],
               "truncated": r.get("body_truncated", False),
               "apps_named": r.get("apps_named_in_text", [])}
        decision = decide(r["answers"], r["sentences"], job["apps_named"], cfg.thresholds)
        counts[decision.status] += 1
        if decision.status in ("candidate", "borderline"):
            item = _item(job, r["answers"], decision, r.get("model", ""))
            if r["id"] in old_items:   # keep hand-edited review/notes
                item["review"] = old_items[r["id"]].get("review", "pending")
                item["notes"] = old_items[r["id"]].get("notes", "")
            db["items"].append(item)
    store.save_db()
    for k, v in sorted(counts.items()):
        print(f"  {k:<12} {v}")
    print(f"Rebuilt {store.db_path} from {len(latest)} logged judgments")
    return 0


def cmd_stats(args) -> int:
    cfg = load_config(args.data_dir)
    store = Store(cfg.data_dir)
    seen, skips, statuses, kinds, tokens = 0, Counter(), Counter(), Counter(), 0
    for r in store.iter_log():
        seen += 1
        if r.get("skip_reason"):
            skips[r["skip_reason"]] += 1
        else:
            statuses[r.get("status", "?")] += 1
            kinds[r.get("answers", {}).get("kind", {}).get("choice", "?")] += 1
            tokens += r.get("usage", {}).get("input_tokens", 0)
    apps = Counter((i["app"], i["status"]) for i in store.db()["items"])
    print(f"messages seen: {seen}")
    print("skipped before the model:")
    for k, v in skips.most_common():
        print(f"  {k:<30} {v}")
    print("judged by the model:")
    for k, v in statuses.most_common():
        print(f"  {k:<30} {v}")
    print("message kinds:")
    for k, v in kinds.most_common():
        print(f"  {k:<30} {v}")
    print("testimonials by app:")
    for (app, status), v in sorted(apps.items()):
        print(f"  {app:<12} {status:<10} {v}")
    print(f"input tokens: {tokens:,}  (~${tokens / 1e6 * PRICE_PER_MTOK_USD:.3f})")
    return 0


def cmd_web(args) -> int:
    """Serve the private local review dashboard."""
    from .web import serve_dashboard
    data_dir = Path(args.data_dir or os.environ.get("DATA_DIR") or PROJECT_ROOT / "data")
    serve_dashboard(data_dir, host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="testimonial_miner",
                                description="Mine quotable user praise from Gmail with TypeSafe.")
    p.add_argument("--data-dir", help="where to keep the log and database (default: ./data)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="fetch, classify, and store new messages")
    s.add_argument("--since", help="only messages after this date, YYYY-MM-DD (default 2025-03-01)")
    s.add_argument("--account", help="scan only this Gmail address")
    s.add_argument("--limit", type=int, help="stop after N messages (newest first); good for a trial")
    s.add_argument("--eml-dir", help="read .eml files from this directory instead of Gmail")
    s.add_argument("--dry-run", action="store_true", help="fetch and clean but do not call the model")
    s.add_argument("--rejudge", action="store_true", help="re-run the model on already processed messages")
    s.add_argument("--full", action="store_true", help="ignore the saved IMAP cursor and rescan since --since")
    s.add_argument("--workers", type=int, default=4, help="parallel model requests (default 4)")
    s.set_defaults(func=cmd_scan)

    ck = sub.add_parser("check", help="verify the API key and each mailbox login; no judging")
    ck.add_argument("--since", help="date used for the message count, YYYY-MM-DD")
    ck.set_defaults(func=cmd_check)

    ls = sub.add_parser("list", help="print stored testimonials grouped by app")
    ls.add_argument("--app")
    ls.add_argument("--status", choices=["candidate", "borderline"])
    ls.add_argument("--review", help="filter by your review field, e.g. pending, approved, rejected")
    ls.add_argument("--min-quality", type=float)
    ls.add_argument("--full", action="store_true", help="also print the cleaned email body")
    ls.set_defaults(func=cmd_list)

    sh = sub.add_parser("show", help="print one stored item as JSON")
    sh.add_argument("id")
    sh.set_defaults(func=cmd_show)

    rd = sub.add_parser("redecide", help="re-apply thresholds to logged answers without calling the model")
    rd.set_defaults(func=cmd_redecide)

    st = sub.add_parser("stats", help="counts of what was skipped, judged, and kept")
    st.set_defaults(func=cmd_stats)

    web = sub.add_parser("web", help="open the local testimonial review dashboard")
    web.add_argument("--host", default="127.0.0.1",
                     help="interface to bind (default: 127.0.0.1, local machine only)")
    web.add_argument("--port", type=int, default=8765,
                     help="port for the dashboard (default: 8765)")
    web.set_defaults(func=cmd_web)

    args = p.parse_args(argv)
    sys.exit(args.func(args))
