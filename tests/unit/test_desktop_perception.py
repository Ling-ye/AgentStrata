import asyncio
from types import SimpleNamespace

from integrations.open_llm_vtuber.agentstrata_vtuber.perception import DesktopObserver


class Bridge:
    def __init__(self, text="有新的提示"):
        self.text, self.calls, self.wait = text, [], None
    def select_history(self, *args):
        self.history = args
    async def chat(self, text):
        self.calls.append(text)
        if self.wait is not None:
            await self.wait.wait()
        return SimpleNamespace(text=self.text, state="completed", session_id="session", run_id="run")
    async def close(self):
        pass


def test_changes_coalesce_at_interval_and_unchanged_screen_does_not_call_model():
    async def scenario():
        bridge, notices, now = Bridge(), [], [0.0]
        observer = DesktopObserver(bridge, clock=lambda: now[0])
        async def present(*args): notices.append(args)
        observer.present = present
        await observer.update(True, "a" * 64)
        await observer._task
        assert len(bridge.calls) == len(notices) == 1
        now[0] = 20
        await observer.update(True, "b" * 64)
        assert len(bridge.calls) == 1
        now[0] = 31
        await observer.update(True, "b" * 64)
        await observer._task
        assert len(bridge.calls) == 2 and len(notices) == 1
        now[0] = 100
        await observer.update(True, "b" * 64)
        assert len(bridge.calls) == 2
        await observer.close()
    asyncio.run(scenario())


def test_hide_or_user_interaction_cancels_pending_notice_without_replay():
    async def scenario():
        bridge, notices = Bridge(), []
        bridge.wait = asyncio.Event()
        observer = DesktopObserver(bridge)
        async def present(*args): notices.append(args)
        observer.present = present
        await observer.update(True, "a" * 64)
        await asyncio.sleep(0)
        await observer.begin_interaction()
        assert len(bridge.calls) == 1 and not notices
        assert (await observer.update(True, "b" * 64))["state"] == "busy"
        await observer.update(False)
        bridge.wait.set()
        observer.end_interaction()
        assert not notices and not observer.enabled
        await observer.close()
    asyncio.run(scenario())


def test_no_useful_notice_does_not_speak_even_with_source_footer():
    async def scenario():
        observer = DesktopObserver(Bridge("无需提醒。\n\nAgentStrata · source"))
        async def present(*args): raise AssertionError("Should not speak")
        observer.present = present
        await observer.update(True, "a" * 64)
        await observer._task
        assert observer.last_error is None
        await observer.close()
    asyncio.run(scenario())
