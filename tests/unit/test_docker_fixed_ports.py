from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
_SERVICES_PATH = _ROOT / "deploy" / "docker" / "services.sh"
_PORT_KEYS = ("XHS_MCP_PORT", "SEARXNG_PORT", "PLAYWRIGHT_MCP_PORT")


def _copy_runtime(tmp_path: Path, env_text: str) -> tuple[Path, Path]:
    docker_dir = tmp_path / "repo" / "deploy" / "docker"
    docker_dir.mkdir(parents=True)
    shutil.copy2(_SERVICES_PATH, docker_dir / "services.sh")
    (docker_dir / ".env").write_text(env_text, encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_log = tmp_path / "docker.log"
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$DOCKER_CALL_LOG"\n',
        encoding="utf-8",
    )
    fake_docker.chmod(fake_docker.stat().st_mode | stat.S_IXUSR)
    return docker_dir / "services.sh", docker_log


def _run_services(
    tmp_path: Path,
    env_text: str,
    *,
    exported_port: tuple[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    script, docker_log = _copy_runtime(tmp_path, env_text)
    env = os.environ.copy()
    for key in _PORT_KEYS:
        env.pop(key, None)
    if exported_port is not None:
        env[exported_port[0]] = exported_port[1]
    env["DOCKER_CALL_LOG"] = str(docker_log)
    env["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}{env['PATH']}"
    result = subprocess.run(
        ("bash", str(script), "status", "playwright-mcp"),
        cwd=script.parent,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    return result, docker_log


def test_services_use_reviewed_fixed_runtime_ports() -> None:
    source = _SERVICES_PATH.read_text(encoding="utf-8")

    assert 'XHS_PORT="18060"' in source
    assert 'SEARXNG_HTTP_PORT="18064"' in source
    assert 'PLAYWRIGHT_PORT="18066"' in source
    assert "compose_port_env.py" not in source


def test_normal_environment_reaches_docker(tmp_path: Path) -> None:
    result, docker_log = _run_services(tmp_path, "XHS_PROXY=\n")

    assert result.returncode == 0, result.stderr
    assert docker_log.is_file()


def test_exported_port_override_fails_before_docker(tmp_path: Path) -> None:
    result, docker_log = _run_services(
        tmp_path,
        "XHS_PROXY=\n",
        exported_port=("PLAYWRIGHT_MCP_PORT", "19066"),
    )

    assert result.returncode == 2
    assert "no longer configurable" in result.stderr
    assert "19066" not in result.stderr
    assert not docker_log.exists()


def test_compose_env_port_override_fails_before_docker(tmp_path: Path) -> None:
    result, docker_log = _run_services(
        tmp_path,
        "XHS_PROXY=\nSEARXNG_PORT=19064\n",
    )

    assert result.returncode == 2
    assert "port override keys are unsupported" in result.stderr
    assert "19064" not in result.stderr
    assert not docker_log.exists()
