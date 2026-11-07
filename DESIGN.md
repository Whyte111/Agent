# 设计说明

## 1. 设计目标

一句话：

> 单进程 Python，FastAPI 的职责用 stdlib 替代；一个工具循环管对话，三条固定流水线管会议/PDF/PPT；SQLite 落一切。

优先保证：

1. 6 个测试点都能在本机演示
2. 外部副作用可控：只有会议链能发信
3. 失败可解释：证据、枚举、发信由代码校验，不交给模型
4. 依赖少：一个进程、一个 DB、一个 `python -m app`

## 2. 为什么这样分层

```text
api → orchestrator → tools → 能力模块 → store/llm
```

依赖单向向下，禁止反向 import。

| 层 | 职责 | 不做什么 |
|---|---|---|
| `api` | 路由、参数校验、序列化、SSE | 不写业务 |
| `orchestrator` | 对话主循环、工具循环、截断 | 不直接调外部 API |
| `tools` | 工具描述、入参校验、结果归一化 | 不含业务规则 |
| `meeting` / `ppt` | 固定顺序的流水线 | 不是 agent |
| `ingest` / `retrieval` / `memory` / `mail` | 单一职责 | 不感知上层 |
| `store` / `llm` / `config` | 持久化、模型、配置 | 不含业务 |

这样拆的原因：

- 模型只决定“调哪个工具”，**决定“要不要发邮件”的是代码**
- 会议链的副作用最重，单独成流水线，便于审查
- PDF、PPT、记忆彼此独立，出问题不会拖垮会议链

## 3. 核心结构

### 3.1 对话链

```text
收消息
→ 落 Message
→ 记忆召回：最近实体 + 最近会议行
→ 拼 system prompt
→ LLM(带工具白名单)
→ 有 tool_calls? 执行并回填（最多 5 步）
→ 强制收敛
→ 落 Message
→ 抽取实体 upsert
→ 返回 reply + references + artifacts + tool_trace
```

关键约束：

- 工具白名单：`memory_read / doc_search / web_search / make_ppt`
- **没有 `send_mail`**
- 历史窗口：最近 12 条 / 6000 字，最后一条永远保留
- 工具调用最多 5 步，超了强制收敛
- 工具失败只进 trace，不拖垮对话

### 3.2 会议链（唯一有外部副作用）

```text
上传 TXT
→ 落盘 + sha256
→ 规范化
→ LLM 抽取（元信息 + action_items + risks，均带原文证据）
→ 三道闸门：
   ① 证据校验：evidence_quote 在原文匹配不到 → 整条丢弃
   ② 枚举校验：severity 非法 → low；basis 非法 → action
   ③ 发信门控：severity == high 且证据通过 → 才发信
→ 风险结论/待办先提交
→ DB claim dedupe_key（status=sending）
→ SMTP 发送（失败重试 1 次）
→ 成功转 sent / 失败释放 dedupe
→ 落 send_log
```

为什么发信要代码说了算：

- 纯 LLM 会误发信
- 纯规则对一周 Demo 过度设计
- 因此：LLM 判“风险是什么 + 给原文证据”，代码判“证据真不真、枚举合不合法、发不发”

幂等：

- `docs.sha256` 唯一：同内容只入库一次
- `send_logs.dedupe_key` 唯一：发送前 claim，成功后占用；失败释放；崩溃残留 `sending` 10 分钟回收
- 同一 S1 再上传：`reused=true` + `skipped_duplicate`，不会重发

### 3.3 PDF 链

```text
上传 PDF（≤50 页 / ≤20MB）
→ pypdf 逐页抽文本
→ 扫描件检测：平均每页 <20 字 → 明确报错，不返回空
→ 按页切块（430–500 字，块间重叠 80，含重叠后 ≤600）
→ jieba 预分词写 FTS5
→ doc_search 检索 top-k
→ 回答带 doc_id / page / chunk_id
```

为什么用 FTS5 + jieba：

- FTS5 默认 `unicode61` 不切中文，整句一个 token
- 演示文档量小，不需要向量库
- 中文先 `jieba.cut_for_search`，查询词 OR 连接，bm25 排序

### 3.4 PPT 链

```text
对话 make_ppt 或 POST /api/ppt
→ LLM 生成 5 页大纲（失败降级为通用大纲）
→ 套 16:9 模板
→ 固定版式绘制：封面 + 背景/核心/计划/总结
→ data/generated/<ppt_id>.pptx
→ ppt_jobs
→ 返回 download_url
```

为什么固定 5 页：

- 题目只要求“能下载打开”
- 动态排版风险高、收益低
- 模板只承载尺寸/主题，版式由 builder 固定绘制，保证每次可打开

### 3.5 记忆

- 四类实体：`person / time / place / topic`
- 全局跨会话，`session_id` 不隔离记忆
- `entity_sources` 记录 `meeting:<id>` / `message:<id>`
- 对话自述身份打 `attrs.self=true`
- 最近会议行（topic/held_at/place/attendees）进入 system prompt，解决“只有零散实体、答不出上次会议”的问题
- `Commitment` 只建 schema，不实现

### 3.6 搜索

- 百度千帆 AI 搜索：`POST https://qianfan.baidubce.com/v2/ai_search`
- `search_source=baidu_search_v2`
- 返回 `references`，由 LLM 组织回答并带 URL
- 失败：同 query 缓存降级，`cached=true` + 缓存时间；无缓存明确报错

## 4. 数据模型（SQLite）

核心表：

```text
sessions / messages
docs / chunks / chunk_fts
meetings / risk_items / todos / send_logs
entities / entity_sources / commitments(只建表)
tool_calls
ppt_jobs
search_cache
```

关键不变式：

- `docs.sha256` 唯一
- `SendLog.dedupe_key` 唯一，只允许成功/发送中的行占用
- `Todo` 必须挂在 `RiskItem` 上
- `Entity` 在 `(type, norm_name)` 唯一
- `RiskItem.evidence_quote` 必须能在原文匹配，否则丢弃
- `RiskItem.severity` 只能是 `high/mid/low`
- `Commitment` 不参与 G4 四类召回

## 5. 关键取舍

| 取舍 | 结论 | 原因 |
|---|---|---|
| Web 框架 | stdlib `http.server` | 接入时环境无法安装 FastAPI；handler 签名按 FastAPI 对齐，后续可替换 |
| 存储 | SQLite + WAL | 单进程、零运维、演示足够；FTS5 可直接用 |
| 搜索 | 百度千帆 AI 搜索 | 需求方提供 key；只做单供应商，不做多源容灾 |
| PDF 检索 | FTS5 + jieba | 演示文档量小，向量库是过度设计 |
| PPT | 固定 5 页模板 | 题目只要求可下载打开；动态排版风险高 |
| 记忆作用域 | 全局跨会话 | 符合“外脑”定位，G4 验收最直观 |
| PDF 作用域 | 全局累积 | 多次上传都可检索，演示更直观 |
| 收件人 | 上传表单填写 | 最稳、可测、演示可控 |
| 认证 | 无登录，单用户 | 题目非目标；默认只绑 127.0.0.1 |

## 6. 安全边界

- 对话工具白名单不含 `send_mail`
- 发信唯一入口：`meeting.pipeline`
- 上传扩展名白名单：`.pdf / .txt`
- 落盘名服务端 UUID，不用用户文件名
- PDF 下载只允许 `data/generated/` 下的文件
- 所有 POST 校验 Origin/Referer/Sec-Fetch-Site 同源
- `/api/chat`、`/api/ppt` 强制 `application/json`
- SMTP_SSL / STARTTLS 证书与 hostname 必须校验
- 非 loopback 绑定默认拒绝，需显式 `APP_ALLOW_REMOTE=1`（仍无认证）

## 7. 扩展点

- 换 Web 框架：替换 `app/api/server.py`，路由表已分离
- 换搜索供应商：只改 `app/tools/baidu_search.py`
- 换 PPT 模板：改 `app/ppt/template.pptx` + `builder.py` 版式函数
- 换存储：`store/repo.py` 是唯一写库入口
- 加 L2 承诺漂移：`commitments` 表已建，规则层追加即可
