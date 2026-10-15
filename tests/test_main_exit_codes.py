"""CLI exit-code contract + scheduler preflight wiring (Phase 0 regressions)."""

from __future__ import annotations

import pytest

import main as cli


def test_no_command_prints_help_and_exits_2(capsys):
    assert cli.main([]) == cli.EXIT_USAGE


def test_help_exits_zero():
    # argparse handles --help before the try block, so it raises SystemExit(0).
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"])
    assert excinfo.value.code in (0, None)


def test_unknown_command_is_usage_error(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["definitely-not-a-command"])
    assert excinfo.value.code != 0


def test_status_json_runs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # No config on disk: main auto-creates from the example beside it? No - the
    # example is next to main.py, and the config path defaults to ./config.yaml
    # in the cwd, so point it at the shipped example explicitly.
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    code = cli.main(["--config", str(example), "--tracker", str(tmp_path / "t.json"), "status", "--json"])
    assert code == cli.EXIT_OK


def test_systemexit_zero_maps_to_zero(monkeypatch):
    """Regression: `SystemExit(0)` used to become exit code 1."""
    import argparse

    def fake_build_parser():
        parser = argparse.ArgumentParser(prog="fake")
        parser.parse_args = lambda *_a, **_k: argparse.Namespace(command="status", func=_raise_exit_zero)
        return parser

    def _raise_exit_zero(_args):
        raise SystemExit(0)

    monkeypatch.setattr(cli, "build_parser", fake_build_parser)
    assert cli.main([]) == 0


def test_systemexit_string_maps_to_error(monkeypatch):
    import argparse

    def fake_build_parser():
        parser = argparse.ArgumentParser(prog="fake")
        parser.parse_args = lambda *_a, **_k: argparse.Namespace(command="status", func=_raise_exit_str)
        return parser

    def _raise_exit_str(_args):
        raise SystemExit("boom")

    monkeypatch.setattr(cli, "build_parser", fake_build_parser)
    assert cli.main([]) == cli.EXIT_ERROR



def test_scheduler_preflight_skips_nameless_sponsor(tmp_path, repo_root):
    """senders.preflight is wired into Scheduler._deliver."""
    import shutil

    from config_loader import Config
    from models import Sponsor
    from scheduler import Scheduler
    from tracker import SponsorTracker

    shutil.copyfile(repo_root / "config.example.yaml", tmp_path / "config.yaml")
    config = Config.load(tmp_path / "config.yaml", create_from_example=False)

    tracker = SponsorTracker(
        tmp_path / "sponsors.json",
        seed_sponsors=[{"name": "Acme", "email": "a@b.com"}],
        rate_limits={"email": 10, "forum": 10},
    )
    sched = Scheduler(config, tracker, dry_run=True, batch_size=5)

    # A sponsor whose name was stripped fails preflight and is skipped.
    broken = Sponsor(name="", channel="email", email="x@y.com")
    result = sched.deliver([broken])
    assert result.skipped == 1
    assert any("no name" in note for note in result.notes), str(result.notes)
