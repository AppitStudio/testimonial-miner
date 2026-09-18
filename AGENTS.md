# AGENTS.md — testimonial miner

Context for any coding agent working in this repo. Read fully before changing anything.

## What this project is

A small Python CLI (`testimonial_miner`) that scans AppIt Studio's two business Gmail
mailboxes for emails since **2025-03-01**, classifies inbound user emails with
**TypeSafe's Jev model**, attributes each to one of our apps (**DockFlow, ExtraDock,
ExtraBar, CoolDock, Shiori**), and saves quotable praise / testimonials into
`data/testimonials.json` for the owner to review and later use on the apps' websites.

Owner: Asaf (AppIt Studio). Built 2026-09-18 with Claude Code. Status: **code complete;
verified against the live TypeSafe API and both live Gmail mailboxes; the first full historical
scan completed successfully; a private local dashboard is available for reviewing results;
published at <https://github.com/AppitStudio/testimonial-miner> under MIT and proposed for
the Community projects section of AppitStudio/awesome-jev**.

## Mandatory: use the TypeSafe skill

The owner asked that the TypeSafe agent skill be used for all work here.

- Claude Code: the plugin `typesafe@typesafe-ai` is installed (user scope); invoke
  `/typesafe:typesafe-ai` before designing or changing any model question.
- Other agents: `npx skills add typesafe-ai/skills --skill typesafe-ai`, or read
  <https://raw.githubusercontent.com/typesafe-ai/skills/main/skills/typesafe-ai/SKILL.md>.
- The skill's rule: the live docs are the source of truth. Index:
  <https://docs.typesafe.ai/llms.txt>. Append `.md` to any docs path for Markdown. Read the
  relevant primitive page (Choice / Score / Noul), the Python SDK page, and the closest
  cookbook before writing or editing questions. Do not invent API details from memory.

Key facts from the docs (as of 2026-09-18, verify before relying on them):

- Endpoint `POST https://api.typesafe.ai/v1/systemone`; SDK `typesafe-sdk` (installed 0.7.0),
  `TypeSafeClient(api_key=..., model=...)`, `client.system_one(state, questions)`.
- Model alias `jev-latest` → `jev-1.13.0`. Price ≈ $0.042 per million input tokens; output
  free. Context 64k tokens per request, 32k for state + longest question. Choice ≤ 255
  options, Score 2–10 levels.
- Jev is literal and does not generate text or do arithmetic. Put all logic in code; ask
  narrow atomic questions; ask everything in ONE request (speculative fan-out); keep
  irrelevant text out of `state`. See `/model-jaggedness/jev-1.13.md`.

## Stack and layout

- Python ≥ 3.12 (local: 3.14), managed with **uv** (`uv sync`, `uv run ...`). Deps:
  `typesafe-sdk`, `python-dotenv`. Everything else is stdlib (`imaplib`, `email`, `html.parser`).
- Public git repository: <https://github.com/AppitStudio/testimonial-miner> (MIT). `.gitignore`
  excludes `.env`, `.venv/`, `data/` and `data-*/`; never commit them.
- Tests: `uv run python -m unittest discover -s tests -v` (stdlib unittest, offline: fake IMAP
  source and a scripted model). `scan --eml-dir fixtures --data-dir data-test` is the live
  regression check. CI (`.github/workflows/tests.yml`) runs the offline suite on push and PR.

```
.env                 API_KEY (TypeSafe) and GMAIL_n_EMAIL / GMAIL_n_APP_PASSWORD pairs; local
                     only, git-ignored, never commit it
.env.example         template
apps.json            app catalogue: name → {aliases, description}. Descriptions are empty;
                     filling them improves app attribution.
README.md            user-facing docs: setup, commands, output format, tuning, caveats
fixtures/*.eml       11 synthetic emails covering praise, bug+praise, outreach, newsletter,
                     HTML with quoted reply, German, outbound, noreply, feature request
tests/               offline unittest suite: incremental cursors, policy and quote assembly,
                     scripted fixture scan (no network, no .env)
LICENSE              MIT
.github/workflows/   tests.yml runs the offline suite on push and pull request
testimonial_miner/
  config.py          env loading (loads PROJECT_ROOT/.env explicitly), Account, Thresholds,
                     Config, apps.json loading, app-name regexes
  sources.py         MessageHead; GmailImap (IMAP4_SSL imap.gmail.com, app password, finds the
                     \All folder, X-GM-RAW "after:YYYY/MM/DD" search, header fetch then body
                     fetch for survivors); iter_eml_dir for .eml files
  prefilter.py       header rules → skip reason (own address/domain, noreply-style local parts,
                     List-Unsubscribe/List-Id, Precedence bulk, Auto-Submitted, auto-reply subjects)
  cleaning.py        MIME → text (text/plain preferred, HTML→text drops blockquote/gmail_quote),
                     strip quoted replies (many languages), Outlook header blocks, signatures,
                     closings; split into sentences (ellipsis only splits before a capital)
  judge.py           build_state / build_questions / Judge (one request per email); question
                     wording and criteria live here
  policy.py          decide(): thresholds → candidate | borderline | rejected; app resolution;
                     quote assembly from per-sentence probabilities (pure, no model calls)
  store.py           data/classified.jsonl (append-only audit log, also dedupe index),
                     data/testimonials.json (the DB), data/cursors.json (IMAP cursors)
  cli.py             check | scan | list | show | stats | redecide | web
  web.py             dependency-free localhost server for the private review dashboard
  web_assets/        responsive HTML/CSS/JS dashboard; no external assets or data uploads
data/                git-ignored output (created on first run)
```

## How the pipeline works

1. **Fetch**: per account, `UID SEARCH X-GM-RAW "after:2025/03/01"` on All Mail (read-only),
   newest first. Incremental runs use `cursors.json` (last UID per account + UIDVALIDITY);
   `--full` ignores it. `--limit N` processes the newest N and does not advance the cursor.
   A cursor advances only after an error-free account run; incomplete fetches or model errors
   leave the prior cursor in place for retry. Cursor records also include audit-only UID/date
   bounds and counts for the last completed scan.
2. **Prefilter** on headers only (no body download): outbound from own addresses/domains,
   automated senders, bulk mail, auto replies. Each skip is logged with a reason.
3. **Clean** the body: sender-written text only, then sentences `S00..S39` (max 40,
   body capped at 8000 chars).
4. **Judge**: ONE TypeSafe request per email with state
   `{email:{from_name, from_email, subject, sentences:["S00| ...", ...]}, our_apps, apps_named_in_text}`
   and questions: `kind` (Choice), `app` (Choice incl. multiple/unclear), `sender_is_user`
   (Noul), `has_praise` (Noul), `praise_quality` (Score, 4 levels), `mentions_problem` (Noul),
   `is_english` (Noul), plus `quote_i` (Noul) per sentence with the sentence text in the
   question. Runs on a thread pool (`--workers`, default 4).
5. **Decide** (`policy.decide`, thresholds in `config.Thresholds`):
   candidate = sender_is_user ≥ 0.5 AND has_praise ≥ 0.6 AND praise_quality ≥ 1.5 (of 3);
   borderline = has_praise ≥ 0.35 AND praise_quality ≥ 1.0; kind ∈ {automated,
   business_outreach} → rejected regardless. Quote = sentences with quote prob ≥ 0.6, joined in
   order, contiguous runs separated by " […] "; fallback top-2 ≥ 0.4. App = model choice if
   confidence ≥ 0.5, else the single app named in text, else "unclear".
6. **Store**: every judged message → `classified.jsonl` (answers, sentences, body); candidates
   and borderline → `testimonials.json` items. `review` ("pending") and `notes` fields are
   the owner's and are preserved on re-runs and on `redecide`.

## Verified so far

- Live API: key in `.env` works; model `jev-1.13.0` answers.
- Fixtures (`uv run python -m testimonial_miner --data-dir data-test scan --eml-dir fixtures`
  with `OWN_EMAILS=appitstudio@gmail.com`): 5 candidates (one per app), 3 rejected (license
  question, SEO outreach that praised the app, "got it thanks"), 3 prefilter skips. ≈ 22k
  input tokens total.
- 8 real inbound emails (taken from the appitstudio@gmail.com mailbox via the Claude Gmail
  connector; kept only in a temporary scratchpad, NOT in the repo): genuine CoolDock praise →
  candidate with a clean quote; "Thanks for the great support and apps!" → borderline; refund
  requests, bug reports, "that worked!" → rejected. Cleaner handled Gmail, Apple Mail,
  Outlook and Spanish quote headers correctly.
- Both Gmail app passwords are configured. `check` logs into both live All Mail folders:
  `appitstudio@gmail.com` and `support@appitstudio.com`.
- Live IMAP UID delta search, header fetch, and body fetch are verified for both accounts.
- A live `scan --limit 100 --dry-run` followed by `scan --limit 100` completed: 74 messages
  were judged, producing 1 candidate, 8 borderline, and 65 rejected at about $0.011.
- The first full historical scan completed: 220 candidates and 370 borderline messages were
  retained from the run, at 19,254,294 input tokens (about $0.809). The current database may
  contain a few additional records from the earlier limited trial.
- Incremental-scan regression tests cover successful cursor advancement, error-withheld
  cursors, single IMAP entry, UTC date bounds, and owner-only cursor permissions.
- `list`, `show`, `stats`, `redecide`, `check`, and the local `web` dashboard all run.

## What is left to actually use it

1. **Review and curate**: run `uv run python -m testimonial_miner web`, inspect candidate and
   borderline results, and obtain customer consent before publishing private-email quotes.
2. **Tune on real data**: adjust `Thresholds` and, if needed, question wording in
   `judge.py`; re-apply thresholds with `redecide` (no API cost). Changing question wording
   requires `scan --rejudge`.
3. Optional: fill `apps.json` descriptions; add dashboard editing for `review` / `notes` or an
   export (e.g. Markdown/HTML per app) if the owner wants one. The dashboard is read-only.

## Conventions and gotchas

- Keep code in control; the model only answers narrow questions. Do not add prompts that
  ask Jev to write, summarize, or count.
- Ask new questions in the same single request (`build_questions`); never add a second
  round trip unless the second request's state depends on the first answer.
- `load_dotenv` is called with an explicit path in `config.py`; calling it without a path
  from `python -` / stdin crashes on this machine.
- IMAP mailbox names must be passed quoted (`'"[Gmail]/All Mail"'`); imaplib does not quote.
- Real customer emails are personal data: never commit them as fixtures; use the scratchpad.
- Quotes are for the owner's website; the tool does not handle consent. Do not add anything
  that publishes automatically.
- Run commands with `uv run python -m testimonial_miner ...` from the repo root.
