"""API auth and ownership rules, without loading models or calling the LLM."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from shopassist import api
from shopassist.service import SupportService


@pytest.fixture
def client(repo, kb, monkeypatch):
    monkeypatch.setattr(api, "SupportService", lambda: SupportService(repo=repo, kb=kb, agent=object()))
    with TestClient(api.app) as c:
        yield c


def test_customer_routes_require_authentication(client):
    assert client.post("/v1/conversations").status_code == 401
    assert client.post("/v1/conversations", headers={"X-Customer-Id": "C999"}).status_code == 401


def test_conversations_are_private_to_their_owner(client):
    conv = client.post("/v1/conversations", headers={"X-Customer-Id": "C001"}).json()["conversation_id"]
    assert client.get(f"/v1/conversations/{conv}", headers={"X-Customer-Id": "C001"}).status_code == 200
    # Another customer gets the same 404 as for a missing conversation: existence isn't revealed.
    assert client.get(f"/v1/conversations/{conv}", headers={"X-Customer-Id": "C002"}).status_code == 404
    assert client.post(f"/v1/conversations/{conv}/messages", json={"text": "hi"},
                       headers={"X-Customer-Id": "C002"}).status_code == 404


def test_message_validation(client):
    conv = client.post("/v1/conversations", headers={"X-Customer-Id": "C001"}).json()["conversation_id"]
    assert client.post(f"/v1/conversations/{conv}/messages", json={"text": ""},
                       headers={"X-Customer-Id": "C001"}).status_code == 422


def test_admin_routes_require_key_outside_demo_mode(client, monkeypatch):
    monkeypatch.setattr(api, "settings", replace(api.settings, demo_mode=False))
    assert client.get("/v1/admin/metrics").status_code == 403
    assert client.get("/v1/admin/metrics", headers={"X-Admin-Key": "dev-admin-key"}).status_code == 200
    assert client.post("/v1/admin/approvals/999", json={"approve": True},
                       headers={"X-Admin-Key": "dev-admin-key"}).status_code == 409


def test_demo_mode_opens_the_support_desk(client):
    assert client.get("/v1/admin/metrics").status_code == 200


def test_access_code_gates_everything_but_config(client, monkeypatch):
    monkeypatch.setattr(api, "settings", replace(api.settings, demo_access_code="let-me-in"))
    assert client.get("/v1/demo/config").json()["access_code_required"] is True
    assert client.get("/v1/demo/customers").status_code == 401
    assert client.post("/v1/conversations", headers={"X-Customer-Id": "C001"}).status_code == 401
    assert client.get("/v1/demo/customers", headers={"X-Demo-Code": "let-me-in"}).status_code == 200


def test_conversation_length_is_capped(client, monkeypatch):
    monkeypatch.setattr(api, "settings", replace(api.settings, demo_turns_per_conversation=0))
    conv = client.post("/v1/conversations", headers={"X-Customer-Id": "C001"}).json()["conversation_id"]
    r = client.post(f"/v1/conversations/{conv}/messages", json={"text": "hi"}, headers={"X-Customer-Id": "C001"})
    assert r.status_code == 429 and "length limit" in r.json()["detail"]
