# -*- coding: utf-8 -*-
"""
_compat.py - 向后兼容测试 mock / patch 解析原语
================================================
当测试通过 unittest.mock.patch("src.runtime.stage_services.<sym>") 替换依赖时，
允许拆散在 src/runtime/stages/* 下的实现透明感知该 patch。
"""

import sys
from typing import Any


def resolve_dep(name: str, default: Any) -> Any:
    mod = sys.modules.get("src.runtime.stage_services")
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return default
