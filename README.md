# GraphQL Query Cost Analyzer

为 GraphQL 网关提供**静态查询最坏成本分析**的 Python FastAPI 服务。

客户端提交 SDL schema、查询文档、变量样例与成本预算，服务会：

- 展开 **fragments**（含内联片段）与 **aliases**，按真实字段出现计费；
- 按**字段深度**（几何权重）与**列表乘数**（分页参数 / `@listSize` / 缺省值）计算最坏成本；
- 检测**片段循环**（`...A → ...B → ...A`、自引用、跨字段循环）并返回 422 与环路径；
- 返回**逐字段**与**逐根→叶路径**的成本解释；
- 超预算时指出**首个超限字段出现**及其所属路径、越界金额；
- 以 **SHA-256 请求指纹**为键，用 **SQLite** 缓存分析结果并提供缓存管理 API。

## 快速启动（Docker Compose）

```bash
docker compose up --build
```

服务监听 **http://localhost:8000**

| 用途 | 地址 |
| --- | --- |
| 服务信息 | http://localhost:8000/ |
| 健康检查 | http://localhost:8000/health |
| Swagger 文档（可在线试调） | http://localhost:8000/docs |
| ReDoc | http://localhost:8000/redoc |

SQLite 数据库持久化在命名卷 `cost-data`（容器内 `/data/cost_cache.db`）。

停止并清理：

```bash
docker compose down            # 停止
docker compose down -v         # 停止并删除缓存数据卷
```

## 本地直接运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试：

```bash
pip install pytest
pytest -q
```

## 配置（环境变量）

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `COST_DB_PATH` | `/data/cost_cache.db` | SQLite 数据库文件路径（容器内） |
| `COST_DEFAULT_FIELD_WEIGHT` | `1` | 未标注 `@cost` 的字段基础权重 |
| `COST_DEFAULT_LIST_SIZE` | `50` | 无法静态推断列表长度时的假定条数（最坏情况） |
| `COST_MAX_LIST_SIZE` | `1000` | 列表条数推断上限（钳制值，防 `first: 10_000_000`） |
| `COST_DEPTH_MULTIPLIER` | `1.5` | 深度因子底数；第 d 层字段乘以 `m^(d-1)`，设为 `1` 关闭深度加权 |
| `COST_TYPENAME_WEIGHT` | `0.1` | `__typename` 元字段权重 |
| `COST_MAX_FRAGMENT_DEPTH` | `64` | 展开深度硬上限（异常嵌套兜底） |

可用 `.env`（参见 `.env.example`，复制为 `.env` 后 `docker compose` 会自动加载）。

## API

### `POST /analyze`

请求体：

```json
{
  "schema_sdl": "type Query { ... }",
  "query": "query Q($n: Int) { ... }",
  "variables": { "n": 10 },
  "operation_name": "Q",
  "budget": 5000,
  "no_cache": false
}
```

- `schema_sdl`（必填）：GraphQL 服务 SDL。若使用 `@cost` / `@listSize` 指令，需在 SDL 中声明它们（见 `examples/`）。
- `query`（必填）：查询 / mutation 文档。
- `variables`（可选）：变量样例，用于确定 `first` / `limit` / `@listSize(size: $var)` 等的静态值；无法静态确定时按缺省列表大小处理。
- `operation_name`（多 operation 文档必填其一）。
- `budget`（必填，≥ 0）：最坏成本预算。
- `no_cache`：为 `true` 时跳过缓存读写。

响应中 `cached` 表示是否命中指纹缓存，`fingerprint` 为缓存键；分析结果包含：

| 字段 | 含义 |
| --- | --- |
| `total_cost` / `over_budget` | 最坏总成本、是否超预算 |
| `max_depth` / `field_count` | 最大字段深度、展开后字段出现次数 |
| `fields[]` | 每个字段出现的明细：路径、alias、深度、基础权重、深度因子、继承的列表乘数、列表维度、采纳的条数、单项贡献 |
| `paths[]` | 每条根→叶路径的成本与累计，并在首条超限路径上标记 `first_over_budget` |
| `first_over_budget` | 首个使累计成本越过预算的字段：字段路径、前后累计值、该字段贡献、越界金额 `exceeded_by`、所属叶路径与定位 |
| `cost_model` | 本次使用的成本模型参数（也参与指纹计算） |

错误响应统一为 `422`：

```json
{ "error": { "code": "fragment_cycle", "message": "...", "details": [ ... ] } }
```

错误码：`empty_schema` / `invalid_schema` / `empty_query` / `syntax_error` /
`fragment_cycle` / `validation_error` / `no_operation` / `ambiguous_operation` /
`operation_not_found` / `unsupported_operation` / `invalid_budget` /
`expansion_too_deep` / `unknown_fragment`。

### 缓存 API

| 方法 & 路径 | 说明 |
| --- | --- |
| `GET /cache?limit=50&offset=0` | 列出缓存（按创建时间倒序） |
| `GET /cache/stats` | 条目数、命中次数、超预算条目数 |
| `GET /cache/{fingerprint}` | 读取单条（命中计数 +1） |
| `DELETE /cache/{fingerprint}` | 删除单条 |

指纹 = SHA-256（规范化的 SDL + 查询 + operation_name + variables + budget + 成本模型参数），因此预算或成本模型不同不会互相命中。

## 成本模型

单个字段出现的贡献：

```
contribution = weight × depth_factor × inherited_list_multiplier
depth_factor  = COST_DEPTH_MULTIPLIER ^ (depth - 1)        # 根字段 depth=1
total_cost    = Σ contribution（按文档顺序累加，alias 分支分别计入）
```

**列表乘数**（作用于子树；片段/内联片段不增加深度）：

1. 查询字段上的 `@listSize(size: N)`（字面量或变量样例），钳制到 `COST_MAX_LIST_SIZE`；
2. 切片参数 `first` / `last` / `limit` / `take` / `count` 的静态值；
3. 回退到 `COST_DEFAULT_LIST_SIZE`。

- 对象型列表（如 `[User!]!`）：列表字段自身只解析一次（计 1 次），其**子树**乘以条数；嵌套列表连乘（如 `friends(first:10){ posts(limit:5){...} }` 内层乘数为 `10×5`）。
- 标量叶列表（如 `matrix: [[Int!]!]`）无下层子树，数据项本身计入字段贡献，按 `size^维度数` 计费。
- 多维列表（`[[Item!]!]`）按维度连乘。
- 片段类型条件按“成立”处理（最坏情况），但不按抽象类型的实现数量重复计费。
- `@skip(if: true)` / `@include(if: false)` 可被字面量或变量样例静态判定时裁剪分支；不可静态判定则保留（最坏情况）。
- 字段权重取 SDL 中的 `@cost(weight: N)`，否则用默认值；`__typename` 单独配置。

## 示例

```bash
# 直接用仓库内示例文件（jq 可选）
curl -s http://localhost:8000/analyze \
  -H 'Content-Type: application/json' \
  -d @examples/request.json | jq '.total_cost, .over_budget, .first_over_budget'
```

最小内联示例：

```bash
curl -s http://localhost:8000/analyze \
  -H 'Content-Type: application/json' \
  -d '{
    "schema_sdl": "directive @cost(weight: Float!) on FIELD_DEFINITION\ntype Query { users(first: Int): [User!]! @cost(weight: 5) }\ntype User { id: ID! @cost(weight: 1) }",
    "query": "{ users(first: 3) { id } }",
    "variables": {},
    "budget": 10
  }'
```

## 项目结构

```
app/
  main.py        FastAPI 路由（/analyze、缓存管理、健康检查）
  analyzer.py    SDL/查询解析、片段展开、循环检测、最坏成本与路径解释
  cache.py       SQLite 缓存（WAL，线程安全）
  fingerprint.py 请求指纹（SHA-256）
  config.py      环境变量配置
  schemas.py     Pydantic 请求/响应模型
examples/        可直接 curl 的 schema、query 与 request.json
tests/           引擎与 API 测试（pytest）
```
