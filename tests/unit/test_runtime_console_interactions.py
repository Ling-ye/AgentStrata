from fastapi.testclient import TestClient
import pytest
from console.backend.app import app
from console.backend.routes import interactions


@pytest.mark.parametrize(
    "origin,header",
    [
        (None, "interaction"),
        ("http://attacker.invalid", "interaction"),
        ("https://127.0.0.1", "interaction"),
        ("http://127.0.0.1", ""),
    ],
)
def test_operator_mutation_requires_same_origin_and_action_header(monkeypatch, origin, header):
    monkeypatch.setattr(
        interactions, "get_instance", lambda _: pytest.fail("must reject before credential lookup")
    )
    headers = {"x-agentstrata-action": header}
    if origin:
        headers["origin"] = origin
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/bots/demo/interactions/interaction_one/resolve",
            headers=headers,
            json={"resolution": {"decision": "approve"}},
        )
    assert response.status_code == 403


def test_browser_cannot_supply_responder_identity(monkeypatch):
    monkeypatch.setattr(
        interactions, "get_instance", lambda _: pytest.fail("must reject forged fields first")
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/bots/demo/interactions/interaction_one/resolve",
            headers={"origin": "http://127.0.0.1", "x-agentstrata-action": "interaction"},
            json={"resolution": {"decision": "approve"}, "actor_ref": "owner"},
        )
    assert response.status_code == 400
