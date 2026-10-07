"""模型工具调用与本地工具之间的协议转换、基础参数校验和结果归一化。"""

import json
from typing import Any, Dict, List, Optional


_STANDARD_RESULT_KEYS = {"ok", "error", "data"}


def parse_tool_arguments(tool_call: Dict[str, Any]) -> Dict[str, Any]:
    """接收服务商返回的参数对象或 JSON 字符串；拒绝数组等无法作为具名参数的值。"""
    function = tool_call.get("function") or {}
    raw_arguments = function.get("arguments")
    if raw_arguments is None or raw_arguments == "":
        return {}
    if isinstance(raw_arguments, dict):
        return raw_arguments
    if not isinstance(raw_arguments, str):
        raise ValueError("tool arguments must be a JSON object")
    parsed = json.loads(raw_arguments)
    if not isinstance(parsed, dict):
        raise ValueError("tool arguments must be a JSON object")
    return parsed


def tool_call_name(tool_call: Dict[str, Any]) -> str:
    return str((tool_call.get("function") or {}).get("name") or "").strip()


def tool_call_id(tool_call: Dict[str, Any], fallback_index: int) -> str:
    return str(tool_call.get("id") or "tool_call_{0}".format(fallback_index))


def is_standard_tool_result(result: Any) -> bool:
    return isinstance(result, dict) and _STANDARD_RESULT_KEYS.issubset(result)


def tool_result_error(result: Any) -> Optional[str]:
    if not isinstance(result, dict):
        return "tool result must be a JSON object"
    if is_standard_tool_result(result) and result.get("ok") is True:
        return None
    error = result.get("error")
    if error is None:
        return None
    return str(error).strip() or None


def tool_success_result(data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"ok": True, "error": None, "data": data if isinstance(data, dict) else {}}


def tool_error_result(
    error: Any,
    data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    message = str(error or "tool failed").strip() or "tool failed"
    return {"ok": False, "error": message, "data": data if isinstance(data, dict) else {}}


def normalize_tool_result(result: Any) -> Dict[str, Any]:
    """统一为 ok/error/data，兼容旧工具直接返回数据字典的形式。"""
    if not isinstance(result, dict):
        return tool_error_result("tool result must be a JSON object")
    if is_standard_tool_result(result):
        data = result.get("data")
        ok = bool(result.get("ok"))
        error = None if ok else str(result.get("error") or "tool failed").strip()
        return {
            "ok": ok,
            "error": error or (None if ok else "tool failed"),
            "data": data if isinstance(data, dict) else {},
        }
    data = dict(result)
    error = data.pop("error", None)
    return tool_error_result(error, data) if error is not None else tool_success_result(data)


def tool_result_data(result: Any) -> Dict[str, Any]:
    if is_standard_tool_result(result):
        data = result.get("data")
        return data if isinstance(data, dict) else {}
    return result if isinstance(result, dict) else {}


def _matches_schema_type(value: Any, schema_type: str) -> bool:
    checks = {
        "string": lambda: isinstance(value, str),
        "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
        "number": lambda: isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": lambda: isinstance(value, bool),
        "object": lambda: isinstance(value, dict),
        "array": lambda: isinstance(value, list),
        "null": lambda: value is None,
    }
    check = checks.get(schema_type)
    return True if check is None else check()


def validate_tool_arguments(arguments: Dict[str, Any], schema: Any) -> Optional[str]:
    """返回基础 Schema 校验错误或 None；只检查必填、类型、额外字段和数值范围。

    这不是完整 JSON Schema 验证器，也不替代工具内部对文档存在性等业务条件的检查。
    """
    if not isinstance(arguments, dict):
        return "tool arguments must be a JSON object"
    if not isinstance(schema, dict):
        return None
    if schema.get("type") not in (None, "object"):
        return "tool input_schema type must be object"

    properties = schema.get("properties") or {}
    if not isinstance(properties, dict):
        properties = {}
    required = schema.get("required") or []
    if not isinstance(required, list):
        required = []
    for field in required:
        if field not in arguments or arguments.get(field) is None:
            return "tool argument '{0}' is required".format(field)
    if schema.get("additionalProperties") is False:
        for field in arguments:
            if field not in properties:
                return "unexpected tool argument '{0}'".format(field)

    for field, value in arguments.items():
        field_schema = properties.get(field)
        if not isinstance(field_schema, dict):
            continue
        expected_type = field_schema.get("type")
        if isinstance(expected_type, str):
            expected_types: List[str] = [expected_type]
        elif isinstance(expected_type, list):
            expected_types = [item for item in expected_type if isinstance(item, str)]
        else:
            expected_types = []
        if expected_types and not any(
            _matches_schema_type(value, item) for item in expected_types
        ):
            return "tool argument '{0}' must be {1}".format(
                field,
                " or ".join(expected_types),
            )
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        minimum = field_schema.get("minimum")
        if minimum is not None and value < minimum:
            return "tool argument '{0}' must be >= {1}".format(field, minimum)
        maximum = field_schema.get("maximum")
        if maximum is not None and value > maximum:
            return "tool argument '{0}' must be <= {1}".format(field, maximum)
    return None


def tool_call_signature(tool_name: str, arguments: Dict[str, Any]) -> str:
    return "{0}:{1}".format(
        tool_name,
        json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True),
    )


__all__ = [
    "normalize_tool_result",
    "parse_tool_arguments",
    "tool_call_id",
    "tool_call_name",
    "tool_call_signature",
    "tool_error_result",
    "tool_result_data",
    "tool_result_error",
    "validate_tool_arguments",
]
