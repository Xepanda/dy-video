# -*- coding: utf-8 -*-
"""
cut_optimizer.py - 切点联合微调与局部自修复（V3 PR-07 核心）
============================================================
负责：
  1. 依据 ContinuityAuditor 的审查建议 (SHIFT_CUT / GAIN_MATCH)，
     对问题切点进行 ±200~500ms 毫秒级微调；
  2. 修复从句未闭合与动作截断，同时保证字幕时间轴同步修正；
  3. 局部修复后复验，避免整片重排重试。
"""

import os
import sys
from typing import Dict, Any, List, Optional, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class CutOptimizer:
    """切点微调与局部自修复器。"""

    def optimize_cuts(
        self,
        clips: List[Dict[str, Any]],
        continuity_review: Dict[str, Any],
        plan: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """依据接缝审查报告执行微调，返回修复后的 clips 列表与操作日志。"""
        fixed_clips = [dict(c) for c in clips]
        actions_taken = []

        seam_map = {s["left_candidate_id"]: s for s in continuity_review.get("seams", [])}

        for i, clip in enumerate(fixed_clips[:-1]):
            cid = clip.get("candidate_id") or clip.get("id") or f"clip_{i}"
            seam = seam_map.get(cid)
            if not seam:
                continue

            action = seam.get("suggested_action")
            shift_ms = seam.get("suggested_time_shift_ms", 0)

            if action == "SHIFT_CUT" and shift_ms != 0:
                old_en = clip.get("end")
                if old_en is None and "source_end_ms" in clip:
                    old_en = round(clip["source_end_ms"] / 1000.0, 2)
                old_en = float(old_en or 0.0)

                start_sec = clip.get("st") if "st" in clip else clip.get("start")
                if start_sec is None and "source_start_ms" in clip:
                    start_sec = clip["source_start_ms"] / 1000.0
                start_sec = float(start_sec or 0.0)

                shift_sec = shift_ms / 1000.0
                new_en = round(old_en + shift_sec, 2)
                if new_en > start_sec + 0.5:
                    clip["end"] = new_en
                    clip["dur"] = round(new_en - start_sec, 2)
                    if "source_end_ms" in clip:
                        clip["source_end_ms"] = int(round(new_en * 1000))
                    clip["_cut_shifted_ms"] = shift_ms

                    # 同步更新字幕映射（避免微调后尾字时间轴错位）
                    if "subtitles" in clip and isinstance(clip["subtitles"], list):
                        for sub in clip["subtitles"]:
                            if float(sub.get("end", 0.0)) > new_en:
                                sub["end"] = new_en

                    actions_taken.append(
                        f"接缝 {seam['seam_id']} ({cid}): 切点微调 {shift_ms:+d}ms "
                        f"({old_en}s → {new_en}s) 以闭合语句/动作"
                    )
            elif action == "GAIN_MATCH":
                clip["_audio_gain_match"] = True
                actions_taken.append(f"接缝 {seam['seam_id']} ({cid}): 启用跨源增益匹配与微淡变")

        if plan and actions_taken:
            plan["total_expected_duration"] = round(sum(c.get("dur", 0.0) for c in fixed_clips), 2)

        return fixed_clips, actions_taken
