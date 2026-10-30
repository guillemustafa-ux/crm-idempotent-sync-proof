"""The live smoke script is safe to run anywhere: without a key it does nothing, and it reads no file unasked."""

from __future__ import annotations

import os
import subprocess
import sys

from tests.conftest import ROOT


def run(tmp_path, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("HUBSPOT_")}
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "live_smoke.py"), *args],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=60,
    )


def test_without_a_key_the_live_smoke_skips_and_exits_zero(tmp_path):
    proc = run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("live smoke skipped: HUBSPOT_SERVICE_KEY not set")


def test_an_env_file_without_the_key_still_skips(tmp_path):
    env_file = tmp_path / "other.env"
    env_file.write_text("SOMETHING_ELSE=1\n", encoding="utf-8")
    proc = run(tmp_path, "--env-file", str(env_file))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("live smoke skipped:")
