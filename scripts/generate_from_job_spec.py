#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
generate_from_job_spec.py - 依据 job_spec.json 直播任务契约生成最终带货成片
===========================================================================
定位：Agent-in-the-Loop 体系下的「规范驱动总入口」。
      不替代 AgentDirector 决策中枢，而是把 job_spec.json 任务契约
      解析成流水线可直接消费的可执行 spec，并端到端调度执行。

职责：
  1. 解析 job_spec.json 任务契约（品牌 / 商品 / 素材 / 时长 / 关键词 / 分屏策略）；
  2. 解析直播素材清单：
       - spec.raw_video_paths 显式列表优先（填表工具产出）；
       - 否则 raw_video_path 指向目录 → 扫描其下全部直播视频（按文件名排序）；
       - 否则按单文件处理；
  3. 校验素材可解码（ffprobe）、商品图存在；
  4. 派生 Hook 卡 / CTA 文案（严格取自行契约商品信息，零硬编码），
     并尊重 split_strategy=force_off（不注入 split_pairs → 强制关闭分屏）；
  5. 落盘可执行 spec（resolved spec，原始 job_spec.json 不被修改）；
  6. 调用 AgentDirector 端到端流水线：
     候选提取（镜头边界/封面/ASR）→ Agent 决策 → 封面渲染
     → FFmpeg 音画强锁组装 → QC 门禁；
  7. 汇总交付物清单并写 generation_manifest.json。

用法：
  python scripts/generate_from_job_spec.py --spec job_spec.json
  python scripts/generate_from_job_spec.py --spec job_spec.json --no-cache
"""

import os
import sys
import json
import shutil
import argparse
import subprocess

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.core.path_resolver import resolve_path

VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".flv", ".m4v", ".ts")
SPECS_DIR = os.path.join(PROJECT_ROOT, "specs")


# ---------------------------------------------------------------- 工具函数

def _ffprobe_ok(video_path: str) -> dict:
    """ffprobe 校验视频可解码，返回 {width,height,fps,duration}；失败抛异常。"""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,duration",
        "-of", "json", video_path,
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise RuntimeError(f"ffprobe 执行失败: {e}")
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe 返回非零: {out.stderr.strip()[:200]}")
    info = json.loads(out.stdout or "{}")
    streams = info.get("streams") or []
    if not streams:
        raise RuntimeError("无视频流")
    s = streams[0]
    return {
        "width": s.get("width"),
        "height": s.get("height"),
        "fps": s.get("r_frame_rate"),
        "duration": float(s.get("duration") or 0),
    }


def collect_source_videos(spec: dict, spec_dir: str) -> list:
    """按契约优先级收集直播素材（返回绝对路径列表，顺序稳定）。"""
    paths = []

    # 1) 显式列表优先
    for p in spec.get("raw_video_paths") or []:
        rp = resolve_path(p, spec_dir)
        if rp and os.path.exists(rp) and os.path.isfile(rp):
            paths.append(os.path.abspath(rp))

    # 2) raw_video_path：目录 → 扫描；文件 → 单源
    primary = spec.get("raw_video_path")
    if primary:
        primary_abs = resolve_path(primary, spec_dir) or os.path.abspath(primary)
        if os.path.isdir(primary_abs):
            for name in sorted(os.listdir(primary_abs)):
                if name.lower().endswith(VIDEO_EXTS):
                    ap = os.path.abspath(os.path.join(primary_abs, name))
                    if ap not in paths:
                        paths.append(ap)
        elif os.path.isfile(primary_abs):
            if primary_abs not in paths:
                paths.insert(0, primary_abs)
        else:
            raise FileNotFoundError(
                f"raw_video_path 既不是目录也不是已存在的文件: {primary_abs}")

    if not paths:
        raise FileNotFoundError(
            "契约中未找到任何可用直播素材（raw_video_paths 为空且 "
            "raw_video_path 下无视频文件）")
    return paths


from src.tools.build_plan import _derive_hook_text, _derive_cta_text


def derive_texts(spec: dict) -> dict:
    """从契约商品信息派生 Hook 卡 / CTA 文案（与 build_plan 派生规则严格一致）。"""
    return {
        "hook_card_text": _derive_hook_text(spec),
        "cta_text": _derive_cta_text(spec),
    }


def build_resolved_spec(spec: dict, videos: list, texts: dict) -> dict:
    """生成可执行 spec：显式素材清单 + 派生文案 + 尊重分屏策略。"""
    resolved = json.loads(json.dumps(spec))  # 深拷贝，不动原始契约
    resolved["raw_video_paths"] = list(videos)
    resolved["raw_video_path"] = videos[0]
    options = resolved.setdefault("options", {})
    options["hook_card_text"] = texts["hook_card_text"]
    options["cta_text"] = texts["cta_text"]
    # split_strategy=force_off → 确保不注入 split_pairs（强制关闭分屏）
    if str(options.get("split_strategy") or "").lower() == "force_off":
        options.pop("split_pairs", None)
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(
        description="依据 job_spec.json 直播任务契约生成最终带货成片")
    parser.add_argument("--spec", default="job_spec.json",
                        help="任务契约文件路径（默认仓库根 job_spec.json）")
    parser.add_argument("--no-cache", action="store_true",
                        help="禁用阶段缓存（强制全量重算）")
    args = parser.parse_args()

    spec_path = os.path.abspath(args.spec)
    if not os.path.exists(spec_path):
        print(f"[FATAL] 任务契约不存在: {spec_path}")
        return 2
    with open(spec_path, "r", encoding="utf-8") as f:
        spec = json.load(f)

    spec_dir = os.path.dirname(spec_path)
    project_id = spec.get("project_id", "demo")

    print("=" * 66)
    print(f"任务契约解析: {spec_path}")
    print(f"  project_id : {project_id}")
    brand = spec.get("brand", {}) or {}
    print(f"  品牌       : {brand.get('title_en', '')} / {brand.get('subtitle_cn', '')}")
    product = spec.get("product", {}) or {}
    print(f"  商品       : {product.get('title_line1', '')}{product.get('title_line2', '')}")
    options = spec.get("options", {}) or {}
    print(f"  时长要求   : {options.get('duration_range', '45-65')}s | "
          f"分屏策略: {options.get('split_strategy', 'auto')}")
    print(f"  高亮关键词 : {', '.join(options.get('highlight_keywords', []))}")
    print("=" * 66)

    # —— 1. 收集直播素材 ——
    videos = collect_source_videos(spec, spec_dir)
    print(f"\n[1/5] 直播素材清单（{len(videos)} 段）:")
    total_dur = 0.0
    for i, v in enumerate(videos, 1):
        meta = _ffprobe_ok(v)
        total_dur += meta["duration"]
        print(f"  [{i}] {os.path.basename(v)}  "
              f"{meta['width']}x{meta['height']} @{meta['fps']} "
              f"{meta['duration']:.1f}s")
    print(f"  合计 {total_dur:.1f}s 原始直播素材")

    # —— 2. 校验商品图 ——
    print(f"\n[2/5] 商品图校验:")
    item_images = []
    for img in (product.get("item_images") or []):
        rp = resolve_path(img, spec_dir)
        if rp and os.path.exists(rp):
            item_images.append(os.path.abspath(rp))
            print(f"  [OK] {os.path.basename(rp)}")
        else:
            print(f"  [MISS] {img} （不存在，封面将由流水线占位兜底）")
    if not item_images:
        print("  [WARN] 无可用商品图，封面商品位将走占位图路径")

    # —— 3. 派生文案并落盘可执行 spec ——
    texts = derive_texts(spec)
    resolved_spec = build_resolved_spec(spec, videos, texts)
    os.makedirs(SPECS_DIR, exist_ok=True)
    resolved_path = os.path.join(
        SPECS_DIR, f"job_spec_{project_id}.resolved.json")
    with open(resolved_path, "w", encoding="utf-8") as f:
        json.dump(resolved_spec, f, ensure_ascii=False, indent=2)
    print(f"\n[3/5] 可执行 spec 已落盘: {resolved_path}")
    print(f"  Hook 卡文案 : {texts['hook_card_text']}")
    print(f"  CTA 文案    : {texts['cta_text']}")

    # —— 4. 端到端流水线 ——
    print(f"\n[4/5] 启动 Agent 剪辑导演中枢（端到端）...")
    from src.agent.agent_director import AgentDirector
    director = AgentDirector(resolved_path, use_cache=not args.no_cache)
    manifest = director.run_director_workflow()

    # —— 5. 交付汇总 ——
    print(f"\n[5/5] 交付物汇总:")
    deliverables = manifest.get("final_deliverables", {})
    gen_manifest = {
        "project_id": project_id,
        "source_spec": spec_path,
        "resolved_spec": resolved_path,
        "derived_texts": texts,
        "source_videos": videos,
        "source_total_duration_sec": round(total_dur, 2),
        "item_images": item_images,
        "final_duration_sec": manifest.get("total_duration"),
        "qc_passed": manifest.get("qc_passed"),
        "final_deliverables": deliverables,
    }
    gen_path = os.path.join(director.output_dir, "generation_manifest.json")
    with open(gen_path, "w", encoding="utf-8") as f:
        json.dump(gen_manifest, f, ensure_ascii=False, indent=2)

    for k, v in deliverables.items():
        exists = "✓" if v and os.path.exists(v) else "✗"
        print(f"  [{exists}] {k}: {v}")
    print(f"  [✓] generation_manifest: {gen_path}")
    print(f"\n质检结论: {'ALL PASS' if manifest.get('qc_passed') else 'BLOCK FAILED'}")
    print("=" * 66)
    return 0 if manifest.get("qc_passed") else 1


if __name__ == "__main__":
    sys.exit(main())
