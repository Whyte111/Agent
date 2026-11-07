"""模型调用层：统一 chat / structured，超时重试，不含 prompt 业务。

两种实现：
- LiveLLM  : OpenAI 兼容 /chat/completions（DeepSeek / 通义 / vLLM 均可）
- MockLLM  : 无 key / 无网络时的确定性替身，从 data/mock/llm/<name>/<key>.json 取结果。
Mock 只替换"模型会怎么答"，三道闸门、发信门控仍是真实代码。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

JSON_SYSTEM = (
    "你是结构化信息抽取器。只输出一个 JSON 对象，不要输出解释、不要输出 Markdown 代码块。"
)


class LLMError(RuntimeError):
    pass


# ---------------- JSON / schema 校验（只实现本项目用到的子集） ----------------
def parse_json(content: str) -> Any:
    if not content or not content.strip():
        raise LLMError("模型返回为空")
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError as exc:
                raise LLMError("JSON 解析失败: %s" % exc)
        raise LLMError("响应中找不到 JSON 对象")


_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "null": type(None),
}


def _type_ok(data: Any, expected: str) -> bool:
    py = _TYPES.get(expected)
    if py is None:
        return False
    if expected in ("number", "integer") and isinstance(data, bool):
        return False
    return isinstance(data, py)


def validate_schema(data: Any, schema: Dict[str, Any], path: str = "$") -> None:
    """极简 JSON Schema 校验：type / required / properties / items / enum。"""
    expected = schema.get("type")
    if isinstance(expected, list):  # 联合类型（本项目只用于 "string|null"）
        if not any(_type_ok(data, t) for t in expected):
            raise LLMError(
                "%s: 期望 %s，实际 %s" % (path, "/".join(expected), type(data).__name__)
            )
        return
    if expected:
        if expected not in _TYPES:
            raise LLMError("%s: 未支持的 schema 类型 %r" % (path, expected))
        if not _type_ok(data, expected):
            raise LLMError("%s: 期望 %s，实际 %s" % (path, expected, type(data).__name__))
    if "enum" in schema and data not in schema["enum"]:
        raise LLMError("%s: 值 %r 不在枚举 %s 中" % (path, data, schema["enum"]))
    if expected == "object":
        props = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in data:
                raise LLMError("%s: 缺少必填字段 %r" % (path, key))
        for key, sub in props.items():
            if key in data and data[key] is not None:
                validate_schema(data[key], sub, "%s.%s" % (path, key))
    if expected == "array" and schema.get("items"):
        for i, item in enumerate(data):
            validate_schema(item, schema["items"], "%s[%d]" % (path, i))


# ---------------- Live ----------------
class LiveLLM:
    mode = "live"

    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    # --- 底层 HTTP ---
    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        import requests  # 延迟导入：mock 模式不依赖网络栈

        resp = requests.post(
            self.base_url + "/chat/completions",
            json=payload,
            headers={
                "Authorization": "Bearer %s" % self.api_key,
                "Content-Type": "application/json",
            },
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise LLMError("HTTP %s: %s" % (resp.status_code, resp.text[:300]))
        try:
            data = resp.json()
        except ValueError:
            raise LLMError("响应不是 JSON: %s" % resp.text[:200])
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("响应缺少 choices: %s" % json.dumps(data, ensure_ascii=False)[:300])
        message = choices[0].get("message") or {}
        return {
            "content": message.get("content") or "",
            "tool_calls": message.get("tool_calls") or [],
        }

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        json_mode: bool = False,
        temperature: float = 0.2,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            return self._post(payload)
        except LLMError as exc:
            # 某些兼容网关不支持 response_format：去掉重试一次
            if json_mode and "response_format" in str(exc).lower():
                payload.pop("response_format", None)
                return self._post(payload)
            raise

    def structured(
        self,
        prompt: str,
        schema: Dict[str, Any],
        *,
        name: str = "structured",
        system: Optional[str] = None,
        fixture_key: Optional[str] = None,  # live 模式忽略
    ) -> Dict[str, Any]:
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system or JSON_SYSTEM},
            {
                "role": "user",
                "content": prompt
                + "\n\n输出必须严格符合以下 JSON Schema：\n"
                + json.dumps(schema, ensure_ascii=False, indent=2),
            },
        ]
        last_error = ""
        for attempt in (1, 2):  # 骨架约定：校验失败重试 1 次
            try:
                out = self.chat(messages, json_mode=True, temperature=0.0)
                data = parse_json(out["content"])
                validate_schema(data, schema)
                return data
            except LLMError as exc:
                last_error = str(exc)
                messages.append(
                    {
                        "role": "user",
                        "content": "上一次输出不合法（%s）。请只输出一个合法 JSON 对象。" % last_error,
                    }
                )
        raise LLMError("%s: 两次结构化输出均失败: %s" % (name, last_error))


# ---------------- Mock ----------------
class MockLLM:
    """确定性替身：同名 fixture 按内容 sha256 取，取不到用 _default。"""

    mode = "mock"

    def __init__(self, fixture_dir: Path):
        self.fixture_dir = Path(fixture_dir)
        self.misses: List[str] = []

    def chat(self, messages, tools=None, json_mode=False, temperature=0.2):
        return {"content": "[mock] 未接入真实模型", "tool_calls": []}

    def structured(
        self,
        prompt: str,
        schema: Dict[str, Any],
        *,
        name: str = "structured",
        system: Optional[str] = None,
        fixture_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        folder = self.fixture_dir / name
        candidates = []
        if fixture_key:
            candidates.append(folder / ("%s.json" % fixture_key))
        candidates.append(folder / "_default.json")
        for path in candidates:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                validate_schema(data, schema)
                return data
        self.misses.append("%s:%s" % (name, fixture_key))
        raise LLMError(
            "MOCK 缺少 fixture: %s（可用 fixture_key=%s）" % (folder, fixture_key)
        )


_CACHE: Dict[str, Any] = {}


def get_llm(settings) -> Any:
    key = "mock" if not settings.llm_live else "live:%s:%s" % (settings.llm_base_url, settings.llm_model)
    if key in _CACHE:
        return _CACHE[key]
    if key == "mock":
        client: Any = MockLLM(settings.mock_dir / "llm")
    else:
        client = LiveLLM(
            settings.llm_base_url, settings.llm_api_key, settings.llm_model, settings.llm_timeout
        )
    _CACHE[key] = client
    return client
