"""GraphQL 静态查询最坏成本分析引擎。

成本模型（最坏情况）
-------------------
* 每个字段出现一次计一次成本；alias 产生的重复字段分支分别计费
  （响应中可能返回多份数据，按最坏情况相加）。
* 字段基础权重：schema 字段定义上的 ``@cost(weight: N)`` 指令，
  否则取配置 ``COST_DEFAULT_FIELD_WEIGHT``（``__typename`` 取
  ``COST_TYPENAME_WEIGHT``）。
* 深度加权：字段位于第 ``d`` 层（根字段 d=1），深度因子为
  ``COST_DEPTH_MULTIPLIER ** (d - 1)``。片段 / 内联片段不增加深度。
* 列表乘数：字段返回列表时，其**子树**按假定条数放大（字段本身只解析
  一次）。条数推断优先级：
  1. 查询字段上的 ``@listSize(size: N)`` 指令（支持字面量与变量）；
  2. 切片参数 ``first`` / ``last`` / ``limit`` / ``take`` / ``count``
     的静态字面量或 ``variables`` 样例中的整数值；
  3. ``COST_DEFAULT_LIST_SIZE``。
  推断值会被钳制到 ``COST_MAX_LIST_SIZE``；多维列表（如 ``[[Item]]``）
  按维度连乘。
* 片段类型条件按“成立”处理（最坏情况），但不会按抽象类型的实现数量
  重复计费；``@skip(if: true)`` / ``@include(if: false)`` 在静态可知
  （字面量或变量样例）时裁剪分支，不可知则保留。

预算判定
--------
按文档顺序累加每个字段出现的 contribution，与 ``total_cost`` 口径一致；
记录第一个使累计成本越过 budget 的字段出现，以及它所属的根→叶路径。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from graphql import (
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLError,
    GraphQLField,
    GraphQLList,
    GraphQLNamedType,
    GraphQLSchema,
    InlineFragmentNode,
    OperationDefinitionNode,
    SelectionSetNode,
    build_schema,
    get_named_type,
    parse,
    validate,
)
from graphql.language import (
    BooleanValueNode,
    DirectiveNode,
    IntValueNode,
    VariableNode,
)

from .config import settings

# 常见的切片/分页参数名（参数值为正整数时视为列表上界）
SLICING_ARG_NAMES = frozenset({"first", "last", "limit", "take", "count"})

_ROUND = 6


class AnalysisError(Exception):
    """静态分析阶段的可预期错误（非法输入、循环片段等）。"""

    def __init__(self, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or []


@dataclass
class FieldContribution:
    path: list[str]
    field_name: str
    alias: str | None
    depth: int
    base_weight: float
    depth_factor: float
    inherited_multiplier: float
    list_dimensions: int
    imposed_list_size: int | None
    contribution: float
    location: dict[str, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "field_name": self.field_name,
            "alias": self.alias,
            "depth": self.depth,
            "base_weight": self.base_weight,
            "depth_factor": round(self.depth_factor, _ROUND),
            "inherited_list_multiplier": round(self.inherited_multiplier, _ROUND),
            "list_dimensions": self.list_dimensions,
            "imposed_list_size": self.imposed_list_size,
            "contribution": round(self.contribution, _ROUND),
            "location": self.location,
        }


@dataclass
class PathExplanation:
    """一条根 → 叶路径的成本解释（path_cost 为该分支上的字段成本和）。"""

    path: list[str]
    leaf_field: str
    path_cost: float
    first_over_budget: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "leaf_field": self.leaf_field,
            "path_cost": round(self.path_cost, _ROUND),
            "first_over_budget": self.first_over_budget,
        }


@dataclass
class _FirstCross:
    field_path: list[str]
    field_name: str
    alias: str | None
    cumulative_before: float
    cumulative_after: float
    contribution: float
    location: dict[str, int] | None
    leaf_path: list[str] | None = None
    leaf_path_cost: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_path": self.field_path,
            "field_name": self.field_name,
            "alias": self.alias,
            "cumulative_before": round(self.cumulative_before, _ROUND),
            "cumulative_after": round(self.cumulative_after, _ROUND),
            "contribution": round(self.contribution, _ROUND),
            "exceeded_by": round(
                self.cumulative_after - 0, _ROUND  # 占位，下面用 budget 重算
            ),
            "leaf_path": self.leaf_path,
            "leaf_path_cost": (
                round(self.leaf_path_cost, _ROUND)
                if self.leaf_path_cost is not None
                else None
            ),
            "location": self.location,
        }


@dataclass
class _Accumulator:
    budget: float
    fields: list[FieldContribution] = field(default_factory=list)
    paths: list[PathExplanation] = field(default_factory=list)
    total: float = 0.0
    max_depth: int = 0
    first_cross: _FirstCross | None = None


# ---------------------------------------------------------------------------
# 解析与校验
# ---------------------------------------------------------------------------
def build_schema_from_sdl(sdl: str) -> GraphQLSchema:
    if not sdl or not sdl.strip():
        raise AnalysisError("empty_schema", "schema_sdl 不能为空")
    try:
        return build_schema(sdl)
    except GraphQLError as exc:
        raise AnalysisError(
            "invalid_schema", f"SDL schema 解析失败: {exc.message}", _error(exc)
        )


def parse_query(query: str) -> DocumentNode:
    if not query or not query.strip():
        raise AnalysisError("empty_query", "query 不能为空")
    try:
        return parse(query)
    except GraphQLError as exc:
        raise AnalysisError(
            "syntax_error", f"查询文档语法错误: {exc.message}", _error(exc)
        )


def _error(exc: GraphQLError) -> list[dict[str, Any]]:
    return [
        {
            "message": exc.message,
            "locations": [
                {"line": loc.line, "column": loc.column}
                for loc in (exc.locations or [])
            ],
        }
    ]


def _index_fragments(
    document: DocumentNode,
) -> dict[str, FragmentDefinitionNode]:
    return {
        d.name.value: d
        for d in document.definitions
        if isinstance(d, FragmentDefinitionNode)
    }


def detect_fragment_cycles(
    fragments: dict[str, FragmentDefinitionNode],
) -> list[dict[str, Any]]:
    """返回检测到的全部片段环，每个环给出名称链与位置。"""
    cycles: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    state: dict[str, int] = {}  # 0=访问中, 1=完成
    stack: list[str] = []

    def direct_spreads(frag: FragmentDefinitionNode) -> list[FragmentSpreadNode]:
        out: list[FragmentSpreadNode] = []
        _collect_spreads(frag.selection_set, out)
        return out

    def dfs(name: str) -> None:
        state[name] = 0
        stack.append(name)
        for spread in direct_spreads(fragments[name]):
            target = spread.name.value
            if target not in fragments:
                continue  # 未知片段交给 schema 校验报错
            mark = state.get(target)
            if mark == 0:
                idx = stack.index(target)
                chain = stack[idx:] + [target]
                sig = tuple(sorted(chain[:-1]))
                if sig not in seen:
                    seen.add(sig)
                    cycles.append(
                        {
                            "cycle": chain,
                            "message": "检测到片段循环: " + " -> ".join(chain),
                            "location": _node_location(spread),
                        }
                    )
            elif mark is None:
                dfs(target)
        stack.pop()
        state[name] = 1

    for frag_name in fragments:
        if state.get(frag_name) is None:
            dfs(frag_name)
    return cycles


def _collect_spreads(node: Any, sink: list[FragmentSpreadNode]) -> None:
    if isinstance(node, SelectionSetNode):
        for child in node.selections:
            _collect_spreads(child, sink)
        return
    if isinstance(node, FragmentSpreadNode):
        sink.append(node)
    selection_set = getattr(node, "selection_set", None)
    if selection_set is not None:
        for child in selection_set.selections:
            _collect_spreads(child, sink)


def _node_location(node: Any) -> dict[str, int] | None:
    loc = getattr(node, "loc", None)
    if loc is not None:
        pos = loc.source.get_location(loc.start)
        return {"line": pos.line, "column": pos.column}
    return None


# ---------------------------------------------------------------------------
# 指令 / 参数静态求值
# ---------------------------------------------------------------------------
def _directive_bool(
    directive: DirectiveNode, arg_name: str, variables: dict[str, Any]
) -> bool | None:
    for arg in directive.arguments:
        if arg.name.value != arg_name:
            continue
        value = arg.value
        if isinstance(value, BooleanValueNode):
            return value.value
        if isinstance(value, VariableNode):
            sample = variables.get(value.name.value)
            if isinstance(sample, bool):
                return sample
    return None


def _is_skipped(
    directives: tuple[DirectiveNode, ...], variables: dict[str, Any]
) -> bool:
    """静态判断选择集是否被 @skip / @include 裁掉；不可知则保留（最坏情况）。"""
    skipped = False
    include: bool | None = None
    for directive in directives:
        name = directive.name.value
        if name == "skip" and _directive_bool(directive, "if", variables) is True:
            skipped = True
        elif name == "include":
            val = _directive_bool(directive, "if", variables)
            if val is False:
                include = False
            elif val is True and include is None:
                include = True
    return skipped or include is False


def _static_int(value_node: Any, variables: dict[str, Any]) -> int | None:
    if isinstance(value_node, IntValueNode):
        try:
            return int(value_node.value)
        except ValueError:
            return None
    if isinstance(value_node, VariableNode):
        sample = variables.get(value_node.name.value)
        if isinstance(sample, bool) or not isinstance(sample, int):
            return None
        return sample
    return None


def _clamp_size(size: int) -> int:
    return max(0, min(size, settings.max_list_size))


def _infer_list_size(field_node: FieldNode, variables: dict[str, Any]) -> int:
    """推断列表字段的最坏条数（调用方已确认该字段返回列表）。"""
    # 1) @listSize(size / assumedSize: N)
    for directive in field_node.directives:
        if directive.name.value != "listSize":
            continue
        for arg in directive.arguments:
            if arg.name.value in ("size", "assumedSize"):
                size = _static_int(arg.value, variables)
                if size is not None:
                    return _clamp_size(size)
    # 2) 切片参数
    for arg in field_node.arguments:
        if arg.name.value in SLICING_ARG_NAMES:
            size = _static_int(arg.value, variables)
            if size is not None and size >= 0:
                return _clamp_size(size)
    # 3) 缺省
    return settings.default_list_size


# ---------------------------------------------------------------------------
# schema 元数据
# ---------------------------------------------------------------------------
def _field_weight(field_def: GraphQLField | None, field_name: str) -> float:
    if field_name == "__typename":
        return settings.typename_weight
    if field_def is not None and field_def.ast_node is not None:
        for directive in field_def.ast_node.directives:
            if directive.name.value == "cost":
                for arg in directive.arguments:
                    if arg.name.value == "weight":
                        try:
                            return float(arg.value.value)  # type: ignore[attr-defined]
                        except (AttributeError, ValueError):
                            pass
    return settings.default_field_weight


def _list_dimensions(field_def: GraphQLField | None) -> int:
    if field_def is None:
        return 0
    dims = 0
    type_: Any = field_def.type
    while hasattr(type_, "of_type"):
        if isinstance(type_, GraphQLList):
            dims += 1
        type_ = type_.of_type
    return dims


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def analyze_document(
    schema: GraphQLSchema,
    document: DocumentNode,
    *,
    variables: dict[str, Any],
    operation_name: str | None,
    budget: float,
) -> dict[str, Any]:
    fragments = _index_fragments(document)
    operations = [
        d for d in document.definitions if isinstance(d, OperationDefinitionNode)
    ]
    operation = _select_operation(operations, operation_name)

    root_type: GraphQLNamedType | None = {
        "query": schema.query_type,
        "mutation": schema.mutation_type,
        "subscription": schema.subscription_type,
    }.get(operation.operation.value)
    if root_type is None:
        raise AnalysisError(
            "unsupported_operation",
            f"schema 未定义 {operation.operation.value} 根类型",
        )

    acc = _Accumulator(budget=budget)
    _visit_selection(
        operation.selection_set,
        parent_type=root_type,
        schema=schema,
        fragments=fragments,
        variables=variables,
        path=[],
        depth=0,
        multiplier=1.0,
        path_charge=0.0,
        frag_stack=set(),
        acc=acc,
    )

    # 将“首个超限字段”关联到它所属的根→叶路径
    first_over = acc.first_cross
    if first_over is not None:
        prefix_len = len(first_over.field_path)
        prefix = tuple(first_over.field_path)
        for p in acc.paths:
            if tuple(p.path[:prefix_len]) == prefix:
                p.first_over_budget = True
                first_over.leaf_path = p.path
                first_over.leaf_path_cost = p.path_cost
                break

    over = acc.total > budget + 1e-9
    return {
        "operation": {
            "name": operation.name.value if operation.name else None,
            "type": operation.operation.value,
        },
        "budget": budget,
        "total_cost": round(acc.total, _ROUND),
        "over_budget": over,
        "max_depth": acc.max_depth,
        "field_count": len(acc.fields),
        "fields": [f.to_dict() for f in acc.fields],
        "paths": [p.to_dict() for p in acc.paths],
        "first_over_budget": _serialize_cross(first_over, budget),
        "cost_model": {
            "default_field_weight": settings.default_field_weight,
            "typename_weight": settings.typename_weight,
            "depth_multiplier": settings.depth_multiplier,
            "default_list_size": settings.default_list_size,
            "max_list_size": settings.max_list_size,
        },
    }


def _serialize_cross(cross: _FirstCross | None, budget: float) -> dict[str, Any] | None:
    if cross is None:
        return None
    data = cross.to_dict()
    data["budget"] = budget
    data["exceeded_by"] = round(cross.cumulative_after - budget, _ROUND)
    return data


def _select_operation(operations: list[Any], operation_name: str | None) -> Any:
    if not operations:
        raise AnalysisError("no_operation", "查询文档中没有可执行的 operation")
    if operation_name is not None:
        for op in operations:
            if op.name and op.name.value == operation_name:
                return op
        raise AnalysisError(
            "operation_not_found",
            f"未找到名为 {operation_name!r} 的 operation",
        )
    if len(operations) == 1:
        return operations[0]
    names = [op.name.value if op.name else "(anonymous)" for op in operations]
    raise AnalysisError(
        "ambiguous_operation",
        "文档包含多个 operation，必须通过 operation_name 指定: "
        + ", ".join(names),
    )


def _visit_selection(
    selection_set: Any,
    *,
    parent_type: GraphQLNamedType | None,
    schema: GraphQLSchema,
    fragments: dict[str, FragmentDefinitionNode],
    variables: dict[str, Any],
    path: list[str],
    depth: int,
    multiplier: float,
    path_charge: float,
    frag_stack: set[str],
    acc: _Accumulator,
) -> None:
    if depth > settings.max_fragment_depth:
        raise AnalysisError(
            "expansion_too_deep",
            f"字段/片段展开深度超过上限 {settings.max_fragment_depth}，"
            "可能存在异常嵌套",
        )
    for selection in selection_set.selections:
        if _is_skipped(getattr(selection, "directives", ()), variables):
            continue
        if isinstance(selection, FieldNode):
            _visit_field(
                selection,
                parent_type=parent_type,
                schema=schema,
                fragments=fragments,
                variables=variables,
                path=path,
                depth=depth,
                multiplier=multiplier,
                path_charge=path_charge,
                frag_stack=frag_stack,
                acc=acc,
            )
        elif isinstance(selection, FragmentSpreadNode):
            name = selection.name.value
            fragment = fragments.get(name)
            if fragment is None:
                # 正常情况下 validate() 已拦截，这里做防御处理
                raise AnalysisError(
                    "unknown_fragment", f"未定义的片段 ...{name}"
                )
            if name in frag_stack:
                raise AnalysisError(
                    "fragment_cycle",
                    f"检测到片段循环（片段 ...{name} 间接展开了自身）",
                    [{"cycle": [name, name], "reentrant_fragment": name}],
                )
            frag_type = schema.get_type(fragment.type_condition.name.value)
            frag_stack.add(name)
            _visit_selection(
                fragment.selection_set,
                parent_type=frag_type,
                schema=schema,
                fragments=fragments,
                variables=variables,
                path=path,
                depth=depth,  # 片段展开不增加字段深度
                multiplier=multiplier,
                path_charge=path_charge,
                frag_stack=frag_stack,
                acc=acc,
            )
            frag_stack.discard(name)
        elif isinstance(selection, InlineFragmentNode):
            inline_type = parent_type
            if selection.type_condition is not None:
                inline_type = schema.get_type(
                    selection.type_condition.name.value
                )
            _visit_selection(
                selection.selection_set,
                parent_type=inline_type,
                schema=schema,
                fragments=fragments,
                variables=variables,
                path=path,
                depth=depth,
                multiplier=multiplier,
                path_charge=path_charge,
                frag_stack=frag_stack,
                acc=acc,
            )


def _visit_field(
    field_node: FieldNode,
    *,
    parent_type: GraphQLNamedType | None,
    schema: GraphQLSchema,
    fragments: dict[str, FragmentDefinitionNode],
    variables: dict[str, Any],
    path: list[str],
    depth: int,
    multiplier: float,
    path_charge: float,
    frag_stack: set[str],
    acc: _Accumulator,
) -> None:
    field_name = field_node.name.value
    response_key = field_node.alias.value if field_node.alias else field_name

    field_def: GraphQLField | None = None
    fields_map = getattr(parent_type, "fields", None)
    if fields_map is not None:
        try:
            field_def = fields_map.get(field_name)
        except Exception:
            field_def = None

    child_depth = depth + 1
    acc.max_depth = max(acc.max_depth, child_depth)

    weight = _field_weight(field_def, field_name)
    depth_factor = settings.depth_multiplier ** (child_depth - 1)

    dims = _list_dimensions(field_def)
    imposed_size: int | None = None
    child_multiplier = multiplier
    if dims > 0:
        imposed_size = _infer_list_size(field_node, variables)
        # 对象型列表：字段本身只解析一次，子树按条数放大；
        # 标量/枚举/叶列表（无 selection set，如 [[Int!]!]）：
        # 数据项本身即返回成本，按维度连乘计入字段自身。
        child_multiplier = multiplier * float(imposed_size) ** dims

    self_multiplier = (
        child_multiplier
        if field_node.selection_set is None
        else multiplier
    )
    contribution = weight * depth_factor * self_multiplier
    new_path_charge = path_charge + contribution

    field_path = path + [response_key]
    before = acc.total
    acc.total += contribution

    acc.fields.append(
        FieldContribution(
            path=field_path,
            field_name=field_name,
            alias=field_node.alias.value if field_node.alias else None,
            depth=child_depth,
            base_weight=weight,
            depth_factor=depth_factor,
            inherited_multiplier=multiplier,
            list_dimensions=dims,
            imposed_list_size=imposed_size,
            contribution=contribution,
            location=_node_location(field_node),
        )
    )

    if acc.first_cross is None and acc.total > acc.budget + 1e-9:
        acc.first_cross = _FirstCross(
            field_path=list(field_path),
            field_name=field_name,
            alias=field_node.alias.value if field_node.alias else None,
            cumulative_before=before,
            cumulative_after=acc.total,
            contribution=contribution,
            location=_node_location(field_node),
        )

    if field_node.selection_set is not None:
        child_type = (
            get_named_type(field_def.type) if field_def is not None else None
        )
        _visit_selection(
            field_node.selection_set,
            parent_type=child_type,
            schema=schema,
            fragments=fragments,
            variables=variables,
            path=field_path,
            depth=child_depth,
            multiplier=child_multiplier,
            path_charge=new_path_charge,
            frag_stack=frag_stack,
            acc=acc,
        )
    else:
        acc.paths.append(
            PathExplanation(
                path=list(field_path),
                leaf_field=response_key,
                path_cost=new_path_charge,
            )
        )


def run_analysis(
    sdl: str,
    query: str,
    *,
    variables: dict[str, Any] | None = None,
    operation_name: str | None = None,
    budget: float,
) -> dict[str, Any]:
    """完整流水线：解析 SDL/查询 → 循环检测 → 校验 → 成本分析。"""
    if budget is None or not math.isfinite(float(budget)):
        raise AnalysisError("invalid_budget", "budget 必须是有限的非负数字")
    if budget < 0:
        raise AnalysisError("invalid_budget", "budget 不能为负数")

    schema = build_schema_from_sdl(sdl)
    document = parse_query(query)

    # operation 选择（多 operation 消歧）在校验前完成
    operations = [
        d for d in document.definitions if isinstance(d, OperationDefinitionNode)
    ]
    _select_operation(operations, operation_name)

    fragments = _index_fragments(document)
    cycles = detect_fragment_cycles(fragments)
    if cycles:
        raise AnalysisError(
            "fragment_cycle", cycles[0]["message"], cycles
        )

    validation_errors = validate(schema, document)
    if validation_errors:
        raise AnalysisError(
            "validation_error",
            "查询未通过 schema 校验: "
            + "; ".join(e.message for e in validation_errors[:5]),
            [
                {
                    "message": e.message,
                    "locations": [
                        {"line": loc.line, "column": loc.column}
                        for loc in (e.locations or [])
                    ],
                }
                for e in validation_errors
            ],
        )

    return analyze_document(
        schema,
        document,
        variables=variables or {},
        operation_name=operation_name,
        budget=float(budget),
    )
