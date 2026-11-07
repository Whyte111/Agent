-- AI 外脑 Demo · 表结构（骨架 5 节）
-- 全量建表：M0 只用 docs / meetings / risk_items / todos / send_logs，
-- 其余表先建好，避免后续返工（不写业务逻辑，只占位）。

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------- 对话 ----------
CREATE TABLE IF NOT EXISTS sessions (
  id          TEXT PRIMARY KEY,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id          TEXT PRIMARY KEY,
  session_id  TEXT NOT NULL REFERENCES sessions(id),
  role        TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
  content     TEXT NOT NULL,
  refs_json   TEXT,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_messages_session ON messages(session_id, created_at);

-- ---------- 文档 ----------
CREATE TABLE IF NOT EXISTS docs (
  id          TEXT PRIMARY KEY,
  session_id  TEXT,
  kind        TEXT NOT NULL CHECK (kind IN ('txt', 'meeting', 'pdf', 'ppt')),
  path        TEXT NOT NULL,
  sha256      TEXT NOT NULL,
  pages       INTEGER,
  status      TEXT NOT NULL DEFAULT 'ready',
  created_at  TEXT NOT NULL
);
-- 同一份内容只存一份：重复上传命中同一 doc（幂等的第一道保证）
CREATE UNIQUE INDEX IF NOT EXISTS ux_docs_sha256 ON docs(sha256);

CREATE TABLE IF NOT EXISTS chunks (
  id       TEXT PRIMARY KEY,
  doc_id   TEXT NOT NULL REFERENCES docs(id),
  ordinal  INTEGER NOT NULL,
  page     INTEGER,              -- 骨架 5 节未列；7.3 要求每块可指页码，故补此列
  text     TEXT NOT NULL,
  UNIQUE (doc_id, ordinal)
);

-- PDF 检索用的 FTS5 索引。body 存 jieba 预分词文本，text 保留原文用于回填。
-- FTS5 默认 unicode61 不切中文，必须由 retrieval/index.py 预分词后写入。
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
  chunk_id UNINDEXED,
  doc_id   UNINDEXED,
  page     UNINDEXED,
  ordinal  UNINDEXED,
  text     UNINDEXED,
  body,
  tokenize = 'unicode61'
);

-- ---------- 记忆 ----------
CREATE TABLE IF NOT EXISTS entities (
  id          TEXT PRIMARY KEY,
  type        TEXT NOT NULL CHECK (type IN ('person', 'time', 'place', 'topic')),
  name        TEXT NOT NULL,
  norm_name   TEXT NOT NULL,
  attrs_json  TEXT,
  first_seen  TEXT NOT NULL,
  last_seen   TEXT NOT NULL,
  UNIQUE (type, norm_name)
);

CREATE TABLE IF NOT EXISTS entity_sources (
  entity_id   TEXT NOT NULL REFERENCES entities(id),
  source_ref  TEXT NOT NULL,
  PRIMARY KEY (entity_id, source_ref)
);

-- L2 预留：只建表，不实现（骨架 U7）
CREATE TABLE IF NOT EXISTS commitments (
  id          TEXT PRIMARY KEY,
  meeting_id  TEXT,
  content     TEXT,
  owner       TEXT,
  due         TEXT,
  status      TEXT CHECK (status IN ('pending', 'done', 'broken')),
  source_ref  TEXT
);

-- ---------- 会议 ----------
CREATE TABLE IF NOT EXISTS meetings (
  id              TEXT PRIMARY KEY,
  doc_id          TEXT NOT NULL UNIQUE REFERENCES docs(id),
  session_id      TEXT,
  held_at         TEXT,
  place           TEXT,
  attendees_json  TEXT,
  topic           TEXT,
  raw_text        TEXT NOT NULL,
  created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_items (
  id              TEXT PRIMARY KEY,
  meeting_id      TEXT NOT NULL REFERENCES meetings(id),
  basis           TEXT NOT NULL CHECK (basis IN ('owner', 'deadline', 'action')),
  severity        TEXT NOT NULL CHECK (severity IN ('high', 'mid', 'low')),
  evidence_quote  TEXT NOT NULL,
  memory_ref      TEXT,
  suggestion      TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_risk_meeting ON risk_items(meeting_id);

CREATE TABLE IF NOT EXISTS todos (
  id          TEXT PRIMARY KEY,
  risk_id     TEXT NOT NULL REFERENCES risk_items(id),
  owner       TEXT,
  action      TEXT NOT NULL,
  due         TEXT,
  status      TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed')),
  created_at  TEXT NOT NULL
);

-- dedupe_key 唯一且只允许"发送成功"的行占用它（NULL 可重复），
-- 保证"同文件同风险不重发"，同时不阻止失败后重发。
CREATE TABLE IF NOT EXISTS send_logs (
  id          TEXT PRIMARY KEY,
  meeting_id  TEXT NOT NULL REFERENCES meetings(id),
  dedupe_key  TEXT UNIQUE,
  recipients  TEXT NOT NULL,
  subject     TEXT NOT NULL,
  status      TEXT NOT NULL,
  error       TEXT,
  ts          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_send_meeting ON send_logs(meeting_id);

-- ---------- 其他 ----------
CREATE TABLE IF NOT EXISTS tool_calls (
  id            TEXT PRIMARY KEY,
  session_id    TEXT,
  name          TEXT NOT NULL,
  args_json     TEXT,
  result_digest TEXT,
  ms            INTEGER,
  ok            INTEGER,
  created_at    TEXT NOT NULL
);

-- 搜索失败降级用：按归一化 query 缓存最近一次成功结果（骨架 7 节"降级到缓存并标注"）。
CREATE TABLE IF NOT EXISTS search_cache (
  query_norm    TEXT PRIMARY KEY,
  query         TEXT NOT NULL,
  results_json  TEXT NOT NULL,
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ppt_jobs (
  id          TEXT PRIMARY KEY,
  session_id  TEXT,
  path        TEXT,
  pages       INTEGER,
  status      TEXT,
  created_at  TEXT NOT NULL
);
