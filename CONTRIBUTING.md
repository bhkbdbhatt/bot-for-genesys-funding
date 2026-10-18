# Contributing

Thanks for taking a look. This project is a small, dependency-light Python tool
that runs against real accounts, so the bar for a change is: **it must be safe to
run unattended and obvious to the next reader.**

## Getting set up

Python 3.10+ is required. Runtime dependencies are only `pyyaml` and `requests`.

```powershell
git clone https://github.com/bhkbdbhatt/bot-for-genesys-funding.git
cd bot-for-genesys-funding
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

`.[dev]` adds `pytest` and `ruff`. Node is only needed for `tests/driver.js`, and
it is optional.

## Before you open a pull request

Run the same checks CI runs:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe main.py platforms   # smoke test, read-only
```

All three must be green. If you touched a publisher or the platform registry,
also run `_spatest.py`; if you touched the web UI, also run `_webtest.py` and
`node tests/driver.js`.

## Guidelines

- **No new mandatory dependencies.** The two runtime deps are a feature. Reach
  for the standard library first; if you truly need a package, open an issue
  before writing code.
- **Never write secrets to disk.** Config stores env var *names*. New
  credentials go through `resolve_secret()` and the `*_env` convention.
- **Atomic writes only.** Route every JSON write through `state.write_json_atomic`
  / `AtomicJsonStore`; never write in place.
- **Fail soft per item.** One bad address or platform must not stop a batch —
  record the error and continue.
- **A dry run has no side effects.** Never touch quota, attempts or cooldowns when
  `dry_run` is set.
- **No comments unless they earn their place.** Prefer clear names over narration;
  reserve comments for *why*.
- **Match the existing style.** `typing.Dict` / `typing.List` are used
  deliberately; ruff is configured for `F` + `E9` only (`UP` is off).

## Adding a syndication target

Platforms are registry-driven, so it is three edits, no core changes:

1. Add the id to `models.PLATFORM_IDS`.
2. Add a `PlatformSpec` to `platforms.py` (`kind`, `content_format`, `token_env`,
   URLs, limits).
3. Add `publishers/<id>.py` exposing `build(config, *, dry_run)`.

Config validation, the CLI (`main.py platforms`) and the web UI pick the target
up automatically. Add a test under `tests/` covering the payload shape and the
`check_ready()` gate.

## Adding a prospect source

Implement the `ProspectSource` protocol in `discovery.py` (`name`,
`search_repositories`, `get_owner`, `find_public_email`) returning GitHub-shaped
repository dictionaries, then register the id in `models.PROSPECT_SOURCES` and
`discovery.sources_from_config`. Filters under `github.filters` apply to every
source, so keep the record shape stable.

## Commit and PR style

- One logical change per pull request; keep the diff reviewable.
- Write commit subjects in the imperative mood ("Add Bluesky publisher", not
  "Added").
- Describe *why* in the PR body, and note anything a reviewer should verify
  manually (there are no live-network tests by design).
- Update `CHANGELOG.md` under `[Unreleased]` for user-visible changes, and update
  the docs the change affects (`README.md`, `ARCHITECTURE.md`).
- Update `SECURITY.md` if the change affects the threat model.

## Reporting bugs and requesting features

Open a GitHub issue with the version, what you expected, what happened, and the
smallest reproduction. For anything that looks security-sensitive, follow
[SECURITY.md](SECURITY.md) instead of opening a public issue.

## Releasing to PyPI

Publishing is automated. `.github/workflows/publish.yml` runs on a published
GitHub Release: it re-runs `ruff` and `pytest`, builds the sdist and wheel,
checks them with `twine check --strict`, verifies the release tag matches
`project.version` in `pyproject.toml`, and uploads with **PyPI trusted
publishing** - no API tokens are stored in GitHub.

One-time maintainer setup: on pypi.org, open *Account settings → Publishing*
and add a *pending publisher* for project `genesys-plugin-sponsor-bot`, owner
`bhkbdbhatt`, repository `bot-for-genesys-funding`, workflow `publish.yml`,
environment `pypi` (create the `pypi` environment in the repository settings).

Per release:

1. Move the `[Unreleased]` notes in `CHANGELOG.md` under a new version
   heading with the date.
2. Bump `version` in `pyproject.toml` - it is the single source of truth and
   the workflow rejects a tag that disagrees with it.
3. Run the checks: `pytest -q`, `ruff check .`, `main.py platforms`.
4. Optionally rehearse locally:
   `python -m build && twine check --strict dist/*`, or upload to TestPyPI
   first with `twine upload --repository testpypi dist/*` (API token from
   test.pypi.org) and `pip install --index-url https://test.pypi.org/simple/
   genesys-plugin-sponsor-bot`.
5. Commit, tag `vX.Y.Z`, and create a GitHub Release for that tag - the
   workflow publishes it.

PyPI rejects re-uploading an existing version, so a broken release needs a
new patch version, never a re-run of the same tag. `workflow_dispatch` on the
publish workflow can re-run the build for an unpublished version.
