from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "verify_guided_runtime_matrix.sh"


def test_guided_runtime_matrix_script_has_exact_supported_images_and_smokes() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    for image in (
        "ubuntu:22.04",
        "ubuntu:24.04",
        "ubuntu:26.04",
        "debian:11",
        "debian:12",
        "debian:13",
    ):
        assert image in text
    assert "--no-system-packages" in text
    assert "PYTHONPATH=src .venv/bin/python" in text
    assert "bots/lingye-copilot-qq/bot.yaml" in text
    assert "--pull=always" in text
    assert '"$REPO_ROOT:/source:ro"' in text
    assert "/home/" + "agentstrata" not in text
    assert "--home-dir /tmp/agentstrata-home" in text
    assert "docker buildx imagetools inspect --raw" in text
    assert '"amd64", "arm64"' in text
    assert "qq_gateway.sh" not in text
    assert "systemctl" not in text


def test_guided_runtime_matrix_help_and_dry_run_need_no_docker() -> None:
    help_result = subprocess.run(
        ["bash", str(SCRIPT), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_result.returncode == 0
    assert "--all" in help_result.stdout
    assert "--manifest-only" in help_result.stdout

    dry_result = subprocess.run(
        ["bash", str(SCRIPT), "--image", "ubuntu:24.04", "--dry-run"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert dry_result.returncode == 0, dry_result.stderr
    assert "imagetools inspect --raw" in dry_result.stdout
    assert "ubuntu:24.04" in dry_result.stdout
