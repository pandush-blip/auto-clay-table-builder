# Auto Clay Table Builder

Build a month of Clay audience tables for every client, from your phone, without spending a single credit. Built and used daily by [The Revenue Inbox](https://therevenueinbox.com).

- **No API credits.** Every build uses free moves only, and a tripwire reads your credit balance before and after every run and aborts the moment it moves. Verified: 0 credits on a full 50,000-row build with five added columns.
- **Works on the basic plan.** No paid tiers, no enrichment columns, no tricks.
- **Built to be driven by a bot.** The web UI is a client of plain HTTP endpoints. Connect Grok, Hermes, or any agent and build tables from Telegram while you walk. That is how we run it.
- **Millions of rows, zero babysitting.** Broken search queries get auto-repaired, transient failures retry from the checkpoint, and every build runs until the table is export-ready.
- **One CSV in, clean CSVs out.** Upload one row per audience. The queue builds them one at a time in priority order and drops cleaned CSVs per audience plus one merged file per client.

## Quickstart

```bash
git clone https://github.com/pandush-blip/auto-clay-table-builder && cd auto-clay-table-builder
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env      # fill in the keys (see below)
tri-clay doctor           # config, API key, credits, session, row cap
```

### CLI

```bash
tri-clay lint     audiences.csv   # syntax and parity rules, no API calls
tri-clay validate audiences.csv   # a real sample run per audience
tri-clay plan     audiences.csv   # size each audience, plan shards (48k margin under the 50k cap)
tri-clay build    audiences.csv   # full run: build, wait, export, clean
tri-clay resume   <run_dir>       # continue a stopped run from its checkpoint
tri-clay run-batch <folder>       # run every batch CSV in a folder
tri-clay cleanup  <run_dir>       # remove the tables this tool created (explicit confirm)
```

### The queue web app (Flask)

```bash
pip install -r webapp/requirements.txt
python -m webapp.worker              # the queue worker: one build at a time, retries, resume
flask --app webapp.app run --port 8600
```

## The API (drive it from your bot)

Every action the UI takes is a plain HTTP endpoint, so an agent can run the whole flow:

```text
POST /upload                    # multipart CSV of audiences
POST /batches/<id>/validate     # lint + official API sample + preview count
POST /audiences/<id>/fix        # AI repair for a broken query
POST /batches/<id>/queue        # queue the batch (priority order)
GET  /api/jobs                  # live queue status (JSON)
GET  /api/jobs/<id>             # one job: status, progress, outputs (JSON)
GET  /jobs/<id>/download        # finished exports
```

Session-based: grab the CSRF token from any page once and send it as `X-CSRF-Token`. That is the whole integration.

## How it works (30 seconds)

1. **Validate.** Lint, then a real sample run through the official API so build-time errors surface early. Broken queries go through an AI repair pass and re-validate.
2. **Plan.** Every audience gets previewed and counted; anything above 48,000 rows shards with margin under Clay's 50,000-row table cap.
3. **Queue.** Clean rows queue up. The worker claims jobs atomically, runs one at a time, in priority order, with live progress you can watch from a phone.
4. **Build.** Table from the search, free columns added (Domain, Company LinkedIn, Clay Company ID, Clay Profile ID, List Name), wait for the import, export, download.
5. **Clean.** First names cleaned, domains normalized, company names normalized, dedupe with `also_in_lists`, fixed CSV schema out (see `docs/output-schema.md`).

## What the tool enforces

- **Endpoint allowlist.** Anything outside the known route list raises before a request is sent.
- **Credit tripwire.** Balance compared before and after every run; any change aborts the run.
- **Checkpoints.** Retries and resumes reuse finished tables and idempotent column creation; nothing gets built twice.
- **Rate cap.** 30 tables per hour by default (`TRI_CLAY_MAX_TABLES_PER_HOUR`).
- **Honest states.** An expired session parks the job as `needs_session` with instructions instead of crashing the queue.

## Configuration

See `.env.example`. The short version:

| Key | What |
| --- | --- |
| `CLAY_API_KEY` | Public API key (check layer + credit tripwire) |
| `CLAY_SESSION_COOKIE` | Cookie from your own logged-in Clay session (build layer) |
| `CLAY_WORKSPACE_ID` | Your Clay workspace id |
| `CLAY_TARGET_FOLDER_ID` | Optional folder fence (blank = workspace root) |

## Tests

```bash
pytest
```

## Layout

```text
src/tri_clay/     the engine (lint, validate, plan, build, export, cleanup)
webapp/           the queue app (Flask): upload, validate, fix, queue, jobs, downloads
tests/            engine tests
webapp/tests/     app tests
docs/             output schema
```

---

Built by [The Revenue Inbox](https://therevenueinbox.com). Steal it, self-host it, change it.
