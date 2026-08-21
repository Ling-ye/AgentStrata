from __future__ import annotations

from chatcopilot.middleware.acp.memory_receipt import classify_memory_receipt_requirement


def test_explicit_memory_append_requires_real_append_receipt() -> None:
    requirement = classify_memory_receipt_requirement("请记住我偏好简短回答", caller_role="user")
    assert requirement is not None
    assert requirement.kind == "memory_append"
    assert requirement.successful_tools == frozenset({"append_memory"})


def test_persona_wording_is_not_routed_to_memory_receipt_logic() -> None:
    assert (
        classify_memory_receipt_requirement(
            "记住你以后就是莫宁这个人格",
            caller_role="owner",
        )
        is None
    )
