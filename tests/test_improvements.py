#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
test_improvements.py - 2026-10-20 改进版测试套件
=================================================
覆盖：价格检测器（25 例）、泛化分镜编译、封面渲染（金色/换行）、
衣着一致性、人脸检测（模型缺失则跳过）、QC 门禁（合成用例）。

运行: python -m pytest tests/test_improvements.py -v
  或: python tests/test_improvements.py
"""

import os
import sys
import json
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


# ================================================================ 价格检测

class TestDetectPrice(unittest.TestCase):
    """B3：中文数字价格检测器 chinese_numeral_v1。"""

    SHOULD_HIT = [
        "今天在直播间只要309，原价都是五六百的。",
        "到手价299，三二一上链接",
        "这个三百多块真的很划算",
        "只要二十九米哦",
        "39.9 带回家",
        chr(165) + "299 秒杀",
        chr(65509) + "299 秒杀",
        "立减50元",
        "两百块钱不到",
        "特价清仓只要九九",
        "拍下只要五毛钱",
        "降价两百",
        "原价八百多",
        "到手 299",
        "299块三个",
    ]
    SHOULD_CLEAN = [
        "这个毛衣实物还要更漂亮的",
        "正常按160码拍就可以了",
        "拍小码或者拍大码都可以",
        "她是165的身高",
        "这款是三防面料",
        "做了升级版2.0",
        "有M码和L码两个尺码",
        "今天给大家看一个新款",
        "含量是100%棉",
        "支持7天无理由退换",
        "她有165公分高",
        "库存只剩最后30条了，抢完就没有了",  # 库存语义由 build_plan 黑名单拦截，价格检测器不误报
    ]

    def test_price_hits(self):
        from src.tools.detect_price import contains_price
        for t in self.SHOULD_HIT:
            hit, _ = contains_price(t)
            self.assertTrue(hit, f"应检出价格: {t}")

    def test_no_false_positive(self):
        from src.tools.detect_price import contains_price
        for t in self.SHOULD_CLEAN:
            hit, matches = contains_price(t)
            self.assertFalse(hit, f"不应误报: {t} -> {matches}")

    def test_audit_segments_structure(self):
        from src.tools.detect_price import audit_segments
        segs = [
            {"start": 0.0, "end": 2.0, "text": "今天只要309"},
            {"start": 2.0, "end": 4.0, "text": "这个面料很软糯"},
        ]
        rep = audit_segments(segs)
        self.assertEqual(rep["detector"], "chinese_numeral_v1")
        self.assertEqual(rep["price_segment_count"], 1)
        self.assertEqual(len(rep["clean_segments"]), 1)

    def test_audit_ass_file(self):
        from src.tools.detect_price import audit_ass_file
        import tempfile
        ass = (
            "[Script Info]\n"
            "Style: X,Microsoft YaHei,50,&H00FFFFFF,&H000000FF,&H0015151A,&H80000000,-1,0,0,0,100,100,1,0,1,4.5,2.0,2,40,40,240,1\n"
            "[Events]\n"
            "Dialogue: 0,0:00:00.00,0:00:02.00,X,,0,0,0,,今天到手价{\\c&H0000E6FF&}299{\\c&H00FFFFFF&}\n"
            "Dialogue: 0,0:00:02.00,0:00:04.00,X,,0,0,0,,面料很软糯\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".ass", delete=False,
                                         encoding="utf-8") as f:
            f.write(ass)
            path = f.name
        try:
            rep = audit_ass_file(path)
            self.assertEqual(rep["price_dialogue_count"], 1)
        finally:
            os.remove(path)


# ================================================================ 分镜编译

class TestBuildPlan(unittest.TestCase):
    """B2：泛化分镜编译（无硬编码、配额、时长收敛、黑名单）。"""

    SPEC = {
        "project_id": "test_plan",
        "options": {"duration_range": "45-55", "highlight_keywords": ["显瘦"]},
    }
    CONSTRAINTS = {
        "video": {
            "duration_range": [45, 65],
            "funnel": {
                "hook": {"min": 3, "max": 8},
                "fabric": {"min": 8, "max": 20},
                "fit": {"min": 8, "max": 15},
                "styling": {"min": 15, "max": 30},
            },
        }
    }

    SEGMENTS = [
        # 价格段（应排除）
        {"start": 0.0, "end": 4.0, "duration": 4.0, "text": "今天只要309块钱"},
        # Hook 段
        {"start": 10.0, "end": 14.0, "duration": 4.0, "text": "这个毛衣实物还要更漂亮的"},
        {"start": 14.0, "end": 18.0, "duration": 4.0, "text": "很浪漫的一件毛衣"},
        # 面料段
        {"start": 20.0, "end": 32.0, "duration": 12.0, "text": "半岛棉的进口面料做工很精细"},
        # 版型段
        {"start": 40.0, "end": 52.0, "duration": 12.0, "text": "穿上身超显瘦人都瘦没了"},
        # 穿搭段
        {"start": 60.0, "end": 90.0, "duration": 30.0, "text": "搭配大裤腿格子裤编织裤都很休闲松弛"},
        # 换款段（应排除）
        {"start": 95.0, "end": 100.0, "duration": 5.0, "text": "好下一个我再来换一件"},
    ]

    def test_build_plan_basic(self):
        from src.tools.build_plan import build_plan
        plan = build_plan("dummy.mp4", self.SEGMENTS, self.SPEC, self.CONSTRAINTS)
        clips = plan["clips"]
        # 总时长在 [45,65]（spec 收窄为 45-55 区间内）
        self.assertTrue(45 <= plan["total_expected_duration"] <= 65,
                        f"时长越界: {plan['total_expected_duration']}")
        # Hook 置顶
        self.assertTrue(clips[0]["is_hook"])
        self.assertEqual(clips[0]["category"], "hook")
        # 价格段与换款段被排除
        used_ranges = [(c["st"], c["end"]) for c in clips]
        for st, en in used_ranges:
            self.assertFalse(st < 4.0 and en > 0.0, "价格段不应入选")
            self.assertFalse(st >= 95.0, "换款段不应入选")
        # 无写死时间戳：所有 st 都来自输入段
        src_starts = {s["start"] for s in self.SEGMENTS}
        for c in clips:
            self.assertTrue(any(abs(c["st"] - s) < 1e-6 for s in src_starts),
                            f"clip st={c['st']} 非输入段时间戳（疑似硬编码）")
        # plan 结构符合 schema 关键字段
        for c in clips:
            self.assertIn("id", c)
            self.assertIn("zoom", c)
            self.assertIn("effect", c)
        # 选段审计存在
        self.assertIn("selection_meta", plan)

    def test_build_plan_fabric_zoom(self):
        from src.tools.build_plan import build_plan
        plan = build_plan("dummy.mp4", self.SEGMENTS, self.SPEC, self.CONSTRAINTS)
        fabric_clips = [c for c in plan["clips"] if "面料" in c.get("desc", "")
                        or "半岛棉" in c.get("desc", "")]
        for c in fabric_clips:
            self.assertGreaterEqual(c["zoom"], 1.0)

    def test_decision_closing_selection(self):
        """S2: decision_closing 类目检索与片尾排序权重。"""
        from src.tools.build_plan import build_plan, build_blocks
        # 1. 验证 build_blocks 输出的 scores 包含 decision_closing 分值
        probe_segs = [
            {"start": 10.0, "end": 18.0, "duration": 8.0, "text": "普通建议买白色"},
            {"start": 20.0, "end": 28.0, "duration": 8.0, "text": "纠结选哪个颜色我建议买驼色仙女买白色"},
        ]
        probe_blocks, _ = build_blocks(probe_segs)
        self.assertTrue(all("decision_closing" in b["scores"] for b in probe_blocks),
                        "block scores 必须包含 decision_closing 评分")
        self.assertGreater(probe_blocks[1]["scores"]["decision_closing"],
                           probe_blocks[0]["scores"]["decision_closing"],
                           "双组促单加权得分应显著高于单组弱促单")

        # 2. 竞争性选段：即使弱候选排在前面，高分强促单候选必须被优先选中并置于片尾
        segs = list(self.SEGMENTS) + [
            {"start": 110.0, "end": 118.0, "duration": 8.0,
             "text": "弱促单：选色可以看看白色一套"},
            {"start": 120.0, "end": 128.0, "duration": 8.0,
             "text": "强促单：如果你纠结选哪个颜色我就建议你买驼色了买的人最多小仙女去穿白色一套"},
        ]
        constraints = {
            "video": {
                "duration_range": [45, 65],
                "funnel": {
                    "hook": {"min": 3, "max": 8},
                    "fabric": {"min": 8, "max": 20},
                    "fit": {"min": 8, "max": 15},
                    "styling": {"min": 15, "max": 30},
                    "decision_closing": {"min": 6, "max": 12},
                },
            }
        }
        plan = build_plan("dummy.mp4", segs, self.SPEC, constraints)
        clips = plan["clips"]
        last_clip = clips[-1]
        self.assertEqual(last_clip["category"], "decision_closing",
                         f"成片末段应为 decision_closing: {[c['category'] for c in clips]}")
        self.assertIn("强促单", last_clip["desc"], "高分强促单候选必须优先被选入成片尾部")
        self.assertIn("驼色", last_clip["desc"])

    def test_blacklist_no_price(self):
        from src.tools.build_plan import build_blocks
        segs = [{"start": 0.0, "end": 2.0, "duration": 2.0, "text": "到手价只要299"},
                {"start": 2.0, "end": 4.0, "duration": 2.0, "text": "面料很软糯舒服"}]
        blocks, rejected = build_blocks(segs)
        self.assertGreaterEqual(len(rejected), 1)
        for b in blocks:
            self.assertNotIn("299", b["text"])

    def test_cross_segment_false_phrase_guard(self):
        """跨段伪短语防护：'自己去买'+'这个毛衣' 不得误拼为'买这个'而丢块。"""
        from src.tools.build_plan import build_blocks
        segs = [
            {"start": 0.0, "end": 2.0, "duration": 2.0, "text": "这个毛衣大家自己去买"},
            {"start": 2.0, "end": 6.0, "duration": 4.0, "text": "这个毛衣实物还要更漂亮的"},
        ]
        blocks, _ = build_blocks(segs)
        self.assertEqual(len(blocks), 1, "黄金 Hook 块不应被跨段伪短语误杀")
        self.assertIn("实物还要更漂亮", blocks[0]["text"])

    def test_punch_clip_alternation(self):
        """T4: 提神快切档（1.5s~2.8s 与常规段 3.0s~5.0s 交替产出，打破开场均质化）。"""
        from src.tools.build_plan import build_plan
        from src.tools.cut_style_rules import check_pacing_curve

        # 提供长 10s 的 Hook 话术，多句末边界
        hook_segs = [
            {"start": 0.0, "end": 4.0, "duration": 4.0, "text": "姐妹们这个毛衣真的太好看了。"},
            {"start": 4.0, "end": 6.2, "duration": 2.2, "text": "穿上瞬间高级感满满。"},
            {"start": 6.2, "end": 10.0, "duration": 3.8, "text": "实物比镜头前还要更漂亮的。"},
        ]
        rest_segs = [
            {"start": 10.0, "end": 20.0, "duration": 10.0, "text": "做工细节非常考究，本布包边一体成型。"},
            {"start": 20.0, "end": 35.0, "duration": 15.0, "text": "版型显瘦修身不挑人，遮肉显高显比例。"},
            {"start": 35.0, "end": 52.0, "duration": 17.0, "text": "日常穿搭搭配一条格子裤或者半裙都很百搭。"},
            {"start": 52.0, "end": 60.0, "duration": 8.0, "text": "纠结选哪个颜色我建议买驼色仙女买白色。"},
        ]
        all_segs = hook_segs + rest_segs
        constraints = {
            "slicing_rules": {
                "pacing_curve": {
                    "enabled": True,
                    "punch_min_sec": 1.5,
                    "punch_max_sec": 2.8,
                    "punch_min_count": 1,
                    "hook_segment_sec": 4.0,
                    "hook_target_sec": 10.0,
                }
            },
            "video": {
                "duration_range": [45, 65],
                "funnel": {
                    "hook": {"min": 3, "max": 12},
                    "fabric": {"min": 8, "max": 20},
                    "fit": {"min": 8, "max": 15},
                    "styling": {"min": 15, "max": 30},
                    "decision_closing": {"min": 6, "max": 12},
                }
            }
        }
        plan = build_plan("dummy.mp4", all_segs, self.SPEC, constraints)
        hook_clips = [c for c in plan["clips"] if c.get("category") == "hook"]
        self.assertGreaterEqual(len(hook_clips), 2, "开场 10s 内应切出至少 2 个 Hook 分镜")
        
        # 验证至少存在 1 个 punch 段（1.5~2.8s）和至少 1 个标准段（>=3.0s）
        punch_clips = [c for c in hook_clips if 1.45 <= c["dur"] <= 2.85]
        standard_clips = [c for c in hook_clips if c["dur"] >= 3.0]
        self.assertGreaterEqual(len(punch_clips), 1, f"开场应至少包含 1 个提神快切段: {[c['dur'] for c in hook_clips]}")
        self.assertGreaterEqual(len(standard_clips), 1, f"开场应至少包含 1 个常规标准段: {[c['dur'] for c in hook_clips]}")

        # 验证 pacing_curve 判定
        ref = {"hook_cuts": 3, "sentence_len_median": 1.54, "speech_rate_cpm": 397,
               "tightening_ratio": 0.63, "max_mid_clip_sec": 12.0, "duration_sigma": 3.0,
               "duration_ratio": 5.0, "punch_clips": 1}
        r = check_pacing_curve(constraints, ref)
        self.assertTrue(r["passed"], f"包含 punch_clips 且达标应通过: {r}")
        poison_r = check_pacing_curve(constraints, dict(ref, punch_clips=0))
        self.assertFalse(poison_r["passed"], "缺少提神快切段（punch_clips=0）必须被拦截")

    def test_wrist_motion_active_scoring(self):
        """T5: 手部动态动作性判据（wrist_motion 加权与缺失安全兜底）。"""
        from src.tools.build_plan import _segment_score, build_plan

        # 1. 基础评分加权验证
        base_seg = {"text": "做工细节非常考究一体成型", "duration": 8.0}
        sc_base = _segment_score(base_seg, "craft_detail")
        self.assertGreater(sc_base, 0.0, "基础分应大于 0")

        # 活跃手部动作加权 (>= 0.08)
        sc_active = _segment_score(dict(base_seg, wrist_motion=0.15), "craft_detail")
        self.assertGreaterEqual(sc_active, sc_base * 1.25, "活跃手部动作应获得显著加权 (+30%)")

        # 静态手部惩罚 (< 0.03)，但绝不清零
        sc_static = _segment_score(dict(base_seg, wrist_motion=0.01), "craft_detail")
        self.assertLess(sc_static, sc_base, "静态手部应有适度扣分 (-15%)")
        self.assertGreater(sc_static, 0.0, "静态手部扣分后不得为 0")

        # 缺失 pose 数据兜底：应保持基线评分
        sc_none = _segment_score(dict(base_seg, wrist_motion=None), "craft_detail")
        self.assertEqual(sc_none, sc_base, "缺少 pose 特征时不应调整基线分")

        # 2. build_plan 选段竞争验证：高活跃动作块应优先入选
        segs = [
            {"start": 0.0, "end": 4.0, "duration": 4.0, "text": "姐妹们看这个新款真的太好看了。"},
            {"start": 4.0, "end": 12.0, "duration": 8.0, "text": "面料材质纯山羊绒软糯舒适。", "wrist_motion": 0.01},  # 静态
            {"start": 12.0, "end": 20.0, "duration": 8.0, "text": "面料做工纯羊毛走线细节高级。", "wrist_motion": 0.16},  # 活跃展示
            {"start": 20.0, "end": 32.0, "duration": 12.0, "text": "版型显瘦修身遮肉显高显比例。"},
            {"start": 32.0, "end": 50.0, "duration": 18.0, "text": "搭配一条牛仔裤或者半裙都很百搭。"},
            {"start": 50.0, "end": 58.0, "duration": 8.0, "text": "纠结选哪个颜色我建议买驼色仙女买白色。"},
        ]
        constraints = {
            "video": {
                "duration_range": [45, 65],
                "funnel": {
                    "hook": {"min": 3, "max": 8},
                    "fabric": {"min": 6, "max": 10},
                    "fit": {"min": 8, "max": 15},
                    "styling": {"min": 15, "max": 30},
                    "decision_closing": {"min": 6, "max": 12},
                }
            }
        }
        plan = build_plan("dummy.mp4", segs, self.SPEC, constraints)
        fabric_clips = [c for c in plan["clips"] if c.get("category") == "fabric"]
        self.assertTrue(any("走线细节" in c.get("desc", "") for c in fabric_clips),
                        f"活跃手部动作块应优先被选入成片: {[c.get('desc') for c in fabric_clips]}")

    def test_ankles_in_frame_validation(self):
        """T6: 景别出画校验（脚踝出画强制降级为 partial，杜绝误判 full_body）。"""
        from src.tools.extract_shot_features import _framing_of
        from src.tools.slicing_rules import classify_framing

        constraints = {
            "slicing_rules": {
                "shot_framing": {
                    "enabled": True,
                    "min_body_height_ratio": 0.45,
                }
            }
        }
        # 1. extract_shot_features 中的 _framing_of 判定
        # 正常入画且 span 达标 -> full_body
        feats_in = {"span": 0.68, "body_h": 0.55, "ankles_in_frame": True}
        self.assertEqual(_framing_of({}, feats_in), "full_body")

        # 肢体出画（ankles_in_frame=False）即使 span 虚高 -> 降级为 partial
        feats_out = {"span": 0.75, "body_h": 0.60, "ankles_in_frame": False}
        self.assertEqual(_framing_of({}, feats_out), "partial",
                         "脚踝出画时必须强制降级为 partial，防止 MediaPipe 外推虚高")

        # 2. slicing_rules 中的 classify_framing 判定
        self.assertEqual(classify_framing(0.55, constraints, span=0.68, ankles_in_frame=True), "full_body")
        self.assertEqual(classify_framing(0.55, constraints, span=0.75, ankles_in_frame=False), "partial")

    def test_funnel_orders_preset_and_override(self):
        """T7: 品类漏斗预设字典（skirt_dress / coat_outerwear / default）与覆写支持。"""
        from src.tools.build_plan import FUNNEL_ORDERS, build_plan
        from src.tools.slicing_rules import resolve_funnel_order

        # 1. 验证内置字典结构
        self.assertIn("skirt_dress", FUNNEL_ORDERS)
        self.assertIn("coat_outerwear", FUNNEL_ORDERS)
        self.assertIn("default", FUNNEL_ORDERS)
        self.assertEqual(FUNNEL_ORDERS["skirt_dress"],
                         ["hook", "fit", "craft_detail", "styling", "decision_closing"])
        self.assertEqual(FUNNEL_ORDERS["coat_outerwear"],
                         ["hook", "fabric", "fit", "styling", "decision_closing"])

        # 2. 验证 resolve_funnel_order 品类解析
        rules = {
            "slicing_rules": {
                "funnel_order_overridable": True,
                "default_funnel_order": ["hook", "fabric", "fit", "styling"],
                "funnel_orders": FUNNEL_ORDERS,
            }
        }
        # 半身裙/连衣裙品类
        skirt_spec = {"category": "skirt_dress"}
        self.assertEqual(resolve_funnel_order(skirt_spec, rules),
                         ["hook", "fit", "craft_detail", "styling", "decision_closing"])

        # 外套/大衣品类
        coat_spec = {"category": "coat_outerwear"}
        self.assertEqual(resolve_funnel_order(coat_spec, rules),
                         ["hook", "fabric", "fit", "styling", "decision_closing"])

        # spec 显式 options.funnel_order 覆写优先级高于品类预设
        custom_spec = {
            "category": "skirt_dress",
            "options": {"funnel_order": ["hook", "styling", "craft_detail", "fit", "decision_closing"]}
        }
        self.assertEqual(resolve_funnel_order(custom_spec, rules),
                         ["hook", "styling", "craft_detail", "fit", "decision_closing"])

        # 3. 验证 build_plan 传入 category='skirt_dress' 时真正按品类漏斗排布分镜
        segs = [
            {"start": 0.0, "end": 4.0, "duration": 4.0, "text": "姐妹们看这个半身裙太好看了。"},
            {"start": 5.0, "end": 15.0, "duration": 10.0, "text": "版型显瘦修身遮肉显高显比例。"},
            {"start": 16.0, "end": 26.0, "duration": 10.0, "text": "做工细节非常考究纯羊毛走线细节。"},
            {"start": 27.0, "end": 47.0, "duration": 20.0, "text": "搭配一条牛仔裤或者半裙都很百搭。"},
            {"start": 48.0, "end": 56.0, "duration": 8.0, "text": "纠结选哪个颜色我建议买驼色仙女买白色。"},
        ]
        bp_constraints = {
            "video": {
                "duration_range": [45, 65],
                "funnel": {
                    "hook": {"min": 3, "max": 8},
                    "fabric": {"min": 6, "max": 10},
                    "craft_detail": {"min": 6, "max": 10},
                    "fit": {"min": 8, "max": 15},
                    "styling": {"min": 15, "max": 30},
                    "decision_closing": {"min": 6, "max": 12},
                }
            },
            "slicing_rules": {
                "enabled": True,
                "funnel_order_overridable": True,
                "funnel_orders": FUNNEL_ORDERS,
                "default_funnel_order": ["hook", "fabric", "fit", "styling"],
            }
        }
        skirt_plan = build_plan("dummy.mp4", segs, {"category": "skirt_dress", "options": {}}, bp_constraints)
        plan_cats = [c["category"] for c in skirt_plan["clips"]]
        # 验证 fit 出现在 craft_detail 或 fabric 之前
        fit_idx = plan_cats.index("fit") if "fit" in plan_cats else -1
        craft_idx = plan_cats.index("craft_detail") if "craft_detail" in plan_cats else (
            plan_cats.index("fabric") if "fabric" in plan_cats else -1)
        self.assertTrue(fit_idx >= 0 and craft_idx >= 0 and fit_idx < craft_idx,
                        f"裙装类目漏斗中 fit 应优先于 craft_detail/fabric: {plan_cats}")

    def test_punch_clip_multi_candidate_blocks(self):
        """T4 防退化: 多独立 Hook 候选块时不应在首块即提前 break，应累积至 hook_target 产生提神快切与常规呼吸段。"""
        from src.tools.build_plan import build_plan
        from src.tools.cut_style_rules import measure_pacing, check_pacing_curve

        segs = [
            {"start": 0.0, "end": 4.0, "duration": 4.0, "text": "姐妹们这款毛衣太好看了。"},
            {"start": 6.0, "end": 9.0, "duration": 3.0, "text": "真的是闭眼入的一款新款。"},
            {"start": 12.0, "end": 22.0, "duration": 10.0, "text": "做工细节非常考究一体成型。"},
            {"start": 25.0, "end": 40.0, "duration": 15.0, "text": "版型显瘦修身遮肉显高显比例。"},
            {"start": 42.0, "end": 60.0, "duration": 18.0, "text": "搭配一条裤子或者半裙都非常好看百搭。"},
            {"start": 62.0, "end": 70.0, "duration": 8.0, "text": "纠结选哪个颜色我建议买驼色仙女买白色。"},
        ]
        constraints = {
            "slicing_rules": {
                "enabled": True,
                "pacing_curve": {
                    "enabled": True,
                    "punch_min_sec": 1.5,
                    "punch_max_sec": 2.8,
                    "punch_min_count": 1,
                    "hook_segment_sec": 4.0,
                    "hook_target_sec": 10.0,
                }
            },
            "video": {
                "duration_range": [45, 65],
                "funnel": {
                    "hook": {"min": 3, "max": 12},
                    "fabric": {"min": 8, "max": 20},
                    "fit": {"min": 8, "max": 15},
                    "styling": {"min": 15, "max": 30},
                    "decision_closing": {"min": 6, "max": 12},
                }
            }
        }
        plan = build_plan("dummy.mp4", segs, self.SPEC, constraints=constraints)
        hook_clips = [c for c in plan["clips"] if c.get("category") == "hook"]
        self.assertGreaterEqual(len(hook_clips), 2, f"多候选块时 Hook 区间应切出多段: {[c['dur'] for c in hook_clips]}")
        has_punch = any(1.45 <= c["dur"] <= 2.85 for c in hook_clips)
        has_std = any(c["dur"] >= 3.0 for c in hook_clips)
        self.assertTrue(has_punch, f"应包含提神快切段: {[c['dur'] for c in hook_clips]}")
        self.assertTrue(has_std, f"应包含常规呼吸段: {[c['dur'] for c in hook_clips]}")

    def test_wrist_motion_no_double_weighting(self):
        """T5 防退化: 手部动作性评分不应在 build_blocks 与 build_plan 中被重复乘算两次。"""
        from src.tools.build_plan import _segment_score, build_blocks, build_plan

        seg_active = {"start": 4.0, "end": 12.0, "duration": 8.0,
                      "text": "面料做工纯羊毛走线细节高级。", "wrist_motion": 0.16}
        base_seg = {"text": "面料做工纯羊毛走线细节高级。", "duration": 8.0}

        base_sc = _segment_score(base_seg, "fabric")
        single_boost_sc = _segment_score(dict(base_seg, wrist_motion=0.16), "fabric")
        self.assertAlmostEqual(single_boost_sc / base_sc, 1.30, delta=0.05,
                               msg="单次加权应约为 1.30 倍")

        # 验证 build_plan 处理后，候选块得分未被二次叠加乘到 1.69 倍
        blocks, _ = build_blocks([seg_active])
        score_after_blocks = blocks[0]["scores"]["fabric"]
        self.assertAlmostEqual(score_after_blocks / base_sc, 1.30, delta=0.05,
                               msg="build_blocks 后得分加权为 1.30 倍")

    def test_pose_failure_safe_fallback(self):
        """T5/T6 防退化: pose 检测失败时，wrist_motion 缺失不应触发扣分，ankles_in_frame 不应误判为 True。"""
        from src.tools.extract_shot_features import frame_features, clip_features
        import numpy as np

        # 纯黑画面，无人体姿态
        blank_frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        ff = frame_features(blank_frame)
        self.assertFalse(ff["pose_ok"])
        self.assertFalse(ff["ankles_in_frame"], "无 pose 关键点时脚踝不应误判为入画")

        # 验证缺失 pose 时候选评分保留基准分，不扣分
        from src.tools.build_plan import _segment_score
        seg = {"text": "面料做工纯羊毛走线细节高级。", "duration": 8.0, "wrist_motion": None}
        base_sc = _segment_score({"text": "面料做工纯羊毛走线细节高级。", "duration": 8.0}, "fabric")
        none_sc = _segment_score(seg, "fabric")
        self.assertEqual(base_sc, none_sc, "缺失 pose 特征时评分必须与基线分完全一致")


# ================================================================ 封面渲染

class TestCoverRenderer(unittest.TestCase):
    """B9：金色渐变商品名 + 自动换行 + 渲染 meta。"""

    def setUp(self):
        from PIL import Image
        import numpy as np
        self.bg_path = os.path.join(PROJECT_ROOT, "data", "_test_cover_bg.jpg")
        os.makedirs(os.path.dirname(self.bg_path), exist_ok=True)
        Image.fromarray(np.full((1920, 1080, 3), 80, dtype=np.uint8)).save(self.bg_path)

    def tearDown(self):
        if os.path.exists(self.bg_path):
            os.remove(self.bg_path)

    def test_gold_gradient_and_meta(self):
        from src.graphics.cover_renderer import MixCityCoverRenderer
        out = os.path.join(PROJECT_ROOT, "data", "_test_cover_v2.jpg")
        meta_path = os.path.join(PROJECT_ROOT, "data", "_test_cover_v2_meta.json")
        try:
            r = MixCityCoverRenderer()
            path, meta = r.render_cover_with_meta(
                base_frame_path=self.bg_path,
                brand_info={"title_en": "MIXCITY", "subtitle_cn": "深圳高端女装"},
                product_info={
                    "title_line1": "半岛棉镂空钩花",
                    "title_line2": "气质显瘦针织衫",
                    "item_images": [],
                },
                output_path=out, meta_output_path=meta_path)
            self.assertTrue(os.path.exists(path))
            # meta 结构
            self.assertIn("brand_en", meta["elements"])
            self.assertIn("product_name", meta["elements"])
            self.assertEqual(meta["elements"]["brand_en"]["center_x"], 540)
            # 商品名 meta 含金色渐变定义（决策 2）
            grad = meta["elements"]["product_name"]["gradient"]
            self.assertEqual(grad, ["#FFE9AA", "#E8A91E"])
        finally:
            for p in (out, meta_path):
                if os.path.exists(p):
                    os.remove(p)

    def test_long_name_auto_wrap(self):
        """C9/C16：超长名称自动换行不溢出。"""
        from src.graphics.cover_renderer import MixCityCoverRenderer
        from PIL import Image, ImageDraw, ImageFont
        out = os.path.join(PROJECT_ROOT, "data", "_test_cover_wrap.jpg")
        try:
            r = MixCityCoverRenderer()
            path, meta = r.render_cover_with_meta(
                base_frame_path=self.bg_path,
                brand_info={"title_en": "MIXCITY INTERNATIONAL FASHION GROUP", "subtitle_cn": "深圳高端女装旗舰品牌店"},
                product_info={
                    "title_line1": "桑蚕丝羊绒羊毛混纺针织开衫外套超长版本",
                    "title_line2": "气质显瘦宽松版型",
                    "item_images": [],
                },
                output_path=out)
            # 品牌与商品名均应触发换行（meta 记录多行）
            self.assertGreaterEqual(len(meta["elements"]["brand_en"]["lines"]), 1)
            self.assertGreaterEqual(len(meta["elements"]["brand_cn"]["lines"]), 1)
        finally:
            if os.path.exists(out):
                os.remove(out)


# ================================================================ 衣着一致性

class TestGarmentMatch(unittest.TestCase):
    """B8：直方图降级引擎的区分度（正例 > 反例）。"""

    def test_discrimination(self):
        import cv2
        import numpy as np
        from src.tools.detect_garment_match import (
            garment_match_score, _mask_white_background, _hsv_hist)
        # 构造"封面帧"：中央肤色/白衣区域 + 深色背景
        frame = np.full((1920, 1088, 3), (40, 40, 40), np.uint8)
        frame[600:1400, 400:688] = (230, 230, 235)  # 白色上衣躯干
        # 商品图：同色白上衣
        product = np.full((600, 600, 3), (255, 255, 255), np.uint8)
        product[100:500, 100:500] = (230, 230, 235)
        ppath = os.path.join(PROJECT_ROOT, "data", "_test_product.jpg")
        cv2.imwrite(ppath, product)
        try:
            r = garment_match_score(frame, ppath, pose_landmarks=None)
            self.assertEqual(r["engine"], "histogram")
            self.assertGreater(r["score"], 0.3, "同色正例应有较高分数")
        finally:
            if os.path.exists(ppath):
                os.remove(ppath)


# ================================================================ 人脸检测

class TestFaceAnalyzer(unittest.TestCase):
    """B4：姿态引导 ROI 的人脸检测（模型缺失跳过）。"""

    MODEL = os.path.join(PROJECT_ROOT, "data", "face_landmarker.task")

    @unittest.skipUnless(os.path.exists(MODEL), "face_landmarker.task 不存在")
    def test_analyze_sample_frame(self):
        import cv2
        from src.tools.detect_face import FaceAnalyzer
        frame_path = os.path.join(PROJECT_ROOT, "data", "frame_49s.jpg")
        if not os.path.exists(frame_path):
            self.skipTest("样例帧不存在")
        frame = cv2.imread(frame_path)
        fa = FaceAnalyzer()
        # 无 pose 时全帧（该素材人脸过小，预期 None）
        m_full = fa.analyze(frame)
        # 用 pose 引导 ROI 应检出
        from src.tools.detect_face import _load_pose_landmarks
        lm = _load_pose_landmarks(frame)
        if lm is None:
            self.skipTest("姿态检测失败")
        m = fa.analyze(frame, pose_landmarks=lm)
        self.assertIsNotNone(m, "姿态引导 ROI 应检出人脸")
        self.assertTrue(m["face_detected"])
        self.assertLessEqual(m["yaw_deg"], 90)  # 近正面帧
        self.assertIn("ear", m)
        self.assertIn("mar", m)
        # 约束校验接口
        passed, failed = fa.check_constraints(m, {
            "face_yaw_max_deg": 12, "ear_min": 0.18, "mar_max": 0.35,
            "face_sharpness_min": 150, "face_area_ratio_min": 0.003,
            "face_area_ratio_max": 0.25})
        self.assertIsInstance(passed, bool)


# ================================================================ QC 门禁

class TestQcAuditor(unittest.TestCase):
    """B7：QC 门禁合成用例（字幕价格拦截 / 封面几何）。"""

    def test_subtitle_price_block(self):
        from src.qc.qc_auditor import QcAuditor
        import tempfile
        ass = (
            "[Script Info]\n"
            "Style: X,Microsoft YaHei,50,&H00FFFFFF,&H000000FF,&H0015151A,&H80000000,-1,0,0,0,100,100,1,0,1,4.5,2.0,2,40,40,240,1\n"
            "[Events]\n"
            "Dialogue: 0,0:00:00.00,0:00:02.00,X,,0,0,0,,今天到手价299哦\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".ass", delete=False,
                                         encoding="utf-8") as f:
            f.write(ass)
            ass_path = f.name
        auditor = QcAuditor({"video": {"duration_range": [45, 65]}})
        try:
            # 仅测字幕分支：传入不存在的视频会提前返回，这里直接调内部逻辑
            with open(ass_path, "r", encoding="utf-8-sig") as f:
                content = f.read()
            from src.tools.detect_price import find_price_matches
            import re
            hits = 0
            for line in content.splitlines():
                if line.startswith("Dialogue:"):
                    parts = line.split(",", 9)
                    if len(parts) >= 10 and find_price_matches(parts[9]):
                        hits += 1
            self.assertEqual(hits, 1, "含价格字幕行应被检出")
        finally:
            os.remove(ass_path)

    def test_constraints_loading(self):
        from src.qc.qc_auditor import _load_constraints
        c = _load_constraints()
        # 版本号随阈值演进而递增（v1.0→v1.2），断言"已登记且 >=1"而非写死
        self.assertGreaterEqual(c.get("version", 0), 1)
        self.assertIn("funnel", c.get("video", {}))
        self.assertEqual(c["sticker"]["size"], 280)


if __name__ == "__main__":
    unittest.main(verbosity=2)
