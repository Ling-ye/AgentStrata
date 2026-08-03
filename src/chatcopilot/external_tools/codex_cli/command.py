"""Security-checked Codex CLI command and subprocess-environment builders."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
from pathlib import Path


def build_codex_command(
    template: str,
    *,
    model: str,
    workdir: Path,
    reasoning_effort: str = "medium",
    network_access: bool = False,
    sandbox_mode: str = "workspace-write",
    web_search_mode: str = "disabled",
    skip_git_repo_check: bool = False,
    output_last_message: Path | None = None,
    shell_env_overrides: dict[str, str] | None = None,
    ephemeral: bool = True,
    ignore_user_config: bool = True,
    inherit_shell_environment: bool = False,
    extra_config: tuple[str, ...] = (),
) -> list[str]:
    rendered = (template or "").format(model=model, workdir=str(workdir))
    configured = shlex.split(rendered)
    if not configured:
        raise RuntimeError("code command is empty")
    if len(configured) < 2 or Path(configured[0]).name != "codex" or configured[1] != "exec":
        raise RuntimeError("code command must invoke codex exec")
    _validate_codex_command_template(configured)
    executable = _resolve_executable(configured[0])
    shell_policy = _shell_environment_policy(
        workdir=workdir,
        inherit_all=inherit_shell_environment,
        overrides=shell_env_overrides,
    )
    command = [
        executable,
        "exec",
        "--model",
        model,
        "--config",
        f"model_reasoning_effort={json.dumps(reasoning_effort)}",
        "--sandbox",
        sandbox_mode,
        "--config",
        f"sandbox_workspace_write.network_access={str(network_access).lower()}",
        "--config",
        f"web_search={json.dumps(web_search_mode)}",
    ]
    for entry in shell_policy:
        command.extend(["--config", entry])
    command.extend(["--cd", str(workdir)])
    if ephemeral:
        command.insert(8, "--ephemeral")
    if ignore_user_config:
        command.insert(8, "--ignore-user-config")
    for config_entry in extra_config:
        command.extend(["--config", str(config_entry)])
    if skip_git_repo_check:
        command.append("--skip-git-repo-check")
    if output_last_message is not None:
        command.extend(["--output-last-message", str(output_last_message)])
    return command


def _shell_environment_policy(
    *,
    workdir: Path,
    inherit_all: bool,
    overrides: dict[str, str] | None,
) -> tuple[str, ...]:
    if inherit_all:
        if overrides:
            raise ValueError("shell overrides cannot be combined with full environment inheritance")
        return ('shell_environment_policy.inherit="all"',)
    shell_env = {
        "HOME": str(workdir),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
    }
    for name in ("LANG", "LC_ALL", "LC_CTYPE"):
        if os.environ.get(name):
            shell_env[name] = os.environ[name]
    for name, value in (overrides or {}).items():
        if not name or not isinstance(value, str) or "\x00" in value:
            raise ValueError(f"invalid Codex shell environment override: {name!r}")
        shell_env[name] = value
    inline_env = ", ".join(
        f"{name} = {json.dumps(value)}" for name, value in sorted(shell_env.items())
    )
    return (
        'shell_environment_policy.inherit="none"',
        f"shell_environment_policy.set={{ {inline_env} }}",
    )


def _validate_codex_command_template(command: list[str]) -> None:
    value_options = {"--model", "-m", "--cd", "-C", "--cwd", "--reasoning-effort"}
    assignment_prefixes = tuple(
        option + "=" for option in value_options if option.startswith("--")
    )
    index = 2
    while index < len(command):
        argument = command[index]
        if argument in value_options:
            if index + 1 >= len(command):
                raise RuntimeError(f"code command option requires a value: {argument}")
            index += 2
            continue
        if argument.startswith(assignment_prefixes):
            index += 1
            continue
        raise RuntimeError(
            f"code command contains an unsupported or unsafe option: {argument}"
        )


def _resolve_executable(executable: str) -> str:
    if "/" in executable or "\\" in executable:
        return executable
    found = shutil.which(executable)
    if found:
        return found
    if executable != "codex":
        return executable
    for candidate in _codex_candidates():
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise FileNotFoundError(
        "codex CLI was not found in the service PATH. Install Codex CLI in WSL PATH, "
        "create a ~/.local/bin/codex shim, or set CHATCOPILOT_CODEX_BIN/CODEX_BIN. "
        f"PATH={os.environ.get('PATH', '')}"
    )


def _codex_candidates() -> list[Path]:
    candidates: list[Path] = []
    for env_name in ("CHATCOPILOT_CODEX_BIN", "CODEX_BIN"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            candidates.append(Path(raw).expanduser())
    home = Path.home()
    candidates.extend(
        [
            home / ".local" / "bin" / "codex",
            home / ".npm-global" / "bin" / "codex",
            home / ".codex" / "bin" / "codex",
        ]
    )
    users_root = Path("/mnt/c/Users")
    if users_root.is_dir():
        candidates.extend(sorted(users_root.glob("*/.codex/bin/wsl/*/codex")))
    deduped: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            deduped.append(candidate)
    return deduped


def build_codex_subprocess_env(
    executable: str,
    *,
    runtime_home: Path | None = None,
    inherit_all: bool = False,
) -> dict[str, str]:
    source = os.environ
    if inherit_all:
        if runtime_home is not None:
            raise ValueError(
                "runtime_home cannot be combined with full environment inheritance"
            )
        env = dict(source)
        if not env.get("CODEX_HOME"):
            source_home = source.get("CHATCOPILOT_CODEX_HOME") or _infer_codex_home(
                Path(executable)
            )
            if source_home:
                env["CODEX_HOME"] = str(source_home)
        return env
    env = {
        name: source[name]
        for name in (
            "HOME",
            "USER",
            "LOGNAME",
            "PATH",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "TMPDIR",
            "TERM",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
        )
        if source.get(name)
    }
    if runtime_home is not None:
        runtime_home = Path(
            os.path.abspath(os.path.expanduser(str(runtime_home)))
        )
        try:
            runtime_info = runtime_home.lstat()
        except OSError as exc:
            raise RuntimeError("isolated Codex runtime home is unavailable") from exc
        if (
            stat.S_ISLNK(runtime_info.st_mode)
            or not stat.S_ISDIR(runtime_info.st_mode)
            or stat.S_IMODE(runtime_info.st_mode) != 0o700
        ):
            raise RuntimeError("isolated Codex runtime home must be a 0700 directory")
        env["CODEX_HOME"] = str(runtime_home)
        env["CODEX_SQLITE_HOME"] = str(runtime_home)
    return env


def _infer_codex_home(executable: Path) -> Path | None:
    for parent in executable.expanduser().resolve().parents:
        if parent.name == ".codex" and (parent / "auth.json").is_file():
            return parent
    candidates = [Path.home() / ".codex"]
    users_root = Path("/mnt/c/Users")
    if users_root.is_dir():
        candidates.extend(sorted(users_root.glob("*/.codex")))
    for candidate in candidates:
        if (candidate / "auth.json").is_file():
            return candidate
    return None


__all__ = ["build_codex_command", "build_codex_subprocess_env"]
