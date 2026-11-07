# 测试说明

> 本文中的 `evidence/...` 是本地交付证据，**不随 GitHub 仓库上传**；  
> 仓库只保留测试命令和结论，避免截图/API JSON 中的个人信息进入公开仓库。

## 1. 当前测试结果

```powershell
$env:PYTHONIOENCODING='utf-8'
python -m scripts.verify_all
```

```text
8 组，199 项全部通过
```

| 组 | 项数 | 覆盖 |
|---|---:|---|
| M0 会议链 | 43 | 三道闸门、S1/S2/S3、幂等、dry-run、发信失败重发 |
| 对话链 | 28 | 多轮、工具循环、5 步收敛、截断、G4 会议行进 prompt |
| 长期记忆 | 24 | 四类实体、合并、来源、对话自述、非法类型 |
| G2 PDF | 35 | 抽取、扫描件、超过 50 页限制、切块、FTS5、跨文档 |
| G6 PPT | 20 | 5 页渲染、工具、下载、fallback、错误 spec |
| G5 搜索 | 16 | 解析、去重、HTML 清洗、top_k、缓存降级、无 key |
| D6 加固 | 10 | body 上限、慢客户端、错误 Content-Length、404 |
| 安全修复 | 23 | CSRF、Content-Type、TLS、claim、上限、repr |

> 这些自检大部分使用 **mock LLM / fake mailer / 临时 DB**，用于验证代码分支，不等同于真实模型/真实 SMTP 质量。

## 2. 真实链路证据

| 测试点 | 证据 | 结果 |
|---|---|---|
| G1 对话 | `evidence/screenshots/01-g1-chat.png` | 同会话两轮，第二轮答出“林小满 / 杭州” |
| G2 PDF | `evidence/screenshots/02-g2-pdf.png` | 2 页 / 2 块，预算 480 万 / 上限 180 万，第 1 页 `c001`；显示复用已有索引 |
| G3 会议发信 | `04-g3-inbox.png`、`03-g3-risk.png`、`api/g3-*.json` | 2 high / 2 待办；`send_log=1 sent`；163 收件箱确认收到 |
| G4 记忆 | `05-g4-memory.png`、`api/g4-memory-result.json` | 修复后答出主题/时间/地点/参会人 |
| G5 搜索 | `06-g5-search.png` | 需求方确认默认通过；QA 未独立验证截图中的 URL 可见性 |
| G6 PPT | `07-g6-ppt.png`、`08-g6-slide1.png`、对应 `.pptx` | 对话生成、下载、WPS 打开，5 页 |

G3 幂等：

```text
同 S1 再上传：
reused = true
mail_status = skipped_duplicate
send_logs = 1（未新增）
```

证据：`api/g3-idempotency-result.json`。

D7 汇总：`evidence/D7-自测表.md`。

## 3. 已明确未验证的内容

| 项 | 状态 | 说明 |
|---|---|---|
| G5 的 URL 可见性 | 需求方默认通过 | 截图里没有可见 URL；QA 未独立验证，交付说明中保留此例外 |
| 干净库完整 UI 彩排 | 需求方明确不做 | 只做了空库建表 smoke：20 张表，服务可启动；没有从清库开始走完整页面流程 |
| G2 首次上传 | 复用已有索引 | 截图证明知识库和问答正确，但不是首次上传 |
| G3 页面完整风险表 | 只截到结果摘要 | `03-g3-risk.png` 显示 2 风险/2 待办/`skipped_duplicate`；完整表格用 API JSON + 收件箱补证 |
| 多进程并发 | 未验证 | 单进程有 `_MEETING_LOCK`；DB claim 可防重复发信，但同文件多进程仍可能撞 `docs.sha256` |
| 真实 SMTP 失败路径 | 未验证 | 证书/STARTTLS 逻辑有单测；没有真实认证失败、证书失败、STARTTLS 禁用测试 |
| LLM 超时/429/非法 JSON | 未验证 | 代码有超时/重试/降级，但没有故障注入测试 |
| 磁盘满/权限失败 | 未验证 | 会返回 500/502，未做故障演练 |
| 浏览器自动化 | 未验证 | 没有 Playwright/Selenium；页面为人工截图 |
| 搜索 429/HTTP 500 真实响应 | 未验证 | 缓存降级用 fake 网络失败验证 |
| 数据库损坏/旧 schema 升级 | 未验证 | 只验证空库建表；老库迁移在代码里有，但未做损坏库测试 |

## 4. 最值得复跑的测试命令

### 4.1 全量

```powershell
python -m scripts.verify_all
```

### 4.2 安全与异常

```powershell
python -m scripts.selfcheck_security
python -m scripts.selfcheck_hardening
python -m scripts.selfcheck_m0
```

### 4.3 空库启动

```powershell
$env:DATA_DIR="$PWD\.qa-data"
$env:APP_PORT=8011
$env:MAIL_DRY_RUN=1
$env:LLM_MOCK=1
python -m app
```

预期：

```text
http://127.0.0.1:8011/ 可打开
/api/health 返回 200
DB 自动建 20 张表
```

### 4.4 G2 真实问答

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/upload/pdf `
  -F "file=@data/samples/G2_星云项目背景.pdf;type=application/pdf" `
  -F "session_id=qa-g2"

curl.exe -X POST http://127.0.0.1:8000/api/chat `
  -H "Content-Type: application/json" `
  -d '{"session_id":"qa-g2","text":"首期预算和软件采购上限是多少？给出页码和 chunk_id"}'
```

### 4.5 G3 真实发信（有外部副作用）

只在确认收件人后执行：

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/upload/meeting `
  -F "file=@data/samples/S1_高风险.txt;type=text/plain" `
  -F "mail_to=你的邮箱@example.com" `
  -F "session_id=qa-g3"
```

预期：

```text
risks = 2 high
todos = 2
mail_status = sent
send_logs = 1 sent
```

同一文件再次上传预期：

```text
reused = true
mail_status = skipped_duplicate
send_logs 不增加
```

### 4.6 G6 PPT

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/ppt `
  -H "Content-Type: application/json" `
  -d '{"session_id":"qa-g6","spec":"星云项目季度汇报"}'

curl.exe -OJ "http://127.0.0.1:8000/api/files/<ppt_id>"
python -c "from pptx import Presentation; p=Presentation('<ppt_id>.pptx'); print(len(p.slides))"
```

预期：`5`。

## 5. 验收口径

- 代码自检：**199/199 通过**
- 6 个测试点：均有实现
- 证据完整度：
  - G1 / G2 / G3 / G4 / G6：有截图或 API 证据
  - G5：需求方确认默认通过，QA 未独立验证
  - D7：干净库完整彩排未做，需求方明确豁免

因此验收结论应写成：

> **6 个测试点均有实现；G1/G2/G3/G4/G6 有交付证据；G5 按需求方口径默认通过；干净库完整彩排经需求方同意不做。**
