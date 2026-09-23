"""Pydantic request/response models for the analysis API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

AnalysisStatus = Literal["ok", "over_budget", "invalid", "error"]


class AnalysisOptions(BaseModel):
    default_list_multiplier: int | None = Field(
        default=None,
        ge=1,
        description="Per-request override for the default list multiplier.",
    )
    validate_document: bool = Field(
        default=True,
        description="Run standard GraphQL validation before cost analysis.",
    )


class AnalyzeRequest(BaseModel):
    schema_sdl: str = Field(..., min_length=1, description="GraphQL SDL schema")
    query: str = Field(..., min_length=1, description="GraphQL query document")
    variables: dict[str, Any] = Field(
        default_factory=dict, description="Variable sample used for resolution"
    )
    budget: int = Field(default=1000, ge=0, description="Cost budget (>= 0)")
    operation_name: str | None = Field(
        default=None, description="Operation to analyze in multi-op documents"
    )
    refresh: bool = Field(
        default=False, description="Bypass the fingerprint cache when true"
    )
    options: AnalysisOptions = Field(default_factory=AnalysisOptions)


class ErrorDetail(BaseModel):
    code: str
    message: str
    path: str | None = None
    details: dict[str, Any] | None = None


class PathExplanation(BaseModel):
    path: str = Field(..., description="Root-to-field response path")
    field: str
    alias: str | None
    type: str = Field(..., description="SDL return type of the field")
    depth: int
    complexity: int
    multiplier: int
    multiplier_source: Literal[
        "none", "default", "argument:first", "argument:last",
        "argument:limit", "argument:size", "argument:take", "directive:@cost",
    ]
    effective_multiplier: int = Field(
        ..., description="Product of all multipliers from root to this field"
    )
    cost: int = Field(..., description="Effective contribution of this field")
    cumulative_cost: int = Field(
        ..., description="Running total in document traversal order"
    )
    via_fragment: str | None


class BudgetBreach(BaseModel):
    path: str
    cost: int
    cumulative_cost: int
    budget: int
    effective_multiplier: int


class OperationInfo(BaseModel):
    name: str | None
    type: str


class CostSummary(BaseModel):
    total_cost: int
    budget: int
    within_budget: bool
    remaining_budget: int
    max_depth: int
    field_count: int
    fragments_used: list[str]
    fragment_count: int


class AnalysisMeta(BaseModel):
    cost_model_version: str
    default_list_multiplier: int
    analysis_ms: float


class AnalyzeResponse(BaseModel):
    fingerprint: str
    cached: bool
    status: AnalysisStatus
    operation: OperationInfo | None
    summary: CostSummary | None
    first_over_budget_path: BudgetBreach | None
    paths: list[PathExplanation]
    warnings: list[ErrorDetail]
    errors: list[ErrorDetail]
    meta: AnalysisMeta


class AnalysisSummary(BaseModel):
    fingerprint: str
    operation_name: str | None
    operation_type: str | None
    status: AnalysisStatus
    total_cost: int | None
    budget: int | None
    within_budget: bool | None
    created_at: str
    last_accessed_at: str
    hit_count: int


class AnalysisListResponse(BaseModel):
    items: list[AnalysisSummary]
    total: int
    limit: int
    offset: int


class AnalysisDetailResponse(BaseModel):
    summary: AnalysisSummary
    request: dict[str, Any]
    result: AnalyzeResponse
