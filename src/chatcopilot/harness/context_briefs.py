"""Bounded, deterministic navigation for role context and repair retries."""
from __future__ import annotations

from collections import Counter
import hashlib
import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from chatcopilot.core.private_sqlite import json_text
from chatcopilot.core.source_manifest import is_private_environment_path
from chatcopilot.core.source_snapshot import manifest_digest
from chatcopilot.harness.agent_types import retry_role


SOURCE_INDEX_BYTES = 16 * 1024
FAILURE_BRIEF_BYTES = 8 * 1024
TARGET_CONTEXT_BYTES = 16 * 1024
MAX_SEEDS = 12
MAX_CANDIDATES = 24
MAX_EXCERPTS = 2
MAX_RULES = 6
MAX_TEXT_FILE = 1024 * 1024
_PATH = re.compile(r"(?<![\w.-])(?:src|tests|console|scripts|docs|specs|deploy)/[A-Za-z0-9_./-]+")
_IDENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_.:-]{3,79}\b")
_BACKTICK = re.compile(r"`([^`\n]{2,120})`")
_TEXT_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".md", ".toml", ".yaml", ".yml", ".json", ".sh", ".txt", ".ini", ".cfg", ".service", ".timer"}
_STOP = {"true", "false", "none", "null", "error", "failed", "failure", "expected", "behavior",
         "source", "result", "status", "repair", "harness", "agent", "main", "test", "plan", "coding",
         "agentstrata", "repository", "analyze"}


def _strings(value: Any, *, limit: int = 300) -> list[str]:
    found: list[str] = []

    def visit(item: Any) -> None:
        if len(found) >= limit:
            return
        if isinstance(item, str):
            found.append(item)
        elif isinstance(item, dict):
            for key, child in item.items():
                if key not in {"content", "raw", "stdout", "stderr", "events", "rows", "kind", "bot_id",
                               "run_id", "repository", "digest", "sha256", "source_id", "task_id", "request_id"}:
                    visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item[:100]:
                visit(child)

    visit(value)
    return found


def _path_name(value: str) -> str:
    return re.sub(r":\d+(?:-\d+)?$", "", value.split("#", 1)[0]).strip().replace("\\", "/")


def _seed_values(source: dict[str, Any], manifest: dict[str, Any], failure: dict[str, Any] | None) -> tuple[list[dict[str, str]], set[str], int]:
    values = _strings(source)
    if failure:
        values.extend(_strings({key: failure.get(key) for key in ("failed_checks", "changed_paths", "diagnostics")}))
    explicit: set[str] = set()
    seeds: list[dict[str, str]] = []
    seen: set[str] = set()
    omitted = 0

    def add(value: str, kind: str) -> None:
        nonlocal omitted
        value = value.strip().strip("'\"")[:120]
        key = value.casefold()
        if not value or key in seen:
            return
        seen.add(key)
        if len(seeds) >= MAX_SEEDS:
            omitted += 1
            return
        seeds.append({"value": value, "kind": kind})

    for text in values:
        for raw in _PATH.findall(text):
            path = _path_name(raw)
            if path in manifest:
                explicit.add(path)
                add(path, "path")
    for text in values:
        for raw in _IDENT.findall(text):
            low = raw.casefold()
            if low in _STOP or raw.islower() and not any(char in raw for char in "_.:-"):
                continue
            add(raw, "identifier")
            for part in re.split(r"[.:-]+", raw):
                if part != raw and len(part) >= 4 and part.casefold() not in _STOP:
                    add(part, "identifier_part")
        for raw in _BACKTICK.findall(text):
            path = _path_name(raw)
            add(path if path in manifest else raw, "path" if path in manifest else "quoted")
    return seeds, explicit, omitted


def _repository_map(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for name in manifest:
        parts = PurePosixPath(name).parts
        if parts[:2] == ("src", "chatcopilot") and len(parts) > 2:
            key = "/".join(parts[:3])
        elif parts[:2] == ("console", "web") and len(parts) > 2:
            key = "/".join(parts[:3])
        else:
            key = parts[0]
        counts[key] += 1
    return [{"path": key, "files": count} for key, count in sorted(counts.items())[:32]]


def _rank(name: str, explicit: set[str], filename: bool, content: bool) -> int:
    if name in explicit:
        return 0
    if filename:
        return 1
    if name.startswith(("src/", "console/", "scripts/")) and content:
        return 2
    if name.startswith("tests/") and content:
        return 3
    if name.startswith(("specs/", "docs/")) and content:
        return 4
    return 5


def _clip(text: str, limit: int = 240) -> str:
    value = " ".join(text.strip().split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _fit(value: dict[str, Any], *, limit: int, removable: Iterable[str]) -> dict[str, Any]:
    result = value
    keys = tuple(removable)
    while len(json_text(result).encode()) > limit:
        removed = False
        for key in keys:
            rows = result.get(key)
            if isinstance(rows, list) and rows:
                rows.pop()
                omitted = result.setdefault("omitted_counts", {})
                omitted[key] = omitted.get(key, 0) + 1
                removed = True
                break
        if not removed:
            break
    result["limits"]["bytes"] = limit
    omitted = result.get("omitted_counts", {})
    result["limits"]["truncated"] = any(omitted.get(key, 0) for key in removable)
    return result


def build_source_index(*, baseline: Path, manifest: dict[str, Any], source: dict[str, Any], rules: list[dict[str, Any]],
                       attempt: int, failure: dict[str, Any] | None = None) -> dict[str, Any]:
    seeds, explicit, seed_omitted = _seed_values(source, manifest, failure)
    terms = [row["value"] for row in seeds if row["kind"] != "path"]
    matches: list[tuple[int, int, str, dict[str, Any]]] = []
    skipped_large = skipped_binary = 0
    for name in sorted(manifest):
        path = baseline / name
        suffix = path.suffix.casefold()
        if is_private_environment_path(name):
            continue
        stem = path.stem.casefold()
        filename = any(term.casefold() == stem for term in terms)
        is_explicit = name in explicit
        if not is_explicit and "/vendor/" in "/" + name:
            continue
        size = path.stat().st_size
        if size > MAX_TEXT_FILE:
            skipped_large += 1
            if is_explicit:
                matches.append((0, 0, name, {"path": name, "sha256": manifest[name]["sha256"], "reasons": ["explicit_large"], "excerpts": []}))
            continue
        if suffix not in _TEXT_SUFFIXES and not is_explicit:
            skipped_binary += 1
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            skipped_binary += 1
            continue
        content_terms = [term for term in terms if term.casefold() in text.casefold()]
        if not (is_explicit or filename or content_terms):
            continue
        lines = text.splitlines()
        excerpts = []
        for line_no, line in enumerate(lines, 1):
            if is_explicit and not terms or any(term.casefold() in line.casefold() for term in terms):
                excerpts.append({"line": line_no, "text": _clip(line)})
                if len(excerpts) >= MAX_EXCERPTS:
                    break
        reasons = (["explicit_path"] if is_explicit else []) + (["filename_match"] if filename else [])
        reasons.extend("term:" + term for term in content_terms[:3])
        matches.append((_rank(name, explicit, filename, bool(content_terms)), -len(content_terms), name, {
            "path": name, "sha256": manifest[name]["sha256"], "reasons": reasons, "excerpts": excerpts,
        }))
    matches.sort(key=lambda row: (row[0], row[1], row[2]))
    candidates = [row[3] for row in matches[:MAX_CANDIDATES]]
    rule_refs = []
    for row in rules:
        content = str(row.get("content", ""))
        hit = next((term for term in terms if term.casefold() in (row.get("path", "") + "\n" + content).casefold()), None)
        if row.get("path") == "docs/reference/harness-principles.md" or hit:
            line_no, excerpt = 1, ""
            if hit:
                for index, line in enumerate(content.splitlines(), 1):
                    if hit.casefold() in line.casefold():
                        line_no, excerpt = index, _clip(line)
                        break
            rule_refs.append({"path": row["path"], "sha256": row["sha256"], "line": line_no,
                              "reason": "principles" if not hit else "term:" + hit, "excerpt": excerpt})
    matched_rules = len(rule_refs)
    rule_refs = rule_refs[:MAX_RULES]
    value = {
        "version": 1, "baseline_digest": manifest_digest(manifest), "attempt": attempt,
        "source_digest": hashlib.sha256(json_text(source).encode()).hexdigest(),
        "failure_brief_digest": hashlib.sha256(json_text(failure).encode()).hexdigest() if failure else "",
        "seeds": seeds, "candidates": candidates, "rule_refs": rule_refs,
        "repository_map": _repository_map(manifest),
        "omitted_counts": {"seeds": seed_omitted,
                           "candidates": max(0, len(matches) - len(candidates)),
                           "rule_refs": max(0, matched_rules - len(rule_refs)),
                           "large_files": skipped_large, "non_text_files": skipped_binary},
        "limits": {"seeds": MAX_SEEDS, "candidates": MAX_CANDIDATES, "excerpts_per_path": MAX_EXCERPTS,
                   "rules": MAX_RULES, "bytes": SOURCE_INDEX_BYTES, "truncated": False},
    }
    return _fit(value, limit=SOURCE_INDEX_BYTES, removable=("candidates", "rule_refs", "repository_map", "seeds"))


def _unique(values: Iterable[Any], limit: int) -> tuple[list[str], int]:
    rows = list(dict.fromkeys(str(value) for value in values if value not in (None, "")))
    return rows[:limit], max(0, len(rows) - limit)


def build_failure_brief(*, attempt: dict[str, Any], stage: str, code: str, message: str, signature: str,
                        evidence_ref: dict[str, Any] | None = None, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    failed: list[str] = []
    passed: list[str] = []
    diagnostics: list[dict[str, str]] = []
    diagnostic_count = 0
    refs: list[dict[str, Any]] = []
    for phase in ("verification", "confirmation", "repository_regressions"):
        row = attempt.get(phase) or {}
        failed.extend(row.get("failed_cases") or ())
        passed.extend(row.get("passed_cases") or ())
        if row.get("report"):
            refs.append({"kind": phase + "_report", "path": row["report"]})
    for row in (evidence or {}).get("checks", []):
        if row.get("exit_code"):
            failed.extend(row.get("failed_ids") or [row.get("name")])
            diagnostic = str(row.get("diagnostic") or row.get("first_failure") or "").strip()
            if diagnostic:
                diagnostic_count += 1
                if len(diagnostics) < 4:
                    diagnostics.append({"check": str(row.get("name", "unknown")), "text": _clip(diagnostic, 800)})
    if evidence_ref:
        refs.append({key: evidence_ref[key] for key in ("kind", "path", "sha256", "revision") if key in evidence_ref})
    failed_rows, failed_omitted = _unique(failed, 8)
    passed_rows, passed_omitted = _unique(passed, 8)
    paths, paths_omitted = _unique(attempt.get("changed_files") or (), 16)
    missing, missing_omitted = _unique((attempt.get("feedback") or {}).get("requirements") or [stage], 8)
    passed_req, passed_req_omitted = _unique((attempt.get("feedback") or {}).get("passed_requirements") or (), 8)
    value = {
        "version": 1, "attempt": attempt.get("number"), "stage": stage, "code": code,
        "message": _clip(message, 1000), "signature": signature,
        "recommended_role": retry_role(stage, code).value,
        "missing_requirements": missing, "passed_requirements": passed_req, "changed_paths": paths,
        "failed_checks": failed_rows, "passed_checks": passed_rows, "diagnostics": diagnostics,
        "evidence_refs": refs,
        "omitted_counts": {"missing_requirements": missing_omitted, "passed_requirements": passed_req_omitted,
                           "changed_paths": paths_omitted, "failed_checks": failed_omitted,
                           "passed_checks": passed_omitted, "diagnostics": max(0, diagnostic_count - len(diagnostics))},
        "limits": {"bytes": FAILURE_BRIEF_BYTES, "truncated": False},
    }
    return _fit(value, limit=FAILURE_BRIEF_BYTES, removable=("diagnostics", "failed_checks", "passed_checks", "changed_paths"))


def build_target_context(target: dict[str, Any], rules: list[dict[str, Any]]) -> dict[str, Any]:
    by_path = {row["path"]: row for row in rules}
    selected_rules = []
    for reference in target.get("principle_refs", [])[:MAX_RULES]:
        name = _path_name(reference)
        row = by_path.get(name)
        if not row:
            continue
        line = int((re.search(r":(\d+)", reference) or [None, 1])[1])
        lines = row["content"].splitlines()
        start = max(0, line - 2)
        excerpt = _clip("\n".join(lines[start:start + 5]), 1200)
        selected_rules.append({"path": name, "sha256": row["sha256"], "reference": reference, "excerpt": excerpt})
    evidence = [{**row, "excerpt": _clip(str(row.get("excerpt", "")), 800)}
                for row in target.get("evidence", [])[:8]]
    value = {"version": 1, "target": {**target, "evidence": evidence}, "rules": selected_rules,
             "omitted_counts": {"evidence": max(0, len(target.get("evidence", [])) - len(evidence)),
                                "rules": max(0, len(target.get("principle_refs", [])) - len(selected_rules))},
             "limits": {"bytes": TARGET_CONTEXT_BYTES, "truncated": False}}
    return _fit(value, limit=TARGET_CONTEXT_BYTES, removable=("rules",))
