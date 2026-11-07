"""一条命令跑全部离线自检（新会话接手时先跑这个）。

用法: python -m scripts.verify_all
"""
from __future__ import annotations

import subprocess
import sys

CHECKS = [
    ("M0 会议链（闸门/幂等/发信）", "scripts.selfcheck_m0"),
    ("对话链（工具循环/收敛/截断）", "scripts.selfcheck_chat"),
    ("长期记忆（实体/合并/召回）", "scripts.selfcheck_memory"),
    ("G2 PDF（抽取/切块/FTS5/跨文档）", "scripts.selfcheck_retrieval"),
    ("G6 PPT（5 页渲染/工具/下载）", "scripts.selfcheck_ppt"),
    ("G5 搜索（解析/引用/缓存降级）", "scripts.selfcheck_search"),
    ("D6 加固（超限/超时/错误路径/锁等待）", "scripts.selfcheck_hardening"),
    ("安全修复（CSRF/TLS/上限/失败隔离）", "scripts.selfcheck_security"),
]


def main() -> int:
    print("=" * 70)
    failed = []
    for title, module in CHECKS:
        print("\n▶ %s  (%s)" % (title, module))
        result = subprocess.run(
            [sys.executable, "-m", module],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        tail = [line for line in (result.stdout or "").splitlines() if "项通过" in line or "FAIL" in line]
        for line in tail[-6:]:
            print("   " + line.strip())
        if result.returncode != 0:
            failed.append(title)
            err = (result.stderr or "").strip().splitlines()[-3:]
            for line in err:
                print("   ! " + line)

    print("\n" + "=" * 70)
    if failed:
        print("未通过：%s" % "、".join(failed))
        return 1
    print("全部通过：%d 组自检" % len(CHECKS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
