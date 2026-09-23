"""核心分析引擎测试：片段展开、alias、列表乘数、深度权重、循环检测、超限路径。"""
import pytest

from app.analyzer import AnalysisError, detect_fragment_cycles, run_analysis
from graphql import parse
from app.analyzer import _index_fragments, settings as analyzer_settings

SDL = """
directive @cost(weight: Float!) on FIELD_DEFINITION
directive @listSize(size: Int) on FIELD

type Query {
  me: User @cost(weight: 2)
  users(first: Int): [User!]! @cost(weight: 5)
  matrix: [[Int!]!] @cost(weight: 1)
}

type User {
  id: ID! @cost(weight: 1)
  name: String! @cost(weight: 1)
  friends(first: Int, last: Int): [User!]! @cost(weight: 3)
  posts(limit: Int): [Post!]! @cost(weight: 4)
  avatar: Media
}

type Media {
  url: String! @cost(weight: 1)
  width: Int @cost(weight: 1)
}

type Post {
  id: ID! @cost(weight: 1)
  title: String! @cost(weight: 1)
}
"""

# 成本模型参数在 tests/conftest.py 中通过环境变量设定


@pytest.fixture
def depth_x2(monkeypatch):
    monkeypatch.setattr(analyzer_settings, "depth_multiplier", 2.0)
    yield
    analyzer_settings.depth_multiplier = 1.0


def test_basic_scalar_cost():
    query = "{ me { id name } }"
    r = run_analysis(SDL, query, budget=100)
    # me=2 (depth1), id=1, name=1 (depth2) → 4
    assert r["total_cost"] == 4.0
    assert r["over_budget"] is False
    assert r["max_depth"] == 2
    assert len(r["fields"]) == 3
    paths = {tuple(p["path"]): p["path_cost"] for p in r["paths"]}
    assert paths[("me", "id")] == 3.0
    assert paths[("me", "name")] == 3.0
    assert r["first_over_budget"] is None


def test_depth_multiplier(depth_x2):
    query = "{ me { id } }"
    r = run_analysis(SDL, query, budget=1000)
    # me: 2 * 2^0 = 2 ; id: 1 * 2^1 = 2 → 4
    assert r["total_cost"] == 4.0


def test_list_multiplier_from_literal():
    query = "{ users(first: 5) { id name } }"
    r = run_analysis(SDL, query, budget=1000)
    # users 字段本身: 5（不乘列表）；子树 ×5: id=5, name=5 → 15
    assert r["total_cost"] == 15.0
    users_field = r["fields"][0]
    assert users_field["field_name"] == "users"
    assert users_field["imposed_list_size"] == 5
    assert users_field["contribution"] == 5.0
    assert r["fields"][1]["contribution"] == 5.0


def test_list_multiplier_from_variables():
    query = "query($n: Int) { users(first: $n) { id } }"
    r = run_analysis(SDL, query, variables={"n": 3}, budget=1000)
    # users=5, id=3 → 8
    assert r["total_cost"] == 8.0


def test_list_size_clamped_to_max():
    query = "{ users(first: 9999) { id } }"
    r = run_analysis(SDL, query, budget=1e9)
    assert r["fields"][0]["imposed_list_size"] == 100
    # users=5 + id×100=100
    assert r["total_cost"] == 105.0


def test_default_list_size():
    query = "{ users { id } }"
    r = run_analysis(SDL, query, budget=1e9)
    # users=5 + id×10=10
    assert r["total_cost"] == 15.0


def test_nested_lists_compound():
    query = "{ users(first: 2) { friends(last: 3) { id } } }"
    r = run_analysis(SDL, query, budget=1e9)
    # users=5; friends 3 (weight) ×2 =6; id 1×2×3=6 → 17
    assert r["total_cost"] == 17.0
    friends = [f for f in r["fields"] if f["field_name"] == "friends"][0]
    assert friends["inherited_list_multiplier"] == 2.0
    assert friends["imposed_list_size"] == 3
    idf = [f for f in r["fields"] if f["field_name"] == "id"][0]
    assert idf["inherited_list_multiplier"] == 6.0


def test_2d_list_dimensions():
    query = "{ matrix }"
    r = run_analysis(SDL, query, budget=1e9)
    f = r["fields"][0]
    assert f["list_dimensions"] == 2
    assert f["imposed_list_size"] == 10
    # 叶子标量本身 weight=1 ×10^2 =100（无后续子树）
    assert f["contribution"] == 100.0


def test_aliases_charged_separately():
    query = """
    {
      a: users(first: 2) { id }
      b: users(first: 2) { id }
    }
    """
    r = run_analysis(SDL, query, budget=1e9)
    # 每个分支 5 + 2 = 7，共 14
    assert r["total_cost"] == 14.0
    aliases = {f["alias"] for f in r["fields"] if f["field_name"] == "users"}
    assert aliases == {"a", "b"}
    paths = {tuple(p["path"]) for p in r["paths"]}
    assert ("a", "id") in paths
    assert ("b", "id") in paths


def test_named_fragment_expansion():
    query = """
    { users(first: 4) { ...UserFields } }
    fragment UserFields on User { id name }
    """
    r = run_analysis(SDL, query, budget=1e9)
    # users=5, id×4=4, name×4=4 → 13
    assert r["total_cost"] == 13.0
    paths = {tuple(p["path"]) for p in r["paths"]}
    assert ("users", "id") in paths
    assert ("users", "name") in paths


def test_inline_fragment_and_typename():
    query = """
    { me { __typename ... on User { id avatar { url } } } }
    """
    r = run_analysis(SDL, query, budget=1e9)
    # me=2, __typename=0.5, id=1, avatar=1, url=1 → 5.5
    assert r["total_cost"] == pytest.approx(5.5)


def test_skip_include_static(monkeypatch):
    query = """
    query($s: Boolean!) {
      me {
        id @skip(if: true)
        name @include(if: false)
        avatar { url @skip(if: $s) }
      }
    }
    """
    r = run_analysis(SDL, query, variables={"s": True}, budget=1e9)
    names = [f["field_name"] for f in r["fields"]]
    assert "id" not in names
    assert "name" not in names
    assert "url" not in names
    assert "avatar" in names  # avatar 自身保留，只是子字段被裁
    # me=2 + avatar=1 = 3
    assert r["total_cost"] == 3.0


def test_skip_unknown_kept():
    # 变量未提供样例 → 不可知 → 保留（最坏情况）
    query = "query($s: Boolean!) { me { id @skip(if: $s) } }"
    r = run_analysis(SDL, query, variables={}, budget=1e9)
    assert any(f["field_name"] == "id" for f in r["fields"])


def test_fragment_cycle_direct():
    cyclic = """
    query { me { ...A } }
    fragment A on User { ...B id }
    fragment B on User { ...A name }
    """
    with pytest.raises(AnalysisError) as ei:
        run_analysis(SDL, cyclic, budget=100)
    assert ei.value.code == "fragment_cycle"
    assert "A" in ei.value.details[0]["cycle"]
    assert "B" in ei.value.details[0]["cycle"]


def test_fragment_self_cycle():
    cyclic = """
    query { me { ...A } }
    fragment A on User { ...A id }
    """
    with pytest.raises(AnalysisError) as ei:
        run_analysis(SDL, cyclic, budget=100)
    assert ei.value.code == "fragment_cycle"


def test_cycle_detection_helper():
    doc = parse("fragment A on User { ...B } fragment B on User { ...A }")
    cycles = detect_fragment_cycles(_index_fragments(doc))
    assert len(cycles) == 1
    assert cycles[0]["cycle"][0] == cycles[0]["cycle"][-1]


def test_over_budget_first_path():
    # me=2, id=1, name=1 → 4；预算 2.5，第一个超限字段是 id（累计 3）
    query = "{ me { id name } }"
    r = run_analysis(SDL, query, budget=2.5)
    assert r["over_budget"] is True
    first = r["first_over_budget"]
    assert first is not None
    assert first["field_path"] == ["me", "id"]
    assert first["cumulative_before"] == 2.0
    assert first["cumulative_after"] == 3.0
    assert first["exceeded_by"] == pytest.approx(0.5)
    assert first["leaf_path"] == ["me", "id"]
    flagged = [p for p in r["paths"] if p["first_over_budget"]]
    assert len(flagged) == 1
    assert flagged[0]["path"] == ["me", "id"]


def test_first_cross_on_list_field():
    # users=5 即超限（budget=4），叶路径 users.id
    query = "{ users(first: 2) { id name } }"
    r = run_analysis(SDL, query, budget=4)
    first = r["first_over_budget"]
    assert first["field_path"] == ["users"]
    assert first["leaf_path"] == ["users", "id"]
    flagged = {tuple(p["path"]) for p in r["paths"] if p["first_over_budget"]}
    assert flagged == {("users", "id")}


def test_branch_path_charges_independent():
    # 两个兄弟分支的 path_cost 互不污染
    query = """
    {
      me { id }
      users(first: 2) { id name }
    }
    """
    r = run_analysis(SDL, query, budget=1e9)
    paths = {tuple(p["path"]): p["path_cost"] for p in r["paths"]}
    assert paths[("me", "id")] == 3.0          # me=2 + id=1
    assert paths[("users", "id")] == 7.0       # users=5 + id=2
    assert paths[("users", "name")] == 7.0


def test_validation_error():
    with pytest.raises(AnalysisError) as ei:
        run_analysis(SDL, "{ me { nonExistent } }", budget=100)
    assert ei.value.code == "validation_error"


def test_syntax_error():
    with pytest.raises(AnalysisError) as ei:
        run_analysis(SDL, "{ me { ", budget=100)
    assert ei.value.code == "syntax_error"


def test_ambiguous_operation():
    q = "query A { me { id } } query B { me { name } }"
    with pytest.raises(AnalysisError) as ei:
        run_analysis(SDL, q, budget=100)
    assert ei.value.code == "ambiguous_operation"


def test_operation_name_selection():
    q = "query A { me { id } } query B { me { name } }"
    r = run_analysis(SDL, q, operation_name="B", budget=100)
    assert r["operation"]["name"] == "B"
    assert [f["field_name"] for f in r["fields"][-1:]] == ["name"]


def test_negative_budget_rejected():
    with pytest.raises(AnalysisError) as ei:
        run_analysis(SDL, "{ me { id } }", budget=-1)
    assert ei.value.code == "invalid_budget"
