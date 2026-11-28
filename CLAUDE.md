# subtrack

A personal tool that finds my subscriptions in my Gmail, tracks when each one renews, and warns me before a charge happens, and especially before a charge fails.

Built for one user (me, in Lagos). My subscriptions are paid in a mix of USD and NGN, across naira cards, virtual dollar cards, App Store / Google Play and airtime. Payments that fail (FX limits, an unfunded virtual card, decline fees, cards terminated after too many declines) matter as much as payments I forget.

## Goals

- Find at least 90% of my real subscriptions from email alone, measured against a list I write by hand.
- For each subscription: merchant, price, currency, billing cycle, next charge date, which card pays, and status.
- Show what's due in the next N days and how much each card needs.
- Flag failed payments, ending trials and price increases.

## Non-goals (for now)

- Multiple users, a public launch, Google app verification or the CASA security assessment.
- Cancelling subscriptions on my behalf.
- Linking bank accounts (open banking).
- A mobile app.

## How it works

```
Gmail search -> prefilter -> LLM extraction -> events (SQLite) -> reconcile -> CLI output
```

1. **Search**: run a few narrow Gmail queries. Never iterate over the whole mailbox.
2. **Prefilter**: a cheap keyword and amount score drops promos and newsletters before any LLM call.
3. **Extract**: an LLM reads one email and returns structured billing *events*.
4. **Store**: save the events, never email bodies.
5. **Reconcile**: turn events into subscriptions (merge duplicates, infer the cycle, decide the status). Subscriptions are derived on every run and never stored, so improving this logic fixes old data too.

## Tech stack

- Python 3.11+, SQLite (stdlib `sqlite3`), pytest
- Gmail API: `google-api-python-client` and `google-auth-oauthlib`, scope `gmail.readonly` only
- Extraction: Gemini API (free tier) via the `google-genai` SDK. The model is set by env var `SUBTRACK_MODEL`; before choosing a default, check Google's current model list and free-tier rate limits and pick a cheap Flash or Flash-Lite model.
- Structured output: set `response_mime_type="application/json"` and pass a `response_schema`. Keep that schema simple (strings, numbers, enums, nullable fields) and separate from `BillingEvent`, since Gemini accepts only a subset of JSON Schema. Validate the parsed result into `BillingEvent` afterwards.
- `pydantic` to validate extraction output; `beautifulsoup4` for HTML-to-text
- CLI with `argparse`. No web UI until Phase 8.
- Ask before adding any dependency not on this list.

## Target layout

```
subtrack/
  config.py        # paths under ~/.subtrack, env vars
  gmail_client.py  # OAuth, search, fetch, MIME -> clean text
  prefilter.py     # keyword/amount scoring, with reasons
  extract.py       # prompt, tool schema, redaction, parsing
  models.py        # Email, BillingEvent, enums
  reconcile.py     # dedupe, cycle, status, upcoming, funding totals
  db.py            # schema and queries
  cli.py
tests/
  fixtures/        # synthetic emails only, never real ones
```

## Data model

**Tables**

- `emails`: message_id, sender, subject, received_at, prefilter score and reasons, status (`skipped | extracted | error`), extractor version, processed_at.
- `events`: one row per billing fact, linked to `emails`.
- `overrides`: merchant_key -> `ignore | confirm`.
- `cards` (Phase 6): last4 -> label.

**Event types**

`charge`, `renewal_notice`, `trial_started`, `trial_ending`, `payment_failed`, `cancelled`, `price_change` (amount = the new price), `refund`.

**Event fields**

- `merchant` (common service name)
- `event_type`
- `amount`, `currency` (ISO code, exactly as shown, never converted)
- `event_date`, `next_charge_date`
- `period` (weekly | monthly | quarterly | semiannual | yearly)
- `plan`
- `card_last4`
- `channel` (card | apple | google_play | paypal | carrier | unknown)
- `source` (merchant | app_store | bank_alert | card_alert | other)
- `confidence` (0–1)
- `evidence` (a short quote from the email)

**Subscription status**

| Status | Meaning |
|---|---|
| active | Has a charge, and the next charge is in the future |
| due | Next charge date has passed but is still within the grace period |
| lapsed | Past the grace period with no new charge |
| trial | A trial event came after the last charge |
| failing | A `payment_failed` came after the last charge |
| cancelled | A cancellation on or after the last charge |
| possible | Not enough evidence (e.g. one charge, no stated cycle). Hidden by default. |

## Domain rules

- **Money**: always `Decimal`, never float. Store amounts as text.
- **Duplicates**: charges from the same merchant within 3 days are one payment (e.g. a receipt plus a bank alert). Take the amount from the merchant receipt and the card's last 4 digits from whichever email has them. Rank sources: merchant > app_store > other > card_alert > bank_alert.
- **Billing cycle**: a cycle stated in the email wins. Otherwise infer it from gaps between charges:

  | Cycle | Gap (days) | Grace (days) |
  |---|---|---|
  | weekly | 5–9 | 3 |
  | monthly | 26–35 | 10 |
  | quarterly | 84–98 | 20 |
  | semiannual | 175–190 | 30 |
  | yearly | 350–380 | 45 |

  Ignore gaps that match no cycle. They usually mean a receipt is missing.
- **Next charge**: an explicit date from a recent email (renewal date, trial end) beats arithmetic. Add months with end-of-month clamping (Jan 31 -> Feb 28).
- **One-off purchases** are not subscriptions. A single charge with no stated cycle stays `possible`.
- **Bank and card alerts**: keep only those that name an online service (Netflix, Spotify, OpenAI, Apple, Google, AWS, DStv, Showmax...). Bank footers like "www.bank.com" or "internet banking" must not count as online.
- **Merchant names**: normalise so "Netflix, Inc." and "NETFLIX.COM" both become `netflix`, plus a small alias map (ChatGPT Plus -> openai). The App Store's merchant is the app, not Apple.

## Privacy and security (non-negotiable)

- Request only `gmail.readonly`.
- Never store email bodies. Store only sender, subject, date and extracted events.
- Before sending text to any API, mask every run of 8+ digits (card, account and phone numbers) down to its last 4.
- Send the LLM only emails that pass the prefilter, cut to about 6,000 characters.
- Gemini free tier: Google may use free-tier prompts and responses to improve its products, and human reviewers may read them. So before sending, also mask email addresses, my name and street addresses, not just long numbers. Never send OTPs or password-reset emails (the prefilter should drop them).
- Secrets and data live in `~/.subtrack/` (`credentials.json`, `token.json`, `subtrack.db`), never in the repo.
- `.gitignore` must cover those files plus `.env` and `truth.csv`.
- Tests use synthetic fixtures only. Never commit real email content.

## Gmail and OAuth notes

- One-time setup: create a Google Cloud project, enable the Gmail API, and set the OAuth consent screen to External / Testing with me as a test user. Then create a **Desktop app** OAuth client.
- In Testing mode, refresh tokens expire after 7 days. Catch `RefreshError` and re-run the browser sign-in. Alternatively, switch the publishing status to "In production" (unverified is fine, since I'm the only user).
- Search with Gmail query syntax and merge the results of these queries. Every query is limited to `newer_than:{SUBTRACK_LOOKBACK}` (default `18m`):
  - `category:purchases`
  - `subject:(receipt OR invoice OR "payment confirmation" OR "payment received")`
  - `subject:(subscription OR renewal OR renews OR "your plan" OR membership OR billing)`
  - `subject:(trial OR "trial ends")`
  - `subject:("payment failed" OR "payment declined" OR "update your payment" OR declined)`
  - `subject:("debit alert" OR "transaction alert" OR "card transaction" OR debited)`

## How to work on this project (for Claude)

- Work one phase at a time. At the start of a phase, propose the files, functions and tests, and wait for my OK before writing code.
- Keep each step small. Run `pytest` after every change.
- Write tests alongside the pure logic (parsing, prefilter, extraction parsing, reconcile).
- No network calls in tests. Mock the Gmail service and the LLM client.
- When a phase's "Done when" is met, tick its boxes in this file and stop for my review.
- If something in this plan looks wrong, say so rather than quietly working around it.
- Briefly explain non-obvious design choices when you make them.

## Commands

Keep this list updated as commands are added:

```
pytest
python -m subtrack auth
python -m subtrack sync [--dry-run] [--limit N] [--recheck-skipped]
python -m subtrack list [--all]
python -m subtrack upcoming [--days 14]
python -m subtrack events NAME
python -m subtrack ignore|confirm|reset NAME
python -m subtrack eval truth.csv
```

## Roadmap

### Phase 0: Setup
- [x] `pyproject.toml`, package skeleton, pytest running, `.gitignore`
- [x] Config: paths under `~/.subtrack`; env vars `GEMINI_API_KEY`, `SUBTRACK_MODEL`, `SUBTRACK_LOOKBACK` (default `18m`)
- [x] `auth` command: OAuth flow, save the token, print my Gmail address via `users.getProfile`

Done when: `python -m subtrack auth` prints my address.

### Phase 1: Find candidate emails
- [x] Run the search queries, merge and deduplicate the message IDs
- [x] Fetch and parse MIME:
  - walk the parts and decode base64url using the part's charset
  - use the HTML version when the plain-text part is under 200 characters
  - strip zero-width characters
- [x] `emails` table; skip IDs that were already processed
- [x] `sync --dry-run` prints sender and subject for each candidate

Done when: a dry run on my inbox lists my receipts.

### Phase 2: Prefilter
- [x] Score each email and record the reasons:
  - billing keywords
  - urgent keywords (failed, trial, cancelled)
  - an amount pattern ($, ₦, USD, NGN, N1,000)
  - a penalty for marketing words and for List-Unsubscribe
- [x] Bank and card alert rule (see Domain rules)
- [x] The dry run shows keep/skip with reasons; add `--recheck-skipped`

Done when: no receipt from my truth list is skipped, and few kept emails are obvious junk.

### Phase 3: Extraction
- [ ] `BillingEvent` model with lenient validators: a bad optional field becomes `None`; an invalid `event_type` drops that event
- [ ] Prompt and response schema (JSON mode). One email can produce several events or none.
- [ ] Treat an empty or unparseable response (e.g. output cut off at the token limit) as an `error`, not as "no events".
- [ ] Respect free-tier rate limits: throttle requests, back off on HTTP 429, and if the daily quota runs out, stop cleanly and leave the rest for the next sync.
- [ ] Redact before sending, then save the events. Failed calls get status `error` and are retried on the next sync. Record the extractor and prompt version.

Done when: events from the last 18 months are in the DB and spot checks look right.

### Phase 4: Reconcile and list
- [ ] Merchant normalisation and duplicate merging
- [ ] Billing cycle, next charge, status, price changes
- [ ] `list` with monthly totals per currency; `events NAME` shows the evidence

Done when: `list` matches what I believe I pay for.

### Phase 5: Measure
- [ ] `truth.csv` (merchant, amount, currency, cycle, card); `eval` prints recall, misses and extras
- [ ] `ignore` / `confirm` / `reset` overrides
- [ ] Iterate on queries, prefilter and prompt

Done when: recall is at least 90% on my list.

### Phase 6: Nigeria features
- [ ] Card labels (`4417` -> "Grey USD card")
- [ ] `card 4417`: everything billed to that card, for when a card is terminated or replaced
- [ ] `upcoming --days N`: items due, plus the funding each card needs per currency
- [ ] Naira equivalent using an FX rate, plus a funding/FX fee setting per card
- [ ] Failed payments, ending trials and price increases shown first

Done when: I've funded a card based on `upcoming` at least once.

### Phase 7: Automation
- [ ] Scheduled daily sync (cron or Windows Task Scheduler)
- [ ] Telegram bot alerts: funding needed within 3 days, failed payment, trial ending
- [ ] Incremental sync using Gmail `historyId`

Done when: alerts arrive without me running anything.

### Phase 8 (optional): Small web dashboard

## Open decisions

- FX rate source for the naira conversion.
- Where the daily job runs: my laptop or a small server.
- Which banks' and virtual-card providers' alert formats to support first (mine only, to start).
- The final project name.
