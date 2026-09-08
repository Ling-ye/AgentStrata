from chatcopilot.core.visible_model_response import project_visible_response


def test_public_response_selection_omits_provider_fields_and_marks_tool_limit():
    tool = {"id": "call", "function": {"name": "lookup", "arguments": "{}", "reasoning": "private"},
            "reasoning_content": "private"}
    response = project_visible_response("visible", [tool] * 130)
    assert response["content"] == "visible"
    assert response["capture_state"] == "truncated"
    assert len(response["tool_calls"]) == 128
    assert "private" not in repr(response)
    assert "tool_call_limit" in response["omitted"]


def test_response_capture_failure_is_explicit_and_does_not_raise():
    response = project_visible_response("visible", [{"id": "broken"}])
    assert response["capture_state"] == "capture_failed"
    assert "visible_response_projection_failed" in response["omitted"]


def test_adapter_capture_preserves_observed_text_and_truncation():
    response = project_visible_response("partial response", coverage="adapter_visible", truncated=True,
                                        omitted=("provider_internal_turns",))
    assert response["content"] == "partial response"
    assert response["coverage"] == "adapter_visible"
    assert response["capture_state"] == "truncated"
