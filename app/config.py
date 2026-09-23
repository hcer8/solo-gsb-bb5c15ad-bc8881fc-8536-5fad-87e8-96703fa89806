"""应用配置，全部来自环境变量。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| COST_DB_PATH | /data/cost_cache.db | SQLite 数据库路径 |
| COST_DEFAULT_FIELD_WEIGHT | 1 | 字段默认基础成本 |
| COST_DEFAULT_LIST_SIZE | 50 | 未声明 ``@listSize`` / 切片参数时假定的列表长度 |
| COST_MAX_LIST_SIZE | 1000 | 切片参数静态值超过该值时钳制（最坏情况上限） |
| COST_DEPTH_MULTIPLIER | 1.5 | 每层深度的复合乘数（1 表示不按深度加权） |
| COST_TYPENAME_WEIGHT | 0.1 | ``__typename`` 元字段基础成本 |
| COST_MAX_FRAGMENT_DEPTH | 64 | 展开片段时的最大嵌套深度（兜底保护） |
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class Settings:
    db_path: str = os.getenv("COST_DB_PATH", "/data/cost_cache.db")
    default_field_weight: float = _float_env("COST_DEFAULT_FIELD_WEIGHT", 1.0)
    default_list_size: int = _int_env("COST_DEFAULT_LIST_SIZE", 50)
    max_list_size: int = _int_env("COST_MAX_LIST_SIZE", 1000)
    depth_multiplier: float = _float_env("COST_DEPTH_MULTIPLIER", 1.5)
    typename_weight: float = _float_env("COST_TYPENAME_WEIGHT", 0.1)
    max_fragment_depth: int = _int_env("COST_MAX_FRAGMENT_DEPTH", 64)


settings = Settings()
