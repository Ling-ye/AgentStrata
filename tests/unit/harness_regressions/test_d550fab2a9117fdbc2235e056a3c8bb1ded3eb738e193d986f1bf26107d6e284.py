"""Deterministic regressions for the installed Live2D desktop observer path."""

import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from integrations.open_llm_vtuber.agentstrata_vtuber.bridge import Reply
from integrations.open_llm_vtuber.agentstrata_vtuber.perception import DesktopObserver
from integrations.open_llm_vtuber.agentstrata_vtuber import runtime


A, B, C = (character * 64 for character in "abc")


class HeldGatewayReplies:
    """Only the external Gateway turn is held; DesktopObserver is production code."""

    def __init__(self):
        self.prompts = []
        self.pending = []
        self.closed = False

    def select_history(self, configuration_id, history_id):
        self.history = configuration_id, history_id

    async def chat(self, prompt):
        self.prompts.append(prompt)
        future = asyncio.get_running_loop().create_future()
        self.pending.append(future)
        return await future

    async def close(self):
        self.closed = True

    def finish(self, index, text):
        self.pending[index].set_result(Reply("session", f"run-{index}", text, "completed"))


async def started(bridge, count):
    for _ in range(10):
        if len(bridge.pending) >= count:
            return
        await asyncio.sleep(0)
    assert len(bridge.pending) == count


def install_audio_boundary(monkeypatch):
    """Replace only the upstream VTuber audio dependency used by runtime.attach."""
    for name in ("open_llm_vtuber", "open_llm_vtuber.agent", "open_llm_vtuber.utils"):
        package = ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    output = ModuleType("open_llm_vtuber.agent.output_types")
    output.DisplayText = lambda text, name: SimpleNamespace(text=text, name=name)
    audio = ModuleType("open_llm_vtuber.utils.stream_audio")
    audio.prepare_audio_payload = lambda path, *, display_text: {
        "type": "audio", "path": path, "text": display_text.text,
    }
    monkeypatch.setitem(sys.modules, output.__name__, output)
    monkeypatch.setitem(sys.modules, audio.__name__, audio)


@pytest.mark.parametrize("changes,latest", [((B,), B), ((B, A), A)])
def test_latest_digest_supersedes_slow_observation_without_speaking_stale_reply(
    monkeypatch, changes, latest
):
    async def scenario():
        install_audio_boundary(monkeypatch)
        now = [0.0]
        bridge = HeldGatewayReplies()
        observer = DesktopObserver(bridge, clock=lambda: now[0])
        sent, synthesized = [], []

        async def send(raw):
            sent.append(json.loads(raw))

        class TTS:
            async def async_generate_audio(self, *, text, file_name_no_ext):
                synthesized.append(text)
                return "/tmp/synthetic-notice.wav"

            def remove_file(self, path):
                assert path == "/tmp/synthetic-notice.wav"

        context = SimpleNamespace(
            tts_engine=TTS(),
            character_config=SimpleNamespace(character_name="test avatar"),
        )
        monkeypatch.setattr(runtime, "_observer", observer)
        monkeypatch.setattr(runtime, "_services", (SimpleNamespace(mcp_token=None), SimpleNamespace()))
        async def prepare_without_worker(func, *args, **kwargs):
            return func(*args, **kwargs)
        monkeypatch.setattr(runtime.asyncio, "to_thread", prepare_without_worker)
        runtime.attach("avatar-test", "test-model", send, context)
        assert observer.present is not None
        assert bridge.history == ("desktop-observer", "primary-display")
        try:
            first_state = (await observer.update(True, A))["state"]
            await started(bridge, 1)
            first = observer._task
            change_states = [(await observer.update(True, digest))["state"] for digest in changes]
            queued_while_running = len(bridge.pending)
            bridge.finish(0, "旧画面提醒")
            await first
            stale_sent, stale_synthesized = list(sent), list(synthesized)

            now[0] = 29.9
            await observer.update(True, latest)
            calls_before_cooldown = len(bridge.pending)
            now[0] = 30.0
            cooldown_state = (await observer.update(True, latest))["state"]
            await asyncio.sleep(0)
            if len(bridge.pending) == 2:
                second = observer._task
                bridge.finish(1, "最新画面提醒")
                await second
            calls_after_cooldown = len(bridge.pending)
            final_sent, final_synthesized = list(sent), list(synthesized)
            now[0] = 90.0
            await observer.update(True, latest)
            replayed_calls = len(bridge.pending)
            return (first_state, change_states, queued_while_running,
                    stale_sent, stale_synthesized, calls_before_cooldown,
                    cooldown_state, calls_after_cooldown, final_sent,
                    final_synthesized, replayed_calls)
        finally:
            await observer.close()

    (first_state, change_states, queued_while_running,
     stale_sent, stale_synthesized, calls_before_cooldown,
     cooldown_state, calls_after_cooldown, final_sent,
     final_synthesized, replayed_calls) = asyncio.run(scenario())
    assert first_state == "analyzing"
    assert change_states == ["analyzing"] * len(changes)
    assert queued_while_running == calls_before_cooldown == 1
    assert stale_sent == [] and stale_synthesized == []
    assert cooldown_state == "analyzing" and calls_after_cooldown == 2
    assert [item["type"] for item in final_sent] == [
        "agentstrata-reply", "desktop-notice", "audio", "backend-synth-complete"
    ]
    assert final_sent[1]["text"] == final_sent[2]["text"] == "最新画面提醒"
    assert final_synthesized == ["最新画面提醒"]
    assert replayed_calls == 2


def test_foreground_and_hide_cancel_pending_notice_without_replay():
    async def scenario():
        now = [0.0]
        bridge = HeldGatewayReplies()
        observer = DesktopObserver(bridge, clock=lambda: now[0])
        notices = []

        async def present(*args):
            notices.append(args)

        observer.present = present
        await observer.update(True, A)
        await started(bridge, 1)
        await observer.begin_interaction()
        assert (await observer.update(True, B))["state"] == "busy"
        observer.end_interaction()
        await observer.update(False)
        assert not notices
        assert bridge.pending[0].cancelled()
        now[0] = 60.0
        await observer.update(True, C)
        await started(bridge, 2)
        bridge.finish(1, "关闭后新画面提醒")
        await observer._task
        assert [notice[0] for notice in notices] == ["关闭后新画面提醒"]
        await observer.close()
        assert bridge.closed

    asyncio.run(scenario())


def test_duplicate_notice_is_suppressed_after_distinct_observations():
    async def scenario():
        now = [0.0]
        bridge = HeldGatewayReplies()
        observer = DesktopObserver(bridge, clock=lambda: now[0])
        notices = []

        async def present(*args):
            notices.append(args)

        observer.present = present
        for index, digest in enumerate((A, B)):
            await observer.update(True, digest)
            await started(bridge, index + 1)
            bridge.finish(index, "相同的有用提醒")
            await observer._task
            now[0] += 30.0
        assert len(bridge.prompts) == 2
        assert [notice[0] for notice in notices] == ["相同的有用提醒"]
        await observer.close()

    asyncio.run(scenario())
