# Build Description — Inbound Carrier Sales Automation

HappyRobot Logistics FDE technical challenge: an inbound voice agent
("Alex") that qualifies an inbound carrier, verifies identity, matches
freight, negotiates rate within a hard ceiling, and books the load —
plus the operational surfaces required to run and evaluate it.

## 1. Architecture

```
Carrier (web call)
      |
      v
HappyRobot Workflow ("Alex" voice agent)
      |  tool calls over HTTPS (bearer-authed)
      v
FastAPI integration service (this repo)  ---->  FMCSA QCMobile API (MC lookup)
      |                                  ---->  Legacy TMS (line-oriented TCP, load search/book)
      v
SQLite ops mirror (data/call_log.db)  <----  read by /ops dashboard
      |
      v
HappyRobot Twin  <----  written directly by the workflow's own
                         "Extract Call Outcome" + "Log Call to Twin" nodes
```

The workflow talks to two independent things: this FastAPI service (for
carrier verification, OTP, load search/negotiation/booking — anything
needing the real FMCSA/TMS integrations or a security boundary around
`max_rate`/OTP codes) and HappyRobot Twin directly (for the system-of-record
call log, per the requirement that Twin be the data layer of record).

## 2. Why a custom FastAPI service sits between the agent and FMCSA/TMS

The agent's tools can't call FMCSA or the Legacy TMS directly and safely:

- **`max_rate` must never enter the LLM's context.** Negotiation logic
  (`negotiation.py`) runs server-side; the agent only ever sees
  `action`/`rate` for a counter or accept, never the ceiling. Doing this
  in the workflow itself would require putting the ceiling into a prompt
  or tool response, which is the exact thing the requirement forbids.
- **OTP codes must never be readable by the agent or its prompt.** The
  code is generated, hashed, and delivered out-of-band (`otp.py`); the
  API's own responses never include it, by construction, not by policy.
- **The Legacy TMS is a raw line-oriented TCP protocol** (pipe-delimited
  records, custom retry/fault semantics — see `tms_client.py`), not
  something a workflow HTTP tool node can speak.
- **FMCSA is flaky from this network** (see `fmcsa_client.py` — 403s from
  what looks like a WAF on the corporate egress range) and needs retry/
  backoff logic that belongs in a real client, not a workflow node.

## 3. Data layer: Twin vs. this service's SQLite mirror

Per the requirement, **Twin is the system of record.** The workflow's
"Extract Call Outcome" + "Log Call to Twin" nodes write every call's
structured outcome (MC number, FMCSA status, OTP result, load, rate,
outcome, booking ref) straight to a Twin table from inside the workflow —
no external database is used for that.

This service additionally keeps a **local SQLite mirror**
(`call_log.py`, gitignored `data/` dir) written incrementally by each API
call (verify-carrier, otp/verify, negotiate, book) as the call
progresses. This exists only because:

1. The **ops dashboard needs a store this backend can read directly** for
   real-time signals — Twin's data isn't queryable by this backend
   without a Twin REST integration, which wasn't available as an MCP
   tool in this environment.
2. It captures **per-step detail** (each negotiation round, OTP attempt
   count, a `flagged` action) that the single post-call Twin extraction
   summarizes away by design.

This is explicitly justified as a case where Twin can't support the
requirement (dashboard needs a queryable backing store this service
owns) — Twin remains authoritative for the call outcome itself; this
mirror is disposable operational telemetry.

## 4. Operational UI: custom dashboard vs. HappyRobot Apps

The requirement: *"A custom internal app (HappyRobot Apps) must surface
key operational signals and actions for the... operations manager...
External UIs may be used only where Apps cannot support the requirement
and must be justified."*

**Justification for using a custom dashboard (`static/ops.html`) instead
of HappyRobot Apps:** HappyRobot Apps was not buildable through the MCP
tooling available in this environment — there is no `manage_apps`-style
tool (or equivalent) exposed alongside the workflow/northstar/adversarial
tools that were available. Building it would have required manual
work in the platform UI outside the scope of what could be driven and
verified programmatically here.

The custom dashboard was built to satisfy the same requirement's actual
intent — signals *and* actions, without exposing raw platform logs:

- **Signals**: total calls, booked count/rate, negotiation-failed count,
  MC-active rate, average negotiation rounds, flagged-for-review count,
  plus a per-call table (MC, carrier, FMCSA status, OTP result, load,
  rates, outcome, booking ref) — all derived from structured fields,
  never raw transcripts or logs.
- **Actions**: the ops manager can flag/unflag a call for follow-up
  (`POST /ops/calls/{id}/flag`) directly from the table.
- Auth-gated (bearer token prompt, same credential as the API), served
  from this same FastAPI service at `/ops`.

If/when Apps access becomes available, this dashboard's two endpoints
(`/ops/calls`, `/ops/summary`, `/ops/calls/{id}/flag`) are the exact
surface an Apps-based UI would call instead — no other change needed.

## 5. Negotiation, OTP, and failure-path policy

- **Negotiation**: `negotiation.py` — up to 3 counter rounds, each
  stepping toward (never past) `max_rate`; accept immediately if the
  carrier's ask is already ≤ `max_rate`; reject after 3 rounds with no
  deal. Reject is logged as `outcome=negotiation_failed` and the agent's
  prompt explicitly closes the call professionally with **no transfer**.
- **OTP**: `otp.py` — 6-digit code, hashed (HMAC-SHA256) at rest, 5-minute
  TTL, 3 verify attempts, single-use (can't be replayed after a
  successful verify). No code path returns the plaintext code to any API
  response.
- **All failure paths** (MC inactive, OTP failed/locked out, no matching
  loads, negotiation rejected) end the call politely with no transfer
  offer. The **only** path that mentions a senior rep is a successful
  booking (framed as a documentation follow-up, not a live transfer,
  since transfer is mocked/unavailable for web calls anyway). This was a
  real bug caught and fixed during adversarial testing — see
  `QA_RESULTS.md` §3.

## 6. Evaluation

See `QA_RESULTS.md` for northstar KPIs, the scripted test suite
(33/33 passing), and the 5 adversarial test results — including the one
genuine prompt-policy bug found and fixed (improper transfer offers on
failure paths) and a documented limitation of the adversarial harness's
mocked tool responses.

## 7. Deployment

- `Dockerfile`: single-stage `python:3.12-slim`, installs
  `requirements.txt`, copies `src/` and `static/`, runs
  `uvicorn api:app` on port 8000.
- Config is entirely environment-variable driven (`TMS_HOST`,
  `TMS_PORT`, `TMS_TOKEN`, `FMCSA_WEB_KEY`, `API_AUTH_TOKEN` — see
  `.env.example`); no secrets are baked into the image.
- Deployed durably on Fly.io (see repo README/deployment notes for the
  live URL) — replacing the ephemeral `cloudflared` tunnel used during
  local development.
