"""Managed source -> supervised Trial -> bridge -> independent score -> comparison.

The model endpoint is a local controlled HTTP server; no provider account is used.
Git operations are confined to temporary repositories, never the working checkout.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time

from chatcopilot.core.source_snapshot import git_output, manifest_digest, source_manifest
from chatcopilot.evals.application import EvaluationApplication
from chatcopilot.evals.application.worker_runtime import LocalEvaluationWorker
from chatcopilot.evals.bridge_cases import SUITE_ID
from chatcopilot.evals.conditions import evaluation_conditions
from chatcopilot.evals.report import compare_reports
from tests.application_fixture import write_application

ROOT = Path(__file__).resolve().parents[2]


def test_managed_bridge_evaluation_binds_sources_and_keeps_candidate_out_of_scoring(tmp_path, monkeypatch):
    calls = []

    class Model(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(request["model"])
            if request.get("stream"):
                values = [
                    {"id": "fixture", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "桥接春风"}, "finish_reason": None}]},
                    {"id": "fixture", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                ]
                body = ("".join("data: " + json.dumps(v, ensure_ascii=False) + "\n\n" for v in values) + "data: [DONE]\n\n").encode()
                media = "text/event-stream"
            else:
                body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "桥接春风"}, "finish_reason": "stop"}]}).encode()
                media = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    repository = tmp_path / "repository"
    subprocess.run(["git", "clone", "--quiet", "--shared", str(ROOT), str(repository)], check=True)
    # Overlay current source, including new unstaged modules. The temporary HEAD is only a Git anchor.
    for name in source_manifest(ROOT):
        if name.startswith(("src/", "integrations/open_llm_vtuber/")):
            target = repository / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
    bot_root = repository / "bots/bridge-eval"
    bot_root.mkdir()
    (bot_root / "identity.md").write_text("Bridge assistant")
    (bot_root / "style.md").write_text("Reply concisely.")
    write_application(bot_root / "bot.yaml", {"id": "bridge-eval", "display_name": "Bridge eval",
        "gateway": {"clients": [{"id": "local", "token_env": "LOCAL_TOKEN", "role": "owner"}]},
        "agents": {"runtime": "langgraph"}, "tools": {"packs": []},
        "llm": {"chat": {"binding": "bridge.chat"}, "research": {"binding": "bridge.chat"}},
        "prompts": {"schema_version": 2, "identity": "identity.md", "response_style": "style.md"}})
    models = tmp_path / "models.json"
    models.write_text(json.dumps({"connections": {"fixture": {"kind": "openai_compatible",
        "base_url": f"http://127.0.0.1:{server.server_port}/v1", "auth": {"mode": "api_key", "key_env": "BRIDGE_KEY"}}},
        "profiles": {"fixture": {"connection": "fixture", "model": "local-model"}}, "bindings": {"bridge.chat": "fixture"}}))
    monkeypatch.setenv("AGENTSTRATA_LLM_CONFIG", str(models))
    monkeypatch.setenv("BRIDGE_KEY", "synthetic-bridge-credential")
    monkeypatch.setenv("CHATCOPILOT_SOURCE_URL", "https://example.com/source")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    application = EvaluationApplication(tmp_path / "evaluations", repository_root=repository, worker=LocalEvaluationWorker())
    pending = []
    preview = application.preview(bot_id="bridge-eval", request={"kind": "suite", "suite_id": SUITE_ID,
        "preset": "custom", "case_ids": ["live2d-dialogue"], "repetitions": 1, "max_wall_seconds": 90})
    assert preview["ready"] and preview["conditions"]["cases"]
    assert application.list() == []

    def evaluate(identifier, source, conditions=None):
        record = application.start(bot_id="bridge-eval", evaluation_id=identifier,
            request={"kind": "suite", "suite_id": SUITE_ID, "preset": "custom", "case_ids": ["live2d-dialogue"],
                     "repetitions": 1, "max_wall_seconds": 90},
            code_source={"path": str(source), "sha256": manifest_digest(source_manifest(source))},
            expected_conditions=conditions)
        pending.append(identifier)
        deadline = time.monotonic() + 90
        while record["status"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.1)
            record = application.get(identifier)
        assert record["status"] == "completed", (record, (application.root / identifier / "run.log").read_text())
        return application.result_store.get(identifier)["result"]

    try:
        baseline = evaluate("eval-bridge-base", repository, preview["conditions"])
        from chatcopilot.core.release_identity import model_identity
        import os
        assert baseline["config_snapshot"]["activation_model_identity"] == model_identity(bot_root / "bot.yaml", dict(os.environ))
        assert baseline["trials"][0]["outcome"] == "passed", baseline
        candidate = tmp_path / "candidate"
        git_output(repository, "worktree", "add", "--detach", str(candidate), "HEAD")
        for name in source_manifest(repository):
            target = candidate / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(repository / name, target)
        bridge = candidate / "integrations/open_llm_vtuber/agentstrata_vtuber/bridge.py"
        before = bridge.read_text()
        modified = before.replace('"".join(segment.text for segment in run.segments if isinstance(segment, TextRpcSegment))', '"corrupted client reply"')
        assert modified != before
        bridge.write_text(modified)
        # The service must retain its own scorer even when candidate files try to replace it.
        (candidate / "src/chatcopilot/evals/bridge_scoring.py").write_text("raise RuntimeError('candidate scorer must never load')\n")
        changed = evaluate("eval-bridge-candidate", candidate, evaluation_conditions(baseline["config_snapshot"], baseline["targets"]))
        assert changed["trials"][0]["outcome"] == "failed", changed
        assert changed["trials"][0]["assessment"]["evidence"]["failure_kind"] == "bridge_error"
        base_snapshot, new_snapshot = (r["config_snapshot"]["version_snapshot"] for r in (baseline, changed))
        assert base_snapshot["snapshot_ref"] != new_snapshot["snapshot_ref"]
        assert base_snapshot["verification_sha256"] == new_snapshot["verification_sha256"]
        base_file, new_file = tmp_path / "base.json", tmp_path / "new.json"
        base_file.write_text(json.dumps(baseline))
        new_file.write_text(json.dumps(changed))
        comparison = compare_reports(base_file, new_file)
        assert comparison["comparison_status"] == "regressed"
        assert calls == ["local-model"] * 4
    finally:
        for identifier in pending:
            if application.get(identifier)["status"] in {"queued", "running"}:
                application.cancel(identifier)
        server.shutdown()
        server.server_close()
        serving.join(5)
