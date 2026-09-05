#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地路径解析。

把原先散落在各脚本里的硬编码绝对路径拆成两类处理：

A. 仓库自身路径（TOOL_DIR / BACKEND_DIR / SERVER_PY / VENV_PY）
   由 ``__file__`` 相对推导，不需要任何配置，clone 到任何目录都能跑。

B. 使用者本机的数据目录（论文归档目录、Chrome 下载目录、临时清单等）
   位于仓库之外，相对路径无法表达，必须从外部配置读取，优先级：
       ``local_config.json``  <  环境变量 ``CNKI_<KEY>``  <  显式传参
   其中 ``local_config.json`` 含个人路径，已加入 .gitignore，不会入库；
   仓库只提供 ``local_config.example.json`` 模板。

用法::

    import local_paths as P
    DST = P.user_path("DST")
    SRC = P.user_path("SRC")
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

# --------------------------------------------------------------------------
# A 类：仓库自身路径（相对推导，零配置）
# --------------------------------------------------------------------------
TOOL_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TOOL_DIR / "backend"
SERVER_PY = BACKEND_DIR / "bridge_server.py"
VENV_PY = BACKEND_DIR / ".venv" / "Scripts" / "python.exe"

CONFIG_FILE = TOOL_DIR / "local_config.json"
EXAMPLE_FILE = TOOL_DIR / "local_config.example.json"

# B 类：需要外部配置的用户数据路径键
USER_KEYS = ("DST", "SRC", "TARGETS", "MAP", "LOG")

# 允许兜底到系统临时目录的键（缺失不致命，自动落到 temp）
_FALLBACK_TO_TEMP = {
    "MAP": "downloaded_map.json",
    "LOG": "collab_cnki.log",
}


def _load_user_config() -> dict[str, str]:
    """读取 local_config.json，再用环境变量覆盖。"""
    cfg: dict[str, str] = {}
    if CONFIG_FILE.exists():
        try:
            raw = CONFIG_FILE.read_text(encoding="utf-8")
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                cfg = {str(k): str(v) for k, v in loaded.items() if v}
        except (OSError, ValueError):
            cfg = {}
    # 环境变量优先：CNKI_DST / CNKI_SRC / CNKI_TARGETS ...
    for key in USER_KEYS:
        env_val = os.environ.get(f"CNKI_{key}")
        if env_val:
            cfg[key] = env_val
    return cfg


CONFIG = _load_user_config()


def user_path(key: str, default: str | None = None) -> str:
    """取得一个 B 类用户数据路径。

    优先级：显式 default  >  local_config.json  >  环境变量  >  temp 兜底。
    必需项（DST / SRC / TARGETS）缺失时给出明确指引，而不是带着空路径往下跑。
    """
    value = default or CONFIG.get(key)
    if value:
        return str(value)

    if key in _FALLBACK_TO_TEMP:
        return str(Path(tempfile.gettempdir()) / _FALLBACK_TO_TEMP[key])

    raise SystemExit(
        f"[配置缺失] 未设置路径 {key}。\n"
        f"  请在 {CONFIG_FILE} 中填写（模板见 {EXAMPLE_FILE}），\n"
        f"  或设置环境变量 CNKI_{key}。\n"
        f"  该路径指向你本机的个人目录，仓库内不保存默认值。"
    )


def load_config_json(path_str: str) -> list | dict:
    """读取 JSON 清单文件；不存在返回空列表，不抛异常。"""
    p = Path(path_str)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
