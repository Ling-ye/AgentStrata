from __future__ import annotations

import importlib.util
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

from acp import PromptResponse

from chatcopilot.contracts.identity import Role
from chatcopilot.middleware.acp.turn_orchestrator import AcpTurnOrchestrator
from chatcopilot.middleware.acp.turn_pipeline import (
    CallbackTurnHandler,
    OrderedTurnPipeline,
    TURN_STAGE_ORDER,
    TurnContext,
    TurnOutcome,
)


def _handler(name: str, seen: list[str], *, outcome: TurnOutcome | None = None):
    async def callback(_context: TurnContext) -> TurnOutcome:
        seen.append(name)
        return outcome or TurnOutcome()

    return CallbackTurnHandler(name=name, callback=callback)


class AcpTurnOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_order_runs_all_handlers(self) -> None:
        seen: list[str] = []
        pipeline = OrderedTurnPipeline(tuple(_handler(name, seen) for name in TURN_STAGE_ORDER))
        context = TurnContext("sid", object(), "hello", "mid")

        outcome = await pipeline.run(context)

        self.assertFalse(outcome.stop)
        self.assertEqual(tuple(seen), TURN_STAGE_ORDER)
        self.assertEqual(tuple(context.completed_stages), TURN_STAGE_ORDER)

    async def test_short_circuit_stops_after_current_stage(self) -> None:
        seen: list[str] = []
        response = PromptResponse(stop_reason="end_turn")
        handlers = []
        for name in TURN_STAGE_ORDER:
            outcome = TurnOutcome(response=response, stop=True, reason="matched") if name == "deterministic_shortcuts" else None
            handlers.append(_handler(name, seen, outcome=outcome))
        context = TurnContext("sid", object(), "status", None)

        result = await OrderedTurnPipeline(tuple(handlers)).run(context)

        self.assertIs(result.response, response)
        self.assertEqual(result.reason, "matched")
        self.assertEqual(
            seen,
            [
                "identity",
                "admission",
                "command_authorization",
                "operator_shortcuts",
                "attachments",
                "deterministic_shortcuts",
            ],
        )
        self.assertEqual(context.completed_stages, seen)

    async def test_operator_shortcut_stops_before_attachments(self) -> None:
        seen: list[str] = []
        response = PromptResponse(stop_reason="end_turn")
        handlers = [
            _handler(
                name,
                seen,
                outcome=(
                    TurnOutcome(response=response, stop=True, reason="operator")
                    if name == "operator_shortcuts"
                    else None
                ),
            )
            for name in TURN_STAGE_ORDER
        ]

        result = await OrderedTurnPipeline(tuple(handlers)).run(
            TurnContext("sid", object(), "/state", None)
        )

        self.assertIs(result.response, response)
        self.assertEqual(result.reason, "operator")
        self.assertEqual(
            seen,
            [
                "identity",
                "admission",
                "command_authorization",
                "operator_shortcuts",
            ],
        )

    async def test_handler_exception_is_not_silently_swallowed(self) -> None:
        seen: list[str] = []

        async def fail(_context: TurnContext) -> TurnOutcome:
            raise RuntimeError("boom")

        handlers = [_handler(name, seen) for name in TURN_STAGE_ORDER]
        handlers[1] = CallbackTurnHandler("admission", fail)
        with self.assertRaisesRegex(RuntimeError, "boom"):
            await OrderedTurnPipeline(tuple(handlers)).run(
                TurnContext("sid", object(), "hello", None)
            )
        self.assertEqual(seen, ["identity"])

    async def test_context_is_passed_without_reconstruction(self) -> None:
        identities: list[int] = []

        def handler(name: str) -> CallbackTurnHandler:
            async def callback(context: TurnContext) -> TurnOutcome:
                identities.append(id(context))
                context.metadata[name] = True
                return TurnOutcome()

            return CallbackTurnHandler(name, callback)

        context = TurnContext("sid", object(), "hello", None)
        await OrderedTurnPipeline(tuple(handler(name) for name in TURN_STAGE_ORDER)).run(context)
        self.assertEqual(set(identities), {id(context)})
        self.assertEqual(set(context.metadata), set(TURN_STAGE_ORDER))

    async def test_non_stopping_response_reaches_finish(self) -> None:
        seen: list[str] = []
        response = PromptResponse(stop_reason="end_turn")
        handlers = [
            _handler(
                name,
                seen,
                outcome=TurnOutcome(response=response) if name == "finish" else None,
            )
            for name in TURN_STAGE_ORDER
        ]
        result = await OrderedTurnPipeline(tuple(handlers)).run(
            TurnContext("sid", object(), "hello", None)
        )
        self.assertIs(result.response, response)
        self.assertEqual(seen[-1], "finish")

    async def test_every_non_owner_slash_command_stops_before_attachments(self) -> None:
        updates: list[str] = []
        finishes: list[dict[str, object]] = []

        class Connection:
            async def session_update(self, *, session_id: str, update: str) -> None:
                assert session_id == "sid"
                updates.append(update)

        class Recorder:
            task_id = "task-owner-command"

            def __init__(self) -> None:
                self.events: list[tuple[str, dict[str, object]]] = []

            def record_event(self, kind: str, payload: dict[str, object]) -> None:
                self.events.append((kind, payload))

        recorder = Recorder()
        host = SimpleNamespace(
            _conn=Connection(),
            _finish_turn_task=lambda _recorder, **kwargs: finishes.append(kwargs),
        )
        orchestrator = AcpTurnOrchestrator.__new__(AcpTurnOrchestrator)
        orchestrator._host = host
        orchestrator._update_text = lambda text: text
        commands = (
            "/help",
            "/state",
            "/restart",
            "/debug status",
            "/model code",
            "/task",
            "/cancel",
            "/persona confirm",
            "/unknown",
        )
        for command in commands:
            with self.subTest(command=command):
                updates.clear()
                finishes.clear()
                recorder.events.clear()
                turn = TurnContext(
                    "sid",
                    SimpleNamespace(role=Role.USER),
                    command,
                    "message",
                    turn_task=recorder,
                )

                outcome = await orchestrator._command_authorization(turn)

                self.assertTrue(outcome.stop)
                self.assertEqual(outcome.reason, "owner_command_required")
                self.assertEqual(updates, ["斜杠指令仅限 Owner 使用。"])
                self.assertEqual(
                    finishes[0]["stop_reason"],
                    "owner_command_required",
                )
                self.assertEqual(
                    recorder.events[0][1]["kind"],
                    "middleware.command_authorization",
                )

    async def test_owner_slash_command_continues_and_absolute_path_is_not_a_command(
        self,
    ) -> None:
        orchestrator = AcpTurnOrchestrator.__new__(AcpTurnOrchestrator)
        orchestrator._host = SimpleNamespace()
        orchestrator._update_text = lambda text: text
        orchestrator._record_flow_transition = lambda *_args, **_kwargs: None

        allowed = await orchestrator._command_authorization(
            TurnContext("sid", SimpleNamespace(role=Role.OWNER), "/help", None)
        )
        path = await orchestrator._command_authorization(
            TurnContext("sid", SimpleNamespace(role=Role.USER), "/tmp/report.txt", None)
        )

        self.assertFalse(allowed.stop)
        self.assertFalse(path.stop)

    def test_pipeline_rejects_missing_handler(self) -> None:
        with self.assertRaisesRegex(ValueError, "fixed stage order"):
            OrderedTurnPipeline(tuple(_handler(name, []) for name in TURN_STAGE_ORDER[:-1]))

    def test_pipeline_rejects_duplicate_handler(self) -> None:
        names = list(TURN_STAGE_ORDER)
        names[-1] = names[-2]
        with self.assertRaisesRegex(ValueError, "fixed stage order"):
            OrderedTurnPipeline(tuple(_handler(name, []) for name in names))

    def test_context_rejects_manual_out_of_order_completion(self) -> None:
        context = TurnContext("sid", object(), "hello", None)
        with self.assertRaisesRegex(RuntimeError, "expected 'identity'"):
            context.complete("execution")

    def test_context_metadata_has_no_shared_mutable_default(self) -> None:
        first = TurnContext("a", object(), "one", None)
        second = TurnContext("b", object(), "two", None)
        first.metadata["x"] = 1
        self.assertEqual(second.metadata, {})

    def test_outcome_is_immutable(self) -> None:
        outcome = TurnOutcome(reason="stable")
        with self.assertRaises(FrozenInstanceError):
            outcome.reason = "changed"  # type: ignore[misc]

    def test_legacy_cross_backend_modules_are_removed(self) -> None:
        self.assertIsNone(importlib.util.find_spec("chatcopilot.middleware.acp.code_route"))
        self.assertIsNone(importlib.util.find_spec("chatcopilot.middleware.acp.route_orchestrator"))

    def test_server_has_no_cross_backend_router_reference(self) -> None:
        root = Path(__file__).resolve().parents[2]
        source = (root / "src/chatcopilot/middleware/acp/server.py").read_text(encoding="utf-8")
        self.assertNotIn("route_orchestrator", source)
        self.assertNotIn("run_code_route", source)

    def test_server_delegates_all_real_stages_without_noop_handlers(self) -> None:
        root = Path(__file__).resolve().parents[2]
        source = (root / "src/chatcopilot/middleware/acp/server.py").read_text(
            encoding="utf-8"
        )
        orchestrator = (
            root / "src/chatcopilot/middleware/acp/turn_orchestrator.py"
        ).read_text(encoding="utf-8")
        self.assertIn("AcpTurnOrchestrator", source)
        self.assertNotIn("already_completed", source)
        for stage in TURN_STAGE_ORDER:
            self.assertIn(f'"{stage}"', orchestrator)
            self.assertIn(f"async def _{stage}", orchestrator)


if __name__ == "__main__":
    unittest.main()
