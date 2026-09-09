import os
from pathlib import Path
import subprocess
import sys

import pytest

from chatcopilot.evals.redaction import sanitize_text


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "mytoken",
        "service_token",
        "UPPER-TOKEN",
        "api_key",
        "session-token",
        "prefixpassword",
    ],
)
def test_inline_credentials_remain_redacted(key):
    assert "fixture-value" not in sanitize_text(f"{key}=fixture-value")
    assert "[REDACTED]" in sanitize_text(f"{key}:fixture-value")


def test_long_unbroken_output_is_processed_without_quadratic_scan():
    # A process deadline bounds a regression independently of pytest wall-clock assertions.
    script = """
from chatcopilot.evals.redaction import sanitize_text
text = "z" * 131072
assert sanitize_text(text) == text
assert "hidden-value" not in sanitize_text(text + "token=hidden-value")
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    completed = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=10
    )
    assert completed.returncode == 0, completed.stderr
