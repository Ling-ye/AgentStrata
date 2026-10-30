"""Exercise a delivered source copy with producer checkouts hidden and no external network."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import tempfile

import pytest

from chatcopilot.core.application_configuration import load_application_spec
from chatcopilot.core.source_snapshot import git_output, manifest_digest, source_manifest
from chatcopilot.contracts.deployment import ReleaseSource
from chatcopilot.deployment.application_bundle import export_bundle
from chatcopilot.deployment.application_runtime import install_bundle


ROOT = Path(__file__).resolve().parents[2]

# The acceptance object is a fixture; this test verifies distribution/runtime independence,
# not real worker performance or model quality. The existing Harness tests own acceptance.
PROBE = r'''
from pathlib import Path
import json, os, sys, threading, time
area = Path(sys.argv[1])
root = area / "installed"
hidden = [Path(path) for path in json.loads(sys.argv[2])]
sys.path[:] = [str(root), str(root / "src"), str(area / "dependencies"), *[
    path for path in sys.path if path and not any(Path(path).is_relative_to(parent) for parent in hidden)]]
os.environ.update(CHATCOPILOT_SOURCE_ROOT=str(root), CHATCOPILOT_HARNESS_ROOT=str(area / "state/harness"),
    CHATCOPILOT_HARNESS_ENV=str(area / "state/absent-harness.env"),
    CHATCOPILOT_EVALUATION_SOCKET=str(area / "state/eval.sock"),
    AGENTSTRATA_PLUGINS_ROOT=str(area / "state/plugins"), AGENTSTRATA_LLM_CONFIG=str(area / "state/models.json"),
    TMPDIR=str(area / "state"))
from chatcopilot.core.application_installation import application_identity
from console.control.discovery import discover_instances, repo_root
from console.backend.harness_runtime import create_controller
from chatcopilot.evals.service.server import EvaluationUnixServer, build_runtime
from chatcopilot.evals.service.client import EvaluationServiceClient
from chatcopilot.gateway.state_store import GatewayStateStore
from chatcopilot.gateway.observation_runtime import ObservationRecorder
identity = application_identity(root)
assert identity["source_digest"] == identity["delivered_digest"]
assert repo_root() == root
assert [item.instance_id for item in discover_instances()] == [identity["bot_id"]]
controller = create_controller()
assert controller.repository == root
assert [item["bot_id"] for item in controller.applications()["applications"]] == [identity["bot_id"]]
runtime = build_runtime(repository_root=root, artifact_root=area / "state/evaluations")
server = EvaluationUnixServer(area / "state/eval.sock", runtime)
stop = threading.Event()
thread = threading.Thread(target=server.serve, args=(stop,), daemon=True)
thread.start()
try:
    client = EvaluationServiceClient(area / "state/eval.sock", timeout_seconds=1)
    for attempt in range(50):
        try:
            assert client.health()["ready"] is True
            break
        except Exception:
            if attempt == 49:
                raise
            time.sleep(.05)
finally:
    stop.set()
    thread.join(5)
    server.close()
store = GatewayStateStore(area / "state/gateway")
generation = store.acquire_writer_generation()
recorder = ObservationRecorder(store, generation)
assert recorder.application_version == {"bot_id": identity["bot_id"], "source_digest": identity["source_digest"]}
import pytest
code = pytest.main([str(root / "tests/integration/test_application_bridge_baseline.py"), "-q", "-k", "each_bridge and qq",
                   "-p", "no:cacheprovider", "--basetemp", str(area / "state/bridge-tests")])
origins = [Path(module.__file__).resolve() for name, module in sys.modules.items()
           if (name == "chatcopilot" or name.startswith("chatcopilot.") or name == "console" or name.startswith("console."))
           and getattr(module, "__file__", None)]
assert origins and all(path.is_relative_to(root) for path in origins)
print(json.dumps({"source_digest": identity["source_digest"], "installed_modules": len(origins),
                  "bridge_exit": code, "real_models": False, "real_qq": False}))
raise SystemExit(code)
'''


@pytest.mark.skipif(sys.platform != "linux" or shutil.which("bwrap") is None, reason="Linux Bubblewrap installation boundary")
def test_application_runs_without_producer_checkout_or_delivery_directory():
    manifest = source_manifest(ROOT)
    relative = "bots/lingye-copilot-qq/bot.yaml"
    app = load_application_spec(ROOT / relative)
    bot_id = app.raw.get("deploy", {}).get("instance_id") or app.raw["id"]
    receipt = {"candidate_digest": manifest_digest(manifest), "goal_digest": "synthetic-distribution-fixture",
        "verification_digest": "synthetic-distribution-fixture", "review_binding": "synthetic-distribution-fixture", "attempt": 1}
    source = ReleaseSource("synthetic-distribution-fixture", bot_id, ROOT, manifest, ROOT, manifest,
        receipt, relative, {"fixture": "offline-distribution"}, "synthetic-no-model")
    common = Path(git_output(ROOT, "rev-parse", "--path-format=absolute", "--git-common-dir")).parent
    hidden = tuple(dict.fromkeys((ROOT, common)))
    with tempfile.TemporaryDirectory(prefix="as-app-", dir="/tmp") as directory:
        area = Path(directory)
        (area / "dependencies").mkdir()
        (area / "state").mkdir(mode=0o700)
        export_bundle(source, area / "bundle")
        install_bundle(area / "bundle", area / "installed")
        shutil.rmtree(area / "bundle")
        command = ["bwrap", "--die-with-parent", "--new-session", "--unshare-net", "--ro-bind", "/", "/",
            "--dev", "/dev", "--proc", "/proc", "--ro-bind", sysconfig.get_path("purelib"), str(area / "dependencies")]
        for path in hidden:
            command.extend(("--tmpfs", str(path)))
        command.extend(("--bind", str(area / "state"), str(area / "state"), "--chdir", str(area / "installed"),
            str(Path(sys.base_prefix) / "bin/python3"), "-I", "-B", "-c", PROBE, str(area), json.dumps(list(map(str, hidden)))))
        completed = subprocess.run(command, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, text=True, timeout=180)
        assert completed.returncode == 0, completed.stdout + "\n" + completed.stderr
        summary = json.loads(completed.stdout.splitlines()[-1])
        assert summary["source_digest"] == receipt["candidate_digest"]
        assert summary["bridge_exit"] == 0 and summary["installed_modules"] > 0
