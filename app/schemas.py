"""HTTP 请求 / 响应 Pydantic 模型。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AnalyzeRequest(BaseModel):
    schema_sdl: str = Field(
        ...,
        min_length=1,
        description="GraphQL 服务的 SDL schema 文本",
    )
    query: str = Field(
        ...,
        min_length=1,
        description="待分析的 GraphQL 查询 / mutation 文档",
    )
    variables: dict[str, Any] | None = Field(
        default=None,
        description="变量样例，用于确定 first/limit/@listSize 的静态值",
    )
    operation_name: str | None = Field(
        default=None,
        description="文档含多个 operation 时指定要分析的名称",
    )
    budget: float = Field(
        ...,
        ge=0,
        description="本次查询允许的最坏成本上限",
    )
    no_cache: bool = Field(
        default=False,
        description="为 true 时跳过缓存读写，强制重新分析",
    )


class CacheEntry(BaseModel):
    fingerprint: str
    status: str
    result: dict[str, Any]
    operation_name: str | None
    total_cost: float | None
    budget: float | None
    over_budget: bool
    hit_count: int
    created_at: float
    updated_at: float


class CacheListResponse(BaseModel):
    total_returned: int
    limit: int
    offset: int
    entries: list[CacheEntry]
