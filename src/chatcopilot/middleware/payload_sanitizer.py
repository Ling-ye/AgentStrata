"""Legacy payload projection uses the shared host audience policy."""

from chatcopilot.authorization.payloads import sanitize_tool_payload


def make_payload_sanitizer(role, workspace, *, public_output=False):
    return lambda payload: sanitize_tool_payload(
        payload, role=role, workspace=workspace, public_output=public_output
    )


def sanitize_tool_payload_for_role(payload, role, workspace=None):
    return sanitize_tool_payload(payload, role=role, workspace=workspace)
