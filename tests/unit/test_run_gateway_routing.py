from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from chatcopilot.botspec.model import ChannelsSpec, GatewaySpec, QQChannelSpec
from chatcopilot import run


@pytest.mark.parametrize(
    ("gateway", "channels", "expected"),
    (
        (GatewaySpec(), ChannelsSpec(qq=QQChannelSpec()), "gateway"),
        (None, ChannelsSpec(), "legacy"),
    ),
)
def test_run_routes_gateway_qq_and_legacy_platforms_to_separate_hosts(
    monkeypatch: pytest.MonkeyPatch,
    gateway: GatewaySpec | None,
    channels: ChannelsSpec,
    expected: str,
) -> None:
    runtime = SimpleNamespace(
        gateway=gateway,
        channels=channels,
        source_path=Path("/tmp/bot.yaml"),
    )
    selected: list[str] = []
    monkeypatch.setattr(run, "resolve_bot_spec_path", lambda _: Path("/tmp/bot.yaml"))
    monkeypatch.setattr(run, "load_botspec", lambda _: object())
    monkeypatch.setattr(run, "assemble_runtime_context", lambda _: runtime)
    monkeypatch.setattr(run, "set_bot_spec_env", lambda _: None)
    monkeypatch.setattr(run, "apply_runtime_env", lambda _: None)
    monkeypatch.setattr(run, "_start_codebase_index_warmup", lambda _: None)
    monkeypatch.setattr(
        run,
        "_run_gateway",
        lambda _: selected.append("gateway") or 17,
    )
    monkeypatch.setattr(
        run,
        "_run_legacy_acp",
        lambda _: selected.append("legacy") or 23,
    )

    result = run.main(["--bot", "test-bot"])

    assert selected == [expected]
    assert result == (17 if expected == "gateway" else 23)
