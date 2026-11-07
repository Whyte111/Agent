"""对话侧 prompt。只组装，不做判断。"""
from __future__ import annotations

from typing import Any, Dict, List

BASE_SYSTEM = """你是「AI 外脑」，一个帮助用户处理会议与文档的助手。

行为准则：
1. 用中文回答，简洁、直接，先说结论再说理由。
2. 需要外部信息时先调用工具；不要凭训练数据编造事实、日期、人名。
3. 工具返回为空或失败时，如实说明「没有查到」，不要猜。
4. 引用资料时说明来源（哪份文档 / 哪次会议），便于用户核对。
5. 用户问的是时间敏感信息（今天、最近、最新、新闻、天气、价格等）时，
   先调用 web_search 联网查证；回答必须带上来源 URL。
   工具返回 cached=true 时，要明确说「这是缓存结果，可能不是最新」。
6. 用户问到已上传 PDF / 文档里的内容时，先调用 doc_search 检索，再基于返回片段回答；
   引用时标出页码与 chunk_id（如「第 2 页，doc_xxx:c002」）。
   检索结果为空时如实说「文档里没有」，不要用常识补全。
7. 用户明确要求「做 PPT / 生成演示文稿」时，调用 make_ppt，把返回的 download_url
   原样写进回答；不要自己编链接，也不要在没调用工具时说已经生成。
   如果要求基于已上传 PDF 生成 PPT，先调用 doc_search，把命中的关键事实摘要放进
   make_ppt 的 context 参数，再生成。
8. 搜索结果为空或搜索失败时，如实说明「没有查到」，不要用训练数据冒充最新结果。"""

MEMORY_BLOCK = """
【已知长期记忆（来自历史会议与对话，全局共享）】
{entities}
说明：以上是结构化记忆，不是原文；不确定时以用户当前说法为准。"""

MEETINGS_BLOCK = """
【最近会议（来自历史会议记录，可用于回答人物/时间/地点/主题）】
{meetings}
说明：以上会议行是结构化记录；被问到"上次会议"时优先用这些行，不要声称完全没有记录。"""


def build_system_prompt(
    memory_entities: List[Dict[str, Any]] | None = None,
    meetings: List[Dict[str, Any]] | None = None,
) -> str:
    prompt = BASE_SYSTEM
    if memory_entities:
        lines = []
        for item in memory_entities[:20]:
            attrs = item.get("attrs") or {}
            extra = ""
            if attrs.get("detail"):
                extra += "（%s）" % attrs["detail"]
            if attrs.get("self"):
                extra += "（用户本人在对话中自述）"
            lines.append("- [%s] %s%s" % (item.get("type"), item.get("name"), extra))
        prompt += MEMORY_BLOCK.format(entities="\n".join(lines))
    if meetings:
        lines = []
        for item in meetings[:5]:
            lines.append(
                "- 主题：%s ｜ 时间：%s ｜ 地点：%s ｜ 参会：%s"
                % (
                    item.get("topic") or "未命名",
                    item.get("held_at") or "未记录",
                    item.get("place") or "未记录",
                    "、".join(item.get("attendees") or []) or "未记录",
                )
            )
        prompt += MEETINGS_BLOCK.format(meetings="\n".join(lines))
    return prompt
