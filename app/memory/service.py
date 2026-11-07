"""记忆服务：A（对话）与 B（会议）都只调 MemoryService.upsert，不各自写库（骨架 6 节 D）。"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.config.settings import Settings
from app.memory.normalize import clean_display, normalize, split_person
from app.store import repo

VALID_TYPES = ("person", "time", "place", "topic")


class MemoryError(ValueError):
    pass


def meeting_entities(extraction: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从会议抽取结果里取四类实体（不再多调一次模型）。"""
    entities: List[Dict[str, Any]] = []
    for name in extraction.get("attendees") or []:
        person, role = split_person(name)
        if not person:
            continue
        entities.append(
            {"type": "person", "name": person, "attrs": {"role": role} if role else {}}
        )
    if extraction.get("held_at"):
        entities.append({"type": "time", "name": extraction["held_at"], "attrs": {}})
    if extraction.get("place"):
        entities.append({"type": "place", "name": extraction["place"], "attrs": {}})
    if extraction.get("topic"):
        entities.append({"type": "topic", "name": extraction["topic"], "attrs": {}})
    return entities


class MemoryService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def upsert(
        self,
        entities: List[Dict[str, Any]],
        source_ref: str,
        conn: Any = None,
    ) -> Dict[str, Any]:
        if conn is not None:  # 调用方已有事务：复用连接，避免同库二次写入被锁
            return self._upsert(conn, entities, source_ref)
        with repo.db(self.settings.db_path) as own_conn:
            return self._upsert(own_conn, entities, source_ref)

    def _upsert(
        self, conn: Any, entities: List[Dict[str, Any]], source_ref: str
    ) -> Dict[str, Any]:
        added = merged = skipped = 0
        ids: List[str] = []
        for item in entities or []:
            entity_type = (item.get("type") or "").strip()
            if entity_type not in VALID_TYPES:
                skipped += 1
                continue
            name = clean_display(item.get("name") or "")[:80]
            norm = normalize(name)
            if not name or not norm:
                skipped += 1
                continue
            attrs = item.get("attrs") or {}
            if not isinstance(attrs, dict) or len(
                json.dumps(attrs, ensure_ascii=False)
            ) > 2048:
                attrs = {}
            result = repo.upsert_entity(
                conn,
                entity_type=entity_type,
                name=name,
                norm_name=norm,
                attrs=attrs,
                source_ref=source_ref,
            )
            ids.append(result["id"])
            if result["created"]:
                added += 1
            else:
                merged += 1
        return {"added": added, "merged": merged, "skipped": skipped, "ids": ids}

    def recall(
        self, query: str = "", types: Optional[List[str]] = None, limit: int = 10
    ) -> List[Dict[str, Any]]:
        with repo.db(self.settings.db_path) as conn:
            return repo.query_entities(conn, keyword=query, types=types, limit=limit)

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        return self.recall("", None, limit)

    def summary_lines(self, limit: int = 20) -> List[str]:
        return [
            "[%s] %s" % (item["type"], item["name"]) for item in self.recent(limit)
        ]
