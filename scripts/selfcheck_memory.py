"""G4 记忆自检（离线）：验证四类实体可写、可合并、可召回，且不污染无关类型。

用法: python -m scripts.selfcheck_memory
"""
from __future__ import annotations

import dataclasses
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]

from app.config.settings import settings as base_settings  # noqa: E402
from app.ingest.txt import normalize_txt  # noqa: E402
from app.memory.extract import extract_chat_entities  # noqa: E402
from app.memory.normalize import normalize, split_person  # noqa: E402
from app.memory.service import MemoryService, meeting_entities  # noqa: E402
from app.store import repo  # noqa: E402

RESULTS: List[tuple] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))


class FakeStructuredLLM:
    """只实现 structured 的假模型，用于测对话侧实体抽取。"""

    mode = "fake"

    def __init__(self, payload: Dict[str, Any], fail: bool = False):
        self.payload = payload
        self.fail = fail
        self.calls = 0

    def structured(self, prompt, schema, *, name="", system=None, fixture_key=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("model down")
        return self.payload


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="aiwainao-mem-"))
    try:
        s = dataclasses.replace(base_settings, data_dir=tmp)
        repo.init_db(s.db_path)
        service = MemoryService(s)

        # ---------- 归一化 ----------
        check("全半角与空白归一", normalize("张 伟") == normalize("张伟"))
        check("去标点后相同", normalize("上海，张江") == normalize("上海张江"))
        check("角色后缀被剥离", split_person("张伟（销售）") == ("张伟", "销售"),
              str(split_person("张伟（销售）")))
        check("无角色后缀原样保留", split_person("李娜") == ("李娜", ""))

        # ---------- 会议四类实体 ----------
        extraction = {
            "held_at": "2025-09-18 14:00-15:00",
            "place": "上海张江 3 号楼 5 层会议室",
            "topic": "华东区客户交付复盘会",
            "attendees": ["张伟（销售）", "李娜（项目管理）", "王强（交付）", "陈静（财务）"],
        }
        entities = meeting_entities(extraction)
        types = sorted({e["type"] for e in entities})
        check("会议抽到四类实体", types == ["person", "place", "time", "topic"], str(types))
        stats = service.upsert(entities, "meeting:mtg_1")
        check("首次写入 7 个实体（4 人 + 3 类）", stats["added"] == 7, str(stats))
        check("写入无跳过", stats["skipped"] == 0, str(stats))

        # ---------- 幂等 / 合并 ----------
        again = service.upsert(entities, "meeting:mtg_1")
        check("重复写入全是合并", again["added"] == 0 and again["merged"] == 7, str(again))
        with repo.db(s.db_path) as conn:
            total = conn.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"]
        check("实体总数没有膨胀", total == 7, "entities=%d" % total)

        # 后写覆盖先写：同一个人换了角色
        service.upsert(
            [{"type": "person", "name": "张伟", "attrs": {"role": "区域总监"}}], "meeting:mtg_2"
        )
        with repo.db(s.db_path) as conn:
            row = conn.execute(
                "SELECT attrs_json, first_seen, last_seen FROM entities"
                " WHERE type='person' AND norm_name=?", (normalize("张伟"),)
            ).fetchone()
        import json as _json

        check("属性后写覆盖先写", _json.loads(row["attrs_json"]).get("role") == "区域总监",
              row["attrs_json"])
        check("first_seen 不被覆盖", row["first_seen"] <= row["last_seen"])

        # ---------- 来源追溯 ----------
        with repo.db(s.db_path) as conn:
            person_id = conn.execute(
                "SELECT id FROM entities WHERE type='person' AND norm_name=?", (normalize("张伟"),)
            ).fetchone()["id"]
            sources = repo.entity_sources(conn, person_id)
        check("同一实体合并两个来源", "meeting:mtg_1" in sources and "meeting:mtg_2" in sources,
              str(sources))

        # ---------- 召回 ----------
        hits = service.recall("张伟")
        check("按名字召回人物", len(hits) == 1 and hits[0]["type"] == "person",
              str([h["name"] for h in hits]))
        persons = service.recall("", ["person"], 10)
        check("按类型召回 4 个人物", len(persons) == 4, "persons=%d" % len(persons))
        places = service.recall("", ["place"], 10)
        check("地点召回命中上海", places and "上海" in places[0]["name"], str(places[:1]))
        check("空关键词返回最近实体", len(service.recent(5)) == 5)
        check("查不到就是空", service.recall("不存在的人") == [])

        # ---------- 对话侧抽取 ----------
        llm = FakeStructuredLLM(
            {"entities": [
                {"type": "person", "name": "赵敏", "detail": "产品经理", "is_self": False},
                {"type": "person", "name": "林小满", "detail": None, "is_self": True},
                {"type": "topic", "name": "权限模块改版", "detail": None},
            ]}
        )
        chat_entities, note = extract_chat_entities(llm, "我是林小满，这次负责权限模块改版")
        check("对话抽到 3 个实体", len(chat_entities) == 3, str(chat_entities))
        self_person = [e for e in chat_entities if e.get("attrs", {}).get("self")]
        check("用户自述身份被标记 is_self", len(self_person) == 1 and self_person[0]["name"] == "林小满",
              str(self_person))
        stats = service.upsert(chat_entities, "message:msg_1")
        check("对话实体可写入", stats["added"] == 3, str(stats))
        check("对话来源可追溯",
              "message:msg_1" in [src for e in service.recall("林小满") for src in _sources(service, e)])

        # 过短消息不调模型
        short_llm = FakeStructuredLLM({"entities": []})
        _, short_note = extract_chat_entities(short_llm, "好的")
        check("过短消息跳过抽取", short_llm.calls == 0 and "过短" in short_note, short_note)

        # 模型失败降级
        failing, fail_note = extract_chat_entities(
            FakeStructuredLLM({}, fail=True), "我叫钱多多，是财务部的负责人"
        )
        check("模型失败降级为空", failing == [] and "失败" in fail_note, fail_note)

        # ---------- 不变式 ----------
        bad = service.upsert([{"type": "email", "name": "x@y.com"}], "message:msg_bad")
        check("非法实体类型被跳过", bad["skipped"] == 1 and bad["added"] == 0, str(bad))

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for ok, _, _ in RESULTS if ok)
    print("=" * 68)
    for ok, name, detail in RESULTS:
        print("%s %-34s %s" % ("PASS" if ok else "FAIL", name, detail))
    print("=" * 68)
    print("%d/%d 项通过" % (passed, len(RESULTS)))
    return 0 if passed == len(RESULTS) else 1


def _sources(service, entity) -> List[str]:
    from app.store import repo as r

    with r.db(service.settings.db_path) as conn:
        return r.entity_sources(conn, entity["id"])


if __name__ == "__main__":
    raise SystemExit(main())
