from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from chatcopilot.botspec import cli
from chatcopilot.platforms.base import ExternalCheckItem, ExternalCheckReport


class _ExternalCheckAdapter:
    def __init__(self, verdict: str = "passed") -> None:
        self.verdict = verdict
        self.calls: list[dict[str, object]] = []

    def run_external_checks(self, env, **kwargs):
        self.calls.append({"env": dict(env), **kwargs})
        return ExternalCheckReport(
            platform="qq",
            bot_id=str(kwargs["bot_id"]),
            verdict=self.verdict,  # type: ignore[arg-type]
            checks=(
                ExternalCheckItem(
                    check_id="onebot_boundary",
                    label="OneBot 认证边界",
                    status="passed" if self.verdict == "passed" else "error",
                    required=True,
                    detail="fixture result",
                ),
            ),
            limitations=("fixture does not use a real QQ account",),
        )


def _install_fake_bot(monkeypatch, tmp_path: Path, adapter: _ExternalCheckAdapter) -> None:
    spec = SimpleNamespace(
        id="example-bot",
        base_dir=tmp_path,
        platform=SimpleNamespace(type="qq"),
        deploy=SimpleNamespace(instance_id="example-instance"),
    )
    monkeypatch.setattr(cli, "load_botspec", lambda _path: spec)
    monkeypatch.setattr(cli, "validate_botspec", lambda _spec: ())
    monkeypatch.setattr(cli._registry, "get_adapter", lambda _name: adapter)


def test_external_check_cli_emits_machine_readable_non_evaluation_report(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    adapter = _ExternalCheckAdapter()
    _install_fake_bot(monkeypatch, tmp_path, adapter)

    result = cli.main(["external-check", "--bot", "ignored.yaml", "--json"])

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "external-platform-check/v1"
    assert payload["scope"] == "external_platform"
    assert payload["agent_evaluation"] is False
    assert payload["verdict"] == "passed"
    assert adapter.calls[0]["bot_id"] == "example-instance"
    assert adapter.calls[0]["send_message"] is False
    assert adapter.calls[0]["confirm_external_write"] is False


def test_external_check_cli_forwards_both_one_shot_write_flags(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    adapter = _ExternalCheckAdapter(verdict="error")
    _install_fake_bot(monkeypatch, tmp_path, adapter)

    result = cli.main(
        [
            "external-check",
            "--bot",
            "ignored.yaml",
            "--send-message",
            "--confirm-external-write",
        ]
    )

    assert result == 1
    assert "agent_evaluation=false verdict=error" in capsys.readouterr().out
    assert adapter.calls[0]["send_message"] is True
    assert adapter.calls[0]["confirm_external_write"] is True
