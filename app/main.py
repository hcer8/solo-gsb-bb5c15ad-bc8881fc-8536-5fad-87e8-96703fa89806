"""FastAPI 应用：GraphQL 静态查询成本分析服务。

端点
----
* ``GET  /health``                 健康检查
* ``POST /analyze``                提交 SDL + 查询进行成本分析（带指纹缓存）
* ``GET  /cache``                  列出缓存的分析结果
* ``GET  /cache/stats``            缓存统计
* ``GET  /cache/{fingerprint}``    读取单条缓存
* ``DELETE /cache/{fingerprint}``  删除单条缓存
* ``GET  /``                       服务信息
"""
from __future__ import annotations

from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .analyzer import AnalysisError, run_analysis
from .cache import Cache, cache as default_cache
from .config import settings
from .fingerprint import compute_fingerprint
from .schemas import AnalyzeRequest, CacheEntry, CacheListResponse


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.cache = default_cache
    try:
        yield
    finally:
        default_cache.close()


app = FastAPI(
    title="GraphQL Query Cost Analyzer",
    description=(
        "为 GraphQL 网关提供静态查询最坏成本分析：展开 fragments / aliases，"
        "按字段深度与列表乘数计费，检测片段循环，返回逐路径解释，"
        "并按请求指纹缓存结果。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


def _get_cache(request: Request) -> Cache:
    return getattr(request.app.state, "cache", default_cache)


# ---------------------------------------------------------------------------
# 错误处理
# ---------------------------------------------------------------------------
@app.exception_handler(AnalysisError)
async def analysis_error_handler(request: Request, exc: AnalysisError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            }
        },
    )


# ---------------------------------------------------------------------------
# 基本端点
# ---------------------------------------------------------------------------
@app.get("/", tags=["meta"])
def index() -> dict[str, Any]:
    return {
        "service": "graphql-query-cost-analyzer",
        "docs": "/docs",
        "health": "/health",
        "analyze": "POST /analyze",
        "cost_model": {
            "default_field_weight": settings.default_field_weight,
            "typename_weight": settings.typename_weight,
            "depth_multiplier": settings.depth_multiplier,
            "default_list_size": settings.default_list_size,
            "max_list_size": settings.max_list_size,
        },
    }


@app.get("/health", tags=["meta"])
def health(request: Request) -> dict[str, Any]:
    cache_store = _get_cache(request)
    return {"status": "ok", "cache": cache_store.stats()}


# ---------------------------------------------------------------------------
# 分析
# ---------------------------------------------------------------------------
@app.post("/analyze", tags=["analysis"])
def analyze(payload: AnalyzeRequest, request: Request) -> dict[str, Any]:
    fingerprint = compute_fingerprint(
        schema_sdl=payload.schema_sdl,
        query=payload.query,
        variables=payload.variables,
        operation_name=payload.operation_name,
        budget=payload.budget,
    )
    cache_store = _get_cache(request)

    if not payload.no_cache:
        cached = cache_store.get(fingerprint)
        if cached is not None:
            return {
                "cached": True,
                "fingerprint": fingerprint,
                **cached["result"],
            }

    result = run_analysis(
        payload.schema_sdl,
        payload.query,
        variables=payload.variables,
        operation_name=payload.operation_name,
        budget=payload.budget,
    )
    cache_store.put(
        fingerprint,
        status="ok",
        result=result,
        operation_name=result["operation"]["name"],
        total_cost=result["total_cost"],
        budget=payload.budget,
        over_budget=result["over_budget"],
    )
    return {"cached": False, "fingerprint": fingerprint, **result}


# ---------------------------------------------------------------------------
# 缓存管理
# ---------------------------------------------------------------------------
@app.get("/cache", response_model=CacheListResponse, tags=["cache"])
def list_cache(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> CacheListResponse:
    rows = _get_cache(request).list_entries(limit=limit, offset=offset)
    return CacheListResponse(
        total_returned=len(rows),
        limit=limit,
        offset=offset,
        entries=[CacheEntry(**row) for row in rows],
    )


@app.get("/cache/stats", tags=["cache"])
def cache_stats(request: Request) -> dict[str, Any]:
    return _get_cache(request).stats()


@app.get("/cache/{fingerprint}", tags=["cache"])
def get_cache_entry(fingerprint: str, request: Request) -> dict[str, Any]:
    entry = _get_cache(request).get(fingerprint)
    if entry is None:
        raise HTTPException(status_code=404, detail="缓存条目不存在")
    return entry


@app.delete("/cache/{fingerprint}", tags=["cache"])
def delete_cache_entry(fingerprint: str, request: Request) -> dict[str, Any]:
    removed = _get_cache(request).delete(fingerprint)
    if not removed:
        raise HTTPException(status_code=404, detail="缓存条目不存在")
    return {"deleted": True, "fingerprint": fingerprint}
