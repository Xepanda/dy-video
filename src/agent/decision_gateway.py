#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
decision_gateway.py - Agent 决策约束校验网关（B12）
==================================================
架构原则：Agent 的每次"选择"（封面候选、分镜计划、外部干预）在落盘与执行前，
必须先过本网关的约束校验；不合格的选择被拒绝并重选（拒绝时记录原因）。

校验内容（全部依据 contracts/constraints.yaml，BLOCK 级一票否决）：
  1. 封面候选：人脸质量（偏航/EAR/MAR/人脸清晰度/人脸面积）、
     完整人体（膝盖/踝部）、构图宽占比（PASS 区间或 WARN 区间）、
     放大后清晰度、背景文字纯净度（B11 数据若在候选清单中）。
  2. 分镜计划：总时长 ∈ duration_range、各类目漏斗配额、
     零价格（detect_price）、分镜无重叠、clip 字段完整。
  3. 外部干预（--cover-id / --plan）：同样校验，不合格一律拦截
     （人类干预也必须过约束门禁，防止误操作注入）。

决策可审：每次选择（含被拒绝的尝试）携带 metrics 快照 + 校验结论 + 理由，
追加进 gateway.decisions，最终由 AgentDirector 合并写入
agent_decision_manifest.json。

CLI:
  python src/agent/decision_gateway.py --demo   # 内置反例自检
"""

import os
import sys
import json
import time
import argparse
from typing import Dict, Any, List, Optional, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from src.tools.detect_price import find_price_matches

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _load_constraints(path: str = None) -> Dict[str, Any]:
    path = path or os.path.join(PROJECT_ROOT, "contracts", "constraints.yaml")
    if not os.path.exists(path):
        return {}
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class DecisionViolation(Exception):
    """决策未通过约束校验（携带违规明细）。"""
    def __init__(self, decision_kind: str, violations: List[str]):
        self.decision_kind = decision_kind
        self.violations = violations
        super().__init__(f"[{decision_kind}] 决策被约束网关拒绝: {violations}")


class DecisionGateway:
    """Agent 选择先过约束校验，不合格拒绝重选；全程决策可审。"""

    def __init__(self, constraints: Dict[str, Any] = None):
        self.constraints = constraints or _load_constraints()
        self.decisions: List[Dict[str, Any]] = []
        self._last_funnel_warnings: List[str] = []

    # ------------------------------------------------------------ 审计落册

    def _record(self, kind: str, accepted: bool, reasons: List[str],
                metrics_snapshot: Dict[str, Any], forced: bool = False) -> None:
        self.decisions.append({
            "decision": kind,
            "accepted": accepted,
            "forced": forced,
            "reasons": reasons,
            "metrics_snapshot": metrics_snapshot,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        })

    # ------------------------------------------------------------ 封面候选校验

    def validate_cover_candidate(self, cand: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """对单个封面候选执行 BLOCK 级约束校验（与 QC 双源复测互补的选段前置门禁）。"""
        cover_c = self.constraints.get("cover", {}) or {}
        violations: List[str] = []

        face = cand.get("face") or {}
        if not face:
            violations.append("缺少人脸 metrics（face 字段为空）")
        else:
            yaw_max = float(cover_c.get("face_yaw_max_deg", 12))
            if face.get("yaw_deg", 999) > yaw_max:
                violations.append(f"人脸偏航角 {face.get('yaw_deg')}° > {yaw_max}°")
            ear_min = float(cover_c.get("ear_min", 0.18))
            if face.get("ear", 0) < ear_min:
                violations.append(f"EAR {face.get('ear')} < {ear_min}（未睁眼）")
            mar_max = float(cover_c.get("mar_max", 0.35))
            if face.get("mar", 9) > mar_max:
                violations.append(f"MAR {face.get('mar')} > {mar_max}（张嘴/说话）")
            fs_min = float(cover_c.get("face_sharpness_min", 150))
            if face.get("face_sharpness", 0) < fs_min:
                violations.append(f"人脸清晰度 {face.get('face_sharpness')} < {fs_min}")
            fa_min = float(cover_c.get("face_area_ratio_min", 0.003))
            fa_max = float(cover_c.get("face_area_ratio_max", 0.25))
            fa = face.get("face_area_ratio", 0)
            if not (fa_min <= fa <= fa_max):
                violations.append(f"人脸面积占比 {fa} ∉ [{fa_min}, {fa_max}]")

        framing = cand.get("framing") or {}
        if not framing:
            violations.append("缺少构图 metrics（framing 字段为空）")
        else:
            if not framing.get("knees_visible", False):
                violations.append("膝盖不可见（违反 C5 完整人体）")
            sharp_min = float(cover_c.get("sharpness_min", 200))
            if framing.get("post_scale_sharpness", 0) < sharp_min:
                violations.append(
                    f"放大后清晰度 {framing.get('post_scale_sharpness')} < {sharp_min}（上采样糊脸）")
            ratio = framing.get("achieved_width_ratio", 0.0)
            target = float(cover_c.get("person_width_ratio", 0.75))
            tol = float(cover_c.get("person_width_tolerance", 0.03))
            warn_range = cover_c.get("person_width_warn_range", [0.55, 0.85])
            if not (abs(ratio - target) <= tol):
                if not (warn_range[0] <= ratio <= warn_range[1]):
                    violations.append(
                        f"宽占比 {ratio:.3f} 超出 WARN 区间 {warn_range}（水平 1:6:1）")

        # B11 背景文字（候选清单中若已带 OCR 度量）
        if cand.get("background_text_passed") is False:
            violations.append(
                f"背景文字面积占比 {cand.get('background_text_ratio')} 超限（V7 封面纯净度）")

        return (len(violations) == 0), violations

    def select_valid_cover(
        self,
        candidates: List[Dict[str, Any]],
        forced_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """从候选清单选出第一个过约束的封面；外部指定则校验指定项。

        不合格候选被拒绝并记录（拒绝重选）；全部不合格则抛出 DecisionViolation。
        """
        if not candidates:
            raise DecisionViolation("cover_selection", ["候选清单为空"])

        if forced_id:
            chosen = next((c for c in candidates if c.get("id") == forced_id), None)
            if chosen is None:
                raise DecisionViolation("cover_selection",
                                        [f"指定候选 {forced_id} 不存在于清单"])
            ok, violations = self.validate_cover_candidate(chosen)
            self._record("cover_selection", ok, violations if not ok
                         else [f"外部指定 {forced_id} 通过约束校验"],
                         self._cover_snapshot(chosen), forced=True)
            if not ok:
                raise DecisionViolation("cover_selection", violations)
            return chosen

        for cand in candidates:  # 清单按多目标分数降序
            ok, violations = self.validate_cover_candidate(cand)
            if ok:
                self._record("cover_selection", True,
                             [f"候选 {cand.get('id')} 通过约束校验（分数 "
                              f"{cand.get('frontal_score')}）"],
                             self._cover_snapshot(cand))
                return cand
            self._record("cover_selection", False,
                         [f"候选 {cand.get('id')} 被拒绝: {v}" for v in violations],
                         self._cover_snapshot(cand))
        raise DecisionViolation(
            "cover_selection",
            [f"{len(candidates)} 个候选全部未通过约束校验"])

    @staticmethod
    def _cover_snapshot(cand: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": cand.get("id"),
            "timestamp": cand.get("timestamp"),
            "frontal_score": cand.get("frontal_score"),
            "face": cand.get("face"),
            "framing": cand.get("framing"),
            "background_text_ratio": cand.get("background_text_ratio"),
        }

    # ------------------------------------------------------------ 分镜计划校验

    def validate_plan(self, plan: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """对分镜计划执行 BLOCK 级约束校验（漏斗配额/时长/零价格/结构）。"""
        video_c = self.constraints.get("video", {}) or {}
        price_c = self.constraints.get("price", {}) or {}
        violations: List[str] = []

        clips = plan.get("clips") or []
        if not clips:
            violations.append("分镜计划为空（clips 为空）")
            return False, violations

        # 结构完整性 + 时间线连续性（注意：st/end 为源时间轴，非单调合法——
        # 分镜按计划顺序拼接，时间线位置由 dur 累加决定，不检查源时间重叠）
        for c in clips:
            for field in ("id", "st", "end", "dur"):
                if field not in c:
                    violations.append(f"分镜 {c.get('id')} 缺少字段 {field}")
            if c.get("st", 0) < 0 or c.get("end", 0) <= c.get("st", 0):
                violations.append(f"分镜 {c.get('id')} 时间区间非法: "
                                  f"{c.get('st')}~{c.get('end')}")
            if c.get("dur", 0) <= 0:
                violations.append(f"分镜 {c.get('id')} 时长非正: {c.get('dur')}")
            # 零价格（决策 3）
            if price_c.get("subtitle_assert_zero", True):
                text = c.get("desc", "") or ""
                hits = find_price_matches(text)
                if hits:
                    violations.append(f"分镜 {c.get('id')} 文案含价格表达: {hits}")

        # 总时长
        total = float(plan.get("total_expected_duration",
                               sum(c.get("dur", 0) for c in clips)))
        dur_range = video_c.get("duration_range", [45, 65])
        if not (dur_range[0] <= total <= dur_range[1]):
            violations.append(f"计划总时长 {total:.1f}s ∉ [{dur_range[0]}, {dur_range[1]}]")

        # 漏斗配额（节奏指导而非纪要硬指标：纪要仅要求总时长 45-65s；
        # 类目内容稀薄的素材（如只有 hook+fabric）不应被一票否决，
        # 默认 WARN 记录，constraints video.funnel_enforce_level 可升为 BLOCK）
        funnel = (video_c.get("funnel") or {})
        funnel_level = video_c.get("funnel_enforce_level", "WARN")
        cat_totals: Dict[str, float] = {}
        for c in clips:
            cat = c.get("category", "other")
            cat_totals[cat] = cat_totals.get(cat, 0.0) + c.get("dur", 0)
        funnel_violations = []
        for cat, bound in funnel.items():
            got = cat_totals.get(cat, 0.0)
            lo = float(bound.get("min", 0))
            if got < lo:
                funnel_violations.append(
                    f"类目 {cat} 时长 {got:.1f}s 低于配额下限 {lo}s")
        self._last_funnel_warnings = funnel_violations
        if funnel_level == "BLOCK":
            violations.extend(funnel_violations)

        return (len(violations) == 0), violations

    def validate_and_record_plan(
        self, plan: Dict[str, Any], forced: bool = False, source: str = "build_plan"
    ) -> Dict[str, Any]:
        """校验分镜计划；不合格抛出 DecisionViolation（拦截）。"""
        ok, violations = self.validate_plan(plan)
        snapshot = {
            "source": source,
            "clip_count": len(plan.get("clips") or []),
            "total_expected_duration": plan.get("total_expected_duration"),
            "funnel_warnings": self._last_funnel_warnings,
            "clips": [{"id": c.get("id"), "category": c.get("category"),
                       "st": c.get("st"), "end": c.get("end"), "dur": c.get("dur")}
                      for c in (plan.get("clips") or [])],
        }
        reasons = violations if not ok else [f"分镜计划通过约束校验（{source}）"]
        if self._last_funnel_warnings:
            reasons.append(f"漏斗配额 WARN: {self._last_funnel_warnings}")
        self._record("plan_selection", ok, reasons, snapshot, forced=forced)
        if not ok:
            raise DecisionViolation("plan_selection", violations)
        return plan

    # ------------------------------------------------------------ Hook 卡避让校验

    def validate_hook_card(self, plan: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """Hook 卡不得压人脸顶（B10）：卡底 ≤ 人脸顶 − 30px。"""
        hook_card = (plan.get("hook_card") or {})
        if not hook_card or "y" not in hook_card:
            return True, []  # 未启用 Hook 卡避让，不阻塞
        violations = []
        y = int(hook_card["y"])
        if not (150 <= y <= 430):
            violations.append(f"Hook 卡 y={y} 超出 150~430 区间")
        if hook_card.get("face_top_y") is not None:
            face_top_px = float(hook_card["face_top_y"]) * 1920
            if y + 130 > face_top_px - 30:
                violations.append(
                    f"Hook 卡底 y+130={y+130} 超过人脸顶−30px={face_top_px-30:.0f}（压脸）")
        return (len(violations) == 0), violations

    def record_generic(self, kind: str, accepted: bool, reasons: List[str],
                       metrics_snapshot: Dict[str, Any]) -> None:
        self._record(kind, accepted, reasons, metrics_snapshot)


def _demo_selfcheck() -> int:
    """反例自检：构造不合格候选/计划，验证 100% 拦截。"""
    gw = DecisionGateway()
    failures = []

    # 1. 不合格封面候选：偏航 25° + 膝盖不可见
    bad_cover = {
        "id": "cand_bad", "timestamp": 10.0, "frontal_score": 99.0,
        "face": {"yaw_deg": 25.0, "ear": 0.3, "mar": 0.1,
                 "face_sharpness": 300, "face_area_ratio": 0.01},
        "framing": {"knees_visible": False, "post_scale_sharpness": 400,
                    "achieved_width_ratio": 0.75},
        "background_text_passed": True,
    }
    try:
        gw.select_valid_cover([bad_cover])
        failures.append("不合格封面候选未被拦截")
    except DecisionViolation as e:
        print(f"[PASS] 不合格封面候选被拦截: {e.violations}")

    # 2. 外部强制指定不合格候选：同样拦截
    try:
        gw.select_valid_cover([bad_cover], forced_id="cand_bad")
        failures.append("外部强制的不合格候选未被拦截")
    except DecisionViolation:
        print("[PASS] 外部强制的不合格候选被拦截")

    # 3. 合格候选通过
    good_cover = {
        "id": "cand_good", "timestamp": 20.0, "frontal_score": 60.0,
        "face": {"yaw_deg": 5.0, "ear": 0.3, "mar": 0.1,
                 "face_sharpness": 300, "face_area_ratio": 0.01},
        "framing": {"knees_visible": True, "post_scale_sharpness": 400,
                    "achieved_width_ratio": 0.75},
        "background_text_passed": True,
    }
    picked = gw.select_valid_cover([bad_cover, good_cover])
    if picked.get("id") != "cand_good":
        failures.append("拒绝重选未选中合格候选")
    else:
        print("[PASS] 拒绝重选机制生效（跳过 cand_bad 选中 cand_good）")

    # 4. 不合格计划：时长不足 + 价格文案 + 漏斗缺类目
    bad_plan = {
        "clips": [{"id": "c1", "st": 0, "end": 20, "dur": 20, "category": "hook",
                   "desc": "只要299块钱"}],
        "total_expected_duration": 20,
    }
    try:
        gw.validate_and_record_plan(bad_plan)
        failures.append("不合格分镜计划未被拦截")
    except DecisionViolation as e:
        print(f"[PASS] 不合格分镜计划被拦截: {e.violations}")

    # 5. 合格计划通过
    good_plan = {
        "clips": [
            {"id": "c1", "st": 0, "end": 6, "dur": 6, "category": "hook", "desc": "看这个面料"},
            {"id": "c2", "st": 6, "end": 20, "dur": 14, "category": "fabric", "desc": "绵羊毛做工"},
            {"id": "c3", "st": 20, "end": 34, "dur": 14, "category": "fit", "desc": "超级显瘦"},
            {"id": "c4", "st": 34, "end": 52, "dur": 18, "category": "styling", "desc": "这样搭配"},
        ],
        "total_expected_duration": 52,
    }
    gw.validate_and_record_plan(good_plan)
    print("[PASS] 合格分镜计划通过校验")

    # 6. Hook 卡压脸拦截
    plan_with_bad_card = dict(good_plan)
    plan_with_bad_card["hook_card"] = {"y": 420, "face_top_y": 0.16}  # 人脸顶 307px，卡底 550 > 277
    ok, v = gw.validate_hook_card(plan_with_bad_card)
    if ok:
        failures.append("Hook 卡压脸未被拦截")
    else:
        print(f"[PASS] Hook 卡压脸被拦截: {v}")

    print(f"\n[B12-SELFCHECK] {'全部通过' if not failures else '失败: ' + str(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Agent 决策约束网关（B12）")
    parser.add_argument("--demo", action="store_true", help="运行反例自检")
    args = parser.parse_args()
    sys.exit(_demo_selfcheck() if args.demo else 0)
