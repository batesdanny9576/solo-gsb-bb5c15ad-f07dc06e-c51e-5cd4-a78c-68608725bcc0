"""JSON Schema 2020-12 契约的端到端集成测试（独立临时 SQLite 文件）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def _create(client, content=None, doc_id="cfg"):
    body = {"id": doc_id}
    if content is not None:
        body["content"] = content
    r = client.post("/documents", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _patch(client, doc_id, base_revision, request_id, patch):
    return client.patch(
        f"/documents/{doc_id}",
        json={
            "base_revision": base_revision,
            "request_id": request_id,
            "patch": patch,
        },
    )


def _put_schema(client, doc_id, schema):
    return client.put(f"/documents/{doc_id}/schema", json={"schema": schema})


CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "replicas": {"type": "integer", "minimum": 1},
    },
    "required": ["name", "replicas"],
}


# ---------------- 设置 / 查询 / 删除契约 ----------------


def test_put_and_get_contract(client):
    _create(client, {"name": "web", "replicas": 2})
    r = _put_schema(client, "cfg", CONFIG_SCHEMA)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["document_id"] == "cfg"
    assert body["schema"] == CONFIG_SCHEMA
    assert body["dialect"] == "https://json-schema.org/draft/2020-12/schema"

    g = client.get("/documents/cfg/schema")
    assert g.status_code == 200
    assert g.json()["schema"] == CONFIG_SCHEMA


def test_put_invalid_schema_rejected(client):
    _create(client, {"a": 1})
    r = _put_schema(client, "cfg", {"type": "not-a-type"})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "invalid_schema"
    # 未生效
    assert client.get("/documents/cfg/schema").status_code == 404


def test_put_non_object_schema_rejected(client):
    _create(client, {"a": 1})
    for bad in ("text", 42, [1, 2], None):
        r = _put_schema(client, "cfg", bad)
        assert r.status_code == 422, bad
        assert r.json()["detail"]["error"] == "invalid_schema"


def test_put_schema_failing_current_doc_not_stored(client):
    _create(client, {"name": "web", "replicas": 0})  # replicas 违反 minimum
    r = _put_schema(client, "cfg", CONFIG_SCHEMA)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "schema_validation_failed"
    assert detail["errors"][0]["path"] == "/replicas"
    # 契约不得落库
    assert client.get("/documents/cfg/schema").status_code == 404


def test_put_replaces_existing_contract(client):
    _create(client, {"name": "web", "replicas": 2})
    assert _put_schema(client, "cfg", CONFIG_SCHEMA).status_code == 200
    # 换成更严格的契约，当前文档仍满足 → 替换成功
    stricter = dict(CONFIG_SCHEMA, required=["name", "replicas"], additionalProperties=False)
    assert _put_schema(client, "cfg", stricter).status_code == 200
    assert client.get("/documents/cfg/schema").json()["schema"] == stricter
    # 换成当前文档不满足的契约 → 拒绝且保留旧契约
    impossible = {"type": "object", "required": ["missing"]}
    r = _put_schema(client, "cfg", impossible)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "schema_validation_failed"
    assert client.get("/documents/cfg/schema").json()["schema"] == stricter


def test_get_and_delete_without_contract_404(client):
    _create(client, {"a": 1})
    r = client.get("/documents/cfg/schema")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "schema_not_found"
    r = client.delete("/documents/cfg/schema")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "schema_not_found"


def test_contract_endpoints_on_missing_document_404(client):
    assert client.get("/documents/ghost/schema").status_code == 404
    assert client.delete("/documents/ghost/schema").status_code == 404
    r = _put_schema(client, "ghost", CONFIG_SCHEMA)
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "not_found"


def test_delete_contract_restores_plain_update(client):
    _create(client, {"name": "web", "replicas": 1})
    assert _put_schema(client, "cfg", CONFIG_SCHEMA).status_code == 200
    # 契约存在时非法更新被拒
    assert _patch(client, "cfg", 1, "bad-1", {"replicas": 0}).status_code == 422
    # 删除契约
    d = client.delete("/documents/cfg/schema")
    assert d.status_code == 200
    assert d.json() == {"document_id": "cfg", "deleted": True}
    assert client.get("/documents/cfg/schema").status_code == 404
    # 原更新行为恢复：同样的 patch 现在成功
    r = _patch(client, "cfg", 1, "bad-1", {"replicas": 0})
    assert r.status_code == 200
    assert r.json()["content"]["replicas"] == 0


# ---------------- PATCH 与契约 ----------------


def test_patch_valid_under_contract_succeeds(client):
    _create(client, {"name": "web", "replicas": 1})
    _put_schema(client, "cfg", CONFIG_SCHEMA)
    r = _patch(client, "cfg", 1, "ok-1", {"replicas": 3})
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 2
    assert r.json()["content"] == {"name": "web", "replicas": 3}


def test_patch_violating_contract_422_and_writes_nothing(client):
    _create(client, {"name": "web", "replicas": 1})
    _put_schema(client, "cfg", CONFIG_SCHEMA)
    before = client.get("/documents/cfg").json()

    r = _patch(client, "cfg", 1, "viol-1", {"replicas": 0, "name": 5})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "schema_validation_failed"
    assert detail["request_id"] == "viol-1"
    # 全部错误按实例路径稳定排序：/name 在 /replicas 前
    paths = [e["path"] for e in detail["errors"]]
    assert paths == sorted(paths) == ["/name", "/replicas"]
    assert all(e["validator"] and e["message"] for e in detail["errors"])

    # 不写修订
    after = client.get("/documents/cfg").json()
    assert after == before
    hist = client.get("/documents/cfg/revisions").json()["items"]
    assert [h["revision"] for h in hist] == [1]
    # 不占用 request_id：修正后可用同一 ID 重试
    r2 = _patch(client, "cfg", 1, "viol-1", {"replicas": 5})
    assert r2.status_code == 200
    assert r2.json()["revision"] == 2


def test_patch_delete_required_key_fails(client):
    _create(client, {"name": "web", "replicas": 1})
    _put_schema(client, "cfg", CONFIG_SCHEMA)
    r = _patch(client, "cfg", 1, "del-name", {"name": None})
    assert r.status_code == 422
    errors = r.json()["detail"]["errors"]
    assert errors[0]["path"] == ""  # required 错误挂在实例根
    assert errors[0]["validator"] == "required"


def test_stale_base_replay_validates_merged_result(client):
    _create(client, {"name": "web", "replicas": 1, "mode": "a"})
    _put_schema(client, "cfg", CONFIG_SCHEMA)
    # 服务端另一路写入：mode a → b（rev 2）
    assert _patch(client, "cfg", 1, "srv", {"mode": "b"}).status_code == 200
    # 来稿基于 rev1 把 replicas 改成 0：重放干净但最终候选违反契约 → 422
    r = _patch(client, "cfg", 1, "cli-zero", {"replicas": 0})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "schema_validation_failed"
    assert client.get("/documents/cfg").json()["revision"] == 2
    # 来稿基于 rev1 把 replicas 调为 3：路径不重叠，三方重放后候选合法
    r = _patch(client, "cfg", 1, "cli-scale", {"replicas": 3})
    assert r.status_code == 200, r.text
    assert r.json()["replayed"] is True
    assert r.json()["content"] == {"name": "web", "replicas": 3, "mode": "b"}


def test_conflict_takes_precedence_over_schema(client):
    # 三方冲突在契约校验之前：分歧路径仍返回 409 conflict
    _create(client, {"name": "web", "replicas": 1})
    _put_schema(client, "cfg", CONFIG_SCHEMA)
    _patch(client, "cfg", 1, "srv", {"replicas": 2})
    r = _patch(client, "cfg", 1, "cli", {"replicas": 3})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "conflict"


def test_idempotent_replay_not_revalidated(client):
    _create(client, {"name": "web", "replicas": 1})
    _put_schema(client, "cfg", CONFIG_SCHEMA)
    r1 = _patch(client, "cfg", 1, "idem", {"replicas": 4})
    assert r1.status_code == 200
    r2 = _patch(client, "cfg", 1, "idem", {"replicas": 4})
    assert r2.status_code == 200
    assert r2.json()["idempotent"] is True
    assert r2.json()["revision"] == r1.json()["revision"]


def test_boolean_schemas(client):
    _create(client, {"a": 1})
    # true：任何内容都合法
    assert _put_schema(client, "cfg", True).status_code == 200
    assert _patch(client, "cfg", 1, "any", {"b": 2}).status_code == 200
    # false：任何文档都不合法，当前文档验证不通过 → 契约设置被拒
    r = _put_schema(client, "cfg", False)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "schema_validation_failed"
    # 旧契约（true）保持不变
    assert client.get("/documents/cfg/schema").json()["schema"] is True


def test_errors_sorted_across_nesting_and_indices(client):
    # 初始文档满足契约，设置成功
    _create(client, {"items": [1, 2], "z": "s", "a": "s"})
    schema = {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": {"type": "integer"}},
            "z": {"type": "string"},
            "a": {"type": "string"},
        },
    }
    assert _put_schema(client, "cfg", schema).status_code == 200
    r = _patch(client, "cfg", 1, "multi", {"items": ["x", 1, "y"], "z": 2, "a": 3})
    assert r.status_code == 422
    paths = [e["path"] for e in r.json()["detail"]["errors"]]
    # 实例路径字典序稳定：/a < /items/0 < /items/2 < /z
    assert paths == ["/a", "/items/0", "/items/2", "/z"]


def test_noop_patch_still_checked_against_contract(client):
    # 契约设置后 no-op patch 的候选内容即当前内容（必合法）→ 不生成新修订
    _create(client, {"name": "web", "replicas": 1})
    _put_schema(client, "cfg", CONFIG_SCHEMA)
    r = _patch(client, "cfg", 1, "noop", {"replicas": 1})
    assert r.status_code == 200
    assert r.json()["changed"] is False
    assert r.json()["revision"] == 1
