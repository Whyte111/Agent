"""PPT 生成：固定 5 页模板（封面 + 4 个内容部分）。

链路（骨架 6-C）：
  用户指令 / /api/ppt → 生成大纲(structured) → 套模板渲染 → 落盘 generated/
  → ppt_jobs → 返回 ppt_id + download_url

模板限制：只做固定版式，不做动态排版（7 天计划 §4）。
LLM 失败时降级为通用 5 页大纲，仍然产出可打开的 .pptx，并如实返回 note。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config.settings import Settings
from app.llm.client import get_llm
from app.store import repo

TEMPLATE_PATH = Path(__file__).with_name("template.pptx")
PAGE_COUNT = 5
MAX_BULLETS = 4

_FONT = "Microsoft YaHei"
_NAVY = (0x0F, 0x17, 0x2A)
_BLUE = (0x4C, 0x8D, 0xFF)
_TEXT = (0x1E, 0x29, 0x3B)
_MUTED = (0x64, 0x74, 0x8B)
_LINE = (0xE2, 0xE8, 0xF0)
_WHITE = (0xFF, 0xFF, 0xFF)
_SOFT = (0xCB, 0xD5, 0xE1)

OUTLINE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "subtitle": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "bullets"],
            },
        },
    },
    "required": ["title", "subtitle", "sections"],
}

_SECTION_TITLES = ["背景与目标", "核心内容", "计划与里程碑", "总结与下一步"]


class PptError(ValueError):
    """PPT 生成的可预期错误（空主题 / 渲染失败 / 文件不存在）。"""


def _clean(value: Any, limit: int = 80) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _fallback_outline(request: str) -> Dict[str, Any]:
    topic = _clean(request, 40) or "AI 外脑演示"
    return {
        "title": topic,
        "subtitle": "AI 外脑 · 自动生成",
        "sections": [
            {"title": "背景与目标", "bullets": ["主题：%s" % topic, "为什么现在做", "期望达成的结果"]},
            {"title": "核心内容", "bullets": ["现状与关键问题", "解决思路", "价值与收益"]},
            {"title": "计划与里程碑", "bullets": ["阶段一：准备与验证", "阶段二：试点与迭代", "阶段三：推广与复盘"]},
            {"title": "总结与下一步", "bullets": ["结论", "下一步行动", "需要的支持"]},
        ],
    }


def _outline_prompt(request: str) -> str:
    return (
        "你是演示文稿策划。请根据用户要求，生成一份中文 PPT 大纲。\n"
        "固定结构：封面 + 4 个内容部分（背景与目标、核心内容、计划与里程碑、总结与下一步）。\n"
        "要求：\n"
        "1) title 是整份 PPT 的标题，subtitle 是一句副标题；\n"
        "2) sections 恰好 4 项，每项 title 用上面的固定标题；\n"
        "3) 每项 3-4 条 bullets，每条不超过 50 字，具体、可讲，不要空话；\n"
        "4) 不要编造用户没有提供的数字、人名、日期；不确定就用中性表述。\n"
        "用户要求：%s" % request
    )


def _normalize_outline(data: Dict[str, Any], request: str, source: str) -> Dict[str, Any]:
    title = _clean(data.get("title") or request, 60) or "AI 外脑演示"
    subtitle = _clean(data.get("subtitle") or "AI 外脑 · 自动生成", 80)
    raw_sections = data.get("sections")
    sections: List[Dict[str, Any]] = []
    if isinstance(raw_sections, list):
        for item in raw_sections[:8]:
            if not isinstance(item, dict):
                continue
            section_title = _clean(item.get("title"), 30)
            bullets = item.get("bullets")
            clean_bullets = []
            if isinstance(bullets, list):
                for bullet in bullets[:8]:
                    text = _clean(bullet, 80)
                    if text:
                        clean_bullets.append(text)
            if section_title and clean_bullets:
                sections.append({"title": section_title, "bullets": clean_bullets[:MAX_BULLETS]})

    fallback = _fallback_outline(title)
    while len(sections) < 4:
        sections.append(fallback["sections"][len(sections)])
    sections = sections[:4]
    for index, section in enumerate(sections):
        if not section["title"]:
            section["title"] = _SECTION_TITLES[index]
        if not section["bullets"]:
            section["bullets"] = fallback["sections"][index]["bullets"]

    return {"title": title, "subtitle": subtitle, "sections": sections, "source": source}


def _outline_from_llm(llm: Any, request: str) -> Tuple[Dict[str, Any], str]:
    try:
        data = llm.structured(
            _outline_prompt(request),
            OUTLINE_SCHEMA,
            name="ppt_outline",
            fixture_key=None,
        )
        if not isinstance(data, dict):
            raise ValueError("大纲不是 JSON 对象")
        return _normalize_outline(data, request, "llm"), ""
    except Exception as exc:  # 模型失败不阻断 G6，降级但仍如实标注
        return _normalize_outline(_fallback_outline(request), request, "fallback"), "%s: %s" % (
            type(exc).__name__,
            exc,
        )


def _spec_to_request(spec: Any) -> Tuple[str, Optional[Dict[str, Any]]]:
    if isinstance(spec, str):
        request = _clean(spec, 500)
        if not request:
            raise PptError("PPT 主题不能为空")
        return request, None
    if isinstance(spec, dict):
        if spec.get("sections"):
            # 调用方直接给了大纲：跳过 LLM，按给定内容渲染
            return _clean(spec.get("title") or "AI 外脑演示", 60), spec
        request = _clean(spec.get("topic") or spec.get("request") or spec.get("text"), 500)
        if not request:
            raise PptError("PPT 主题不能为空（spec.topic）")
        return request, None
    raise PptError("PPT spec 必须是字符串或对象")


def _new_presentation() -> Any:
    from pptx import Presentation
    from pptx.util import Inches

    if TEMPLATE_PATH.is_file():
        prs = Presentation(str(TEMPLATE_PATH))
    else:  # 模板丢了也不能让 G6 挂掉：用默认模板 + 同样的版式
        prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    return prs


def _remove_slides(prs: Any) -> None:
    """模板里若残留示例页，先清掉，保证固定 5 页。"""
    from pptx.oxml.ns import qn

    slide_ids = prs.slides._sldIdLst
    for slide_id in list(slide_ids):
        rid = slide_id.get(qn("r:id"))
        try:
            if rid:
                prs.part.drop_rel(rid)
        except Exception:
            pass
        slide_ids.remove(slide_id)


def _rgb(color: Tuple[int, int, int]) -> Any:
    from pptx.dml.color import RGBColor

    return RGBColor(*color)


def _style_run(run: Any, size: int, bold: bool, color: Tuple[int, int, int]) -> None:
    from pptx.oxml.ns import qn
    from pptx.util import Pt

    run.font.name = _FONT
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = _rgb(color)
    # 中文字体要单独写 a:ea，否则某些 PowerPoint 会回退成宋体
    rpr = run._r.get_or_add_rPr()
    ea = rpr.find(qn("a:ea"))
    if ea is None:
        ea = rpr.makeelement(qn("a:ea"), {})
        rpr.append(ea)
    ea.set("typeface", _FONT)


def _textbox(slide: Any, left: float, top: float, width: float, height: float) -> Any:
    from pptx.util import Inches

    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = 0
    tf.margin_right = 0
    tf.margin_top = 0
    tf.margin_bottom = 0
    return box


def _rect(slide: Any, left: float, top: float, width: float, height: float,
          color: Tuple[int, int, int]) -> Any:
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    shape = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, Inches(left), Inches(top), Inches(width), Inches(height)
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(color)
    shape.line.fill.background()
    try:
        shape.shadow.inherit = False
    except Exception:
        pass
    return shape


def _blank_slide(prs: Any) -> Any:
    layouts = prs.slide_layouts
    layout = layouts[6] if len(layouts) > 6 else layouts[-1]
    return prs.slides.add_slide(layout)


def _add_cover(prs: Any, outline: Dict[str, Any]) -> None:
    from pptx.enum.text import PP_ALIGN

    slide = _blank_slide(prs)
    _rect(slide, 0, 0, 13.333, 7.5, _NAVY)
    _rect(slide, 0.9, 2.05, 1.5, 0.08, _BLUE)

    kicker = _textbox(slide, 0.9, 0.8, 11.5, 0.4)
    p = kicker.text_frame.paragraphs[0]
    run = p.add_run()
    run.text = "AI 外脑 · 自动生成"
    _style_run(run, 13, False, _SOFT)

    title = _textbox(slide, 0.9, 2.35, 11.5, 1.8)
    p = title.text_frame.paragraphs[0]
    p.alignment = PP_ALIGN.LEFT
    run = p.add_run()
    run.text = outline["title"]
    _style_run(run, 38, True, _WHITE)

    subtitle = _textbox(slide, 0.9, 4.25, 11.5, 0.9)
    p = subtitle.text_frame.paragraphs[0]
    run = p.add_run()
    run.text = outline["subtitle"]
    _style_run(run, 17, False, _SOFT)

    footer = _textbox(slide, 0.9, 6.85, 11.5, 0.35)
    p = footer.text_frame.paragraphs[0]
    run = p.add_run()
    run.text = "AI 外脑 Demo · 固定 5 页模板"
    _style_run(run, 9, False, _MUTED)


def _add_section(prs: Any, outline: Dict[str, Any], section: Dict[str, Any], index: int) -> None:
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN

    slide = _blank_slide(prs)
    _rect(slide, 0, 0, 13.333, 7.5, _WHITE)
    _rect(slide, 0, 0, 0.16, 7.5, _BLUE)

    kicker = _textbox(slide, 0.8, 0.45, 11.5, 0.35)
    p = kicker.text_frame.paragraphs[0]
    run = p.add_run()
    run.text = "%02d / %02d · %s" % (index + 1, PAGE_COUNT, outline["title"])
    _style_run(run, 10, False, _MUTED)

    title = _textbox(slide, 0.8, 0.85, 11.8, 0.9)
    p = title.text_frame.paragraphs[0]
    run = p.add_run()
    run.text = section["title"]
    _style_run(run, 28, True, _NAVY)

    _rect(slide, 0.8, 1.78, 11.7, 0.02, _LINE)

    body = _textbox(slide, 0.9, 2.05, 11.5, 4.35)
    tf = body.text_frame
    tf.vertical_anchor = MSO_ANCHOR.TOP
    for i, bullet in enumerate(section["bullets"][:MAX_BULLETS]):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.space_after = 12
        p.line_spacing = 1.15
        dot = p.add_run()
        dot.text = "▪  "
        _style_run(dot, 16, True, _BLUE)
        run = p.add_run()
        run.text = bullet
        _style_run(run, 18, False, _TEXT)

    _rect(slide, 0.8, 6.82, 11.7, 0.02, _LINE)
    footer = _textbox(slide, 0.8, 6.95, 8.5, 0.3)
    p = footer.text_frame.paragraphs[0]
    run = p.add_run()
    run.text = "AI 外脑 · %s" % outline["title"]
    _style_run(run, 9, False, _MUTED)

    page = _textbox(slide, 11.2, 6.95, 1.3, 0.3)
    p = page.text_frame.paragraphs[0]
    p.alignment = PP_ALIGN.RIGHT
    run = p.add_run()
    run.text = "%02d / %02d" % (index + 1, PAGE_COUNT)
    _style_run(run, 9, False, _MUTED)


def _render(outline: Dict[str, Any], path: Path) -> None:
    prs = _new_presentation()
    _remove_slides(prs)
    _add_cover(prs, outline)
    for index, section in enumerate(outline["sections"][:4], start=1):
        _add_section(prs, outline, section, index)
    path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(path))


def run_ppt(
    settings: Settings,
    *,
    session_id: str,
    spec: Any,
    llm: Any = None,
) -> Dict[str, Any]:
    request, provided = _spec_to_request(spec)
    note = ""
    if provided is not None:
        outline = _normalize_outline(provided, request, "provided")
    else:
        outline, note = _outline_from_llm(llm or get_llm(settings), request)

    ppt_id = repo.new_id("ppt")
    path = settings.generated_dir / ("%s.pptx" % ppt_id)
    try:
        _render(outline, path)
    except Exception as exc:
        raise PptError("PPT 渲染失败：%s: %s" % (type(exc).__name__, exc))

    try:
        with repo.db(settings.db_path) as conn:
            repo.insert_ppt_job(
                conn,
                ppt_id=ppt_id,
                session_id=session_id,
                path=str(path.resolve()),
                pages=PAGE_COUNT,
                status="ready",
            )
    except Exception as exc:
        try:
            path.unlink()
        except OSError:
            pass
        raise PptError("PPT 落库失败：%s: %s" % (type(exc).__name__, exc))

    return {
        "ppt_id": ppt_id,
        "download_url": "/api/files/%s" % ppt_id,
        "pages": PAGE_COUNT,
        "title": outline["title"],
        "outline_source": outline["source"],
        "note": note,
    }
