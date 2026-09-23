"""/analyze 与缓存端点的集成测试。"""
import sys

import pytest

# conftest 已设置好环境变量；app.cache 在导入时建库到 /tmp/test_cost_cache.db
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "api_cache.db"
    monkeypatch.setenv("COST_DB_PATH", str(db_file))
    # 确保 app 模块以本用例的 DB 路径重新初始化
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    from app.main import app  # noqa: WPS433
    from app import cache as cache_mod  # noqa: WPS433

    with TestClient(app) as c:
        yield c
    cache_mod.cache.close()


SDL = """
directive @cost(weight: Float!) on FIELD_DEFINITION
directive @listSize(size: Int) on FIELD

type Query {
  users(first: Int): [User!]! @cost(weight: 5)
}
type User {
  id: ID! @cost(weight: 1)
  name: String! @cost(weight: 1)
}
"""

BODY = {
    "schema_sdl": SDL,
    "query": "{ users(first: 3) { id name } }",
    "variables": {},
    "budget": 100,
}


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_analyze_ok(client):
    r = client.post("/analyze", json=BODY)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["cached"] is False
    assert data["total_cost"] == 11.0  # users=5, id=3, name=3
    assert data["over_budget"] is False
    assert data["first_over_budget"] is None
    assert len(data["fingerprint"]) == 64
    assert len(data["paths"]) == 2


def test_analyze_cached_on_second_call(client):
    r1 = client.post("/analyze", json=BODY)
    assert r1.json()["cached"] is False
    r2 = client.post("/analyze", json=BODY)
    assert r2.status_code == 200
    assert r2.json()["cached"] is True
    assert r2.json()["total_cost"] == 11.0


def test_no_cache_bypasses(client):
    client.post("/analyze", json=BODY)
    body = {**BODY, "no_cache": True}
    r = client.post("/analyze", json=body)
    assert r.json()["cached"] is False


def test_different_budget_different_fingerprint(client):
    client.post("/analyze", json=BODY)
    r = client.post("/analyze", json={**BODY, "budget": 50})
    assert r.json()["cached"] is False


def test_over_budget_response(client):
    body = {**BODY, "budget": 6}
    r = client.post("/analyze", json=body)
    data = r.json()
    assert data["over_budget"] is True
    first = data["first_over_budget"]
    # users=5 未超；id 累计 8 > 6
    assert first["field_path"] == ["users", "id"]
    assert first["cumulative_before"] == 5.0
    assert first["cumulative_after"] == 8.0
    assert first["exceeded_by"] == 2.0
    assert first["leaf_path"] == ["users", "id"]
    flagged = [p for p in data["paths"] if p["first_over_budget"]]
    assert len(flagged) == 1


def test_fragment_cycle_422(client):
    body = {
        "schema_sdl": SDL,
        "query": """
            { users(first: 1) { ...A } }
            fragment A on User { ...B id }
            fragment B on User { ...A name }
        """,
        "budget": 100,
    }
    r = client.post("/analyze", json=body)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "fragment_cycle"
    assert err["details"][0]["cycle"][0] in ("A", "B")


def test_validation_error_422(client):
    r = client.post(
        "/analyze",
        json={**BODY, "query": "{ users { nonexistent } }"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_bad_payload_422(client):
    r = client.post("/analyze", json={"schema_sdl": SDL})
    assert r.status_code == 422  # pydantic 校验失败


def test_cache_endpoints(client):
    post = client.post("/analyze", json=BODY)
    fp = post.json()["fingerprint"]

    listing = client.get("/cache").json()
    assert listing["total_returned"] == 1
    assert listing["entries"][0]["fingerprint"] == fp
    assert listing["entries"][0]["total_cost"] == 11.0

    stats = client.get("/cache/stats").json()
    assert stats["entries"] == 1

    got = client.get(f"/cache/{fp}")
    assert got.status_code == 200
    assert got.json()["hit_count"] >= 1

    assert client.get("/cache/" + "0" * 64).status_code == 404

    deleted = client.delete(f"/cache/{fp}")
    assert deleted.status_code == 200
    assert client.get(f"/cache/{fp}").status_code == 404
    assert client.delete(f"/cache/{fp}").status_code == 404


def test_alias_and_fragments_are_expanded(client):
    body = {
        "schema_sdl": SDL,
        "query": """
            {
              a: users(first: 1) { ...U }
              b: users(first: 1) { ...U }
            }
            fragment U on User { id name }
        """,
        "budget": 1000,
    }
    data = client.post("/analyze", json=body).json()
    aliases = {
        f["alias"]
        for f in data["fields"]
        if f["field_name"] == "users"
    }
    assert aliases == {"a", "b"}
    # 每分支 users=5 + id=1 + name=1 = 7，共 14
    assert data["total_cost"] == 14.0
    leaf_paths = {tuple(p["path"]) for p in data["paths"]}
    assert ("a", "id") in leaf_paths
    assert ("b", "name") in leaf_paths
