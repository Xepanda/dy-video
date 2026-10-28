#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
build_dialogue_script.py - ASR 驱动完整对话脚本生成器（反馈驱动 2026-10-27）
=================================================================================
职责：把 ASR 审核后台词整理成**完整对话脚本**，作为素材拼接的唯一依据。

解决反馈问题：
  - "对话不完整"：切片从段中间砍断 → 本工具以句子边界为最小单元，
    输出完整语句（utterance），剪辑时 cut 点吸附句末标点；
  - "切片间语言过渡不自然"：相邻切片的话术上下文断裂 → 脚本保留
     chronological 语序与上下文索引，build_plan 选段时优先连续 utterance；
  - "语音过渡不自然"：由 video_assembler 的 acrossfade 转场解决（配套）。

输出 dialogue_script.json:
  {
    "project_id": ...,
    "sources": [{file, n_segments, n_utterances, speech_ratio}],
    "utterances": [{id, source_file, st, end, dur, text, complete,
                    ends_sentence, categories, violation_hits}],
    "violation_summary": {...},
    "stats": {...}
  }

CLI:
  python src/tools/build_dialogue_script.py --spec job_spec.json \
      --segments data/work_dirs/<pid>/whisper_segments_verified.json \
      --out-json data/work_dirs/<pid>/dialogue_script.json
"""

import os
import sys
import json
import argparse
from typing import List, Dict, Any, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from src.tools.detect_violation import ViolationDetector

# 句子结束标点（中文为主，兼容英文）
SENT_END_CHARS = "。！？!?；;…"
# 相邻段合并的最大静默间隔（秒）——超过则视为不同 utterance
MERGE_GAP_SEC = 0.6
# utterance 最短时长（秒）
MIN_UTTER_SEC = 1.0


def _ends_with_sentence_punct(text: str) -> bool:
    t = (text or "").rstrip()
    return bool(t) and t[-1] in SENT_END_CHARS


def _classify_categories(text: str) -> List[str]:
    """轻量漏斗打标（与 build_plan 关键词表同源的精简版，用于脚本可读性）。"""
    cats = []
    hook_kw = ["姐妹们", "好看", "漂亮", "显瘦", "闭眼入", "新款", "上身效果", "气质", "高级感"]
    fabric_kw = ["面料", "材质", "桑蚕丝", "羊绒", "羊毛", "针织", "纱", "手感", "软糯",
                 "亲肤", "透气", "钩花", "镂空", "走线", "工艺"]
    fit_kw = ["版型", "显瘦", "遮肉", "修身", "不挑人", "比例", "收腰", "包裹"]
    styling_kw = ["搭配", "穿搭", "裤子", "裙子", "外套", "叠穿", "风格", "松弛"]
    if any(k in text for k in hook_kw):
        cats.append("hook")
    if any(k in text for k in fabric_kw):
        cats.append("fabric")
    if any(k in text for k in fit_kw):
        cats.append("fit")
    if any(k in text for k in styling_kw):
        cats.append("styling")
    return cats or ["other"]


def build_dialogue_script(
    segments: List[Dict[str, Any]],
    spec_data: Dict[str, Any] = None,
    detector: ViolationDetector = None,
    merge_gap: float = MERGE_GAP_SEC,
) -> Dict[str, Any]:
    """把 ASR 段整理为完整对话脚本（utterance 序列）。"""
    detector = detector or ViolationDetector()
    spec_data = spec_data or {}
    project_id = spec_data.get("project_id", "demo")

    # 1) 按来源文件分组，组内按时间排序（多源时间轴互不相通）
    by_src: Dict[str, List[Dict[str, Any]]] = {}
    for s in segments:
        src = s.get("source_file") or ""
        by_src.setdefault(src, []).append(s)
    for src in by_src:
        by_src[src].sort(key=lambda x: x.get("start", 0.0))

    utterances: List[Dict[str, Any]] = []
    sources_stat: List[Dict[str, Any]] = []
    uid = 0
    for src in sorted(by_src.keys()):
        segs = by_src[src]
        cur: Optional[Dict[str, Any]] = None
        src_n_utt = 0
        for s in segs:
            text = (s.get("text") or "").strip()
            if not text:
                continue
            st = float(s.get("start", 0.0))
            en = float(s.get("end", st))
            if cur is not None and st - cur["end"] <= merge_gap:
                # 合并进当前 utterance
                cur["end"] = max(cur["end"], en)
                cur["text"] = cur["text"] + ("，" if not _ends_with_sentence_punct(cur["text"]) else "") + text
                cur["segments"].append(s)
            else:
                if cur is not None:
                    utterances.append(cur)
                    src_n_utt += 1
                uid += 1
                cur = {
                    "id": f"u{uid:03d}",
                    "source_file": src,
                    "st": round(st, 3),
                    "end": round(en, 3),
                    "dur": round(en - st, 2),
                    "text": text,
                    "segments": [s],
                }
        if cur is not None:
            utterances.append(cur)
            src_n_utt += 1
        # 源统计
        total_speech = sum(
            float(s.get("end", 0)) - float(s.get("start", 0)) for s in segs)
        sources_stat.append({
            "file": src,
            "n_segments": len(segs),
            "n_utterances": src_n_utt,
            "speech_sec": round(total_speech, 1),
        })

    # 2) 丰富字段：句末完整标记 / 漏斗类目 / 违规命中
    for u in utterances:
        u["ends_sentence"] = _ends_with_sentence_punct(u["text"])
        u["complete"] = bool(u["ends_sentence"] and u["dur"] >= MIN_UTTER_SEC)
        u["categories"] = _classify_categories(u["text"])
        hits = detector.scan_text(u["text"])
        u["violation_hits"] = [
            {"word": h["word"], "category": h["category_name"]} for h in hits]
        u.pop("segments", None)  # 明细不回灌（保持脚本精简），审计走 verified 文件

    # 3) 汇总
    violation_utterances = [u for u in utterances if u["violation_hits"]]
    n_complete = sum(1 for u in utterances if u["complete"])
    stats = {
        "n_sources": len(sources_stat),
        "n_utterances": len(utterances),
        "n_complete_utterances": n_complete,
        "complete_ratio": round(n_complete / max(1, len(utterances)), 3),
        "n_violation_utterances": len(violation_utterances),
        "total_dur_sec": round(sum(u["dur"] for u in utterances), 1),
    }
    return {
        "project_id": project_id,
        "sources": sources_stat,
        "utterances": utterances,
        "violation_summary": {
            "n_utterances_with_violation": len(violation_utterances),
            "utterance_ids": [u["id"] for u in violation_utterances],
        },
        "stats": stats,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ASR 驱动完整对话脚本生成器")
    parser.add_argument("--spec", default="job_spec.json")
    parser.add_argument("--segments", required=True,
                        help="ASR 审核后台词 JSON（whisper_segments_verified.json）")
    parser.add_argument("--out-json", required=True)
    args = parser.parse_args(argv)

    spec_data = {}
    if os.path.exists(args.spec):
        with open(args.spec, "r", encoding="utf-8") as f:
            spec_data = json.load(f)
    with open(args.segments, "r", encoding="utf-8") as f:
        segments = json.load(f)

    script = build_dialogue_script(segments, spec_data)
    os.makedirs(os.path.dirname(os.path.abspath(args.out_json)), exist_ok=True)
    with open(args.out_json, "w", encoding="utf-8") as f:
        json.dump(script, f, ensure_ascii=False, indent=2)

    st = script["stats"]
    print(f"[DIALOGUE] 对话脚本已生成: {args.out_json}")
    print(f"  {st['n_sources']} 源 | {st['n_utterances']} 个完整语句 "
          f"(完整率 {st['complete_ratio']:.0%}) | 总语音 {st['total_dur_sec']}s")
    if st["n_violation_utterances"]:
        print(f"  [VIOLATION] {st['n_violation_utterances']} 个语句含违规词，"
              f"已登记待三通道处理")
    return 0


if __name__ == "__main__":
    sys.exit(main())
