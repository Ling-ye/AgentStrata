from __future__ import annotations

from dataclasses import asdict
import json

from chatcopilot.contracts.agent import (
    ContextSnapshotPrepared,
    InputResourceReceipt,
    TurnError,
)
from chatcopilot.evals import capability_executor, isolated_executor, runner


def _sensitive_context_event() -> tuple[ContextSnapshotPrepared, str]:
    secret = "-".join(("synthetic", "context", "credential", "value"))
    event = ContextSnapshotPrepared(
        snapshot_id="ctx_eval_projection",
        backend="native",
        model="test-model",
        iteration=2,
        session_messages=({"role": "user", "content": secret},),
        effective_messages=({"role": "system", "content": secret},),
        tool_schemas=(
            {
                "type": "function",
                "function": {"name": "probe", "description": secret},
            },
        ),
        resources=(
            InputResourceReceipt(
                sequence=0,
                media_type=secret,
                size_bytes=12,
                sha256=secret,
            ),
        ),
        coverage="partial",
        omitted=("provider_private_reasoning", "local_resource_paths"),
        context_kind="sliding_window",
        trace_id="trace-eval",
        span_id="span-eval",
        parent_span_id="parent-eval",
        depth=1,
        estimated_tokens=321,
        model_selection={
            "lane": "code",
            "provider": "openai",
            "model": "test-model",
            "reasoning_effort": "high",
            "scope": "session",
            "source": "profile",
            "profile": "reviewed",
            "access_token": secret,
            "private_payload": {"value": secret},
        },
        private_reasoning_omission_count=3,
        resource_path_omission_count=2,
    )
    return event, secret


def test_context_snapshot_projection_omits_sensitive_bodies_in_all_eval_paths() -> None:
    event, secret = _sensitive_context_event()
    projections = (
        runner._event_to_dict(event),
        isolated_executor._event_to_dict(event),
        capability_executor._event_dict(event),
    )

    assert isolated_executor._event_to_dict is runner._event_to_dict
    for projected in projections:
        serialized = json.dumps(projected, ensure_ascii=False)
        assert secret not in serialized
        assert set(projected) == {
            "type",
            "snapshot_id",
            "backend",
            "model",
            "iteration",
            "coverage",
            "omitted",
            "context_kind",
            "trace_id",
            "span_id",
            "parent_span_id",
            "depth",
            "estimated_tokens",
            "model_selection",
            "message_count",
            "effective_message_count",
            "tool_schema_count",
            "resource_count",
            "private_reasoning_omission_count",
            "resource_path_omission_count",
        }
        assert projected["message_count"] == 1
        assert projected["effective_message_count"] == 1
        assert projected["tool_schema_count"] == 1
        assert projected["resource_count"] == 1
        assert projected["private_reasoning_omission_count"] == 3
        assert projected["resource_path_omission_count"] == 2
        assert projected["model_selection"] == {
            "lane": "code",
            "provider": "openai",
            "model": "test-model",
            "reasoning_effort": "high",
            "scope": "session",
            "source": "profile",
            "profile": "reviewed",
        }


def test_context_snapshot_mapping_reprojection_cannot_restore_sensitive_bodies() -> None:
    event, secret = _sensitive_context_event()
    omitted_overflow_secret = "-".join(
        ("synthetic", "omitted", "overflow", "credential")
    )
    bounded_omissions = ("o" * 10_000,) + tuple(
        f"safe-omission-{index}" for index in range(63)
    )
    unsafe_mapping = {"type": type(event).__name__, **asdict(event)}
    unsafe_mapping.update(
        {
            "snapshot_id": "s" * 10_000,
            "backend": "b" * 10_000,
            "model": "m" * 10_000,
            "iteration": 10**5000,
            "coverage": "c" * 10_000,
            "omitted": (*bounded_omissions, omitted_overflow_secret),
            "context_kind": "k" * 10_000,
            "estimated_tokens": float("nan"),
            "trace_id": "t" * 10_000,
            "span_id": "i" * 10_000,
            "parent_span_id": "p" * 10_000,
            "private_reasoning_omission_count": float("inf"),
            "resource_path_omission_count": 10**5000,
            "model_selection": {
                "model": "test-model",
                "provider": "p" * 10_000,
                "scope": float("nan"),
                "source": 10**5000,
                "profile": {"private": secret},
                "access_token": secret,
            },
        }
    )

    projected = capability_executor._event_dict(unsafe_mapping)

    serialized = json.dumps(projected, ensure_ascii=False, allow_nan=False)
    assert secret not in serialized
    assert omitted_overflow_secret not in serialized
    assert projected["snapshot_id"] == "s" * 512
    assert projected["backend"] == "b" * 512
    assert projected["model"] == "m" * 512
    assert projected["coverage"] == "c" * 512
    assert projected["context_kind"] == "k" * 512
    assert projected["trace_id"] == "t" * 512
    assert projected["span_id"] == "i" * 512
    assert projected["parent_span_id"] == "p" * 512
    assert len(projected["omitted"]) == 64
    assert projected["omitted"][0] == "o" * 512
    assert all(len(item) <= 512 for item in projected["omitted"])
    assert projected["message_count"] == 1
    assert projected["effective_message_count"] == 1
    assert projected["tool_schema_count"] == 1
    assert projected["resource_count"] == 1
    assert projected["iteration"] == (1 << 63) - 1
    assert projected["estimated_tokens"] == 0
    assert projected["private_reasoning_omission_count"] == 0
    assert projected["resource_path_omission_count"] == (1 << 63) - 1
    assert projected["model_selection"] == {
        "provider": "p" * 512,
        "model": "test-model",
    }


def test_non_context_event_projection_remains_compatible() -> None:
    event = TurnError(code="synthetic_error", message="safe diagnostic")
    expected = {
        "code": "synthetic_error",
        "message": "safe diagnostic",
        "type": "TurnError",
    }

    assert runner._event_to_dict(event) == expected
    assert isolated_executor._event_to_dict(event) == expected
    assert capability_executor._event_dict(event) == expected
    assert capability_executor._event_dict({"type": "CustomEvent", "value": 1}) == {
        "type": "CustomEvent",
        "value": 1,
    }
