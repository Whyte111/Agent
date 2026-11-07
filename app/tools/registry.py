"""工具注册表：只做描述、入参校验、结果归一化，不含业务规则。

骨架 7 节的对话侧白名单：web_search / doc_search / memory_read / make_ppt，
**不含 send_mail**——发信唯一入口是 meeting.pipeline。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


class ToolError(Exception):
    pass


@dataclass
class Tool:
    name: str
    description: str
    parameters: Dict[str, Any]
    func: Callable[..., Any]

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _check_type(value: Any, expected: str) -> bool:
    mapping = {
        "string": str,
        "number": (int, float),
        "integer": int,
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    py = mapping.get(expected)
    if py is None:
        return True
    if expected in ("number", "integer") and isinstance(value, bool):
        return False
    return isinstance(value, py)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError("工具重复注册: %s" % tool.name)
        self._tools[tool.name] = tool

    def names(self) -> List[str]:
        return sorted(self._tools)

    def schemas(self) -> List[Dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def validate(self, name: str, args: Dict[str, Any]) -> None:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError("未知工具: %s" % name)
        spec = tool.parameters or {}
        required = spec.get("required") or []
        props = spec.get("properties") or {}
        for key in required:
            if key not in args:
                raise ToolError("缺少必填参数: %s" % key)
        for key, value in args.items():
            expected = (props.get(key) or {}).get("type")
            if expected and value is not None and not _check_type(value, expected):
                raise ToolError("参数 %s 类型应为 %s" % (key, expected))

    def call(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """统一返回 {ok, data, error}，绝不抛给上层（骨架 4.2）。"""
        try:
            self.validate(name, args)
            tool = self._tools[name]
            data = tool.func(**args)
            return {"ok": True, "data": data, "error": None}
        except ToolError as exc:
            return {"ok": False, "data": None, "error": str(exc)}
        except TypeError as exc:
            return {"ok": False, "data": None, "error": "参数不匹配: %s" % exc}
        except Exception as exc:  # 工具内部故障不拖垮对话
            return {"ok": False, "data": None, "error": "%s: %s" % (type(exc).__name__, exc)}

    def digest(self, result: Dict[str, Any]) -> str:
        try:
            return json.dumps(result, ensure_ascii=False)[:300]
        except (TypeError, ValueError):
            return str(result)[:300]
