import os

# 必须在导入 app 之前设定成本模型默认值
os.environ.setdefault("COST_DEPTH_MULTIPLIER", "1")
os.environ.setdefault("COST_DEFAULT_LIST_SIZE", "10")
os.environ.setdefault("COST_DEFAULT_FIELD_WEIGHT", "1")
os.environ.setdefault("COST_MAX_LIST_SIZE", "100")
os.environ.setdefault("COST_TYPENAME_WEIGHT", "0.5")
os.environ.setdefault("COST_DB_PATH", "/tmp/test_cost_cache.db")
