#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
chatcut_bridge.py - OpenChatCut 时间线桥接（B14）
================================================
背景：OpenChatCut（github.com/0xsline/OpenChatCut，AGPL-3.0）是本地优先的
Agent 原生视频编辑器，项目以"多轨时间线"结构化数据存在（video/audio/text
tracks + clips + transitions + captions），可通过 MCP 端点
（http://localhost:5199/api/external-mcp/mcp）被外部 Agent 读写。

本桥接实现 dy-live-editor 剪辑计划 ↔ OpenChatCut 风格时间线的双向转换：

  plan.json ──plan_to_timeline──▶ timeline.json（可导入 OpenChatCut 人工精修）
  plan.json ◀──timeline_to_plan── timeline.json（人工精修后的回注）

设计约定（OpenChatCut 概念模型的适配子集，v1）：
  - 时间线坐标为秒（float）；source_in/out 指向原始媒体时间轴；
  - 每个 clip 携带 meta{plan_clip_id, category, desc}，保证回注时可恢复
    计划语义（category 供 B12 网关复测漏斗配额）；
  - 人工可编辑字段：timeline_start/end（重排/裁剪）、source_in/out（换素材区间）、
    clips 增删、zoom、text 轨字幕文本；
  - 回注后重新组装 + QC（B12 网关 + qc_auditor 全程复测）。

CLI:
  python src/tools/chatcut_bridge.py --plan work_dirs/x/plan.json \
      --spec job_spec.json --out timeline.json
  python src/tools/chatcut_bridge.py --timeline timeline.json \
      --plan-out work_dirs/x/plan_refined.json
"""

import os
import sys
import json
import argparse
from typing import Dict, Any, List, Optional, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

TIMELINE_VERSION = 1
CANVAS_W, CANVAS_H, FPS = 1080, 1920, 30


# ---------------------------------------------------------------- plan → timeline

def plan_to_timeline(plan: Dict[str, Any], spec_data: Dict[str, Any] = None,
                     subtitle_texts: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """把 dy-live-editor 剪辑计划转换为 OpenChatCut 风格多轨时间线。"""
    spec_data = spec_data or {}
    clips = plan.get("clips", []) or []
    video_track_clips = []
    # 多源（2026-10-21）：每个源文件一个 media 条目，clip 通过 media_id 指向各自来源。
    # 单源时沿用 m_source，向后兼容既有时间线。
    media_list = []
    media_id_by_path = {}
    default_path = plan.get("raw_video_path", spec_data.get("raw_video_path", ""))
    for c in clips:
        spath = c.get("source_file") or default_path
        if spath not in media_id_by_path:
            mid = "m_source" if spath == default_path else f"m_src{len(media_id_by_path)}"
            media_id_by_path[spath] = mid
            media_list.append({"id": mid, "type": "video", "path": spath,
                               "role": "source_livestream"})
    if not media_list:
        media_list.append({"id": "m_source", "type": "video",
                           "path": default_path, "role": "source_livestream"})

    t_cursor = 0.0
    for c in clips:
        dur = float(c.get("dur", c.get("end", 0) - c.get("st", 0)))
        spath = c.get("source_file") or default_path
        video_track_clips.append({
            "id": f"v_{c.get('id', len(video_track_clips))}",
            "media_id": media_id_by_path.get(spath, "m_source"),
            "timeline_start": round(t_cursor, 3),
            "timeline_end": round(t_cursor + dur, 3),
            "source_in": round(float(c.get("st", 0)), 3),
            "source_out": round(float(c.get("end", 0)), 3),
            "zoom": c.get("zoom", 1.0),
            "y_anchor": c.get("y_anchor", 0.5),
            "hflip": bool(c.get("hflip", False)),
            "effect": c.get("effect", "none"),
            "meta": {
                "plan_clip_id": c.get("id"),
                "category": c.get("category", "other"),
                "desc": c.get("desc", ""),
                "is_hook": bool(c.get("is_hook", False)),
            },
        })
        t_cursor += dur

    # 文本轨：Hook 卡 + CTA + 字幕
    text_clips = []
    hook_card = plan.get("hook_card") or {}
    if hook_card.get("text"):
        text_clips.append({
            "id": "t_hook_card", "kind": "hook_card",
            "text": hook_card.get("text", ""),
            "timeline_start": 0.0,
            "timeline_end": round(float(hook_card.get("dur", 2.5)), 3),
            "y": hook_card.get("y", 280),
        })
    cta = plan.get("cta") or {}
    if cta.get("text"):
        text_clips.append({
            "id": "t_cta", "kind": "cta",
            "text": cta.get("text", ""),
            "timeline_start": round(t_cursor - float(cta.get("dur", 2.5)), 3),
            "timeline_end": round(t_cursor, 3),
        })
    for i, st in enumerate(subtitle_texts or []):
        text_clips.append({
            "id": f"t_sub_{i:04d}", "kind": "subtitle",
            "text": st.get("text", ""),
            "timeline_start": round(float(st.get("start", 0)), 3),
            "timeline_end": round(float(st.get("end", 0)), 3),
        })

    # 音效轨：Boom
    boom = plan.get("boom") or {}
    audio_clips = []
    if boom.get("file"):
        audio_clips.append({
            "id": "a_boom", "media_id": "m_boom",
            "source": boom.get("file"),
            "timeline_start": 0.0,
            "timeline_end": round(float(boom.get("dur", 0.5)), 3),
            "volume": boom.get("volume", 0.45),
        })

    total = float(plan.get("total_expected_duration", t_cursor))
    timeline = {
        "version": TIMELINE_VERSION,
        "generator": "dy-live-editor chatcut_bridge v1",
        "project": {
            "name": plan.get("project_id", spec_data.get("project_id", "untitled")),
            "resolution": [CANVAS_W, CANVAS_H],
            "fps": FPS,
            "duration": round(total, 3),
        },
        "media": media_list + ([{"id": "m_boom", "type": "audio",
                                 "path": boom.get("file"),
                                 "role": "transition_boom"}] if boom.get("file") else []),
        "tracks": [
            {"id": "track_video", "type": "video", "clips": video_track_clips},
            {"id": "track_audio_fx", "type": "audio", "clips": audio_clips},
            {"id": "track_text", "type": "text", "clips": text_clips},
        ],
        "constraints_ref": {
            "version": 1,
            "duration_range": [45, 65],
            "note": "回注计划将由 B12 决策网关按 contracts/constraints.yaml 复测",
        },
    }
    return timeline


# ---------------------------------------------------------------- timeline → plan

def timeline_to_plan(timeline: Dict[str, Any]) -> Dict[str, Any]:
    """把（可能被人工精修过的）OpenChatCut 风格时间线回注为剪辑计划。

    人工编辑语义：
      - 视频轨 clip 增删/重排/改边界 → 计划分镜相应变化（按 timeline 顺序）;
      - zoom/effect/meta 原样带回;
      - Hook 卡 y / CTA / Boom 从文本轨与音效轨恢复。
    """
    if timeline.get("version") != TIMELINE_VERSION:
        raise ValueError(f"不支持的时间线版本: {timeline.get('version')}")

    tracks = {t["id"]: t for t in timeline.get("tracks", []) if isinstance(t, dict)}
    video_track = next((t for t in timeline.get("tracks", [])
                        if t.get("type") == "video"), {"clips": []})
    audio_track = next((t for t in timeline.get("tracks", [])
                        if t.get("type") == "audio"), {"clips": []})
    text_track = next((t for t in timeline.get("tracks", [])
                       if t.get("type") == "text"), {"clips": []})

    # 多源回注：按 clip 的 media_id 还原其来源文件（缺省回退首个源媒体）
    media_by_id = {m.get("id"): m for m in timeline.get("media", [])}
    source_media = next((m for m in timeline.get("media", [])
                         if m.get("role") == "source_livestream"), {})
    raw_video = source_media.get("path", "")
    # 多源判定：source_livestream 角色的 media 实体数 > 1
    multi_source_timeline = sum(
        1 for m in timeline.get("media", [])
        if m.get("role") == "source_livestream") > 1

    clips = []
    t_cursor = 0.0
    for tc in sorted(video_track.get("clips", []),
                     key=lambda c: c.get("timeline_start", 0)):
        st = float(tc.get("source_in", 0))
        end = float(tc.get("source_out", st))
        dur = round(end - st, 3)
        meta = tc.get("meta") or {}
        entry = {
            "id": meta.get("plan_clip_id") or tc.get("id", f"c{len(clips)+1}"),
            "st": st,
            "end": end,
            "dur": dur,
            "zoom": tc.get("zoom", 1.0),
            "y_anchor": tc.get("y_anchor", 0.5),
            "hflip": bool(tc.get("hflip", False)),
            "effect": tc.get("effect", "none"),
            "category": meta.get("category", "other"),
            "desc": meta.get("desc", ""),
            "is_hook": bool(meta.get("is_hook", False)),
        }
        # 多源：还原该分镜的来源文件（否则重新组装会从错误文件切片）
        src_media = media_by_id.get(tc.get("media_id"))
        if src_media and src_media.get("path") and src_media.get("path") != raw_video:
            entry["source_file"] = src_media["path"]
            entry["source_tag"] = os.path.basename(src_media["path"])
        elif multi_source_timeline:
            # 多源时间线：即使等于默认源也显式标注，避免回注后与单源计划混淆
            if src_media and src_media.get("path"):
                entry["source_file"] = src_media["path"]
                entry["source_tag"] = os.path.basename(src_media["path"])
        clips.append(entry)
        t_cursor += dur

    if not clips:
        raise ValueError("时间线视频轨为空，无法回注计划")

    # 文本轨恢复 Hook 卡 / CTA（按 kind 匹配，缺失用默认值）
    hook_card = {"text": "极简松弛感 · 显瘦显气质", "dur": 2.5, "y": 280}
    cta = {"text": "戳左下方小黄车查看上身搭配", "dur": 2.5}
    for tc in text_track.get("clips", []):
        if tc.get("kind") == "hook_card":
            hook_card = {"text": tc.get("text", hook_card["text"]),
                         "dur": round(float(tc.get("timeline_end", 2.5))
                                      - float(tc.get("timeline_start", 0)), 3),
                         "y": tc.get("y", 280)}
        elif tc.get("kind") == "cta":
            cta = {"text": tc.get("text", cta["text"]),
                   "dur": round(float(tc.get("timeline_end", 0))
                                - float(tc.get("timeline_start", 0)), 3)}

    # 音效轨恢复 Boom
    boom = {"volume": 0.45, "file": "boom.wav"}
    for ac in audio_track.get("clips", []):
        if ac.get("id") == "a_boom" or ac.get("role") == "transition_boom":
            boom = {"volume": ac.get("volume", 0.45),
                    "file": ac.get("source", "boom.wav"),
                    "dur": round(float(ac.get("timeline_end", 0.5))
                                 - float(ac.get("timeline_start", 0)), 3)}

    plan = {
        "project_id": timeline.get("project", {}).get("name", "refined"),
        "raw_video_path": raw_video,
        "total_expected_duration": round(t_cursor, 3),
        "clips": clips,
        "hook_card": hook_card,
        "cta": cta,
        "boom": boom,
        "subtitles": {"keywords": []},
        "refined_from": "chatcut_timeline",
    }
    return plan


def validate_timeline(timeline: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """时间线结构校验（导入前自检）。"""
    errors = []
    if timeline.get("version") != TIMELINE_VERSION:
        errors.append(f"版本不支持: {timeline.get('version')}")
    tracks = timeline.get("tracks")
    if not isinstance(tracks, list) or not tracks:
        errors.append("tracks 缺失或为空")
        return False, errors
    has_video = False
    for t in tracks:
        if not isinstance(t, dict) or "type" not in t or "clips" not in t:
            errors.append(f"轨道结构不完整: {t}")
            continue
        if t["type"] == "video":
            has_video = True
            prev_end = None
            for c in t["clips"]:
                for f in ("timeline_start", "timeline_end", "source_in", "source_out"):
                    if f not in c:
                        errors.append(f"视频 clip 缺少字段 {f}: {c.get('id')}")
                if c.get("timeline_end", 0) <= c.get("timeline_start", 0):
                    errors.append(f"clip {c.get('id')} 时间线区间非法")
                if c.get("source_out", 0) <= c.get("source_in", 0):
                    errors.append(f"clip {c.get('id')} 源区间非法")
                if prev_end is not None and c.get("timeline_start", 0) < prev_end - 1e-6:
                    errors.append(f"clip {c.get('id')} 与前 clip 时间线重叠")
                prev_end = c.get("timeline_end", 0)
    if not has_video:
        errors.append("缺少视频轨")
    return (len(errors) == 0), errors


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main(argv=None):
    parser = argparse.ArgumentParser(description="OpenChatCut 时间线桥接（B14）")
    parser.add_argument("--plan", help="plan.json 路径（导出模式）")
    parser.add_argument("--spec", default=None, help="job_spec（导出模式，补充媒体信息）")
    parser.add_argument("--subtitles", default=None, help="字幕文本 JSON（导出模式可选）")
    parser.add_argument("--out", help="导出：timeline.json 输出路径")
    parser.add_argument("--timeline", help="导入：timeline.json 路径")
    parser.add_argument("--plan-out", help="导入：回注 plan.json 输出路径")
    args = parser.parse_args(argv)

    if args.plan and args.out:
        plan = _load_json(args.plan)
        spec = _load_json(args.spec) if args.spec else {}
        subs = _load_json(args.subtitles) if args.subtitles else None
        timeline = plan_to_timeline(plan, spec, subs)
        ok, errors = validate_timeline(timeline)
        if not ok:
            print(f"[BRIDGE] 生成的时间线未通过自检: {errors}")
            return 2
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(timeline, f, ensure_ascii=False, indent=2)
        print(f"[BRIDGE] 时间线已导出: {args.out} "
              f"({len(timeline['tracks'][0]['clips'])} 视频 clip，"
              f"总长 {timeline['project']['duration']}s)")
        return 0

    if args.timeline and args.plan_out:
        timeline = _load_json(args.timeline)
        ok, errors = validate_timeline(timeline)
        if not ok:
            print(f"[BRIDGE] 时间线校验失败，拒绝回注: {errors}")
            return 2
        plan = timeline_to_plan(timeline)
        os.makedirs(os.path.dirname(os.path.abspath(args.plan_out)), exist_ok=True)
        with open(args.plan_out, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)
        print(f"[BRIDGE] 计划已回注: {args.plan_out} "
              f"({len(plan['clips'])} 分镜，总长 {plan['total_expected_duration']}s)")
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
