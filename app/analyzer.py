"""Static worst-case cost analysis for GraphQL queries.

Cost model
----------
Each field contributes::

    contribution(field) = complexity(field) * effective_multiplier(field)

``effective_multiplier`` is the product of the list multipliers of the field
itself and all of its ancestors, so nested lists compound with field depth.
The total cost is the sum of all field contributions, which is equivalent to::

    cost(field) = multiplier(field) * (complexity(field) + sum(cost(children)))

Multiplier resolution order for a field:

1. ``@cost(multiplier: N)`` directive on the field definition in the SDL.
2. A pagination argument (``first`` / ``last`` / ``limit`` / ``size`` /
   ``take``) on the field in the query document — a literal or a variable
   resolved from the supplied variable sample.
3. ``default_list_multiplier`` when the field returns a list, otherwise ``1``.

``@cost(complexity: N)`` overrides the base complexity of a field
(default ``1``).

Worst-case semantics:

* selections guarded by unknown ``@skip`` / ``@include`` conditions are
  counted (only statically-false conditions are excluded);
* inline-fragment selections on interfaces/unions are all summed;
* list fields use the resolved multiplier as an upper bound.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from graphql import (
    GraphQLInterfaceType,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLUnionType,
    build_schema,
    get_named_type,
    parse,
    specified_rules,
    validate,
    value_from_ast_untyped,
)
from graphql.error import GraphQLError
from graphql.language import ast as gql_ast

COST_DIRECTIVE_SDL = (
    "directive @cost(complexity: Int, multiplier: Int) on FIELD_DEFINITION"
)
LIST_SIZE_ARGUMENTS = ("first", "last", "limit", "size", "take")
ROOT_LABELS = {
    "query": "Query",
    "mutation": "Mutation",
    "subscription": "Subscription",
}
FIELD_CONTAINERS = (GraphQLObjectType, GraphQLInterfaceType)
COMPOSITE_TYPES = (GraphQLObjectType, GraphQLInterfaceType, GraphQLUnionType)


@dataclass(frozen=True)
class AnalyzerConfig:
    default_list_multiplier: int = 10
    max_depth: int = 50
    max_expanded_nodes: int = 20_000
    cost_model_version: str = "v1"
    validate_document: bool = True


class AnalysisAbort(Exception):
    """A fatal analysis problem that should be reported to the caller."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def compute_fingerprint(payload: dict[str, Any]) -> str:
    """Return a stable SHA-256 fingerprint of a canonicalized request."""
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def analyze_query(
    *,
    schema_sdl: str,
    query: str,
    variables: dict[str, Any] | None = None,
    budget: int = 1000,
    operation_name: str | None = None,
    config: AnalyzerConfig | None = None,
) -> dict[str, Any]:
    """Analyze ``query`` against ``schema_sdl``.

    Never raises: schema/document problems are reported in the returned
    ``errors`` list with an ``invalid``/``error`` status.
    """
    cfg = config or AnalyzerConfig()
    started = time.perf_counter()
    try:
        schema = _build_schema(schema_sdl)
        document = parse(query)
        result = _Analyzer(
            schema, document, variables or {}, budget, operation_name, cfg
        ).run()
    except AnalysisAbort as exc:
        result = _empty_result("error")
        result["errors"].append(
            {"code": exc.code, "message": exc.message, "details": exc.details}
        )
    except GraphQLError as exc:
        result = _empty_result("invalid")
        result["errors"].append(
            {
                "code": "GRAPHQL_ERROR",
                "message": exc.message,
                "details": {"locations": _locations(exc)},
            }
        )
    result["meta"] = {
        "cost_model_version": cfg.cost_model_version,
        "default_list_multiplier": cfg.default_list_multiplier,
        "analysis_ms": round((time.perf_counter() - started) * 1000.0, 3),
    }
    return result


# --------------------------------------------------------------------------- #
# Module-level helpers
# --------------------------------------------------------------------------- #


def _empty_result(status: str) -> dict[str, Any]:
    return {
        "status": status,
        "operation": None,
        "summary": None,
        "first_over_budget_path": None,
        "paths": [],
        "warnings": [],
        "errors": [],
        "meta": {},
    }


def _locations(exc: GraphQLError) -> list[dict[str, int]]:
    return [
        {"line": loc.line, "column": loc.column}
        for loc in exc.locations or []
    ]


def _build_schema(schema_sdl: str):
    # Inject the @cost directive definition when the SDL does not declare it,
    # so schemas can simply annotate fields without boilerplate.
    sdl = schema_sdl
    if "directive @cost" not in sdl:
        sdl = f"{sdl.rstrip()}\n\n{COST_DIRECTIVE_SDL}\n"
    return build_schema(sdl)


def _contains_list(type_) -> bool:
    while isinstance(type_, GraphQLNonNull):
        type_ = type_.of_type
    return isinstance(type_, GraphQLList)


def _resolve_value(node, variables: dict[str, Any]):
    """Resolve an AST value: variables come from the sample, literals are parsed."""
    if isinstance(node, gql_ast.VariableNode):
        return variables.get(node.name.value)
    return value_from_ast_untyped(node)


def _should_include(directives, variables: dict[str, Any]) -> bool:
    """Evaluate @skip/@include. Unknown conditions are kept (worst case)."""
    for directive in directives or ():
        name = directive.name.value
        if name not in ("skip", "include"):
            continue
        argument = next(
            (a for a in directive.arguments or () if a.name.value == "if"), None
        )
        if argument is None:
            continue
        value = _resolve_value(argument.value, variables)
        if not isinstance(value, bool):
            # Unknown variable -> count the selection for the worst case.
            continue
        if name == "skip" and value:
            return False
        if name == "include" and not value:
            return False
    return True


def _directive_cost_arg(field_def, key: str) -> int | None:
    """Read an integer argument from a ``@cost(...)`` SDL directive."""
    ast_node = getattr(field_def, "ast_node", None)
    if ast_node is None:
        return None
    for directive in ast_node.directives or ():
        if directive.name.value != "cost":
            continue
        for argument in directive.arguments or ():
            if (
                argument.name.value == key
                and isinstance(argument.value, gql_ast.IntValueNode)
            ):
                try:
                    value = int(argument.value.value)
                except (TypeError, ValueError):
                    return None
                return value if value >= 0 else None
    return None


def _collect_spreads(selection_set) -> list[str]:
    """All fragment-spread names reachable inside a selection set."""
    names: list[str] = []
    if selection_set is None:
        return names
    for selection in selection_set.selections:
        if isinstance(selection, gql_ast.FragmentSpreadNode):
            names.append(selection.name.value)
        elif isinstance(
            selection, (gql_ast.FieldNode, gql_ast.InlineFragmentNode)
        ):
            names.extend(_collect_spreads(selection.selection_set))
    return names


# --------------------------------------------------------------------------- #
# Analyzer
# --------------------------------------------------------------------------- #


class _Analyzer:
    def __init__(self, schema, document, variables, budget, operation_name, cfg):
        self.schema = schema
        self.document = document
        self.variables = variables
        self.budget = budget
        self.operation_name = operation_name
        self.cfg = cfg

        self.fragments: dict[str, gql_ast.FragmentDefinitionNode] = {
            d.name.value: d
            for d in document.definitions
            if isinstance(d, gql_ast.FragmentDefinitionNode)
        }

        self.total = 0
        self.cumulative = 0
        self.max_depth_seen = 0
        self.expanded_nodes = 0
        self.fragments_used: dict[str, None] = {}
        self.paths: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = []
        self.first_over: dict[str, Any] | None = None
        self.operation_info: dict[str, Any] | None = None

    def run(self) -> dict[str, Any]:
        cycles = self._fragment_cycles()
        if cycles:
            result = _empty_result("error")
            for cycle in cycles:
                result["errors"].append(
                    {
                        "code": "FRAGMENT_CYCLE",
                        "message": "Fragment cycle detected: "
                        + " -> ".join(cycle),
                        "details": {"cycle": cycle},
                    }
                )
            return result

        if self.cfg.validate_document:
            validation_errors = validate(
                self.schema, self.document, specified_rules
            )
            if validation_errors:
                result = _empty_result("invalid")
                for err in validation_errors:
                    result["errors"].append(
                        {
                            "code": "VALIDATION_FAILED",
                            "message": err.message,
                            "details": {"locations": _locations(err)},
                        }
                    )
                return result

        operation = self._pick_operation()
        op_type = operation.operation.value
        root_type = {
            "query": self.schema.query_type,
            "mutation": self.schema.mutation_type,
            "subscription": self.schema.subscription_type,
        }[op_type]
        if root_type is None:
            raise AnalysisAbort(
                "NO_ROOT_TYPE",
                f"Schema does not define a root type for '{op_type}' operations.",
            )

        self.operation_info = {
            "name": operation.name.value if operation.name else None,
            "type": op_type,
        }
        self.variables = self._merge_variables(operation)
        self._walk(
            operation.selection_set,
            root_type,
            (ROOT_LABELS[op_type],),
            depth=1,
            effective_multiplier=1,
            via_fragment=None,
        )
        status = "over_budget" if self.total > self.budget else "ok"
        return self._finalize(status)

    def _finalize(self, status: str) -> dict[str, Any]:
        result = _empty_result(status)
        result["operation"] = self.operation_info
        result["summary"] = {
            "total_cost": self.total,
            "budget": self.budget,
            "within_budget": self.total <= self.budget,
            "remaining_budget": self.budget - self.total,
            "max_depth": self.max_depth_seen,
            "field_count": len(self.paths),
            "fragments_used": list(self.fragments_used),
            "fragment_count": len(self.fragments_used),
        }
        result["first_over_budget_path"] = self.first_over
        result["paths"] = self.paths
        result["warnings"] = self.warnings
        return result

    # ----- fragments ------------------------------------------------------ #

    def _fragment_cycles(self) -> list[list[str]]:
        """Detect cycles in the fragment dependency graph (DFS, grey stack)."""
        graph = {
            name: _collect_spreads(fragment.selection_set)
            for name, fragment in self.fragments.items()
        }
        state: dict[str, int] = {}  # 1 = in stack, 2 = finished
        stack: list[str] = []
        cycles: list[list[str]] = []

        def visit(node: str) -> None:
            state[node] = 1
            stack.append(node)
            for nxt in graph.get(node, ()):
                if nxt not in graph:  # undefined spread — validation reports it
                    continue
                if state.get(nxt, 0) == 0:
                    visit(nxt)
                elif state[nxt] == 1:
                    index = stack.index(nxt)
                    cycles.append([*stack[index:], nxt])
            stack.pop()
            state[node] = 2

        for name in graph:
            if state.get(name, 0) == 0:
                visit(name)
        return cycles

    # ----- operation / variables ----------------------------------------- #

    def _pick_operation(self) -> gql_ast.OperationDefinitionNode:
        operations = [
            d
            for d in self.document.definitions
            if isinstance(d, gql_ast.OperationDefinitionNode)
        ]
        if not operations:
            raise AnalysisAbort(
                "NO_OPERATION", "Document contains no operation definition."
            )
        if self.operation_name:
            for op in operations:
                if op.name and op.name.value == self.operation_name:
                    return op
            available = [op.name.value for op in operations if op.name]
            raise AnalysisAbort(
                "OPERATION_NOT_FOUND",
                f"Operation '{self.operation_name}' not found. "
                f"Available operations: {available}",
            )
        if len(operations) > 1:
            available = [op.name.value for op in operations if op.name]
            raise AnalysisAbort(
                "AMBIGUOUS_OPERATION",
                f"Document contains {len(operations)} operations; "
                f"specify operation_name. Available: {available}",
            )
        return operations[0]

    def _merge_variables(self, operation) -> dict[str, Any]:
        """Operation variable defaults overlaid with the supplied sample."""
        merged: dict[str, Any] = {}
        for var_def in operation.variable_definitions or ():
            if var_def.default_value is not None:
                merged[var_def.variable.name.value] = value_from_ast_untyped(
                    var_def.default_value
                )
        merged.update(self.variables)
        return merged

    # ----- traversal ------------------------------------------------------ #

    def _walk(
        self,
        selection_set,
        parent_type,
        path: tuple[str, ...],
        depth: int,
        effective_multiplier: int,
        via_fragment: str | None,
    ) -> None:
        for selection in selection_set.selections:
            self.expanded_nodes += 1
            if self.expanded_nodes > self.cfg.max_expanded_nodes:
                raise AnalysisAbort(
                    "EXPANSION_LIMIT",
                    "Query expansion exceeds the limit of "
                    f"{self.cfg.max_expanded_nodes} selection nodes.",
                )

            if isinstance(selection, gql_ast.FieldNode):
                if not _should_include(selection.directives, self.variables):
                    continue
                self._visit_field(
                    selection,
                    parent_type,
                    path,
                    depth,
                    effective_multiplier,
                    via_fragment,
                )
            elif isinstance(selection, gql_ast.InlineFragmentNode):
                if not _should_include(selection.directives, self.variables):
                    continue
                target = self._fragment_type(selection.type_condition, parent_type, path)
                if target is None:
                    continue
                self._walk(
                    selection.selection_set,
                    target,
                    path,
                    depth,
                    effective_multiplier,
                    via_fragment,
                )
            elif isinstance(selection, gql_ast.FragmentSpreadNode):
                if not _should_include(selection.directives, self.variables):
                    continue
                name = selection.name.value
                fragment = self.fragments.get(name)
                if fragment is None:
                    self._warn(
                        "UNDEFINED_FRAGMENT",
                        f"Fragment '{name}' is not defined.",
                        ".".join(path),
                    )
                    continue
                target = self._fragment_type(
                    fragment.type_condition, parent_type, path
                )
                if target is None:
                    continue
                self.fragments_used.setdefault(name, None)
                self._walk(
                    fragment.selection_set,
                    target,
                    path,
                    depth,
                    effective_multiplier,
                    name,
                )

    def _fragment_type(self, type_condition, parent_type, path):
        if type_condition is None:
            return parent_type
        name = type_condition.name.value
        resolved = self.schema.get_type(name)
        if resolved is None:
            self._warn(
                "UNKNOWN_TYPE",
                f"Unknown type '{name}' in fragment type condition.",
                ".".join(path),
            )
            return None
        return resolved

    def _visit_field(
        self,
        node: gql_ast.FieldNode,
        parent_type,
        path: tuple[str, ...],
        depth: int,
        effective_multiplier: int,
        via_fragment: str | None,
    ) -> None:
        if depth > self.cfg.max_depth:
            raise AnalysisAbort(
                "MAX_DEPTH_EXCEEDED",
                f"Selection depth exceeds the analysis limit of "
                f"{self.cfg.max_depth}.",
            )

        name = node.name.value
        alias = node.alias.value if node.alias else None
        response_key = alias or name
        field_path = ".".join((*path, response_key))

        named_type = None
        if name.startswith("__"):
            # Introspection meta fields (__typename/__schema/__type).
            type_str, is_list, field_def = "String", False, None
        else:
            if not isinstance(parent_type, FIELD_CONTAINERS):
                self._warn(
                    "FIELD_ON_NON_COMPOSITE",
                    f"Field '{name}' cannot be selected on type "
                    f"'{parent_type.name}'.",
                    field_path,
                )
                return
            field_def = parent_type.fields.get(name)
            if field_def is None:
                self._warn(
                    "UNKNOWN_FIELD",
                    f"Cannot query field '{name}' on type "
                    f"'{parent_type.name}'.",
                    field_path,
                )
                return
            type_str = str(field_def.type)
            is_list = _contains_list(field_def.type)
            named_type = get_named_type(field_def.type)

        complexity = (
            _directive_cost_arg(field_def, "complexity") if field_def else None
        )
        if complexity is None:
            complexity = 1
        multiplier, multiplier_source = self._multiplier(
            field_def, node, is_list
        )

        new_effective = effective_multiplier * multiplier
        contribution = complexity * new_effective
        self.total += contribution
        self.cumulative += contribution
        self.max_depth_seen = max(self.max_depth_seen, depth)

        self.paths.append(
            {
                "path": field_path,
                "field": name,
                "alias": alias,
                "type": type_str,
                "depth": depth,
                "complexity": complexity,
                "multiplier": multiplier,
                "multiplier_source": multiplier_source,
                "effective_multiplier": new_effective,
                "cost": contribution,
                "cumulative_cost": self.cumulative,
                "via_fragment": via_fragment,
            }
        )

        if self.first_over is None and self.cumulative > self.budget:
            self.first_over = {
                "path": field_path,
                "cost": contribution,
                "cumulative_cost": self.cumulative,
                "budget": self.budget,
                "effective_multiplier": new_effective,
            }

        if (
            node.selection_set is not None
            and named_type is not None
            and isinstance(named_type, COMPOSITE_TYPES)
        ):
            self._walk(
                node.selection_set,
                named_type,
                (*path, response_key),
                depth + 1,
                new_effective,
                via_fragment,
            )

    def _multiplier(self, field_def, node, is_list: bool) -> tuple[int, str]:
        # 1. SDL directive wins.
        if field_def is not None:
            directive_value = _directive_cost_arg(field_def, "multiplier")
            if directive_value is not None:
                return directive_value, "directive:@cost"
        if not is_list:
            return 1, "none"
        # 2. Pagination argument resolved from the query / variable sample.
        for argument in node.arguments or ():
            if argument.name.value not in LIST_SIZE_ARGUMENTS:
                continue
            value = _resolve_value(argument.value, self.variables)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)) and value >= 0:
                return int(value), f"argument:{argument.name.value}"
        # 3. Configured default for lists.
        return self.cfg.default_list_multiplier, "default"

    def _warn(self, code: str, message: str, path: str | None) -> None:
        self.warnings.append({"code": code, "message": message, "path": path})
