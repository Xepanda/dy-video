#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
test_v33_visual_director.py - V3.3 视觉导演套件、混合视觉感知与安全门禁回归测试
==============================================================================
覆盖：
  1. P1-01: ContinuityAuditor -> EditorialQcAuditor 接缝 BLOCK 接线；
  2. V33-01: 女装品类档案 contracts/profiles/womenswear.yaml 加载与子品类 ROI 解析；
  3. V33-02: VideoFrameProvider 顺序 3 点批抽（单次打开 + 同 stem 不同扩展名隔离）；
  4. V33-03: HybridGarmentVisionBackend（躯干 ROI + 去白底 + Mock ONNX 高置信/灰区/漂移 + 离线降级）；
  5. V33-04: build_director_digest 压缩与 4×4 Contact Sheet / cover_sheet 生成 + inspect-candidate；
  6. V33-05: Proposal v3.3 微调与 visual_overrides 全部硬门禁（8 项安全断言）；
  7. V33-07: inspect-render 生成 seams_review.jpg 与 CutOptimizer 建议且只读不改计划。
"""

import os
import sys
import json
import hashlib
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.core.category_profile import load_category_profile, resolve_subcategory
from src.core.frame_provider import VideoFrameProvider
from src.core.vision import HybridGarmentVisionBackend
from src.core.director_digest import build_director_digest
from src.core.project_context import compute_full_file_sha256
from src.runtime.stage_services import (
    compute_candidate_pool_hash,
    stage_validate_plan,
    stage_inspect_candidate,
    stage_inspect_render,
    stage_qc,
    stage_deliver,
)


def _make_synthetic_video(path: str, n_frames: int = 90, fps: int = 30, bgr=(60, 120, 210), size=(360, 640)) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(path, fourcc, fps, size)
    for i in range(n_frames):
        frame = np.full((size[1], size[0], 3), bgr, dtype=np.uint8)
        cv2.rectangle(frame, (80, 150), (280, 440), (20, 180, 220), -1)
        cv2.putText(frame, f"F{i:03d}", (110, 300), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 2)
        writer.write(frame)
    writer.release()


def _make_solid_image(path: str, bgr=(20, 180, 220), bg=(255, 255, 255), size=(300, 300)) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img = np.full((size[1], size[0], 3), bg, dtype=np.uint8)
    cv2.rectangle(img, (60, 60), (240, 240), bgr, -1)
    cv2.imwrite(path, img)


class _MockOnnxEncoder:
    def __init__(self, ready: bool = True, sim_map=None):
        self._ready = ready
        self.sim_map = sim_map or {}
        self.calls = 0

    def is_ready(self) -> bool:
        return self._ready

    def encode(self, image_bgr):
        if not self._ready:
            return None
        self.calls += 1
        # 用图像均值构造可预测单位向量
        mean_val = float(image_bgr.mean())
        if mean_val > 180:
            return np.array([1.0, 0.0], dtype=np.float32)
        if mean_val > 100:
            return np.array([0.85, 0.5268], dtype=np.float32)  # cos ~0.85 with [1,0]
        return np.array([0.60, 0.80], dtype=np.float32)  # cos = 0.60 (gray zone)


class TestV33VisualDirector(unittest.TestCase):

    def test_v33_01_womenswear_profile_and_subcategory(self):
        prof = load_category_profile({"product": {"domain_profile": "womenswear"}})
        self.assertEqual(prof["profile_id"], "womenswear")
        self.assertIn("换下一款", prof["changeover_patterns"])
        self.assertIn("羊绒", prof["factual_claim_tokens"])

        sub_knit = resolve_subcategory("法式羊毛针织上衣", prof)
        self.assertEqual(sub_knit["subcategory"], "knitwear")
        self.assertEqual(sub_knit["roi_region"], "upper_torso")

        sub_dress = resolve_subcategory("法式碎花连衣裙", prof)
        self.assertEqual(sub_dress["subcategory"], "dress")
        self.assertEqual(sub_dress["roi_region"], "full_body_dress")

        sub_pants = resolve_subcategory("高腰垂坠西裤", prof)
        self.assertEqual(sub_pants["subcategory"], "pants")
        self.assertEqual(sub_pants["roi_region"], "lower_body")

    def test_v33_02_sequential_3point_batch_extraction_and_stem_isolation(self):
        with tempfile.TemporaryDirectory() as td:
            work_dir = os.path.join(td, "work", "run_batch")
            os.makedirs(work_dir, exist_ok=True)
            v1 = os.path.join(td, "live01.mp4")
            v2 = os.path.join(td, "live01.mov")
            _make_synthetic_video(v1, n_frames=90, fps=30, bgr=(50, 90, 180))
            _make_synthetic_video(v2, n_frames=90, fps=30, bgr=(180, 90, 50))

            provider = VideoFrameProvider(work_dir=work_dir)
            provider.register_source("src_mp4", v1)
            provider.register_source("src_mov", v2)

            cands = []
            for i in range(12):
                cands.append({
                    "candidate_id": f"c_mp4_{i:03d}",
                    "source_id": "src_mp4",
                    "source_file": v1,
                    "source_start_ms": i * 100,
                    "source_end_ms": i * 100 + 1600,
                })
                cands.append({
                    "candidate_id": f"c_mov_{i:03d}",
                    "source_id": "src_mov",
                    "source_file": v2,
                    "source_start_ms": i * 100,
                    "source_end_ms": i * 100 + 1600,
                })

            provider.extract_frames_batch(cands)
            # 2 个源视频总共只需打开 2 次 VideoCapture（而非 24 次）
            self.assertEqual(provider.stats["video_opens"], 2)
            # 同 stem 不同扩展名的帧文件名完全隔离
            mp4_frames = set(cands[0]["frame_refs"])
            mov_frames = set(cands[1]["frame_refs"])
            self.assertTrue(len(mp4_frames) >= 2)
            self.assertTrue(len(mov_frames) >= 2)
            self.assertTrue(mp4_frames.isdisjoint(mov_frames))

    def test_v33_03_hybrid_vision_backend_onnx_and_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            ref_img = os.path.join(td, "ref.png")
            f_match = os.path.join(td, "f_match.jpg")
            f_gray = os.path.join(td, "f_gray.jpg")
            _make_solid_image(ref_img, bgr=(220, 220, 220), bg=(255, 255, 255))
            # 构造与背景截然不同、中心躯干区域与商品一致的测试图
            img_m = np.full((400, 300, 3), (10, 10, 10), dtype=np.uint8)
            img_m[80:290, 60:240] = (220, 220, 220)
            cv2.imwrite(f_match, img_m)

            img_g = np.full((400, 300, 3), (30, 30, 30), dtype=np.uint8)
            img_g[80:290, 60:240] = (60, 60, 60)
            cv2.imwrite(f_gray, img_g)

            prof = {"name": "羊毛针织上衣", "category": "针织上衣", "reference_image_ids": [ref_img]}

            # 1) Mock ONNX 高置信同款
            be_onnx = HybridGarmentVisionBackend(onnx_encoder=_MockOnnxEncoder(ready=True), enable_pose=False)
            c1 = {"candidate_id": "c1", "text": "羊毛手感柔软"}
            dec1 = be_onnx.analyze_candidate(c1, [f_match, f_match], prof)
            self.assertEqual(dec1["vision_engine"], "onnx_semantic")
            self.assertEqual(dec1["sku_relation"], "SAME_SKU")
            self.assertFalse(dec1["visual_gray_zone"])

            # 2) Mock ONNX 灰区判定
            c2 = {"candidate_id": "c2", "text": "侧身展示"}
            dec2 = be_onnx.analyze_candidate(c2, [f_gray, f_gray], prof)
            self.assertEqual(dec2["sku_relation"], "UNCERTAIN")
            self.assertTrue(dec2["visual_gray_zone"])

            # 3) Mock ONNX 3点段内漂移检测
            c3 = {"candidate_id": "c3", "text": "段尾换款"}
            dec3 = be_onnx.analyze_candidate(c3, [f_match, f_gray], prof)
            self.assertIn("MID_CLIP_SKU_DRIFT", c3.get("risks", []))
            self.assertEqual(dec3["motion_phase"], "INCOMPLETE")

            # 4) 无权重离线降级为 ROI HSV
            be_fb = HybridGarmentVisionBackend(onnx_encoder=_MockOnnxEncoder(ready=False), enable_pose=False)
            c4 = {"candidate_id": "c4", "text": "正面展示"}
            dec4 = be_fb.analyze_candidate(c4, [f_match], prof)
            self.assertEqual(dec4["vision_engine"], "hsv_roi_fallback")
            self.assertEqual(dec4["vision_tier"], "weak_fallback")
            self.assertEqual(dec4["roi_mode"], "center_crop")

    def test_v33_04_director_digest_and_inspect_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            work_dir = os.path.join(td, "my_proj", "work", "run_digest")
            analysis_dir = os.path.join(work_dir, "analysis")
            os.makedirs(analysis_dir, exist_ok=True)
            f_img = os.path.join(work_dir, "candidate_frames", "f0.jpg")
            _make_solid_image(f_img, bgr=(40, 150, 220))

            cands = []
            scores = []
            roles = ["HOOK", "FABRIC", "FIT", "STYLING", "COLOR", "CLOSING", "PROOF"]
            for i in range(120):
                cid = f"c_s1_{i:03d}"
                is_gray = (i >= 100)
                rel = "UNCERTAIN" if is_gray else "SAME_SKU"
                cands.append({
                    "candidate_id": cid,
                    "source_id": "s1",
                    "source_file": "s1.mp4",
                    "source_start_ms": i * 3000,
                    "source_end_ms": i * 3000 + 2800,
                    "product_relation": rel,
                    "eligible": not is_gray,
                    "visual_gray_zone": is_gray,
                    "vision_similarity": 0.62 if is_gray else 0.88,
                    "shot_scale": "FULL_BODY",
                    "composite_score": 80.0 - (i % 20),
                    "frame_refs": [f_img],
                    "selling_point_evidence_refs": ["羊毛"] if i % 2 == 0 else [],
                    "text": f"第{i}段法式羊毛针织显瘦口播句子完整。",
                    "risks": [],
                })
                scores.append({
                    "candidate_id": cid,
                    "eligible": not is_gray,
                    "product_relation": rel,
                    "narrative_role": roles[i % len(roles)],
                    "duplicate_cluster_id": f"cl_{i // 2}",
                    "rejection_reasons": ["严格模式下未通过身份核实 (UNCERTAIN)"] if is_gray else [],
                })

            covers = [{"id": "cov_01", "image_path": f_img, "frontal_score": 95.0, "framing": {"post_scale_sharpness": 320}}]
            res = build_director_digest(work_dir, cands, scores, covers, {"name": "羊毛针织上衣"})
            self.assertLessEqual(res["featured_count"], 54)
            self.assertGreater(res["featured_count"], 10)
            self.assertGreater(res["gray_zone_count"], 0)
            self.assertTrue(os.path.isfile(res["director_brief_md"]))
            self.assertTrue(os.path.isfile(res["cover_sheet_path"]))
            self.assertTrue(len(res["storyboard_sheets"]) >= 1)
            self.assertTrue(os.path.isfile(res["storyboard_sheets"][0]))

            # 写入 candidates/scores 测试 inspect-candidate
            with open(os.path.join(analysis_dir, "candidates.json"), "w", encoding="utf-8") as f:
                json.dump(cands, f, ensure_ascii=False)
            with open(os.path.join(analysis_dir, "scores.json"), "w", encoding="utf-8") as f:
                json.dump(scores, f, ensure_ascii=False)

            insp = stage_inspect_candidate(work_dir, ids=["c_s1_004"])
            self.assertEqual(insp["matched_count"], 1)
            self.assertEqual(insp["candidates"][0]["candidate_id"], "c_s1_004")
            self.assertTrue(insp["candidates"][0]["prev_context"])
            self.assertTrue(insp["candidates"][0]["next_context"])

            insp_gz = stage_inspect_candidate(work_dir, gray_zone_only=True, top=5)
            self.assertEqual(insp_gz["matched_count"], 5)

    def _setup_mock_run(self, td: str):
        proj_root = os.path.join(td, "proj_v33")
        mat_dir = os.path.join(proj_root, "素材")
        run_id = "run_v33_test"
        work_dir = os.path.join(proj_root, "work", run_id)
        out_dir = os.path.join(proj_root, "output", run_id)
        analysis_dir = os.path.join(work_dir, "analysis")
        sb_dir = os.path.join(analysis_dir, "storyboards")
        os.makedirs(mat_dir, exist_ok=True)
        os.makedirs(sb_dir, exist_ok=True)
        os.makedirs(out_dir, exist_ok=True)

        v1 = os.path.join(mat_dir, "live01.mp4")
        _make_synthetic_video(v1, n_frames=300, fps=30)
        v1_sha = compute_full_file_sha256(v1)
        sheet_p = os.path.join(sb_dir, "sheet_01.jpg")
        _make_solid_image(sheet_p, bgr=(80, 120, 200))

        spec = {
            "project_id": "proj_v33",
            "raw_video_paths": [v1],
            "product": {
                "name": "法式羊毛针织上衣",
                "category": "针织上衣",
                "selling_points": ["羊毛混纺", "显瘦版型"],
                "domain_profile": "womenswear",
            },
            "options": {
                "duration_range": "4-12",
                "automation": {"quality_policy": "strict", "visual_verification": "required"},
            },
        }
        spec_p = os.path.join(mat_dir, "job_spec.json")
        with open(spec_p, "w", encoding="utf-8") as f:
            json.dump(spec, f, ensure_ascii=False)
        with open(os.path.join(work_dir, "spec_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump(spec, f, ensure_ascii=False)

        manifest = {
            "source_videos": [{"source_id": "src_01", "path": v1, "filename": "live01.mp4", "sha256": v1_sha}],
            "product_images": [],
        }
        with open(os.path.join(work_dir, "input_manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False)

        with open(os.path.join(work_dir, "product_profile.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "法式羊毛针织上衣", "selling_points": ["羊毛混纺"], "quality_policy": "strict"}, f, ensure_ascii=False)

        ctx = {
            "schema_version": "3.3",
            "run_id": run_id,
            "project_id": "proj_v33",
            "project_root": proj_root,
            "materials_dir": mat_dir,
            "work_dir": work_dir,
            "output_dir": out_dir,
            "spec_path": spec_p,
            "spec_fingerprint": "fp_v33_test",
        }
        with open(os.path.join(work_dir, "run_context.json"), "w", encoding="utf-8") as f:
            json.dump(ctx, f, ensure_ascii=False)

        cands = [
            {
                "candidate_id": "c_ok_1",
                "source_id": "src_01",
                "source_file": v1,
                "source_file_sha256": v1_sha,
                "source_start_ms": 500,
                "source_end_ms": 3500,
                "product_relation": "SAME_SKU",
                "eligible": True,
                "text": "姐妹们看这件法式羊毛混纺针织上衣手感太软糯了。",
                "selling_point_evidence_refs": ["羊毛混纺"],
                "frame_ids": ["f1.jpg"],
                "risks": [],
            },
            {
                "candidate_id": "c_gray_2",
                "source_id": "src_01",
                "source_file": v1,
                "source_file_sha256": v1_sha,
                "source_start_ms": 4200,
                "source_end_ms": 7200,
                "product_relation": "UNCERTAIN",
                "eligible": False,
                "visual_gray_zone": True,
                "text": "这个正肩显瘦版型上身特别气质。",
                "selling_point_evidence_refs": ["显瘦版型"],
                "frame_ids": ["f2.jpg"],
                "risks": [],
                "rejection_reasons": ["严格模式下未通过身份核实 (UNCERTAIN)"],
            },
            {
                "candidate_id": "c_other_3",
                "source_id": "src_01",
                "source_file": v1,
                "source_file_sha256": v1_sha,
                "source_start_ms": 7500,
                "source_end_ms": 9500,
                "product_relation": "OTHER_SKU",
                "eligible": False,
                "text": "我们换下一款牛仔裤看看。",
                "selling_point_evidence_refs": [],
                "frame_ids": ["f3.jpg"],
                "risks": [],
                "rejection_reasons": ["非主推商品或属于换款/离场区间"],
            },
        ]
        scores = [
            {"candidate_id": "c_ok_1", "eligible": True, "product_relation": "SAME_SKU", "narrative_role": "HOOK", "rejection_reasons": []},
            {"candidate_id": "c_gray_2", "eligible": False, "product_relation": "UNCERTAIN", "narrative_role": "FIT", "rejection_reasons": ["严格模式下未通过身份核实 (UNCERTAIN)"]},
            {"candidate_id": "c_other_3", "eligible": False, "product_relation": "OTHER_SKU", "narrative_role": "PROOF", "rejection_reasons": ["非主推商品或属于换款/离场区间"]},
        ]
        pool_h = compute_candidate_pool_hash(cands)
        scores_h = hashlib.sha256(json.dumps(scores, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:24]
        with open(os.path.join(analysis_dir, "candidates.json"), "w", encoding="utf-8") as f:
            json.dump(cands, f, ensure_ascii=False)
        with open(os.path.join(analysis_dir, "scores.json"), "w", encoding="utf-8") as f:
            json.dump(scores, f, ensure_ascii=False)
        with open(os.path.join(analysis_dir, "candidate_pool_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump({"candidate_pool_hash": pool_h, "scores_hash": scores_h}, f, ensure_ascii=False)

        return work_dir, out_dir, pool_h, v1

    def test_v33_05_and_07_proposal_gates_continuity_and_inspect_render(self):
        with tempfile.TemporaryDirectory() as td:
            work_dir, out_dir, pool_h, v1 = self._setup_mock_run(td)
            prop_path = os.path.join(work_dir, "proposal.json")

            def _write_prop(d):
                with open(prop_path, "w", encoding="utf-8") as f:
                    json.dump(d, f, ensure_ascii=False)

            # 1) 严禁覆盖 OTHER_SKU
            _write_prop({
                "schema_version": "3.3",
                "candidate_pool_hash": pool_h,
                "ordered_candidate_ids": ["c_ok_1", "c_other_3"],
                "visual_overrides": [{
                    "candidate_id": "c_other_3",
                    "override_relation": "SAME_SKU",
                    "inspected_frame_refs": ["analysis/storyboards/sheet_01.jpg#03"],
                    "visual_evidence_note": "试图强行覆盖他款商品必须被硬拦截",
                }],
            })
            with self.assertRaises(ValueError) as cm1:
                stage_validate_plan(work_dir, prop_path)
            self.assertIn("OTHER_SKU", str(cm1.exception))

            # 2) 越界伪造帧路径必须拦截
            _write_prop({
                "schema_version": "3.3",
                "candidate_pool_hash": pool_h,
                "ordered_candidate_ids": ["c_ok_1", "c_gray_2"],
                "visual_overrides": [{
                    "candidate_id": "c_gray_2",
                    "override_relation": "SAME_SKU",
                    "inspected_frame_refs": ["../../素材/live01.mp4"],
                    "visual_evidence_note": "试图引用工作区外路径伪造帧证据必须拦截",
                }],
            })
            with self.assertRaises(ValueError) as cm2:
                stage_validate_plan(work_dir, prop_path)
            self.assertIn("越界", str(cm2.exception))

            # 3) 超限 cut_adjust_ms (>500ms) 或向内倒切吞字 (<-150ms) 必须拦截
            _write_prop({
                "schema_version": "3.3",
                "candidate_pool_hash": pool_h,
                "ordered_candidate_ids": ["c_ok_1"],
                "clip_overrides": {"c_ok_1": {"cut_adjust_end_ms": 900}},
            })
            with self.assertRaises(ValueError) as cm3:
                stage_validate_plan(work_dir, prop_path)
            self.assertIn("超过硬上限", str(cm3.exception))

            _write_prop({
                "schema_version": "3.3",
                "candidate_pool_hash": pool_h,
                "ordered_candidate_ids": ["c_ok_1"],
                "clip_overrides": {"c_ok_1": {"cut_adjust_end_ms": -300}},
            })
            with self.assertRaises(ValueError) as cm4:
                stage_validate_plan(work_dir, prop_path)
            self.assertIn("吞字", str(cm4.exception))

            # 4) 杜撰未证实材质贴纸（如档案无"100%山羊绒"）必须拦截
            _write_prop({
                "schema_version": "3.3",
                "candidate_pool_hash": pool_h,
                "ordered_candidate_ids": ["c_ok_1"],
                "clip_overrides": {"c_ok_1": {"sticker_text": "100%山羊绒"}},
            })
            with self.assertRaises(ValueError) as cm5:
                stage_validate_plan(work_dir, prop_path)
            self.assertIn("未经商品档案或台词证实", str(cm5.exception))

            # 5) 合法灰区 visual_override + 合法微调 + 贴纸/Hook 覆写 放行并贯通 inspect-render -> qc -> deliver
            _write_prop({
                "schema_version": "3.3",
                "candidate_pool_hash": pool_h,
                "agent_name": "test-director",
                "ordered_candidate_ids": ["c_ok_1", "c_gray_2"],
                "hook_card_override": {"text": "法式羊毛混纺 · 显瘦气质"},
                "cta_override": {"text": "戳左下角小黄车看细节"},
                "clip_overrides": {
                    "c_ok_1": {"cut_adjust_end_ms": 200, "zoom": 1.25, "sticker_text": "羊毛混纺"},
                    "c_gray_2": {"effect": "hflip", "sticker_text": "正肩显瘦版型"},
                },
                "visual_overrides": [{
                    "candidate_id": "c_gray_2",
                    "original_relation": "UNCERTAIN",
                    "override_relation": "SAME_SKU",
                    "inspected_frame_refs": ["analysis/storyboards/sheet_01.jpg#02"],
                    "visual_evidence_note": "肉眼核验联系表第2格主播实穿本品法式羊毛针织上衣且版型领口一致",
                }],
            })
            vres = stage_validate_plan(work_dir, prop_path)
            self.assertEqual(vres["status"], "PLAN_VALIDATED")
            self.assertEqual(vres["agent_visual_overrides_count"], 1)

            with open(os.path.join(work_dir, "validated_plan.json"), "r", encoding="utf-8") as pf:
                vplan = json.load(pf)
            self.assertAlmostEqual(vplan["clips"][0]["end"], 3.7, places=2)
            self.assertEqual(vplan["clips"][0]["sticker_text"], "羊毛混纺")
            self.assertEqual(vplan["clips"][1]["product_relation"], "SAME_SKU")
            self.assertEqual(vplan["hook_card"]["text"], "法式羊毛混纺 · 显瘦气质")

            # 模拟已渲染成片与 render_manifest.json，测试 inspect-render + stage_qc (ContinuityAuditor 接线) + deliver 溯源
            final_mp4 = os.path.join(out_dir, "final.mp4")
            _make_synthetic_video(final_mp4, n_frames=210, fps=30)
            _make_solid_image(os.path.join(out_dir, "cover.jpg"))
            f_sha = compute_full_file_sha256(final_mp4)
            rm = {
                "schema_version": "3.3",
                "run_id": "run_v33_test",
                "validated_plan_hash": vplan["validated_plan_hash"],
                "video_sha256": f_sha,
            }
            with open(os.path.join(out_dir, "render_manifest.json"), "w", encoding="utf-8") as rmf:
                json.dump(rm, rmf, ensure_ascii=False)

            insp_r = stage_inspect_render(work_dir)
            self.assertEqual(insp_r["status"], "INSPECTED")
            self.assertEqual(insp_r["seams_count"], 1)
            self.assertTrue(os.path.isfile(insp_r["seams_review_path"]))

            # 运行 stage_qc（mock QcAuditor 返回 True，真实运行 ContinuityAuditor + EditorialQcAuditor）
            class _PassTechQc:
                def __init__(self, **kwargs):
                    pass

                def audit_final_deliverables(self, report_output_path=None, **kwargs):
                    if report_output_path:
                        with open(report_output_path, "w", encoding="utf-8") as tf:
                            json.dump({"all_passed": True, "passed": True}, tf)
                    return True

            with patch("src.runtime.stage_services.QcAuditor", _PassTechQc), \
                 patch("src.runtime.stage_services.compute_job_fingerprint", return_value="fp_v33_test"):
                qres = stage_qc(work_dir)
                self.assertTrue(qres["dual_qc_passed"])
                self.assertTrue(os.path.isfile(qres["continuity_review_path"]))
                with open(qres["editorial_qc"]["report_path"], "r", encoding="utf-8") as ef:
                    ed_data = json.load(ef)
                gates = {c["gate"]: c["status"] for c in ed_data["checks"]}
                self.assertIn("SeamContinuityGate", gates)
                self.assertTrue(ed_data["has_agent_visual_overrides"])

                dres = stage_deliver(work_dir)
                self.assertEqual(dres["status"], "DELIVERED")
                self.assertTrue(dres["has_agent_visual_overrides"])
                with open(dres["delivery_manifest"], "r", encoding="utf-8") as df:
                    dm = json.load(df)
                self.assertEqual(len(dm["agent_visual_overrides"]), 1)


if __name__ == "__main__":
    unittest.main()
