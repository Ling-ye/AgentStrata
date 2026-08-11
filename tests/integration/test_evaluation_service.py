from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from chatcopilot.evals.service import EvaluationServiceClient, EvaluationServiceError


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix socket boundary")
def test_evaluation_service_real_process_health(tmp_path: Path) -> None:
    socket_path = tmp_path / "runtime" / "evaluation.sock"
    artifact_root = tmp_path / "artifacts"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        (
            str(REPOSITORY_ROOT / "src"),
            *(item for item in environment.get("PYTHONPATH", "").split(os.pathsep) if item),
        )
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "chatcopilot.evals.service",
            "serve",
            "--socket",
            str(socket_path),
            "--repository-root",
            str(REPOSITORY_ROOT),
            "--artifact-root",
            str(artifact_root),
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    client = EvaluationServiceClient(socket_path, timeout_seconds=2)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f"Evaluation service exited with code {process.returncode}")
            try:
                health = client.health()
            except EvaluationServiceError:
                time.sleep(0.05)
                continue
            assert health["ready"] is True
            assert health["service"] == "agentstrata-evaluation"
            break
        else:
            pytest.fail("Evaluation service did not become ready")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    assert process.returncode == 0
