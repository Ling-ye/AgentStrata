from __future__ import annotations

from chatcopilot.agent.persona.interpreter import parse_persona_command


def test_compact_natural_persona_command_is_supported() -> None:
    directive = parse_persona_command(
        "/persona你来模仿异世界情绪，回复始终以一句歌词结尾，使用中文回复"
    )
    assert directive is not None
    assert directive.operation == "set"
    assert directive.confidence == "high"
    assert directive.enrich is True


def test_structured_commands_are_small_and_model_independent() -> None:
    assert parse_persona_command("/persona show group").operation == "show"
    assert parse_persona_command("/persona set global 使用中文").scope == "global"
    assert parse_persona_command("/persona append user 更温柔").operation == "append"
    assert parse_persona_command("/persona research group 模仿莫宁").enrich is True
    assert parse_persona_command("/persona clear group").confidence == "medium"
    assert parse_persona_command("/persona clear group confirm").operation == "help"
    assert parse_persona_command("/persona clear group --confirm").operation == "help"


def test_similar_non_command_prefix_is_not_captured() -> None:
    assert parse_persona_command("/personality test") is None
