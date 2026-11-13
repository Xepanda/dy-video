#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CLI 入口：分镜片段品类与语义对齐核验。"""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.tools.verify_clip_product_alignment import main

if __name__ == "__main__":
    main()
