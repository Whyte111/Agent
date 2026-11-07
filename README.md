# AI 外脑 Demo · 交付说明

> 这份是接手入口。  
> 先读本页，再按需读 [DESIGN.md](DESIGN.md)、[TESTING.md](TESTING.md)、[RISKS.md](RISKS.md)。  
> 自测证据为本地文件，不随仓库上传；测试结论见 `TESTING.md`。

## 1. 项目是什么

单进程 Python Demo，覆盖 6 个测试点：

```text
G1 对话
G2 PDF 知识库（上传 → 检索 → 带页码/chunk_id 回答）
G3 会议 TXT → 风险 → 待办 → 真实发信 → send_log
G4 长期记忆（人物/时间/地点/主题，跨会话）
G5 联网搜索（百度千帆 AI 搜索，带 URL）
G6 对话生成 PPT（固定 5 页，可下载打开）
```

不包含：登录/多用户、向量库、流式输出、PPT 在线预览、移动端、PDF OCR。

## 2. 环境

| 项 | 要求 |
|---|---|
| OS | Windows 10 |
| Python | **3.9.2** |
| Web | 无 FastAPI，使用 stdlib `http.server` |
| PDF | `pypdf 6.21.4`（`requirements.txt`；本地也可放 `.vendor/`） |
| PPT | `python-pptx 1.0.2` |
| 中文检索 | `jieba 0.42.1` + SQLite FTS5 |
| 配置 | 复制 `.env.example` 为 `.env` 后填写 |

`.env` 只在进程启动时读一次，修改后必须重启。

缺少配置时的降级：

- 缺 `LLM_API_KEY` → mock 模式
- 缺 SMTP 配置 → dry-run（写 `.eml`）
- 缺 `SEARCH_API` → `web_search` 明确报错
- PPT 大纲模型调用失败 → 通用 5 页大纲降级

## 3. 启动

首次运行先装依赖：

```powershell
cd C:\path\to\Agent
python -m pip install -r requirements.txt
```

启动：

```powershell
python -m app
```

打开：

```text
http://127.0.0.1:8000/
```

健康检查：

```powershell
curl.exe http://127.0.0.1:8000/api/health
```

正常应看到 DB / LLM / SEARCH / SMTP 四项，且 `ok: true`。

> 默认只绑定 `127.0.0.1`。  
> `APP_HOST` 非 loopback 时默认拒绝启动；确需远程需显式 `APP_ALLOW_REMOTE=1`，但当前**没有认证**，不要直接暴露公网。

## 4. 最短验证

### 4.1 全部离线自检

```powershell
$env:PYTHONIOENCODING='utf-8'
python -m scripts.verify_all
```

当前结果：

```text
8 组，199 项全部通过
M0 43 / 对话 28 / 记忆 24 / PDF 35 / PPT 20 / 搜索 16 / D6 10 / 安全 23
```

### 4.2 单组验证

```powershell
python -m scripts.selfcheck_m0
python -m scripts.selfcheck_chat
python -m scripts.selfcheck_memory
python -m scripts.selfcheck_retrieval
python -m scripts.selfcheck_ppt
python -m scripts.selfcheck_search
python -m scripts.selfcheck_hardening
python -m scripts.selfcheck_security
```

### 4.3 页面验证

1. 打开 `http://127.0.0.1:8000/`
2. 右侧上传 `data/samples/G2_星云项目背景.pdf`
3. 问：`星云项目首期总预算和软件采购上限是多少？请给出页码和 chunk_id。`
4. 预期：480 万 / 180 万，第 1 页 `doc_55f4516050e7:c001`
5. 右侧上传 `data/samples/S1_高风险.txt`，填自己的邮箱
6. 预期：2 条 high、2 条待办、`mail_status=sent`、`send_log` 1 条

G1–G6 证据保存在本地 `evidence/`，**不随仓库上传**；摘要见 `TESTING.md`。

## 5. 常用接口

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/chat` | 对话 |
| POST | `/api/upload/pdf` | PDF 入库 |
| POST | `/api/upload/meeting` | 会议 TXT → 风险/待办/发信 |
| POST | `/api/ppt` | 生成 PPT |
| GET | `/api/files/{ppt_id}` | 下载 PPT |
| GET | `/api/sessions/{id}/memory` | 记忆面板 |
| GET | `/api/sessions/{id}/messages` | 对话历史 |
| GET | `/api/meetings/{id}` | 会议详情 |
| GET | `/api/health` | 依赖探活 |

状态码：

```text
400 输入类错误
403 跨站请求
404 资源不存在
413 请求体/文件/页数超限
415 Content-Type 不对
502 上游 LLM/搜索/IO 故障
```

## 6. 目录

```text
app/
  api/           HTTP 适配器、路由、上传/chat/ppt/memory/health
  orchestrator/  对话主循环、system prompt
  tools/         registry + memory_read / doc_search / web_search / make_ppt
  meeting/       会议链、三道闸门
  ingest/        TXT/PDF 抽取、切块、PDF 入库
  retrieval/     FTS5 + jieba
  ppt/           固定 5 页模板 + builder
  memory/        实体 upsert / recall
  mail/          SMTP / dry-run
  store/         schema + repo（唯一写库入口）
  config/        settings（唯一读 env）
web/             单页 HTML + JS + CSS
data/            samples / uploads / generated / outbox / app.db
evidence/        本地 D7 证据（不随仓库上传）
scripts/         自检与 CLI
.vendor/         pypdf 6.21.4
```

## 7. 交付边界

- 这是**本机单用户 Demo**，不是生产服务。
- 发信是唯一不可逆外部动作，只由 `meeting/pipeline.py` 触发；对话工具白名单没有 `send_mail`。
- PDF 是全局知识库，记忆全局跨会话；`session_id` 只隔离对话历史与文件归属展示。
- G5 的验收口径是需求方确认默认通过，QA 未独立验证截图中的 URL 可见性；详见 `TESTING.md`。
- 全流程干净库彩排需求方明确不做；只做了空库建表 smoke。详见 `RISKS.md`。
