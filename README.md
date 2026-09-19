# 共享 JSON 配置三方合并 API

基于 **Python 3.11 + FastAPI + SQLite** 的共享 JSON 配置存储。文档更新携带
`base_revision`、`request_id` 与 [RFC 7386 JSON Merge Patch](https://www.rfc-editor.org/rfc/rfc7386)；
当基线过期（服务端已有更新修订）时，服务端对 **基线 / 当前版 / 来稿** 做递归三方比较：

- 双方修改的路径互不重叠，或改动结果相同 → **自动重放（replay）**，生成新修订；
- 同一路径被双方改成不同结果 → **409 冲突**，返回路径（RFC 6901 JSON Pointer）
  及三方值，**绝不写入**；
- 同一 `request_id` + 相同内容 → **幂等**，直接返回原结果；
- 同一 `request_id` + 不同内容（patch 或 base_revision 变化）→ **409**。

此外，每个文档可挂载一份可选的 **JSON Schema 2020-12 契约**：挂载后每次更新
（Merge Patch + 三方重放得到最终候选内容）都会先校验候选内容，不满足契约则
**422 且不写入**；删除契约后恢复原有无校验的更新行为。

## 一键启动

需要 Docker 与 Docker Compose v2：

```bash
docker compose up -d --build
```

容器启动时自动建表（FastAPI lifespan 执行 `CREATE TABLE IF NOT EXISTS`），
无需任何初始化命令。SQLite 数据持久化在命名卷 `config-data`（容器内 `/data`）。

停止与清理：

```bash
docker compose down          # 停止，保留数据
docker compose down -v       # 停止并删除数据卷
```

## 配置项

全部通过环境变量配置（均有默认值，可直接使用 `.env`，模板见 `.env.example`）：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CONFIG_PORT` | `8000` | 宿主机映射端口（容器内固定监听 8000） |
| `CONFIG_DB_PATH` | `/data/config.db` | SQLite 数据库文件路径，父目录自动创建 |
| `CONFIG_HOST` | `0.0.0.0` | 监听地址 |
| `CONFIG_HISTORY_MAX_LIMIT` | `200` | 修订历史单页返回上限 |

## 访问地址与端口

默认端口 `8000`：

| 地址 | 说明 |
| --- | --- |
| http://localhost:8000/docs | Swagger UI（可在线调试） |
| http://localhost:8000/redoc | ReDoc 文档 |
| http://localhost:8000/openapi.json | OpenAPI 描述 |
| http://localhost:8000/health | 健康检查（容器 HEALTHCHECK 也使用它） |

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/documents` | 创建文档（修订号从 1 开始） |
| `GET` | `/documents` | 列出文档 |
| `GET` | `/documents/{id}` | 读取当前版本（含 `revision`） |
| `PATCH` | `/documents/{id}` | 提交 Merge Patch（三方合并；有契约时校验候选内容） |
| `GET` | `/documents/{id}/revisions` | 修订历史（支持 `limit`/`offset`/`order=asc|desc`） |
| `GET` | `/documents/{id}/revisions/{rev}` | 查询指定修订的内容与 patch |
| `PUT` | `/documents/{id}/schema` | 设置 / 替换 JSON Schema 2020-12 契约 |
| `GET` | `/documents/{id}/schema` | 查询当前契约 |
| `DELETE` | `/documents/{id}/schema` | 删除契约，恢复原更新行为 |

### 创建文档

```bash
curl -X POST http://localhost:8000/documents \
  -H 'Content-Type: application/json' \
  -d '{"id": "demo", "content": {"a": 1, "b": 2, "c": {"x": 1}}, "author": "alice"}'
# id 可省略，省略时服务端生成；id 重复返回 409 document_exists
```

### 读取文档

```bash
curl http://localhost:8000/documents/demo
# {"id":"demo","revision":1,"content":{"a":1,"b":2,"c":{"x":1}},...}
```

读取后请记下 `revision`，作为下次更新的 `base_revision`。

### 更新（快路径：基线即当前版）

```bash
curl -X PATCH http://localhost:8000/documents/demo \
  -H 'Content-Type: application/json' \
  -d '{"base_revision": 1, "request_id": "req-001", "patch": {"a": 100}}'
```

```json
{
  "id": "demo",
  "revision": 2,
  "base_revision": 1,
  "request_id": "req-001",
  "replayed": false,
  "changed": true,
  "idempotent": false,
  "content": {"a": 100, "b": 2, "c": {"x": 1}}
}
```

### 更新（基线过期：路径不冲突 → 自动重放）

在别人基于 rev1 已把 `a` 改成 100（当前 rev2）之后，你仍基于 rev1 只改 `b`：

```bash
curl -X PATCH http://localhost:8000/documents/demo \
  -H 'Content-Type: application/json' \
  -d '{"base_revision": 1, "request_id": "req-002", "patch": {"b": 200}}'
```

```json
{ "revision": 3, "replayed": true, "changed": true,
  "content": {"a": 100, "b": 200, "c": {"x": 1}} }
```

三方在对象层级递归比较；重放结果与当前版完全相同时不产生新修订
（`"changed": false`，`revision` 保持当前号）。

### 更新（分歧路径 → 409，且不写入）

rev1 中 `a=1`，服务端已把 `a` 改成 `100`，来稿基于 rev1 要改成 `999`：

```bash
curl -i -X PATCH http://localhost:8000/documents/demo \
  -H 'Content-Type: application/json' \
  -d '{"base_revision": 1, "request_id": "req-003", "patch": {"a": 999}}'
```

```json
HTTP/1.1 409 Conflict
{
  "detail": {
    "error": "conflict",
    "document_id": "demo",
    "request_id": "req-003",
    "base_revision": 1,
    "current_revision": 2,
    "conflicts": [
      {
        "path": "/a",
        "values": {"base": 1, "current": 100, "theirs": 999},
        "missing": []
      }
    ]
  }
}
```

- `path` 为 RFC 6901 JSON Pointer，根冲突为 `""`，键中的 `~`/`/` 转义为 `~0`/`~1`；
- `values` 给出该路径上的 **基线值 / 服务端当前值 / 来稿值**；
- 键缺失（被删除）时对应位置为 `null`，并在 `missing` 中标注
  （如 `"missing": ["current"]`，以区别于真实存储的 JSON `null`）；
- 冲突请求在事务内回滚：文档内容、修订号均不变，且该 `request_id` 不被占用，
  调用方修正基线/内容后可用同一 ID 重试。

### 幂等

```bash
# 同 request_id + 同 (base_revision, patch) 再发任意次 → 返回原结果
# 响应中 "idempotent": true，不产生新修订
```

同 `request_id` 但 `base_revision` 或 `patch` 不同 → `409 request_id_conflict`。
指纹为 `sha256(base_revision + 规范化JSON(patch))`，幂等记录按 `(document_id, request_id)` 持久化。

### 修订历史

```bash
curl 'http://localhost:8000/documents/demo/revisions?order=desc&limit=20&offset=0'
curl http://localhost:8000/documents/demo/revisions/3
```

历史列表为轻量条目（修订号、基线号、request_id、是否重放、作者、时间）；
单条修订详情含完整 `content`、原始 `patch` 与重放标记。

## JSON Schema 2020-12 契约（可选）

每个文档可挂载一份契约，统一按 **Draft 2020-12** 方言解释（无论是否声明 `$schema`）。
契约存于 `contracts` 表，随服务启动自动建表，老数据卷升级无需手工迁移。

### 设置 / 替换契约

```bash
curl -X PUT http://localhost:8000/documents/demo/schema \
  -H 'Content-Type: application/json' \
  -d '{
        "schema": {
          "type": "object",
          "properties": {
            "name":     {"type": "string"},
            "replicas": {"type": "integer", "minimum": 1}
          },
          "required": ["name", "replicas"]
        }
      }'
```

设置分两步，任一失败都不会生效：

1. **校验 Schema 本身**——不是合法的 2020-12 Schema（含非对象/非布尔）→
   `422 invalid_schema`；
2. **同一事务内验证当前文档**——当前内容不满足契约 →
   `422 schema_validation_failed`（事务回滚，旧契约若有则保留）。
   事务持写锁，并发 PATCH 会排队，不存在“验证后内容被改”的窗口。

```json
{
  "document_id": "demo",
  "dialect": "https://json-schema.org/draft/2020-12/schema",
  "schema": {"type": "object", "...": "..."},
  "created_at": "2026-09-19T09:40:25.267833Z",
  "updated_at": "2026-09-19T09:40:25.267833Z"
}
```

### 查询契约

```bash
curl http://localhost:8000/documents/demo/schema
# 未挂载契约 → 404 {"detail": {"error": "schema_not_found", ...}}
```

### 契约生效后的更新校验

挂载契约后，`PATCH` 先完成原有的 Merge Patch 与三方重放，**再校验最终候选内容**；
不满足契约时整个事务回滚——不写修订、不占用 `request_id`（修正后可用同一 ID 重试）：

```bash
curl -i -X PATCH http://localhost:8000/documents/demo \
  -H 'Content-Type: application/json' \
  -d '{"base_revision": 2, "request_id": "req-100", "patch": {"replicas": 0, "name": 5}}'
```

```json
HTTP/1.1 422 Unprocessable Entity
{
  "detail": {
    "error": "schema_validation_failed",
    "document_id": "demo",
    "request_id": "req-100",
    "revision": 2,
    "errors": [
      {"path": "/name",     "validator": "type",    "message": "5 is not of type 'string'"},
      {"path": "/replicas", "validator": "minimum", "message": "0 is less than the minimum of 1"}
    ]
  }
}
```

- `errors` 列出**全部**校验错误，按实例路径（RFC 6901 JSON Pointer，根为 `""`）
  稳定排序，数组下标形如 `/items/0`；
- 三方合并的路径冲突（`409 conflict`）优先于契约校验——先解决分歧，再谈合法性；
- 幂等重放（同 `request_id` 同内容）直接返回首次结果，不重复校验。

### 删除契约

```bash
curl -X DELETE http://localhost:8000/documents/demo/schema
# {"document_id":"demo","deleted":true}
# 未挂载契约 → 404 schema_not_found
```

删除后 `PATCH` 恢复原有无校验的更新行为（此前被契约拒绝的 patch 可正常写入）。

## Merge Patch 语义要点（RFC 7386）

- patch 是**对象**时按键合并：键值为 `null` 表示**删除该键**（不是存成 null）；
- patch 为**非对象**（数组 / 字符串 / 数字 / 布尔 / `null`）时**整体替换**文档；
  数组整体替换，不按下标合并；
- 对非对象的目标打对象补丁时，目标先视为空对象；
- 真实的 JSON `null` 作为键值可以正常存储，仅 patch 中的 `null` 才表示删除。

## 状态码

| 状态码 | 场景 |
| --- | --- |
| `200` | 更新成功（含幂等重放）；契约设置 / 查询 / 删除成功 |
| `201` | 文档创建成功 |
| `404 not_found` | 文档不存在 |
| `404 revision_not_found` | 修订不存在 |
| `404 schema_not_found` | 文档未挂载契约（查询 / 删除时） |
| `409 conflict` | 三方合并存在分歧路径 |
| `409 request_id_conflict` | 同一 request_id 对应了不同内容 |
| `409 document_exists` | 创建时 ID 已存在 |
| `422 unknown_base_revision` | base_revision 不是该文档的现存修订 |
| `422 invalid_schema` | 契约本身不是合法的 JSON Schema 2020-12 |
| `422 schema_validation_failed` | 当前文档（设置契约时）或最终候选内容（PATCH 时）不满足契约 |

## 项目结构

```
.
├── app/
│   ├── main.py      # FastAPI 路由与更新编排（事务、幂等、冲突响应、契约端点）
│   ├── merge.py     # RFC 7386 打补丁 + 递归三方合并（无框架依赖，可单测）
│   ├── contracts.py # JSON Schema 2020-12 自校验与实例校验（错误按实例路径排序）
│   ├── db.py        # SQLite 连接、自动建表、文档/修订/幂等/契约表读写
│   ├── schemas.py   # Pydantic 请求模型（额外字段拒绝）
│   └── config.py    # 环境变量配置
├── tests/
│   ├── test_merge.py    # 合并算法单元测试（25 例）
│   ├── test_api.py      # API 集成测试（23 例，独立临时数据库）
│   └── test_contracts.py# 契约集成测试（17 例，独立临时数据库）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

## 本地开发（不用 Docker）

```bash
pip install -r requirements.txt
export CONFIG_DB_PATH=./data/config.db   # 可选，默认 /data/config.db
uvicorn app.main:app --reload --port 8000
```

运行测试：

```bash
python3 -m pytest -q
```
