"""Bounded AgentBench FC controller transport; never uses an upstream model loop."""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from chatcopilot.evals.models import EvalCase, JudgeResult

TASKS = frozenset({"dbbench-std", "os-std", "kg-std", "alfworld-std", "webshop-std"})
_MAX_BYTES = 1024 * 1024


def controller_url() -> str:
    value = os.environ.get("CHATCOPILOT_AGENTBENCH_CONTROLLER_URL", "").strip()
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path.rstrip("/") != "/api"):
        raise ValueError("AgentBench requires a configured loopback HTTP controller /api endpoint")
    return value.rstrip("/") + "/"


def load_cases() -> tuple[EvalCase, ...]:
    value = os.environ.get("CHATCOPILOT_AGENTBENCH_DATA_PATH", "").strip()
    if not value:
        return ()
    path = Path(value).expanduser()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * _MAX_BYTES:
        raise ValueError("AgentBench task catalog must be a bounded regular JSONL file")
    cases = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        task, index = row.get("task"), row.get("index")
        if task not in TASKS or type(index) not in (str, int) or isinstance(index, str) and not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", index) or type(index) is int and index < 0:
            raise ValueError("invalid AgentBench task/index")
        revision = row.get("source_revision")
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("AgentBench catalog requires an immutable source_revision")
        prompt = row.get("input")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 128 * 1024:
            raise ValueError("AgentBench catalog requires a bounded task preview")
        case_id = f"{task}:{index}"
        if case_id in seen:
            raise ValueError("duplicate AgentBench task/index")
        seen.add(case_id)
        cases.append(EvalCase(capability_tags=("环境交互", "工具调用"), case_id=case_id, input=prompt, category=task,
            expected_behavior="Complete the task through the environment's declared tools.",
            metadata={"adapter": "agentbench-fc", "source": "AgentBench FC local catalog", "source_revision": revision,
                      "task": task, "index": index}))
    return tuple(cases)


class Controller:
    def __init__(self) -> None:
        self.url = controller_url()
        self.client = requests.Session()
        self.client.trust_env = False
        self.session_id: int | None = None

    def request(self, operation: str, payload: dict[str, Any] | None = None) -> tuple[Any, Any]:
        if operation not in {"start_sample", "interact", "cancel"}:
            raise ValueError("unsupported AgentBench operation")
        headers = {"session_id": str(self.session_id)} if self.session_id is not None else {}
        with self.client.post(self.url + operation, json=payload, headers=headers,
                              timeout=(5, 45), allow_redirects=False, stream=True) as response:
            if operation == "cancel" and response.status_code in {404, 410}:
                return {}, None
            if operation == "cancel" and response.status_code == 400:
                content = response.raw.read(_MAX_BYTES + 1, decode_content=True)
                if len(content) <= _MAX_BYTES and json.loads(content) == {"message": "session not found"}:
                    return {}, None
            if not 200 <= response.status_code < 300:
                raise ValueError(f"AgentBench controller failed with HTTP {response.status_code}")
            if operation == "start_sample":
                session = response.headers.get("session_id", "")
                if not re.fullmatch(r"[0-9]{1,18}", session):
                    raise ValueError("AgentBench did not return a valid session identity")
                self.session_id = int(session)
                from chatcopilot.evals.trial_capture import record_environment

                record_environment({"kind": "agentbench-fc", "session_id": self.session_id,
                                    "controller_fingerprint": hashlib.sha256(self.url.encode()).hexdigest()})
            content = response.raw.read(_MAX_BYTES + 1, decode_content=True)
            if len(content) > _MAX_BYTES:
                raise ValueError("AgentBench response exceeds evidence budget")
            body = json.loads(content) if content else {}
            return body, response.headers.get("session_id")

    def start(self, case: EvalCase) -> dict[str, Any]:
        response, session = self.request("start_sample", {"name": case.metadata["task"], "index": case.metadata["index"], "custom_task": None})
        if not isinstance(session, str) or not re.fullmatch(r"[0-9]{1,18}", session):
            raise ValueError("AgentBench did not return a valid session identity")
        self.session_id = int(session)
        # The upstream start_sample contract returns only messages/tools, not
        # an interact completion frame. Do not generalize this to interact.
        if (not isinstance(response, dict) or not isinstance(response.get("messages"), list)
                or not isinstance(response.get("tools"), list)):
            raise ValueError("AgentBench start response lacks messages/tools")
        session_state = self.sessions().get(str(self.session_id))
        if (not isinstance(session_state, dict) or session_state.get("name") != case.metadata["task"]
                or type(session_state.get("index")) is not type(case.metadata["index"])
                or session_state.get("index") != case.metadata["index"]):
            raise ValueError("AgentBench environment ended during initialization; inspect the task worker logs")
        return {**response, "finish": False}

    def call(self, name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
        response, _ = self.request("interact", {"messages": [{"role": "assistant", "content": "", "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}]})
        observation = validate_observation(response)
        if observation["finish"]:
            self.session_id = None  # AgentRL removes terminal sessions itself.
        return observation

    def workers(self) -> dict[str, Any]:
        return self._read_catalog("list_workers")

    def sessions(self) -> dict[str, Any]:
        return self._read_catalog("list_sessions")

    def _read_catalog(self, operation: str) -> dict[str, Any]:
        with self.client.get(self.url + operation, timeout=(3, 5), allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise ValueError(f"AgentBench worker query failed with HTTP {response.status_code}")
            content = response.raw.read(_MAX_BYTES + 1, decode_content=True)
            if len(content) > _MAX_BYTES:
                raise ValueError("AgentBench worker catalog exceeds evidence budget")
            result = json.loads(content)
            if not isinstance(result, dict):
                raise ValueError("AgentBench worker catalog is invalid")
            return result

    def close(self) -> None:
        try:
            if self.session_id is not None:
                self.request("cancel")
                self.session_id = None
        finally:
            self.client.close()


def validate_observation(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or type(value.get("finish")) is not bool:
        raise ValueError("AgentBench controller response lacks completion state")
    if not value["finish"] and not isinstance(value.get("messages"), list):
        raise ValueError("AgentBench running response lacks messages")
    if value["finish"] and (type(value.get("reward")) not in (int, float) or not math.isfinite(value["reward"])):
        raise ValueError("AgentBench completed response lacks a finite reward")
    return value


def case_readiness(cases: tuple[EvalCase, ...]) -> dict[str, dict[str, Any]]:
    """Read online worker/index state without starting an environment or model."""
    workers: dict[str, Any] = {}
    failure = ""
    controller = None
    try:
        controller = Controller()
        workers = controller.workers()
    except (ValueError, requests.RequestException) as exc:
        failure = f"AgentBench Controller 未就绪：{type(exc).__name__}。请启动本地 DB/OS 环境。"
    finally:
        if controller:
            controller.client.close()
    result = {}
    for case in cases:
        task = case.metadata["task"]
        row = workers.get(task, {})
        if not isinstance(row, dict):
            row = {}
        pool = row.get("workers", {})
        pool = pool if isinstance(pool, dict) else {}
        alive = [w for w in pool.values() if isinstance(w, dict) and w.get("status") == "ALIVE" and w.get("stale") is False]
        idle = [w for w in alive if type(w.get("capacity")) is int and type(w.get("current")) is int
                and w["capacity"] > w["current"] >= 0]
        indices = row.get("indices", [])
        matched = isinstance(indices, list) and any(type(i) is type(case.metadata["index"]) and i == case.metadata["index"] for i in indices)
        reason = failure or (f"未启动 {task} worker" if not alive else "所选题目不在当前 worker 的索引中" if not matched else "任务 worker 正忙，请稍后重试" if not idle else "")
        result[case.case_id] = {"ready": not reason, "state": "ready" if not reason else "not_prepared",
                                "missing_tools": [], "environment": f"AgentBench FC / {task}", "reason": reason}
    return result


def judge_response(value: dict[str, Any]) -> JudgeResult:
    if not value.get("finish"):
        return JudgeResult(0.0, 1.0, False, ("Agent stopped before environment completion",))
    if value.get("status") in {"task error", "cancelled", "server error"}:
        raise ValueError(f"AgentBench environment error: {value['status']}")
    if value.get("status") not in {"completed", "agent validation failed", "agent invalid action", "task limit reached"}:
        raise ValueError("AgentBench returned an unknown terminal status")
    reward = value["reward"]
    return JudgeResult(float(reward), 1.0, reward >= 1.0,
                       (f"Environment reward={reward}; status={value.get('status', 'unknown')}",))
