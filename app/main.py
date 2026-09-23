"""FastAPI application exposing the GraphQL cost analysis service."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request

from . import __version__
from .analyzer import AnalyzerConfig, analyze_query, compute_fingerprint
from .config import get_settings
from .db import AnalysisStore
from .models import (
    AnalysisDetailResponse,
    AnalysisListResponse,
    AnalysisSummary,
    AnalyzeRequest,
    AnalyzeResponse,
)

logger = logging.getLogger("costguard")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.store = AnalysisStore(settings.db_path)
    logger.info("analysis store ready at %s", settings.db_path)
    yield
    app.state.store.close()


app = FastAPI(
    title="GraphQL Cost Guard",
    description=(
        "Static worst-case cost analysis for GraphQL queries. "
        "Expands fragments/aliases, applies field-depth and list multipliers, "
        "detects fragment cycles and explains cost per path."
    ),
    version=__version__,
    lifespan=lifespan,
)


def get_store(request: Request) -> AnalysisStore:
    return request.app.state.store


# --------------------------------------------------------------------------- #
# Meta endpoints
# --------------------------------------------------------------------------- #


@app.get("/healthz", tags=["meta"])
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/v1/config", tags=["meta"])
def effective_config() -> dict[str, object]:
    settings = get_settings()
    return {
        "cost_model_version": settings.cost_model_version,
        "default_list_multiplier": settings.default_list_multiplier,
        "max_depth": settings.max_depth,
        "max_expanded_nodes": settings.max_expanded_nodes,
        "db_path": settings.db_path,
    }


# --------------------------------------------------------------------------- #
# Analysis endpoints
# --------------------------------------------------------------------------- #


@app.post(
    "/v1/analyze",
    response_model=AnalyzeResponse,
    tags=["analysis"],
    summary="Analyze a GraphQL query",
)
def analyze(
    request_body: AnalyzeRequest,
    store: AnalysisStore = Depends(get_store),
) -> AnalyzeResponse:
    settings = get_settings()
    default_multiplier = (
        request_body.options.default_list_multiplier
        or settings.default_list_multiplier
    )
    config = AnalyzerConfig(
        default_list_multiplier=default_multiplier,
        max_depth=settings.max_depth,
        max_expanded_nodes=settings.max_expanded_nodes,
        cost_model_version=settings.cost_model_version,
        validate_document=request_body.options.validate_document,
    )

    fingerprint = compute_fingerprint(
        {
            "schema_sdl": request_body.schema_sdl,
            "query": request_body.query,
            "variables": request_body.variables,
            "budget": request_body.budget,
            "operation_name": request_body.operation_name,
            "options": {
                "default_list_multiplier": default_multiplier,
                "validate_document": request_body.options.validate_document,
            },
            "cost_model_version": config.cost_model_version,
        }
    )

    if not request_body.refresh:
        cached = store.get(fingerprint)
        if cached is not None:
            store.record_hit(fingerprint)
            return AnalyzeResponse(
                fingerprint=fingerprint, cached=True, **cached["result"]
            )

    result = analyze_query(
        schema_sdl=request_body.schema_sdl,
        query=request_body.query,
        variables=request_body.variables,
        budget=request_body.budget,
        operation_name=request_body.operation_name,
        config=config,
    )
    store.save(
        fingerprint,
        request_body.model_dump(mode="json"),
        result,
    )
    return AnalyzeResponse(fingerprint=fingerprint, cached=False, **result)


@app.get(
    "/v1/analyses",
    response_model=AnalysisListResponse,
    tags=["analysis"],
    summary="List cached analyses",
)
def list_analyses(
    status: str | None = Query(
        default=None,
        pattern="^(ok|over_budget|invalid|error)$",
        description="Optional status filter.",
    ),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    store: AnalysisStore = Depends(get_store),
) -> AnalysisListResponse:
    items, total = store.list(limit=limit, offset=offset, status=status)
    return AnalysisListResponse(
        items=[AnalysisSummary(**row) for row in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@app.get(
    "/v1/analyses/{fingerprint}",
    response_model=AnalysisDetailResponse,
    tags=["analysis"],
    summary="Fetch one cached analysis by fingerprint",
)
def get_analysis(
    fingerprint: str,
    store: AnalysisStore = Depends(get_store),
) -> AnalysisDetailResponse:
    # Count the hit before reading so the returned summary is up to date.
    store.record_hit(fingerprint)
    record = store.get(fingerprint)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"analysis '{fingerprint}' not found",
        )
    return AnalysisDetailResponse(
        summary=AnalysisSummary(**record["summary"]),
        request=record["request"],
        result=AnalyzeResponse(
            fingerprint=fingerprint, cached=True, **record["result"]
        ),
    )


@app.delete(
    "/v1/analyses/{fingerprint}",
    status_code=204,
    tags=["analysis"],
    summary="Delete a cached analysis",
)
def delete_analysis(
    fingerprint: str,
    store: AnalysisStore = Depends(get_store),
) -> None:
    if not store.delete(fingerprint):
        raise HTTPException(
            status_code=404,
            detail=f"analysis '{fingerprint}' not found",
        )
