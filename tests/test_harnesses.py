"""Run the legacy ad-hoc harnesses as independent guard rails.

The pytest suite ports their coverage; these wrappers keep the originals honest
and catch drift between the two. Node-only checks are skipped when node is
absent.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run(args, cwd=ROOT):
    return subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True, timeout=300
    )


@pytest.mark.parametrize("script", ["_selftest.py", "_spatest.py"])
def test_legacy_harness_passes(script):
    result = _run([sys.executable, script])
    if result.returncode != 0:
        pytest.fail(f"{script} failed (exit {result.returncode})\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")


def test_driver_js_when_node_available():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    result = _run([node, str(ROOT / "tests" / "driver.js")])
    if result.returncode != 0:
        pytest.fail(f"driver.js failed\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
