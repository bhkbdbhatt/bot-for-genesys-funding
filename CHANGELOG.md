# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Syndication targets:** Ghost (Admin API, HS256 JWT), Bluesky (AT Protocol
  `createRecord` with a link card) and Substack (manual hand-off). The platform
  registry now drives config validation, the CLI and the web UI, so adding a
  target is a spec plus a publisher module.
- **Discoverable prospect sources:** `discovery.py` takes a
  `ProspectSource` protocol. GitHub and GitLab ship as sources
  (`--source github|gitlab|all`, `discovery.sources`); all sources return
  GitHub-shaped records so filtering and ranking stay source-agnostic.
- **Webhook signing and idempotency.** When `WEBHOOK_SECRET` is set, every
  generic webhook POST carries `X-Timestamp`, `X-Signature:
  sha256=HMAC(timestamp + "." + body)` and an `X-Idempotency-Key` (the article
  fingerprint). `platforms.webhook.signing` overrides `auto` / `on` / `off`.
- **Bounded retries** for transient publish failures (429, 5xx and network
  errors) with exponential backoff, configurable via
  `publishing.retry.attempts` and `publishing.retry.backoff_seconds` and
  floored by `Retry-After`; every other 4xx is terminal.
- **Web UI roles and hardening:** `admin` / `operator` / `viewer` roles enforced
  server-side, per-profile `security.allowed_users`, optional HMAC-sealed
  persistent sessions and a `Secure` cookie flag, plus log redaction of
  token-like values.
- **Quality foundation:** `pyproject.toml`, a full pytest suite, and a GitHub
  Actions workflow running `ruff` and `pytest` on Python 3.10–3.12.
- **Source documents + API renderer:** `draft` (and the web wizard) can build an
  article from a local README / brief file (`--readme`, `--source`,
  `content.source_file`) via the new `sources.py`, merging missing plugin facts
  and passing the raw text to the renderer. `scheduler.llm_api` adds an HTTP
  chat-API renderer (`openai`, `anthropic`, `ollama`, and other OpenAI-compatible
  providers) alongside `scheduler.llm_command`, with the same fail-soft fallback
  to the deterministic composer.
- **Docs and hygiene:** rewritten `SECURITY.md`, this `CHANGELOG.md`,
  `CONTRIBUTING.md`, `AGENTS.md`, and `README.md` / `ARCHITECTURE.md` updates.

### Changed

- `discovery.py` was rewritten around pluggable sources; the GitHub path is
  unchanged for callers.
- `publishers/base.py::_request` now retries transient errors with bounded
  exponential backoff instead of a single retry.
- The legacy self-test harnesses (`_selftest.py`, `_spatest.py`, `_webtest.py`,
  `tests/driver.js`) run as subprocess guards inside the pytest suite, and the
  browser driver moved to `tests/driver.js`.
- `main.py` maps `SystemExit` codes faithfully instead of swallowing them.

### Removed

- The unused `SchedulerHalted` exception and the dead `scheduler.bootstrap()` /
  `promoter.bootstrap()` hooks.

## [1.0.0] - 2026-10-02

### Added

- Initial release: GitHub sponsor outreach (discover → track → generate → send)
  and content syndication (brief → draft → approve → publish → confirm) for
  DEV, Hashnode, Medium, WordPress, CoderLegion, DevDojo and a generic webhook,
  with an optional standard-library local web wizard.

[Unreleased]: https://github.com/bhkbdbhatt/bot-for-genesys-funding/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/bhkbdbhatt/bot-for-genesys-funding/releases/tag/v1.0.0
