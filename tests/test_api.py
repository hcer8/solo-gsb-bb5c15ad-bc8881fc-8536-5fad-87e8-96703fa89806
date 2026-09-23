"""End-to-end tests for the FastAPI endpoints (including SQLite caching)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app

from .gql_fixtures import CYCLE_QUERY, NESTED_LIST_QUERY, SCHEMA, SIMPLE_QUERY


def analyze_payload(**overrides) -> dict:
    payload = {
        "schema_sdl": SCHEMA,
        "query": SIMPLE_QUERY,
        "variables": {},
        "budget": 1000,
    }
    payload.update(overrides)
    return payload


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COSTGUARD_DB_PATH", str(tmp_path / "test.db"))
    get_settings.cache_clear()
    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_analyze_then_cache_hit(client):
    payload = analyze_payload()

    first = client.post("/v1/analyze", json=payload)
    assert first.status_code == 200
    body = first.json()
    assert body["cached"] is False
    assert body["status"] == "ok"
    assert body["summary"]["total_cost"] == 3
    fingerprint = body["fingerprint"]
    assert len(fingerprint) == 64

    second = client.post("/v1/analyze", json=payload)
    assert second.status_code == 200
    cached = second.json()
    assert cached["cached"] is True
    assert cached["fingerprint"] == fingerprint
    assert cached["summary"]["total_cost"] == 3


def test_list_and_detail_and_delete(client):
    fingerprint = client.post("/v1/analyze", json=analyze_payload()).json()[
        "fingerprint"
    ]

    listing = client.get("/v1/analyses")
    assert listing.status_code == 200
    data = listing.json()
    assert data["total"] == 1
    assert data["items"][0]["fingerprint"] == fingerprint
    assert data["items"][0]["hit_count"] >= 0

    detail = client.get(f"/v1/analyses/{fingerprint}")
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["result"]["fingerprint"] == fingerprint
    assert detail_body["request"]["query"] == SIMPLE_QUERY
    assert detail_body["summary"]["status"] == "ok"

    missing = client.get("/v1/analyses/" + "0" * 64)
    assert missing.status_code == 404

    deleted = client.delete(f"/v1/analyses/{fingerprint}")
    assert deleted.status_code == 204
    assert client.get(f"/v1/analyses/{fingerprint}").status_code == 404
    assert client.delete(f"/v1/analyses/{fingerprint}").status_code == 404


def test_refresh_bypasses_cache(client):
    payload = analyze_payload()
    first = client.post("/v1/analyze", json=payload).json()
    assert first["cached"] is False

    refreshed = client.post(
        "/v1/analyze", json=analyze_payload(refresh=True)
    ).json()
    assert refreshed["cached"] is False
    assert refreshed["fingerprint"] == first["fingerprint"]


def test_different_budgets_get_different_fingerprints(client):
    one = client.post("/v1/analyze", json=analyze_payload(budget=100)).json()
    two = client.post("/v1/analyze", json=analyze_payload(budget=200)).json()
    assert one["fingerprint"] != two["fingerprint"]


def test_over_budget_reports_first_path(client):
    body = client.post(
        "/v1/analyze", json=analyze_payload(query=NESTED_LIST_QUERY, budget=500)
    ).json()
    assert body["status"] == "over_budget"
    assert body["summary"]["within_budget"] is False
    assert (
        body["first_over_budget_path"]["path"]
        == "Query.user.friends.repositories.tags"
    )

    listing = client.get("/v1/analyses", params={"status": "over_budget"}).json()
    assert listing["total"] == 1
    assert listing["items"][0]["status"] == "over_budget"


def test_fragment_cycle_through_api(client):
    body = client.post(
        "/v1/analyze", json=analyze_payload(query=CYCLE_QUERY)
    ).json()
    assert body["status"] == "error"
    assert body["errors"][0]["code"] == "FRAGMENT_CYCLE"
    assert body["errors"][0]["details"]["cycle"]


def test_invalid_query_through_api(client):
    body = client.post(
        "/v1/analyze", json=analyze_payload(query="{ user { nope } }")
    ).json()
    assert body["status"] == "invalid"
    assert body["errors"]


def test_cache_hit_increments_hit_count(client):
    fingerprint = client.post("/v1/analyze", json=analyze_payload()).json()[
        "fingerprint"
    ]
    client.post("/v1/analyze", json=analyze_payload())
    client.post("/v1/analyze", json=analyze_payload())

    detail = client.get(f"/v1/analyses/{fingerprint}").json()
    # Two additional analyze calls + one detail fetch after creation
    assert detail["summary"]["hit_count"] == 3
