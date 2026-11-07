"""AI 外脑 Demo —— 单进程 Python 应用。"""
import pathlib
import sys

# 本机 pip 装全局会被沙箱拒绝（WinError 5），第三方依赖统一装到 .vendor。
# 所有 from app... 的入口都会先执行这里，因此 .vendor 对全项目可见。
_VENDOR = pathlib.Path(__file__).resolve().parents[1] / ".vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))
