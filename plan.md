# plan.md — New Features & Quality Roadmap

Principal-architecture review outcome for `bot-for-genesys-funding`
(`GenesysPluginSponsorBot`). Approved scope: all six areas below, executed in
order, **no commits** (everything left staged/unstaged for maintainer review).

## Context

The project is a dependency-light Python fundraising / developer-relations
tool with two pipelines over one shared runtime:

1. **Outreach** — `discover → track → generate → send` (email / forum)
2. **Content syndication** — `brief → draft → approve → publish → confirm`
   (7 platforms via the `platforms.py` registry + `publishers/*`)

Plus a zero-dependency local web wizard (`webapp/`). Style invariants that
every phase below must preserve: atomic JSON writes, dry-run purity, fail-soft
per item, secrets as env-var names only, registry-driven platform extension,
deterministic composers with optional LLM fallback.

## Constraints

- No git commits. `git rm --cached` staging for untracked artifacts is allowed.
- Verify each phase: `python -m pytest -q` and `ruff check .` green before
  moving on; smoke-test `main.py platforms` / `main.py status`.
- Two runtime deps max (`pyyaml`, `requests`); new functionality must not add
  mandatory runtime dependencies (stdlib only: `hmac`, `hashlib`, `jwt`
  signing implemented manually for Ghost).
- Network paths tested with mocks only — no live API calls in tests.

---

## Phase 0 — Bug fixes & repo hygiene

- Fix exit-code bug in `main.py` (`main()`): `int(exc.code or EXIT_ERROR)`
  maps `SystemExit(0)` → 1. Preserve `0`; handle `None` / non-int codes.
- `.gitignore`: add `ui.log`, `*.log.*`; untrack `ui.log`
  (`git rm --cached ui.log`, staged not committed).
- Dead code audit: `SchedulerHalted` (`scheduler.py`), `scheduler.bootstrap()`,
  `promoter.bootstrap()`, `senders.preflight()` — check callers (incl.
  `webapp/`); wire `preflight` into the send path if it is a real safety win,
  otherwise delete and update ARCHITECTURE.md.
- Relocate/fix `driver.js` (committed Node harness with hard-coded absolute
  path) into `tests/`.

## Phase 1 — Quality foundation: packaging, pytest, Ruff, CI

- `pyproject.toml` (setuptools, flat layout): metadata, `pyyaml>=6.0`,
  `requests>=2.31`; optional `dev = [pytest, ruff]`; console script
  `genesys-sponsor-bot = main:main`.
- Ruff config per ARCHITECTURE.md guidance: `select = ["F", "E9"]`, ignore
  `UP`; `ruff check .` clean.
- `tests/` pytest suite porting the ad-hoc harnesses (temp-dir based, no
  network):
  - `tests/test_selftest.py` ← `_selftest.py` (markdown→HTML, gate, ledger,
    tracker)
  - `tests/test_web.py` ← `_webtest.py` (security boundary, auth, routes)
  - `tests/test_spa.py` ← `_spatest.py` (static wiring checks)
  - Regression tests for Phase 0 fixes and every feature in Phases 2–5
  - Keep `_*.py` harnesses working (documented as legacy in README).
- CI: `.github/workflows/ci.yml` — Python 3.10/3.11/3.12 matrix,
  `ruff check` + `pytest`.

## Phase 2 — New syndication platforms (registry pattern)

Each target touches: `models.PLATFORM_IDS`, `platforms.py` spec,
`publishers/<id>.py`, config validation, tests, docs.

1. **`ghost`** — `kind=api`, Ghost Admin API. JWT (HS256) signed at runtime
   from `GHOST_ADMIN_KEY` + `GHOST_ADMIN_URL` using stdlib `hmac`;
   `content_format=html`; canonical support.
2. **`bluesky`** — `kind=api`, AT Protocol
   (`com.atproto.server.createSession` + `app.bsky.feed.post`).
   `BLUESKY_HANDLE` / `BLUESKY_APP_PASSWORD`; link-card embed; short-body
   shaping; `supports_tags=False`.
3. **`substack`** — `kind=manual`, honest hand-off (no public API), following
   the CoderLegion/DevDojo pattern: outbox markdown + confirm-URL flow.

Update `config.example.yaml`, README platform table, ARCHITECTURE.md.

## Phase 3 — New discovery sources (pluggable source strategy)

- Refactor `discovery.py` around a `ProspectSource` protocol
  (`name`, `search(...)`, `profile(owner) -> identity + website/email`);
  existing GitHub logic becomes `GitHubSource` with unchanged behavior.
- Add `GitLabSource` (gitlab.com REST project search, owner lookup, public
  `website_url` → shared email-scrape path; optional `GITLAB_TOKEN`).
- Config `discovery.sources: [github, gitlab]` (default `[github]`,
  back-compatible); CLI `discover --source github|gitlab|all`; merged dedupe
  through tracker `seen`.
- Tests with mocked HTTP; README + config.example docs.

## Phase 4 — Web UI hardening

- **Roles**: `users.json` gains `role` (`admin` | `operator` | `viewer`);
  sessions carry role; write routes enforce; CLI `auth adduser --role`,
  `auth role`; bootstrap user stays admin. Per-profile grants: optional
  `profiles.<slug>.allowed` email list.
- **Persistent sessions**: optional HMAC-sealed session store file
  (TTL 8 h, atomic writes) under the profile dir; opt-in via config,
  default off (preserves current behavior).
- **Cookie flags**: add `Secure` under `security.secure_cookie:
  auto|on|off` (auto = TLS requests), keep `HttpOnly; SameSite=Strict`.
- **Log redaction**: scrub token-like values (Bearer tokens, `*_KEY` /
  `*_TOKEN` / passwords) in captured job logs.
- Tests: role gate 403s, session persistence round-trip, redaction.

## Phase 5 — Webhook signing & retries

- `publishers/webhook.py`: HMAC-SHA256 over `timestamp.body` with
  `WEBHOOK_SECRET` → `X-Signature: sha256=…`, `X-Timestamp`,
  `X-Idempotency-Key` (item fingerprint); signing on when secret present,
  config `platforms.webhook.signing` override.
- Retries: bounded exponential backoff for 429/5xx/network errors
  (`retry.attempts`, `retry.backoff_seconds`); 4xx → `failed` (fail-soft
  preserved); dry run unchanged.
- Tests: signature verifies with known secret, retry ladder, no retry on 400.

## Phase 6 — Docs & repo hygiene

- `SECURITY.md`: project-specific policy (supported versions, reporting
  contact placeholder, threat-model pointer to README §Security).
- `CHANGELOG.md` (Keep a Changelog); `CONTRIBUTING.md`; `AGENTS.md`
  (install / test / lint / run commands).
- README + ARCHITECTURE.md updates for all features above.

## Verification

After every phase:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe main.py platforms   # smoke
```

## Status

- [x] Plan approved
- [x] Phase 0
- [x] Phase 1
- [x] Phase 2
- [x] Phase 3
- [x] Phase 4
- [x] Phase 5
- [x] Phase 6
