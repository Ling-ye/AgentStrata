#!/usr/bin/env python3
"""Run a frozen repair sample with real coding models and publication disabled.

Invoke this script in separate processes with PYTHONPATH bound to each runtime
checkout. Both executions must use the same sample, model, effort and budget.
The sample names an existing disposable Git fixture, frozen source evidence and
expected outcome. This does not mutate a production task or call GitHub.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--variant", choices=("baseline", "candidate"), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()
    from chatcopilot.core.private_sqlite import private_directory
    from chatcopilot.core.source_snapshot import git_output
    from chatcopilot.harness.codex_adapter import CodexCoder
    from chatcopilot.harness.config import configuration
    from chatcopilot.harness.local_verifier import LocalVerifier
    from chatcopilot.harness.models import PIPELINE_VERSION, RepairOptions
    from chatcopilot.harness.store import HarnessStore
    from chatcopilot.harness.verification import CaseVerification
    import os
    os.umask(0o077)
    settings = configuration()
    for key in ("CHATCOPILOT_CODEX_BIN", "CHATCOPILOT_CODEX_BOT_HOME"):
        if settings.get(key):
            os.environ[key] = settings[key]
    sample_bytes = args.sample.read_bytes()
    sample = json.loads(sample_bytes)
    repository = Path(sample["repository"]).resolve(strict=True)
    base = git_output(repository, "rev-parse", "HEAD")
    if base != sample["base_commit"] or git_output(repository, "status", "--porcelain"):
        raise ValueError("comparison sample repository must match its frozen clean baseline")
    ident = "repair-" + hashlib.sha256((args.variant + sample["id"] + str(args.output.resolve())).encode()).hexdigest()[:32]
    private_directory(args.output)
    store = HarnessStore(args.output)
    options = RepairOptions(args.model, args.effort, 3, args.timeout)
    task, created = store.create({"task_id": ident, "pipeline_version": PIPELINE_VERSION,
        "request_key": ident, "request_digest": ident, "context_key": ident, "match_key": ident,
        "active_key": ident, "repository": str(repository), "base_commit": base,
        "source": sample["source"], "options": asdict(options)})
    if not created:
        raise ValueError("use a fresh output directory; comparison never resumes or replays an uncertain model call")

    class LocalOnly:
        def capabilities(self):
            return {"local_pytest": True, "agent_evaluation": False, "fixtures": [], "external_network": False}

    if PIPELINE_VERSION >= 9:
        from chatcopilot.harness.artifact_repository import ArtifactRepository
        from chatcopilot.harness.repair_runtime import run_task
        from chatcopilot.harness import models
        artifacts = ArtifactRepository(store.root / "jobs" / ident)
        store.update(ident, principles=asdict(artifacts.principles(Path(models.__file__).resolve().parents[3])))
    else:
        from chatcopilot.harness.workflow import run_task
    runner = CodexCoder(lambda path, ref: store.register_trace(ident, path, ref))
    verifier = CaseVerification(LocalOnly(), LocalVerifier(store.root), store)
    started = time.monotonic()
    result = run_task(store, ident, verifier, runner)
    attempts = store.attempts(ident)
    executions = []
    if PIPELINE_VERSION >= 9:
        for step in store.flow_steps(ident):
            ref = step.get("evidence", {}).get("execution")
            if ref:
                executions.append(artifacts.read(ref))
    else:
        for attempt in attempts:
            executions.extend(value for value in [attempt.get("coding"), attempt.get("review", {}).get("execution")] if value)
    usage = {}
    for execution in executions:
        for name, value in execution.get("usage", {}).items():
            if isinstance(value, int):
                usage[name] = usage.get(name, 0) + value
    report = {"sample_id": sample["id"], "sample_sha256": hashlib.sha256(sample_bytes).hexdigest(),
        "sample_kind": sample.get("kind", "supplied"), "variant": args.variant, "pipeline": PIPELINE_VERSION,
        "runtime": str(Path(sys.modules[run_task.__module__].__file__).resolve()), "model": args.model,
        "effort": args.effort, "budget_seconds": args.timeout, "elapsed_seconds": time.monotonic() - started,
        "status": result["status"], "error_code": result.get("error_code"), "message": result.get("message"),
        "expected_status": sample["expected_status"], "expected_outcome": result["status"] == sample["expected_status"],
        "false_acceptance": result["status"] == "fixed" and sample["expected_status"] != "fixed",
        "attempts": len(attempts), "model_calls_with_usage": sum(bool(e.get("usage")) for e in executions), "usage": usage,
        "publication_enabled": False, "task_id": ident}
    path = args.output / "comparison.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["expected_outcome"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
