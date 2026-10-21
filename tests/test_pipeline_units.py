#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
test_pipeline_units.py - 自动化单元测试套件
覆盖 ASR 语境校验体、分镜语义打标器、Agent 决策总导演与质检审计逻辑。
"""

import unittest
import os
import json
import tempfile
import shutil

from src.core.asr_agent_verifier import AsrContextVerifier
from src.tools.analyze_story_segments import classify_segments
from src.agent.agent_director import AgentDirector


class TestAsrContextVerifier(unittest.TestCase):
    def setUp(self):
        self.spec = {
            "project_id": "test_apparel",
            "brand": {"title_en": "MIXCITY"},
            "product": {
                "title_line1": "重磅亲肤半岛棉",
                "title_line2": "知性立领针织衫"
            },
            "options": {
                "highlight_keywords": ["半岛棉", "超显瘦", "实物", "亲肤透气"]
            }
        }
        self.verifier = AsrContextVerifier(self.spec)

    def test_domain_homophone_correction(self):
        raw_segments = [
            {"start": 1.0, "end": 3.5, "text": "姐妹们看一下食物，这个是半导棉的面料。"},
            {"start": 3.6, "end": 5.8, "text": "上身之后人都瘦眉了，而且超选瘦。"},
            {"start": 6.0, "end": 8.0, "text": "平时如果拿不准的就不要卡马，正常按马术拍。"}
        ]
        
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            tmp_report = tmp.name

        try:
            corrected, report = self.verifier.verify_and_correct(raw_segments, report_output_path=tmp_report)
            self.assertEqual(len(corrected), 3)
            # 物理时间戳绝对守恒
            self.assertEqual(corrected[0]["start"], 1.0)
            self.assertEqual(corrected[0]["end"], 3.5)
            # 文本纠错校验
            self.assertIn("实物", corrected[0]["text"])
            self.assertIn("半岛棉", corrected[0]["text"])
            self.assertIn("瘦没了", corrected[1]["text"])
            self.assertIn("超显瘦", corrected[1]["text"])
            self.assertIn("卡码", corrected[2]["text"])
            self.assertIn("码数", corrected[2]["text"])
            self.assertTrue(os.path.exists(tmp_report))
        finally:
            if os.path.exists(tmp_report):
                os.remove(tmp_report)


class TestStorySegmentClassifier(unittest.TestCase):
    def test_price_and_inventory_rejection(self):
        segments = [
            {"start": 0.0, "end": 2.5, "text": "今天在直播间只要309，原价都是五六百的。"},
            {"start": 2.5, "end": 5.0, "text": "库存只剩最后30条了，抢完就没有了。"},
            {"start": 5.0, "end": 9.0, "text": "这个半岛棉针织面料超级软糯舒服。"},
            {"start": 9.0, "end": 13.0, "text": "穿上身非常显瘦，小肚子全部遮住。"},
            {"start": 13.0, "end": 17.0, "text": "下身搭一条老钱风西裤绝了。"}
        ]
        
        classified = classify_segments(segments)
        
        # 验证价格和库存被严格剔除入 rejected_candidates
        rejected = classified["rejected_candidates"]
        rejected_texts = [s["text"] for s in rejected]
        self.assertTrue(any("309" in t for t in rejected_texts))
        self.assertTrue(any("库存" in t for t in rejected_texts))
        
        # 验证面料、版型、穿搭分别归类
        self.assertTrue(len(classified["fabric_candidates"]) >= 1)
        self.assertTrue(len(classified["fit_candidates"]) >= 1)
        self.assertTrue(len(classified["styling_candidates"]) >= 1)


class TestAgentDirector(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.spec_path = os.path.join(self.test_dir, "test_spec.json")
        self.spec_data = {
            "project_id": "test_unit_project",
            "raw_video_path": "non_existent.mp4",
            "brand": {"title_en": "MIXCITY"},
            "product": {"title_line1": "半岛棉", "title_line2": "针织衫"},
            "options": {"target_duration": [48.0, 58.0]}
        }
        with open(self.spec_path, "w", encoding="utf-8") as f:
            json.dump(self.spec_data, f, ensure_ascii=False)

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def test_director_initialization(self):
        director = AgentDirector(self.spec_path)
        self.assertEqual(director.project_id, "test_unit_project")
        self.assertEqual(director.brand_info.get("title_en"), "MIXCITY")


class MockLandmark:
    def __init__(self, x: float, y: float, z: float = 0.0):
        self.x = x
        self.y = y
        self.z = z


class TestFramingNormalization(unittest.TestCase):
    def test_normalize_subject_framing(self):
        import numpy as np
        from src.tools.extract_cover_candidates import normalize_subject_framing
        
        # 模拟 1080x1920 原始画面与 33 关键点
        frame = np.full((1920, 1080, 3), 128, dtype=np.uint8)
        landmarks = [MockLandmark(0.5, 0.5) for _ in range(33)]
        # 鼻尖 (0), 左肩 (11), 右肩 (12), 左踝 (27), 右踝 (28)
        landmarks[0] = MockLandmark(0.5, 0.25)
        landmarks[11] = MockLandmark(0.45, 0.35)
        landmarks[12] = MockLandmark(0.55, 0.35)
        landmarks[27] = MockLandmark(0.48, 0.85)
        landmarks[28] = MockLandmark(0.52, 0.85)

        norm_canvas, meta = normalize_subject_framing(frame, landmarks)
        self.assertEqual(norm_canvas.shape, (1920, 1080, 3))
        self.assertAlmostEqual(meta["normalized_height_ratio"], 0.74, places=2)
        self.assertEqual(meta["head_top_y_px"], 307)
        self.assertEqual(meta["center_x_px"], 540)


if __name__ == "__main__":
    unittest.main()

