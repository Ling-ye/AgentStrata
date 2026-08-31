"""Targeted, redacted diagnostic bundles for one task or background job."""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from console.control import operations
from console.control.discovery import discover_instances
from console.control.instances import BotInstance


SCHEMA_VERSION = 1
LOG_PADDING_SECONDS = 120
MAX_LOG_LINES = 1000
MAX_LOG_BYTES = 128 * 1024
MAX_JOB_STREAM_BYTES = 64 * 1024
MAX_STRING_CHARS = 64 * 1024
_ID_RE = re.compile(r"^(?:task|job)_\d{8}_\d{6}_[0-9a-fA-F]{8}$")
_SECRET_KEY_RE = re.compile(
    r"(?:secret|token|api[_-]?key|password|passwd|cookie|authorization|private[_-]?key|credential)",
    re.IGNORECASE,
)
_PRIVATE_KEY_RE = re.compile(
    r"(?:^prompt$|user[_-]?text|tool[_-]?(?:args|arguments|parameters)|^args$|"
    r"(?:qq|user|chat|receive|owner)[_-]?id(?:s)?$|allow[_-]?from|"
    r"codex.*home|auth.*path)",
    re.IGNORECASE,
)
_SECRET_TEXT_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password)\s*[:=]\s*)['\"]?[^\s,'\";]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r"(?i)((?:user_text|prompt|tool_args|arguments|qq_id|user_id|chat_id)"
        r"\s*[:=]\s*)(?:'[^']*'|\"[^\"]*\"|[^\s,;]+)"
    ),
)
_TS_RE = re.compile(r"\[?(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})")


class DiagnosticError(RuntimeError):
    pass


@dataclass(frozen=True)
class Match:
    instance: BotInstance
    kind: str
    root: Path


@dataclass
class RedactionStats:
    values: int = 0
    truncated_strings: int = 0


def collect_diagnostic_bundle(query_id: str, output_dir: Path) -> dict[str, Any]:
    query_id = str(query_id or "").strip()
    if not _ID_RE.fullmatch(query_id):
        raise DiagnosticError(f"不支持的任务 ID 格式: {query_id}")

    instances = discover_instances()
    matches = _find_matches(query_id, instances)
    if not matches:
        searched = ", ".join(item.instance_id for item in instances) or "(none)"
        raise DiagnosticError(f"找不到 {query_id}；已搜索实例: {searched}")
    if len(matches) > 1:
        locations = ", ".join(f"{m.instance.instance_id}:{m.root}" for m in matches)
        raise DiagnosticError(f"{query_id} 命中多个位置，拒绝自动选择: {locations}")

    match = matches[0]
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    stats = RedactionStats()
    missing: list[str] = []
    truncations: list[str] = []

    task_root, job_roots = _resolve_correlations(match, query_id)
    task_payload = _read_json(task_root / "task.json") if task_root else {}
    job_payloads = [_read_json(path / "request.json") for path in job_roots]
    start, end = _time_window(task_payload, job_roots)

    evidence: list[dict[str, Any]] = []
    if task_root:
        for name in ("task.json", "turn.json", "events.jsonl"):
            source = task_root / name
            if not source.is_file():
                missing.append(f"task/{name}")
                continue
            target = output_dir / "task" / name
            if name.endswith(".json"):
                _write_json(target, redact(_read_json(source), stats))
            else:
                if _copy_redacted_jsonl(source, target, stats):
                    truncations.append(target.relative_to(output_dir).as_posix())
            evidence.append(_evidence(target, output_dir, source))

        subagents = task_root / "subagents"
        if subagents.is_dir():
            for source in sorted(subagents.glob("*.json")):
                target = output_dir / "task" / "subagents" / source.name
                _write_json(target, redact(_read_json(source), stats))
                evidence.append(_evidence(target, output_dir, source))

    for job_root in job_roots:
        job_out = output_dir / "jobs" / job_root.name
        for name in ("request.json", "status.json", "result.json", "notification.json"):
            source = job_root / name
            if source.is_file():
                target = job_out / name
                _write_json(target, redact(_read_json(source), stats))
                evidence.append(_evidence(target, output_dir, source))
            elif name in {"stdout.log", "stderr.log"}:
                missing.append(f"jobs/{job_root.name}/{name}")
        for name in (
            "stdout.log",
            "stderr.log",
            "codex.stdout.log",
            "codex.stderr.log",
        ):
            source = job_root / name
            if source.is_file():
                target = job_out / name
                was_truncated = _copy_text_tail(source, target, MAX_JOB_STREAM_BYTES, stats)
                if was_truncated:
                    truncations.append(target.relative_to(output_dir).as_posix())
                evidence.append(_evidence(target, output_dir, source))
            else:
                missing.append(f"jobs/{job_root.name}/{name}")
        candidate_manifest = job_root / "candidate-files.json"
        if candidate_manifest.is_file():
            target = job_out / candidate_manifest.name
            _write_json(target, redact(_read_json(candidate_manifest), stats))
            evidence.append(_evidence(target, output_dir, candidate_manifest))

    for source_name, sources in _log_sources(match.instance, start, end).items():
        target = output_dir / "logs" / f"{source_name}.log"
        lines, was_truncated = _select_log_lines(sources, start, end, query_id, task_payload, job_payloads, stats)
        if lines:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(lines) + "\n", encoding="utf-8")
            evidence.append(_evidence(target, output_dir, None))
            if was_truncated:
                truncations.append(target.relative_to(output_dir).as_posix())
        else:
            missing.append(f"logs/{source_name}.log")

    runtime = _runtime_snapshot(match.instance, task_payload)
    runtime_target = output_dir / "runtime.json"
    _write_json(runtime_target, redact(runtime, stats))
    evidence.append(_evidence(runtime_target, output_dir, None))

    correlation = {
        "query_id": query_id,
        "query_kind": match.kind,
        "instance_id": match.instance.instance_id,
        "task_id": task_root.name if task_root else str((job_payloads[0] if job_payloads else {}).get("task_id") or ""),
        "job_ids": [path.name for path in job_roots],
        "session_id": task_payload.get("session_id") or next((p.get("notify", {}).get("session_id") for p in job_payloads if isinstance(p.get("notify"), dict)), ""),
        "time_window": {"start": start.isoformat(), "end": end.isoformat()},
    }
    summary_text = str(
        redact(
            _render_summary(correlation, task_payload, job_roots, missing, truncations),
            stats,
        )
    )
    index = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(),
        "correlation": correlation,
        "evidence": evidence,
        "missing_evidence": sorted(set(missing)),
        "truncated_files": sorted(set(truncations)),
        "redaction": {"values": stats.values, "truncated_strings": stats.truncated_strings},
    }
    _write_json(output_dir / "index.json", index)
    (output_dir / "summary.md").write_text(summary_text, encoding="utf-8")
    return {"ok": True, "id": query_id, "instance_id": match.instance.instance_id, "output": str(output_dir)}


def redact(value: Any, stats: RedactionStats, *, key: str = "") -> Any:
    if _SECRET_KEY_RE.search(key) or _PRIVATE_KEY_RE.search(key):
        stats.values += 1
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): redact(v, stats, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item, stats, key=key) for item in value]
    if isinstance(value, str):
        text = value
        for pattern in _SECRET_TEXT_PATTERNS:
            text, count = pattern.subn(lambda m: (m.group(1) if m.lastindex else "") + "[REDACTED]", text)
            stats.values += count
        if len(text) > MAX_STRING_CHARS:
            stats.truncated_strings += 1
            text = text[:MAX_STRING_CHARS] + "\n[TRUNCATED]"
        return text
    return value


def _find_matches(query_id: str, instances: Iterable[BotInstance]) -> list[Match]:
    matches: list[Match] = []
    kind = "task" if query_id.startswith("task_") else "job"
    folder = "tasks" if kind == "task" else "jobs"
    marker = "task.json" if kind == "task" else "request.json"
    for instance in instances:
        root = Path(instance.workspace_root) if instance.workspace_root else None
        if not root or not root.is_dir():
            continue
        for candidate in root.glob(f"**/{folder}/{query_id}"):
            if candidate.is_dir() and (candidate / marker).is_file():
                matches.append(Match(instance=instance, kind=kind, root=candidate))
    return matches


def _resolve_correlations(match: Match, query_id: str) -> tuple[Path | None, list[Path]]:
    workspace_root = Path(match.instance.workspace_root)
    if match.kind == "task":
        task_root = match.root
        task = _read_json(task_root / "task.json")
        job_ids = [str(item) for item in task.get("job_ids", []) if str(item).startswith("job_")]
        jobs = [path for job_id in job_ids for path in workspace_root.glob(f"**/jobs/{job_id}") if path.is_dir()]
        jobs.extend(
            request.parent
            for request in workspace_root.glob("**/jobs/*/request.json")
            if _read_json(request).get("task_id") == query_id and request.parent not in jobs
        )
        return task_root, jobs

    request = _read_json(match.root / "request.json")
    task_id = str(request.get("task_id") or "")
    tasks = [path for path in workspace_root.glob(f"**/tasks/{task_id}") if path.is_dir()] if task_id else []
    if not tasks:
        tasks = [
            task_file.parent
            for task_file in workspace_root.glob("**/tasks/*/task.json")
            if query_id in (_read_json(task_file).get("job_ids") or [])
        ]
    return (tasks[0] if len(tasks) == 1 else None), [match.root]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"_value": value}
    except Exception as exc:  # noqa: BLE001
        return {"_read_error": f"{type(exc).__name__}: {exc}"}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _copy_redacted_jsonl(source: Path, target: Path, stats: RedactionStats) -> bool:
    target.parent.mkdir(parents=True, exist_ok=True)
    output: list[str] = []
    total = 0
    truncated = False
    for raw in source.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item: Any = json.loads(raw)
        except json.JSONDecodeError:
            item = {"_raw": raw}
        line = json.dumps(redact(item, stats), ensure_ascii=False, default=str)
        if len(output) >= MAX_LOG_LINES or total + len(line.encode("utf-8")) > MAX_LOG_BYTES:
            truncated = True
            break
        output.append(line)
        total += len(line.encode("utf-8"))
    target.write_text("\n".join(output) + ("\n" if output else ""), encoding="utf-8")
    return truncated


def _copy_text_tail(source: Path, target: Path, max_bytes: int, stats: RedactionStats) -> bool:
    raw = source.read_bytes()
    truncated = len(raw) > max_bytes
    text = raw[-max_bytes:].decode("utf-8", errors="replace") if truncated else raw.decode("utf-8", errors="replace")
    text = str(redact(text, stats))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(("[TRUNCATED TO TAIL]\n" if truncated else "") + text, encoding="utf-8")
    return truncated


def _time_window(task: dict[str, Any], job_roots: list[Path]) -> tuple[datetime, datetime]:
    epochs: list[float] = []
    for key in ("asked_at", "started_at", "finished_at", "updated_at"):
        if isinstance(task.get(key), (int, float)):
            epochs.append(float(task[key]))
    for root in job_roots:
        for name in ("request.json", "status.json", "result.json"):
            payload = _read_json(root / name)
            for key in ("submitted_at", "started_at", "finished_at", "updated_at"):
                if isinstance(payload.get(key), (int, float)):
                    epochs.append(float(payload[key]))
    now = datetime.now().astimezone()
    start = datetime.fromtimestamp(min(epochs), tz=now.tzinfo) if epochs else now
    end = datetime.fromtimestamp(max(epochs), tz=now.tzinfo) if epochs else now
    if not task.get("finished_at") and any(_read_json(root / "status.json").get("status") in {"queued", "running"} for root in job_roots):
        end = now
    return start - timedelta(seconds=LOG_PADDING_SECONDS), end + timedelta(seconds=LOG_PADDING_SECONDS)


def _log_sources(instance: BotInstance, start: datetime, end: datetime) -> dict[str, list[Path]]:
    if not instance.log_dir:
        return {"runtime": [], "gateway" if instance.runtime_kind == "gateway" else "cc-connect": []}
    root = Path(instance.log_dir)
    dates: list[str] = []
    cursor = start.date()
    while cursor <= end.date():
        dates.append(cursor.isoformat())
        cursor += timedelta(days=1)
    primary_name = "gateway" if instance.runtime_kind == "gateway" else "cc-connect"
    return {
        "runtime": [root / "runtime" / f"{day}.log" for day in dates],
        primary_name: [root / primary_name / f"{day}.log" for day in dates],
    }


def _select_log_lines(
    sources: list[Path], start: datetime, end: datetime, query_id: str,
    task: dict[str, Any], jobs: list[dict[str, Any]], stats: RedactionStats,
) -> tuple[list[str], bool]:
    needles = {query_id, str(task.get("task_id") or ""), str(task.get("session_id") or "")}
    needles.update(str(item.get("job_id") or "") for item in jobs)
    needles.discard("")
    selected: list[str] = []
    for source in sources:
        if not source.is_file():
            continue
        for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
            match = _TS_RE.search(line)
            in_window = False
            if match:
                try:
                    stamp = datetime.fromisoformat(f"{match.group(1)}T{match.group(2)}").replace(tzinfo=start.tzinfo)
                    in_window = start <= stamp <= end
                except ValueError:
                    pass
            if in_window or any(needle in line for needle in needles):
                selected.append(str(redact(line, stats)))
    encoded = 0
    output: list[str] = []
    truncated = False
    for line in selected:
        size = len((line + "\n").encode("utf-8"))
        if len(output) >= MAX_LOG_LINES or encoded + size > MAX_LOG_BYTES:
            truncated = True
            break
        output.append(line)
        encoded += size
    return output, truncated


def _runtime_snapshot(instance: BotInstance, task: dict[str, Any]) -> dict[str, Any]:
    try:
        status = operations.status(instance)
    except Exception as exc:  # noqa: BLE001
        status = {"error": f"{type(exc).__name__}: {exc}"}
    git: dict[str, Any] = {}
    if instance.wsl_home and (Path(instance.wsl_home) / ".git").exists():
        for key, args in (("revision", ["rev-parse", "HEAD"]), ("branch", ["rev-parse", "--abbrev-ref", "HEAD"])):
            proc = subprocess.run(["git", *args], cwd=instance.wsl_home, capture_output=True, text=True, timeout=5, check=False)
            git[key] = proc.stdout.strip() or proc.stderr.strip()
    models = sorted(
        {
            str(call.get("model"))
            for call in task.get("llm_calls", [])
            if isinstance(call, dict) and call.get("model")
        }
    )
    return {
        "instance": instance.to_dict(),
        "status": status,
        "git": git,
        "task_models": models,
        "code_tasks": _code_task_snapshot(instance),
        "binaries": _binary_snapshot(instance),
    }


def _code_task_snapshot(instance: BotInstance) -> dict[str, Any]:
    jobs: list[tuple[float, str, dict[str, Any]]] = []
    root = Path(instance.workspace_root) if instance.workspace_root else None
    if root and root.is_dir():
        for status_path in root.glob("**/jobs/job_*/status.json"):
            request = _read_json(status_path.parent / "request.json")
            if request.get("tool_name") != "start_code_task":
                continue
            status = _read_json(status_path)
            jobs.append(
                (
                    float(status.get("updated_at") or 0),
                    status_path.parent.name,
                    status,
                )
            )
    jobs.sort(reverse=True)
    active_states = {
        "queued",
        "preparing",
        "running",
        "validating",
        "delivering",
        "cancel_requested",
    }
    latest = jobs[0] if jobs else None
    latest_payload: dict[str, Any] = {}
    if latest is not None:
        _, task_id, status = latest
        heartbeat = status.get("heartbeat_at")
        latest_payload = {
            "task_id": task_id,
            "status": status.get("status"),
            "stage": status.get("stage"),
            "heartbeat_age_seconds": (
                max(0, int(datetime.now().timestamp() - float(heartbeat)))
                if heartbeat
                else None
            ),
            "resource": status.get("resource")
            if isinstance(status.get("resource"), dict)
            else {},
            "error_code": status.get("error_code"),
        }
    unit = f"chatcopilot-code-worker@{instance.instance_id}.service"
    active = False
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", unit],
            capture_output=True,
            timeout=5,
            check=False,
        )
        active = result.returncode == 0
    except Exception:  # noqa: BLE001
        pass
    return {
        "worker_unit": unit,
        "worker_active": active,
        "active_count": sum(
            str(status.get("status") or "") in active_states for _, _, status in jobs
        ),
        "latest": latest_payload,
    }


def _binary_snapshot(instance: BotInstance) -> dict[str, Any]:
    worker_env = (
        Path.home()
        / ".config"
        / "chatcopilot-console"
        / f"{instance.instance_id}-code-worker.env"
    )
    configured: dict[str, str] = {}
    if worker_env.is_file():
        for raw in worker_env.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" not in raw or raw.lstrip().startswith("#"):
                continue
            key, value = raw.split("=", 1)
            if key in {"CHATCOPILOT_CODEX_BIN", "CHATCOPILOT_CC_CONNECT_BIN"}:
                configured[key] = value.strip().strip('"')
    configured.setdefault(
        "CHATCOPILOT_CC_CONNECT_BIN",
        str(
            Path.home()
            / ".local"
            / "share"
            / "agentstrata"
            / "node-tools"
            / "cc-connect-1.4.0-beta.3"
            / "node_modules"
            / ".bin"
            / "cc-connect"
        ),
    )
    return {
        "codex": _one_binary(configured.get("CHATCOPILOT_CODEX_BIN", "")),
        "cc_connect": _one_binary(configured["CHATCOPILOT_CC_CONNECT_BIN"]),
    }


def _one_binary(raw: str) -> dict[str, Any]:
    path = Path(raw).expanduser()
    if not path.is_absolute() or not path.is_file():
        return {"configured": bool(raw), "available": False}
    resolved = path.resolve()
    version = ""
    try:
        result = subprocess.run(
            [str(resolved), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        version = (result.stdout or result.stderr).splitlines()[0][:200]
    except Exception:  # noqa: BLE001
        pass
    return {
        "configured": True,
        "available": resolved.is_file(),
        "path": str(resolved),
        "version": version,
    }


def _evidence(target: Path, root: Path, source: Path | None) -> dict[str, Any]:
    return {
        "path": target.relative_to(root).as_posix(),
        "size_bytes": target.stat().st_size,
        "source": str(source) if source else "generated",
    }


def _render_summary(
    correlation: dict[str, Any], task: dict[str, Any], jobs: list[Path],
    missing: list[str], truncations: list[str],
) -> str:
    lines = [
        f"# 任务诊断：{correlation['query_id']}",
        "",
        f"- 实例：`{correlation['instance_id']}`",
        f"- 类型：`{correlation['query_kind']}`",
        f"- task：`{correlation.get('task_id') or '未关联'}`",
        f"- jobs：{', '.join(f'`{item}`' for item in correlation['job_ids']) or '无'}",
        f"- task 状态：`{task.get('status') or 'unknown'}`",
        f"- 进展：{task.get('progress') or '无'}",
        "",
        "## 后台任务",
    ]
    for root in jobs:
        status = _read_json(root / "status.json")
        result = _read_json(root / "result.json")
        lines.append(f"- `{root.name}`：`{status.get('status') or 'unknown'}`；{status.get('message') or result.get('error') or result.get('summary') or '无摘要'}")
    if not jobs:
        lines.append("- 无关联后台任务。")
    lines.extend(["", "## 建议读取顺序", "1. `index.json`：确认缺失、截断和关联关系。"])
    if (Path("task") / "turn.json").as_posix() not in missing:
        lines.append("2. `task/turn.json`：核对本轮输入、最终回复和 stop reason。")
    lines.append("3. `task/events.jsonl`：定位失败工具、subagent 或异常 token 消耗。")
    if jobs:
        lines.append("4. 只读取失败 job 的 `result.json`、`stderr.log` 和 `stdout.log`。")
    lines.append("5. 仍无法解释时，再读取 `logs/` 中的定向时间窗片段。")
    if missing:
        lines.extend(["", "## 证据缺口", *[f"- `{item}`" for item in sorted(set(missing))[:20]]])
    if truncations:
        lines.extend(["", "## 已截断", *[f"- `{item}`" for item in sorted(set(truncations))[:20]]])
    return ("\n".join(lines).strip() + "\n")[:6144]


__all__ = ["DiagnosticError", "RedactionStats", "collect_diagnostic_bundle", "redact"]
