"""Tests for the core static cost analyzer."""

from __future__ import annotations

import pytest

from app.analyzer import AnalyzerConfig, analyze_query

from .gql_fixtures import CYCLE_QUERY, NESTED_LIST_QUERY, SCHEMA


def analyze(query: str, **kwargs):
    kwargs.setdefault("budget", 1000)
    return analyze_query(schema_sdl=SCHEMA, query=query, **kwargs)


def paths_by_name(result):
    return {entry["path"]: entry for entry in result["paths"]}


# --------------------------------------------------------------------------- #
# Basic cost calculation
# --------------------------------------------------------------------------- #


def test_simple_scalar_query():
    result = analyze("{ user(id: \"1\") { id name } }")
    assert result["status"] == "ok"
    assert result["summary"]["total_cost"] == 3
    assert result["summary"]["max_depth"] == 2
    assert result["summary"]["field_count"] == 3
    by_name = paths_by_name(result)
    assert by_name["Query.user"]["multiplier_source"] == "none"
    assert by_name["Query.user.id"]["depth"] == 2


def test_aliases_use_response_keys():
    result = analyze(
        "{ me: user(id: \"1\") { fullName: name } }"
    )
    by_name = paths_by_name(result)
    assert "Query.me" in by_name
    entry = by_name["Query.me.fullName"]
    assert entry["field"] == "name"
    assert entry["alias"] == "fullName"
    assert result["summary"]["total_cost"] == 2


def test_list_uses_default_multiplier():
    result = analyze("{ user(id: \"1\") { friends { name } } }")
    by_name = paths_by_name(result)
    assert by_name["Query.user.friends"]["multiplier"] == 10
    assert by_name["Query.user.friends"]["multiplier_source"] == "default"
    assert by_name["Query.user.friends.name"]["effective_multiplier"] == 10
    # user(1) + friends(10) + name(10)
    assert result["summary"]["total_cost"] == 21


def test_list_multiplier_from_literal_argument():
    result = analyze(
        "{ user(id: \"1\") { friends(first: 5) { name } } }"
    )
    entry = paths_by_name(result)["Query.user.friends"]
    assert entry["multiplier"] == 5
    assert entry["multiplier_source"] == "argument:first"
    assert result["summary"]["total_cost"] == 11


def test_list_multiplier_from_variable_sample():
    result = analyze(
        "query($n: Int) { user(id: \"1\") { friends(first: $n) { name } } }",
        variables={"n": 3},
    )
    assert result["summary"]["total_cost"] == 7  # 1 + 3 + 3


def test_list_multiplier_from_variable_default():
    result = analyze(
        "query($n: Int = 2) { user(id: \"1\") { friends(first: $n) { name } } }"
    )
    assert result["summary"]["total_cost"] == 5  # 1 + 2 + 2


def test_nested_lists_compound_with_depth():
    result = analyze(NESTED_LIST_QUERY)
    by_name = paths_by_name(result)
    # 1 (user) + 10 (friends) + 10*10 (repositories) + 10*10*10 (tags)
    assert by_name["Query.user.friends"]["effective_multiplier"] == 10
    assert by_name["Query.user.friends.repositories"]["effective_multiplier"] == 100
    assert (
        by_name["Query.user.friends.repositories.tags"]["effective_multiplier"]
        == 1000
    )
    assert result["summary"]["total_cost"] == 1111
    assert result["summary"]["max_depth"] == 4


def test_directive_complexity():
    result = analyze("{ user(id: \"1\") { email } }")
    entry = paths_by_name(result)["Query.user.email"]
    assert entry["complexity"] == 3
    assert entry["cost"] == 3
    assert result["summary"]["total_cost"] == 4  # user(1) + email(3)


def test_directive_multiplier():
    result = analyze("{ user(id: \"1\") { followers { name } } }")
    entry = paths_by_name(result)["Query.user.followers"]
    assert entry["multiplier"] == 50
    assert entry["multiplier_source"] == "directive:@cost"
    assert result["summary"]["total_cost"] == 101  # 1 + 50 + 50


def test_custom_default_multiplier():
    result = analyze(
        "{ user(id: \"1\") { friends { name } } }",
        config=AnalyzerConfig(default_list_multiplier=2),
    )
    assert result["summary"]["total_cost"] == 5  # 1 + 2 + 2


# --------------------------------------------------------------------------- #
# Fragments
# --------------------------------------------------------------------------- #


def test_fragment_is_expanded_and_traced():
    query = """
    query { user(id: "1") { ...UserBits } }
    fragment UserBits on User { name email }
    """
    result = analyze(query)
    by_name = paths_by_name(result)
    assert result["summary"]["fragments_used"] == ["UserBits"]
    assert result["summary"]["fragment_count"] == 1
    assert by_name["Query.user.name"]["via_fragment"] == "UserBits"
    assert by_name["Query.user.email"]["via_fragment"] == "UserBits"
    assert result["summary"]["total_cost"] == 5  # 1 + 1 + 3


def test_nested_fragments_are_expanded():
    query = """
    { user(id: "1") { ...A } }
    fragment A on User { name ...B }
    fragment B on User { email }
    """
    result = analyze(query)
    assert result["summary"]["fragments_used"] == ["A", "B"]
    assert result["summary"]["total_cost"] == 5


def test_fragment_cycle_is_detected():
    result = analyze(CYCLE_QUERY)
    assert result["status"] == "error"
    error = result["errors"][0]
    assert error["code"] == "FRAGMENT_CYCLE"
    cycle = error["details"]["cycle"]
    assert cycle[0] == cycle[-1]
    assert set(cycle) >= {"A", "B"}
    assert result["summary"] is None
    assert result["paths"] == []


def test_self_referencing_fragment_is_a_cycle():
    query = """
    fragment A on User { friends { ...A } }
    { user(id: "1") { ...A } }
    """
    result = analyze(query)
    assert result["status"] == "error"
    assert result["errors"][0]["code"] == "FRAGMENT_CYCLE"
    assert result["errors"][0]["details"]["cycle"] == ["A", "A"]


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #


def test_within_budget_when_equal():
    result = analyze(
        "{ user(id: \"1\") { friends { name } } }", budget=21
    )
    assert result["status"] == "ok"
    assert result["summary"]["within_budget"] is True
    assert result["first_over_budget_path"] is None


def test_first_over_budget_path_in_document_order():
    result = analyze(NESTED_LIST_QUERY, budget=500)
    assert result["status"] == "over_budget"
    breach = result["first_over_budget_path"]
    assert breach is not None
    # Order: user(1), friends(10), repositories(100), tags(1000) -> 1111 > 500
    assert breach["path"] == "Query.user.friends.repositories.tags"
    assert breach["cumulative_cost"] == 1111
    assert breach["budget"] == 500


def test_first_over_budget_path_on_shallow_field():
    result = analyze(
        "{ user(id: \"1\") { friends { name } } }", budget=20
    )
    breach = result["first_over_budget_path"]
    assert breach["path"] == "Query.user.friends.name"
    assert breach["cumulative_cost"] == 21


# --------------------------------------------------------------------------- #
# skip / include, interfaces, unions
# --------------------------------------------------------------------------- #


def test_skip_and_include_statically_resolved():
    query = """
    query($skipIt: Boolean!, $inc: Boolean!) {
      user(id: "1") {
        name @skip(if: $skipIt)
        email @include(if: $inc)
        id
      }
    }
    """
    excluded = analyze(
        query, variables={"skipIt": True, "inc": False}
    )
    # user + id only
    assert excluded["summary"]["total_cost"] == 2

    included = analyze(
        query, variables={"skipIt": False, "inc": True}
    )
    # user + name + email(3) + id
    assert included["summary"]["total_cost"] == 6


def test_unknown_skip_condition_counts_for_worst_case():
    query = """
    query($maybe: Boolean!) { user(id: "1") { name @skip(if: $maybe) id } }
    """
    result = analyze(query, variables={})
    # user + name + id: the unresolvable @skip is kept (worst case)
    assert result["summary"]["total_cost"] == 3


def test_union_inline_fragments_are_summed():
    query = """
    { search(text: "x") { ... on User { name } ... on Repository { tags } } }
    """
    result = analyze(query)
    by_name = paths_by_name(result)
    assert by_name["Query.search"]["multiplier"] == 10
    assert by_name["Query.search.name"]["effective_multiplier"] == 10
    # tags is itself a list: 10 * 10
    assert by_name["Query.search.tags"]["effective_multiplier"] == 100
    # search(10) + name(10) + tags(100)
    assert result["summary"]["total_cost"] == 120


def test_interface_field_and_inline_fragment():
    query = """
    { node(id: "1") { id ... on User { email } } }
    """
    result = analyze(query)
    assert result["summary"]["total_cost"] == 5  # node + id + email(3)


# --------------------------------------------------------------------------- #
# Errors / limits
# --------------------------------------------------------------------------- #


def test_invalid_query_is_reported():
    result = analyze("{ user(id: \"1\") { nope } }")
    assert result["status"] == "invalid"
    codes = {e["code"] for e in result["errors"]}
    assert "VALIDATION_FAILED" in codes


def test_invalid_sdl_is_reported():
    result = analyze_query(schema_sdl="type Query {", query="{ __typename }")
    assert result["status"] == "invalid"
    assert result["errors"][0]["code"] == "GRAPHQL_ERROR"


def test_multiple_operations_require_name():
    query = """
    query A { user(id: "1") { id } }
    query B { user(id: "1") { name } }
    """
    ambiguous = analyze(query)
    assert ambiguous["status"] == "error"
    assert ambiguous["errors"][0]["code"] == "AMBIGUOUS_OPERATION"

    selected = analyze(query, operation_name="B")
    assert selected["status"] == "ok"
    assert selected["operation"]["name"] == "B"
    assert selected["summary"]["total_cost"] == 2


def test_unknown_field_when_validation_disabled_is_warning():
    result = analyze(
        "{ user(id: \"1\") { nope } }",
        config=AnalyzerConfig(validate_document=False),
    )
    assert result["status"] == "ok"
    assert result["warnings"][0]["code"] == "UNKNOWN_FIELD"


def test_max_depth_limit():
    result = analyze(
        "{ user(id: \"1\") { friends { name } } }",
        config=AnalyzerConfig(max_depth=2),
    )
    assert result["status"] == "error"
    assert result["errors"][0]["code"] == "MAX_DEPTH_EXCEEDED"
