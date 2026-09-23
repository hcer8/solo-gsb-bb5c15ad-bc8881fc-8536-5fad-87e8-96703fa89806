"""请求指纹：规范化输入后取 SHA-256，作为缓存键。

参与指纹的字段：SDL、查询文本、operation_name、规范化后的 variables 样例、
budget 以及服务端成本模型参数。预算或成本模型不同时视为不同请求。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .config import settings


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def compute_fingerprint(
    *,
    schema_sdl: str,
    query: str,
    variables: dict[str, Any] | None,
    operation_name: str | None,
    budget: float,
) -> str:
    payload = {
        "sdl": schema_sdl.strip(),
        "query": query.strip(),
        "variables": variables or {},
        "operation_name": operation_name,
        "budget": float(budget),
        "cost_model": {
            "default_field_weight": settings.default_field_weight,
            "default_list_size": settings.default_list_size,
            "max_list_size": settings.max_list_size,
            "depth_multiplier": settings.depth_multiplier,
            "typename_weight": settings.typename_weight,
        },
    }
    raw = _canonical(payload)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
