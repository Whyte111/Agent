# GitHub 上传安全说明

本目录（`github_upload/`）是为公开仓库准备的干净副本。

## 已排除的内容

| 路径 | 原因 |
|---|---|
| `.env` | 含真实 LLM / SMTP / 搜索 API Key |
| `data/app.db*` | 含对话、会议、真实收件邮箱、send_log |
| `data/uploads/` | 上传原文 |
| `data/outbox/` | dry-run 邮件正文/收件人 |
| `data/generated/` | 生成的 PPT |
| `data/backups/` | 数据库备份 |
| `evidence/` | 截图/API JSON 含真实收件邮箱 |
| `delivery/` | 历史交接材料含本地路径/个人信息 |
| `.tmp/`、`.vendor/`、`__pycache__/` | 本地工具链/缓存 |

## 公开仓库中保留的内容

- `app/`、`web/`、`scripts/` 源码
- `data/samples/`：演示用合成 PDF/TXT
- `data/mock/`：离线自检 fixture
- `.env.example`：只有空占位
- `requirements.txt`
- 四份交付文档：`README.md`、`DESIGN.md`、`TESTING.md`、`RISKS.md`

## 已经做过的检查

1. 全目录搜索真实 `SEARCH_API` 值：导出目录命中 0 个文件。
2. 搜索真实收件邮箱 / 本地用户名路径：导出目录命中 0 个文件。
3. 检查 `.env` 未被复制。
4. 检查 `evidence/`、`data/app.db`、`data/backups/` 未进入导出目录。
5. `data/samples` 与 `data/mock` 只含合成演示内容，无邮箱/电话/身份证。
6. 在导出目录中用 `PYTHONPATH` 指向本地 `.vendor` 跑过 8 组自检。

## 上传前最后一步

```powershell
cd github_upload
git init
git add .
git status
```

确认 `git status` 里**没有**：

```text
.env
data/app.db
data/backups/
evidence/
delivery/
```

然后再提交并推送。
