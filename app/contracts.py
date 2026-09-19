"""JSON Schema 2020-12 契约：Schema 自校验与实例校验。

契约按文档可选挂载；校验统一使用 Draft 2020-12 方言
（无论 Schema 是否声明 ``$schema``）。错误以实例路径
（RFC 6901 JSON Pointer）稳定排序后全量返回，便于调用方定位。

本模块不依赖 FastAPI / SQLite，可独立单测。
"""
from __future__ import annotations

from typing import Any, Optional

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from .merge import json_pointer


def check_schema(schema: Any) -> Optional[str]:
    """校验 *schema* 本身是否为合法的 JSON Schema 2020-12。

    :returns: 合法返回 ``None``；非法返回人类可读的失败原因。
    """
    if not isinstance(schema, (dict, bool)):
        return "Schema 必须是 JSON 对象或布尔值"
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        return exc.message
    return None


def _error_sort_key(error: ValidationError) -> tuple:
    # 主键：实例路径（整数下标与字符串键分开可比，保证确定性）；
    # 同一路径上的多条错误再按 schema 路径与消息排序，全序稳定。
    path_key = tuple(
        (0, seg) if isinstance(seg, int) else (1, seg) for seg in error.path
    )
    schema_key = tuple(str(seg) for seg in error.schema_path)
    return (path_key, schema_key, error.message)


def validate(schema: Any, instance: Any) -> list[dict[str, Any]]:
    """用 *schema* 校验 *instance*，返回按实例路径稳定排序的全部错误。

    空列表表示通过。每个错误条目含 ``path``（RFC 6901 指针，根为 ``""``）、
    触发的校验关键字 ``validator`` 与可读消息 ``message``。
    """
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(instance), key=_error_sort_key)
    return [
        {
            "path": json_pointer([str(seg) for seg in error.path]),
            "validator": error.validator,
            "message": error.message,
        }
        for error in errors
    ]
