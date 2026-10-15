# AGENTS.md

Guidance for AI agents (and humans driving them) working in this repository.

## What this is

A dependency-light Python tool that promotes one open-source project two ways:
sponsor **outreach** (`discover → track → generate → send`) and **content
syndication** (`brief → draft → approve → publish → confirm`), plus an optional
standard-library local web wizard. Read `ARCHITECTURE.md` before changing code;
its *Cross-cutting invariants* section is binding.

## Environment

Python 3.10+. There is a virtualenv at `.venv`; use it directly. Runtime deps are
only `pyyaml` and `requests`. Node is needed only for `tests/driver.js`.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## Commands

Always run these on the `.venv` interpreter and from the repo root.

| Task | Command |
| ---- | ------- |
| Test | `.\.venv\Scripts\python.exe -m pytest -q` |
| Lint | `.\.venv\Scripts\python.exe -m ruff check .` |
| Run (smoke) | `.\.venv\Scripts\python.exe main.py platforms` |
| Run (dry, safe) | `.\.venv\Scripts\python.exe main.py run --once --dry-run` |
| Self-tests | `.\.venv\Scripts\python.exe _selftest.py` and `_spatest.py` and `_webtest.py` |
| JS probe | `node tests\driver.js` |

`pytest` runs the harnesses above as subprocess guards, so the single `pytest`
command is the source of truth. Ruff is configured for `F` + `E9` with
line-length 110; `UP` is intentionally off.

Before finishing any change, run **both** `pytest` and `ruff check`, and keep
them green. Add or update tests for behaviour you touch.

## Conventions (do not violate)

- No new mandatory runtime dependencies. Prefer the standard library (an LLM is
  optional and must never be required).
- Secrets only from environment variables; config stores env var *names*. Never
  log or persist a secret. Use `config_loader.resolve_secret()`.
- JSON writes go through `state.write_json_atomic` / `AtomicJsonStore`, never in
  place.
- A dry run has **no** side effects: no quota, attempts or cooldowns.
- Fail soft per item: record an error and continue; one bad item must not abort a
  batch.
- Do **not** add comments unless asked; use clear names. Existing
  `typing.Dict` / `typing.List` style is deliberate.
- The web UI and CLI call the same modules; never duplicate pipeline logic.
- Authorisation is decided server-side (`webapp/server.py::required_role`); the
  SPA only hides UI.

## Extension points

- **New syndication target:** add the id to `models.PLATFORM_IDS`, a
  `PlatformSpec` to `platforms.py`, and `publishers/<id>.py` with
  `build(config, *, dry_run)`. Config validation, CLI and UI pick it up
  automatically.
- **New prospect source:** implement the `ProspectSource` protocol in
  `discovery.py`, returning GitHub-shaped repo dicts, and register the id in
  `models.PROSPECT_SOURCES` + `discovery.sources_from_config`.

## Git

Do not commit, amend, push or open PRs unless explicitly asked. When asked,
inspect `git status` and `git diff` first and stage only intended files.
