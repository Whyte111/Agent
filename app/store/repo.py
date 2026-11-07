"""唯一写库入口（骨架 3 节: store 不含业务）。"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return "%s_%s" % (prefix, uuid.uuid4().hex[:12])


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")  # 并发写时等锁，不立刻 database is locked
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(conn)
    return conn


# FTS5 索引定义与 schema.sql 保持一致；老库没有这张表时在这里补建。
_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
  chunk_id UNINDEXED,
  doc_id   UNINDEXED,
  page     UNINDEXED,
  ordinal  UNINDEXED,
  text     UNINDEXED,
  body,
  tokenize = 'unicode61'
)
"""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """库里没有表就补建。防止"运行中删库/换目录"导致 502 no such table。"""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='docs' LIMIT 1"
    ).fetchone()
    if row is None:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()

    # 老库迁移：chunks.page（PDF 出处）与 FTS5 索引表。
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(chunks)").fetchall()}
    if "page" not in columns:
        conn.execute("ALTER TABLE chunks ADD COLUMN page INTEGER")
    try:
        conn.execute(_FTS_DDL)
    except sqlite3.OperationalError as exc:  # 极少见：SQLite 未编入 FTS5
        raise RuntimeError("SQLite 缺少 FTS5 扩展，PDF 检索不可用: %s" % exc)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS search_cache ("
        " query_norm TEXT PRIMARY KEY,"
        " query TEXT NOT NULL,"
        " results_json TEXT NOT NULL,"
        " created_at TEXT NOT NULL)"
    )
    conn.commit()


def init_db(db_path: Path) -> None:
    """幂等建库: 反复调用安全。"""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()


@contextmanager
def db(db_path: Path) -> Iterator[sqlite3.Connection]:
    """一次操作一个连接（ThreadingHTTPServer 下最省心的线程安全做法）。"""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


# ---------------- 对话 ----------------
def ensure_session(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id, created_at) VALUES (?, ?)",
        (session_id, now_iso()),
    )


def insert_message(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    role: str,
    content: str,
    refs: Optional[List[Dict[str, Any]]] = None,
) -> str:
    message_id = new_id("msg")
    conn.execute(
        "INSERT INTO messages (id, session_id, role, content, refs_json, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (
            message_id,
            session_id,
            role,
            content,
            json.dumps(refs, ensure_ascii=False) if refs else None,
            now_iso(),
        ),
    )
    return message_id


def recent_messages(
    conn: sqlite3.Connection, session_id: str, limit: int = 50
) -> List[Dict[str, Any]]:
    """按时间正序返回最近 limit 条（rowid 兜底同一秒内的顺序）。"""
    rows = conn.execute(
        "SELECT id, role, content, refs_json, created_at FROM messages"
        " WHERE session_id = ? AND role IN ('user', 'assistant')"
        " ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    out = []
    for row in reversed(rows):
        item = dict(row)
        item["refs"] = json.loads(item.pop("refs_json") or "[]")
        out.append(item)
    return out


def insert_tool_call(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    name: str,
    args: Dict[str, Any],
    ok: bool,
    ms: int,
    result_digest: str = "",
) -> str:
    call_id = new_id("tool")
    conn.execute(
        "INSERT INTO tool_calls (id, session_id, name, args_json, result_digest, ms, ok,"
        " created_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            call_id,
            session_id,
            name,
            json.dumps(args, ensure_ascii=False),
            result_digest[:500],
            ms,
            1 if ok else 0,
            now_iso(),
        ),
    )
    return call_id


# ---------------- 记忆 ----------------
def upsert_entity(
    conn: sqlite3.Connection,
    *,
    entity_type: str,
    name: str,
    norm_name: str,
    attrs: Optional[Dict[str, Any]] = None,
    source_ref: Optional[str] = None,
) -> Dict[str, Any]:
    """按 (type, 归一化名) 唯一。冲突策略：后写覆盖先写（不做复杂合并）。"""
    now = now_iso()
    row = conn.execute(
        "SELECT id, attrs_json FROM entities WHERE type = ? AND norm_name = ?",
        (entity_type, norm_name),
    ).fetchone()
    if row is None:
        entity_id = new_id("ent")
        conn.execute(
            "INSERT INTO entities (id, type, name, norm_name, attrs_json, first_seen, last_seen)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                entity_id,
                entity_type,
                name,
                norm_name,
                json.dumps(attrs or {}, ensure_ascii=False),
                now,
                now,
            ),
        )
        created = True
    else:
        entity_id = row["id"]
        merged = json.loads(row["attrs_json"] or "{}")
        merged.update(attrs or {})
        conn.execute(
            "UPDATE entities SET name = ?, attrs_json = ?, last_seen = ? WHERE id = ?",
            (name, json.dumps(merged, ensure_ascii=False), now, entity_id),
        )
        created = False
    if source_ref:
        conn.execute(
            "INSERT OR IGNORE INTO entity_sources (entity_id, source_ref) VALUES (?, ?)",
            (entity_id, source_ref),
        )
    return {"id": entity_id, "created": created}


def query_entities(
    conn: sqlite3.Connection,
    *,
    keyword: str = "",
    types: Optional[List[str]] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    sql = "SELECT id, type, name, norm_name, attrs_json, first_seen, last_seen FROM entities WHERE 1=1"
    params: List[Any] = []
    keyword = (keyword or "").strip()
    if keyword:
        like = "%%%s%%" % keyword.replace("%", "").replace("_", "")
        sql += " AND (name LIKE ? OR norm_name LIKE ?)"
        params += [like, like]
    if types:
        sql += " AND type IN (%s)" % ",".join("?" for _ in types)
        params += list(types)
    sql += " ORDER BY last_seen DESC, rowid DESC LIMIT ?"
    params.append(max(1, min(int(limit or 20), 100)))
    rows = conn.execute(sql, params).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["attrs"] = json.loads(item.pop("attrs_json") or "{}")
        out.append(item)
    return out


def entity_sources(conn: sqlite3.Connection, entity_id: str) -> List[str]:
    rows = conn.execute(
        "SELECT source_ref FROM entity_sources WHERE entity_id = ? ORDER BY rowid", (entity_id,)
    ).fetchall()
    return [row["source_ref"] for row in rows]


def recent_meetings(conn: sqlite3.Connection, limit: int = 5) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, topic, held_at, place, created_at FROM meetings"
        " ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (max(1, limit),),
    ).fetchall()
    return [dict(row) for row in rows]


def recent_meeting_summaries(
    conn: sqlite3.Connection, limit: int = 5
) -> List[Dict[str, Any]]:
    """给对话 system prompt 用：带参会人，比 recent_meetings 多这一列。"""
    rows = conn.execute(
        "SELECT id, topic, held_at, place, attendees_json, created_at FROM meetings"
        " ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (max(1, limit),),
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["attendees"] = json.loads(item.pop("attendees_json") or "[]")
        out.append(item)
    return out


# ---------------- docs ----------------
def find_doc_by_sha(conn: sqlite3.Connection, sha256: str) -> Optional[Dict[str, Any]]:
    return _row(conn.execute("SELECT * FROM docs WHERE sha256 = ?", (sha256,)).fetchone())


def insert_doc(
    conn: sqlite3.Connection,
    *,
    session_id: Optional[str],
    kind: str,
    path: str,
    sha256: str,
    pages: Optional[int] = None,
    status: str = "ready",
) -> str:
    doc_id = new_id("doc")
    conn.execute(
        "INSERT INTO docs (id, session_id, kind, path, sha256, pages, status, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (doc_id, session_id, kind, path, sha256, pages, status, now_iso()),
    )
    return doc_id


def get_doc(conn: sqlite3.Connection, doc_id: str) -> Optional[Dict[str, Any]]:
    return _row(conn.execute("SELECT * FROM docs WHERE id = ?", (doc_id,)).fetchone())


def count_chunks(conn: sqlite3.Connection, doc_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM chunks WHERE doc_id = ?", (doc_id,)
    ).fetchone()
    return int(row["c"] if row else 0)


def insert_chunk(
    conn: sqlite3.Connection,
    *,
    chunk_id: str,
    doc_id: str,
    ordinal: int,
    text: str,
    page: Optional[int] = None,
) -> None:
    conn.execute(
        "INSERT INTO chunks (id, doc_id, ordinal, page, text) VALUES (?,?,?,?,?)",
        (chunk_id, doc_id, ordinal, page, text),
    )


def insert_fts_chunk(
    conn: sqlite3.Connection,
    *,
    chunk_id: str,
    doc_id: str,
    page: Optional[int],
    ordinal: int,
    text: str,
    body: str,
) -> None:
    """body 是 jieba 预分词后的文本；text 只用于回填原文，不参与匹配。"""
    conn.execute(
        "INSERT INTO chunk_fts (chunk_id, doc_id, page, ordinal, text, body)"
        " VALUES (?,?,?,?,?,?)",
        (chunk_id, doc_id, page, ordinal, text, body),
    )


def delete_chunks_for_doc(conn: sqlite3.Connection, doc_id: str) -> None:
    conn.execute("DELETE FROM chunk_fts WHERE doc_id = ?", (doc_id,))
    conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))


def chunks_for_doc(conn: sqlite3.Connection, doc_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, doc_id, ordinal, page, text FROM chunks"
        " WHERE doc_id = ? ORDER BY ordinal",
        (doc_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def search_chunks(
    conn: sqlite3.Connection,
    match_query: str,
    limit: int = 5,
    doc_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """返回 FTS5 命中行；score 为原始 bm25（越负越相关），由 retrieval 层转成正分。"""
    sql = (
        "SELECT chunk_id, doc_id, page, ordinal, text, bm25(chunk_fts) AS raw_score"
        " FROM chunk_fts WHERE chunk_fts MATCH ?"
    )
    params: List[Any] = [match_query]
    if doc_id:
        sql += " AND doc_id = ?"
        params.append(doc_id)
    sql += " ORDER BY bm25(chunk_fts), rowid LIMIT ?"
    params.append(max(1, int(limit)))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ---------------- meetings ----------------
def get_meeting_by_doc(conn: sqlite3.Connection, doc_id: str) -> Optional[Dict[str, Any]]:
    row = _row(conn.execute("SELECT * FROM meetings WHERE doc_id = ?", (doc_id,)).fetchone())
    if row:
        row["attendees"] = json.loads(row.pop("attendees_json") or "[]")
    return row


def insert_meeting(
    conn: sqlite3.Connection,
    *,
    doc_id: str,
    session_id: Optional[str],
    held_at: Optional[str],
    place: Optional[str],
    attendees: List[str],
    topic: Optional[str],
    raw_text: str,
) -> str:
    meeting_id = new_id("mtg")
    conn.execute(
        "INSERT INTO meetings (id, doc_id, session_id, held_at, place, attendees_json,"
        " topic, raw_text, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            meeting_id,
            doc_id,
            session_id,
            held_at,
            place,
            json.dumps(attendees, ensure_ascii=False),
            topic,
            raw_text,
            now_iso(),
        ),
    )
    return meeting_id


def insert_risk(
    conn: sqlite3.Connection,
    *,
    meeting_id: str,
    basis: str,
    severity: str,
    evidence_quote: str,
    suggestion: Optional[str],
    memory_ref: Optional[str] = None,
) -> str:
    risk_id = new_id("risk")
    conn.execute(
        "INSERT INTO risk_items (id, meeting_id, basis, severity, evidence_quote,"
        " memory_ref, suggestion, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            risk_id,
            meeting_id,
            basis,
            severity,
            evidence_quote,
            memory_ref,
            suggestion,
            now_iso(),
        ),
    )
    return risk_id


def insert_todo(
    conn: sqlite3.Connection,
    *,
    risk_id: str,
    owner: Optional[str],
    action: str,
    due: Optional[str],
    status: str = "pending",
) -> str:
    todo_id = new_id("todo")
    conn.execute(
        "INSERT INTO todos (id, risk_id, owner, action, due, status, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (todo_id, risk_id, owner, action, due, status, now_iso()),
    )
    return todo_id


def update_todo_status(conn: sqlite3.Connection, todo_id: str, status: str) -> None:
    conn.execute("UPDATE todos SET status = ? WHERE id = ?", (status, todo_id))


def risks_for(conn: sqlite3.Connection, meeting_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM risk_items WHERE meeting_id = ? ORDER BY created_at, rowid",
        (meeting_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def todos_for(conn: sqlite3.Connection, meeting_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT t.*, r.severity, r.evidence_quote FROM todos t"
        " JOIN risk_items r ON r.id = t.risk_id WHERE r.meeting_id = ?"
        " ORDER BY t.created_at, t.rowid",
        (meeting_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def todos_with_risk(conn: sqlite3.Connection, meeting_id: str) -> List[Dict[str, Any]]:
    return todos_for(conn, meeting_id)


# ---------------- send_logs ----------------
def sent_log_for(conn: sqlite3.Connection, meeting_id: str) -> Optional[Dict[str, Any]]:
    return _row(
        conn.execute(
            "SELECT * FROM send_logs WHERE meeting_id = ? AND status = 'sent'"
            " ORDER BY ts DESC LIMIT 1",
            (meeting_id,),
        ).fetchone()
    )


def insert_send_log(
    conn: sqlite3.Connection,
    *,
    meeting_id: str,
    dedupe_key: Optional[str],
    recipients: List[str],
    subject: str,
    status: str,
    error: Optional[str] = None,
) -> str:
    log_id = new_id("send")
    conn.execute(
        "INSERT INTO send_logs (id, meeting_id, dedupe_key, recipients, subject, status,"
        " error, ts) VALUES (?,?,?,?,?,?,?,?)",
        (
            log_id,
            meeting_id,
            dedupe_key,
            json.dumps(recipients, ensure_ascii=False),
            subject,
            status,
            error,
            now_iso(),
        ),
    )
    return log_id


def send_logs_for(conn: sqlite3.Connection, meeting_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM send_logs WHERE meeting_id = ? ORDER BY ts, rowid", (meeting_id,)
    ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["recipients"] = json.loads(item.get("recipients") or "[]")
        out.append(item)
    return out


def claim_send_log(
    conn: sqlite3.Connection,
    *,
    meeting_id: str,
    dedupe_key: str,
    recipients: List[str],
    subject: str,
) -> Optional[str]:
    """在发送前占住 dedupe_key；并发/多进程只有一个请求能拿到。

    失败时把行改成 failed 且 dedupe_key=NULL，允许重发；进程崩溃留下的
    status='sending' 行由 cleanup_stale_sends() 超时回收。
    """
    try:
        return insert_send_log(
            conn,
            meeting_id=meeting_id,
            dedupe_key=dedupe_key,
            recipients=recipients,
            subject=subject,
            status="sending",
        )
    except sqlite3.IntegrityError:
        return None


def finish_send_log(
    conn: sqlite3.Connection,
    log_id: str,
    *,
    status: str,
    dedupe_key: Optional[str],
    error: Optional[str] = None,
) -> None:
    conn.execute(
        "UPDATE send_logs SET status = ?, dedupe_key = ?, error = ? WHERE id = ?",
        (status, dedupe_key, error, log_id),
    )


def cleanup_stale_sends(conn: sqlite3.Connection, before_ts: str) -> int:
    cur = conn.execute(
        "UPDATE send_logs SET status = 'failed', dedupe_key = NULL,"
        " error = COALESCE(error, '') || ' (stale sending claim)'"
        " WHERE status = 'sending' AND ts < ?",
        (before_ts,),
    )
    return int(cur.rowcount or 0)


# ---------------- search_cache ----------------
def get_search_cache(
    conn: sqlite3.Connection, query_norm: str
) -> Optional[Dict[str, Any]]:
    return _row(
        conn.execute(
            "SELECT query_norm, query, results_json, created_at FROM search_cache"
            " WHERE query_norm = ?",
            (query_norm,),
        ).fetchone()
    )


def upsert_search_cache(
    conn: sqlite3.Connection,
    *,
    query_norm: str,
    query: str,
    results_json: str,
) -> None:
    conn.execute(
        "INSERT INTO search_cache (query_norm, query, results_json, created_at)"
        " VALUES (?,?,?,?)"
        " ON CONFLICT(query_norm) DO UPDATE SET"
        " query = excluded.query,"
        " results_json = excluded.results_json,"
        " created_at = excluded.created_at",
        (query_norm, query, results_json, now_iso()),
    )


# ---------------- ppt_jobs ----------------
def insert_ppt_job(
    conn: sqlite3.Connection,
    *,
    ppt_id: str,
    session_id: Optional[str],
    path: str,
    pages: int,
    status: str = "ready",
) -> str:
    conn.execute(
        "INSERT INTO ppt_jobs (id, session_id, path, pages, status, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (ppt_id, session_id, path, pages, status, now_iso()),
    )
    return ppt_id


def get_ppt_job(conn: sqlite3.Connection, ppt_id: str) -> Optional[Dict[str, Any]]:
    return _row(conn.execute("SELECT * FROM ppt_jobs WHERE id = ?", (ppt_id,)).fetchone())


# ---------------- 组装 ----------------
def meeting_bundle(conn: sqlite3.Connection, meeting_id: str) -> Dict[str, Any]:
    meeting = _row(conn.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone())
    if meeting is None:
        raise KeyError(meeting_id)
    meeting["attendees"] = json.loads(meeting.pop("attendees_json") or "[]")
    return {
        "meeting": meeting,
        "risks": risks_for(conn, meeting_id),
        "todos": todos_for(conn, meeting_id),
        "send_logs": send_logs_for(conn, meeting_id),
    }
