#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
path_resolver.py - 任务契约里的路径归一化（可移植性）
==========================================================
背景（2026-10-24）：spec 与 GUI 工具此前把素材/商品图路径硬编码成
`D:/Projects/dy-live-ecommerce-editor/...` 这类**机器专属绝对路径**，
换机器、换 CI runner、换仓库目录位置就全部失效——这是真实的可移植性缺陷。

本模块提供统一入口：
  resolve_path(p, base_dir)  —— 把相对路径 / 旧机器绝对路径统一解析为
                                本机可用的绝对路径；
  relativize(p, base_dir)    —— 把本机绝对路径转成**相对仓库根**的相对路径，
                                便于写回 spec 保持可移植。

解析优先级：
  1. 已是本机存在的绝对路径 → 直接用；
  2. 相对路径（相对 base_dir 或仓库根）→ 解析为绝对路径；
  3. 绝对路径但本机不存在（换机器后的旧机器路径）→ 尝试用**尾部片段**
     在仓库内重新定位（如 D:/old/data/raw_samples/x.mp4 → <repo>/data/raw_samples/x.mp4）。
"""
import os
import glob
from typing import Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def resolve_path(path: str, base_dir: str = None) -> Optional[str]:
    """把契约里的路径解析为本机可用的绝对路径；找不到返回 None。

    base_dir：相对路径的基准目录（通常为 spec 所在目录）。缺省用仓库根。
    """
    if not path:
        return None
    base_dir = base_dir or PROJECT_ROOT
    p = str(path).strip().strip('"').strip("'")
    if not p:
        return None

    # 1) 绝对路径且存在
    if os.path.isabs(p):
        if os.path.exists(p):
            return os.path.abspath(p)
        # 3) 旧机器绝对路径 → 按尾部片段在仓库内重新定位
        relocated = _relocate_by_tail(p)
        if relocated:
            return relocated
        return os.path.abspath(p)  # 保留原样（初始化容错契约）

    # 2) 相对路径
    for base in (base_dir, PROJECT_ROOT, os.getcwd()):
        if not base:
            continue
        cand = os.path.abspath(os.path.join(base, p))
        if os.path.exists(cand):
            return cand
    return os.path.abspath(os.path.join(base_dir, p))


def _relocate_by_tail(abs_path: str) -> Optional[str]:
    """旧机器绝对路径 → 在仓库内按尾部片段重新定位。

    例：D:/old/project/data/raw_samples/8 某商品/x.mp4
        → <repo>/data/raw_samples/8 某商品/x.mp4
    实现：从绝对路径尾部逐级缩短，找到在仓库内存在的最长后缀。
    """
    norm = abs_path.replace("\\", "/")
    parts = [x for x in norm.split("/") if x]
    # 从尾部往前找，尝试 1~6 级后缀
    for tail_len in range(1, min(7, len(parts))):
        tail = "/".join(parts[-tail_len:])
        pattern = os.path.join(PROJECT_ROOT, "**", tail)
        hits = glob.glob(pattern, recursive=True)
        if len(hits) == 1:
            return os.path.abspath(hits[0])
    return None


def relativize(path: str, base_dir: str = None) -> str:
    """把绝对路径转成相对 base_dir 的相对路径（能相对化就相对化）。

    写回 spec 时用这个，保证 spec 不含机器专属前缀。
    路径不在 base_dir 之下时原样返回。
    """
    if not path:
        return path
    base_dir = os.path.abspath(base_dir or PROJECT_ROOT)
    try:
        ap = os.path.abspath(path)
        rel = os.path.relpath(ap, base_dir)
    except (ValueError, OSError):
        return path
    # Windows 跨盘符时 relpath 会抛 ValueError，上面已捕获
    if rel.startswith(".."):
        return path
    return rel.replace("\\", "/")
