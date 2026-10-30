#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""test_zh_script_and_violation_policy.py — 字形归一 + 违规处置策略（2026-10-30）

覆盖：
  1. 繁体 → 简体（用户实测样例：褲/個/類/麼/嗎/覺）
  2. 时间戳守恒 + 幂等
  3. 字幕删词动作（delete：删词并收敛标点；mask：兼容旧行为）
  4. 处置策略：只删字幕、不消音、不打码；宽松（尺码等正常用法放行）
"""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import yaml

from src.tools.normalize_zh_script import (
    contains_traditional, normalize_segments, to_simplified)
from src.tools.detect_violation import mask_text_action
from src.tools.apply_violation_handling import prepare_violation_handling


def _constraints():
    with open(os.path.join(ROOT, "contracts", "constraints.yaml"),
              "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class TestZhNormalization(unittest.TestCase):
    def test_user_samples(self):
        """用户实测繁体样例必须完整转简体。"""
        cases = {
            "那款褲子搭個啥類型，感覺你知道嗎": "那款裤子搭个啥类型，感觉你知道吗",
            "這件桑蠶絲羊絨羊毛針織衫很顯瘦": "这件桑蚕丝羊绒羊毛针织衫很显瘦",
        }
        for src_text, expect in cases.items():
            self.assertEqual(to_simplified(src_text), expect)

    def test_idempotent_and_timestamps(self):
        segs = [{"start": 1.25, "end": 3.5, "text": "這款褲子很顯瘦",
                 "source_file": "a.mp4"}]
        out, rep = normalize_segments(segs)
        self.assertEqual(out[0]["start"], 1.25)
        self.assertEqual(out[0]["end"], 3.5)
        self.assertEqual(out[0]["source_file"], "a.mp4")
        self.assertEqual(out[0]["text"], "这款裤子很显瘦")
        self.assertEqual(rep["residual_traditional_segments"], [])
        out2, rep2 = normalize_segments(out)
        self.assertEqual(rep2["n_changed_segments"], 0, "归一必须幂等")

    def test_contains_traditional(self):
        self.assertTrue(contains_traditional("這個感覺"))
        self.assertFalse(contains_traditional("这个感觉"))


class TestSubtitleDeleteAction(unittest.TestCase):
    def test_delete_removes_word(self):
        text, hits = mask_text_action("这是全网最好的一件，质量不错", ["最好"])
        self.assertEqual(hits, ["最好"])
        self.assertNotIn("最好", text)
        self.assertNotIn("***", text)

    def test_mask_compat(self):
        text, _ = mask_text_action("这是最好的款", ["最好"], action="mask")
        self.assertIn("***", text)


class TestViolationPolicy(unittest.TestCase):
    def test_subtitle_only_no_mute_no_mosaic(self):
        c = _constraints()
        plan = {"raw_video_path": "x.mp4", "clips": [
            {"id": "c01", "st": 0.0, "end": 10.0, "dur": 10.0,
             "source_file": "", "zoom": 1.0}]}
        segs = [
            {"start": 1.0, "end": 3.0, "text": "这个最大码也有货",
             "source_file": ""},
            {"start": 4.0, "end": 6.0, "text": "这是全网最好的一件",
             "source_file": ""},
            {"start": 7.0, "end": 9.0, "text": "能根治痘印", "source_file": ""},
        ]
        wd = tempfile.mkdtemp()
        res = prepare_violation_handling(plan, segs, wd, c)

        # 宽松：尺码用语不产生候选/不删除
        self.assertNotIn("最大码", res["subtitle_delete_words"])
        # 真实违规：强宣传 + 医疗功效 → 仅删字幕
        self.assertIn("最好", res["subtitle_delete_words"])
        self.assertIn("根治", res["subtitle_delete_words"])
        # 不消音、不打码
        self.assertEqual(res["clips"]["c01"]["audio_mute"], [])
        self.assertEqual(res["clips"]["c01"]["video_mosaic"], [])
        self.assertFalse(res["summary"]["channels"]["audio_mute"])
        self.assertFalse(res["summary"]["channels"]["video_mosaic"])
        # 决策可审计：每条都有 action + reason
        for d in res["decisions"]:
            self.assertIn(d["action"], ("none", "delete_subtitle"))
            self.assertTrue(d["reason"])
        # 落盘文件含删词清单
        with open(os.path.join(wd, "violation_handling.json"),
                  "r", encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["subtitle_delete_words"],
                         res["subtitle_delete_words"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
