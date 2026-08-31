from __future__ import annotations

import json

import pytest

from chatcopilot.contracts.gateway_protocol import (
    EventFrame,
    GatewayScope,
    RequestFrame,
    ResponseError,
    ResponseFrame,
)
from chatcopilot.gateway.protocol import (
    GatewayCredentialBinding,
    GatewayHandshakeAuthority,
    GatewayProtocolError,
    StaticGatewayCredentialAuthority,
    decode_frame,
    encode_frame,
    request_fingerprint,
    validate_request_access,
)


TOKEN = "t" * 32
NONCE = "n" * 32


def _connect_frame(
    nonce: str,
    *,
    token: str = TOKEN,
    client_id: str = "acp-edge",
    client_mode: str = "acp",
    min_protocol: int = 1,
    max_protocol: int = 1,
    scopes: list[str] | None = None,
) -> RequestFrame:
    return RequestFrame(
        request_id="request-1",
        method="connect",
        params={
            "nonce": nonce,
            "minProtocol": min_protocol,
            "maxProtocol": max_protocol,
            "client": {"id": client_id, "version": "0.1.0", "mode": client_mode},
            "scopes": scopes or ["gateway.read", "chat.write", "chat.abort"],
            "capabilities": ["session-updates"],
            "auth": {"token": token},
        },
    )


def _credential_authority(
    *,
    token: str = TOKEN,
    client_id: str = "acp-edge",
    client_mode: str = "acp",
    scopes: tuple[GatewayScope, ...] = ("gateway.read", "chat.write", "chat.abort"),
) -> StaticGatewayCredentialAuthority:
    return StaticGatewayCredentialAuthority(
        (
            GatewayCredentialBinding(
                token=token,
                client_id=client_id,
                client_mode=client_mode,
                scopes=scopes,
            ),
        )
    )


@pytest.mark.parametrize(
    "frame",
    [
        RequestFrame("r1", "health", {}),
        RequestFrame("r2", "chat.send", {"sessionId": "s1"}, "idem-1"),
        ResponseFrame("r1", True, result={"ready": True}),
        ResponseFrame(
            "r2",
            False,
            error=ResponseError("scope_denied", "not allowed", {"scope": "chat.write"}),
        ),
        EventFrame("chat.update", 1, {"sessionId": "s1", "text": "hello"}),
    ],
)
def test_gateway_frames_round_trip(frame: object) -> None:
    assert decode_frame(encode_frame(frame)) == frame  # type: ignore[arg-type]


def test_frame_decoder_rejects_unknown_fields_methods_and_oversized_input() -> None:
    unknown_field = {
        "type": "req",
        "id": "r1",
        "method": "health",
        "params": {},
        "role": "owner",
    }
    with pytest.raises(GatewayProtocolError, match="fields are invalid"):
        decode_frame(json.dumps(unknown_field))

    unknown_method = {"type": "req", "id": "r1", "method": "host.shell", "params": {}}
    with pytest.raises(GatewayProtocolError) as exc_info:
        decode_frame(json.dumps(unknown_method))
    assert exc_info.value.code == "unknown_method"

    with pytest.raises(GatewayProtocolError) as exc_info:
        decode_frame(b"{}", max_bytes=1)
    assert exc_info.value.code == "frame_too_large"


def test_frame_encoder_rejects_an_invalid_failed_response() -> None:
    with pytest.raises(GatewayProtocolError, match="failed response"):
        encode_frame(ResponseFrame(request_id="r1", ok=False))


def test_frame_decoder_rejects_deep_json_and_boolean_sequence() -> None:
    nested: object = "leaf"
    for _ in range(34):
        nested = {"next": nested}
    payload = {"type": "req", "id": "r1", "method": "health", "params": nested}
    with pytest.raises(GatewayProtocolError, match="nesting is too deep"):
        decode_frame(json.dumps(payload))

    event = {"type": "event", "event": "chat.update", "seq": True, "payload": {}}
    with pytest.raises(GatewayProtocolError, match="must be an integer"):
        decode_frame(json.dumps(event))

    with pytest.raises(GatewayProtocolError, match="number is invalid"):
        decode_frame('{"type":"req","id":"r1","method":"health","params":{"n":NaN}}')


def test_handshake_challenge_connect_and_hello_ok() -> None:
    now = [100.0]
    authority = GatewayHandshakeAuthority(
        credential_authority=_credential_authority(),
        server_generation=7,
        event_cursor=41,
        policy_version="policy-v2",
        clock=lambda: now[0],
        nonce_factory=lambda: NONCE,
    )
    challenge = authority.issue_challenge()
    challenge_frame = authority.challenge_event(challenge)
    assert challenge_frame.event == "connect.challenge"
    assert decode_frame(encode_frame(challenge_frame)) == challenge_frame

    hello = authority.accept(_connect_frame(challenge.nonce))
    assert hello.protocol == 1
    assert hello.server_generation == 7
    assert hello.event_cursor == 41
    assert hello.methods == (
        "health",
        "status",
        "channels.list",
        "events.replay",
        "sessions.create",
        "sessions.list",
        "sessions.get",
        "sessions.patch",
        "chat.send",
        "chat.abort",
        "runs.get",
        "runs.latest",
        "deliveries.get",
    )
    assert "approval.requested" not in hello.events
    response = authority.hello_response("request-1", hello)
    assert response.result is not None
    assert response.result["type"] == "hello-ok"
    assert decode_frame(encode_frame(response)) == response


def test_failed_authentication_consumes_the_challenge() -> None:
    authority = GatewayHandshakeAuthority(
        credential_authority=_credential_authority(),
        server_generation=1,
        nonce_factory=lambda: NONCE,
    )
    challenge = authority.issue_challenge()
    with pytest.raises(GatewayProtocolError) as exc_info:
        authority.accept(_connect_frame(challenge.nonce, token="x" * 32))
    assert exc_info.value.code == "authentication_failed"

    with pytest.raises(GatewayProtocolError) as exc_info:
        authority.accept(_connect_frame(challenge.nonce))
    assert exc_info.value.code == "invalid_challenge"


def test_credentials_bind_exact_client_mode_and_scopes_without_secret_repr() -> None:
    binding = GatewayCredentialBinding(
        token=TOKEN,
        client_id="acp-edge",
        client_mode="acp",
        scopes=("gateway.read",),
    )
    credentials = StaticGatewayCredentialAuthority((binding,))
    assert TOKEN not in repr(binding)
    assert TOKEN not in repr(credentials)
    assert TOKEN not in repr(_connect_frame(NONCE, scopes=["gateway.read"]))

    rejected_frames = (
        _connect_frame(NONCE, client_id="other", scopes=["gateway.read"]),
        _connect_frame(NONCE, client_mode="console", scopes=["gateway.read"]),
        _connect_frame(
            NONCE,
            scopes=["gateway.read", "chat.write"],
        ),
        _connect_frame(NONCE, token="x" * 32, scopes=["gateway.read"]),
    )
    for rejected in rejected_frames:
        authority = GatewayHandshakeAuthority(
            credential_authority=credentials,
            server_generation=1,
            nonce_factory=lambda: NONCE,
        )
        authority.issue_challenge()
        with pytest.raises(GatewayProtocolError) as exc_info:
            authority.accept(rejected)
        assert exc_info.value.code == "authentication_failed"
        assert str(exc_info.value) == "Gateway authentication failed"
        assert TOKEN not in repr(exc_info.value)
        assert "x" * 32 not in repr(exc_info.value)


def test_handshake_rejects_expiry_protocol_mismatch_and_scope_binding_drift() -> None:
    now = [100.0]
    authority = GatewayHandshakeAuthority(
        credential_authority=_credential_authority(),
        server_generation=1,
        challenge_ttl_seconds=1,
        clock=lambda: now[0],
        nonce_factory=lambda: NONCE,
    )
    challenge = authority.issue_challenge()
    now[0] = 101.001
    with pytest.raises(GatewayProtocolError) as exc_info:
        authority.accept(_connect_frame(challenge.nonce))
    assert exc_info.value.code == "challenge_expired"

    authority = GatewayHandshakeAuthority(
        credential_authority=_credential_authority(),
        server_generation=1,
        nonce_factory=lambda: NONCE,
    )
    challenge = authority.issue_challenge()
    with pytest.raises(GatewayProtocolError) as exc_info:
        authority.accept(_connect_frame(challenge.nonce, min_protocol=2, max_protocol=3))
    assert exc_info.value.code == "protocol_version_mismatch"

    authority = GatewayHandshakeAuthority(
        credential_authority=_credential_authority(scopes=("gateway.read",)),
        server_generation=1,
        nonce_factory=lambda: NONCE,
    )
    challenge = authority.issue_challenge()
    with pytest.raises(GatewayProtocolError) as exc_info:
        authority.accept(_connect_frame(challenge.nonce, scopes=["chat.write"]))
    assert exc_info.value.code == "authentication_failed"


def test_request_access_requires_scope_and_idempotency() -> None:
    with pytest.raises(GatewayProtocolError) as exc_info:
        validate_request_access(
            RequestFrame("r1", "chat.send", {"text": "hello"}),
            scopes={"chat.write"},
        )
    assert exc_info.value.code == "idempotency_key_required"

    validate_request_access(
        RequestFrame("r1", "chat.send", {"text": "hello"}, "idem-1"),
        scopes={"chat.write"},
    )
    with pytest.raises(GatewayProtocolError) as exc_info:
        validate_request_access(
            RequestFrame("r1", "chat.abort", {"runId": "run-1"}, "idem-2"),
            scopes={"chat.write"},
        )
    assert exc_info.value.code == "scope_denied"


def test_request_fingerprint_is_canonical_and_method_bound() -> None:
    first = RequestFrame("r1", "chat.send", {"a": 1, "b": 2}, "idem-1")
    reordered = RequestFrame("r2", "chat.send", {"b": 2, "a": 1}, "idem-2")
    changed = RequestFrame("r3", "sessions.patch", {"a": 1, "b": 2}, "idem-3")
    assert request_fingerprint(first) == request_fingerprint(reordered)
    assert request_fingerprint(first) != request_fingerprint(changed)
