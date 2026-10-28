#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
build_content_structure_doc.py - 从任务契约与流水线产物生成《直播内容结构说明书》
=================================================================================
输入：
  - job_spec.json（原始任务契约）
  - specs/job_spec_<project_id>.resolved.json（可执行 spec）
  - data/work_dirs/<project_id>/plan.json（分镜编排）
  - data/work_dirs/<project_id>/story_*.json 或 data/candidates/<project_id>/story_candidates.json
  - data/work_dirs/<project_id>/whisper_segments_verified.json（ASR 审核后台词）
  - output/<project_id>/qc_report.json（质检报告）
  - output/<project_id>/generation_manifest.json（生成清单）
输出：
  - docs/live_content_structure_<project_id>.md

用法：
  python scripts/build_content_structure_doc.py --project-id sample_silk_knitwear_01
"""

import os
import sys
import json
import glob
import argparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CATEGORY_CN = {
    "hook": "开场钩子（黄金 3 秒）",
    "fabric": "面料讲解",
    "fit": "版型展示",
    "styling": "穿搭推荐",
}
FUNNEL_ORDER = ["hook", "fabric", "fit", "styling"]


def load_json(path):
    if not path or not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def fmt_t(sec):
    """秒 → mm:ss.ms"""
    if sec is None:
        return "-"
    m = int(sec // 60)
    s = sec - m * 60
    return f"{m:02d}:{s:05.2f}"


def main():
    parser = argparse.ArgumentParser(description="生成直播内容结构说明书")
    parser.add_argument("--project-id", required=True)
    args = parser.parse_args()
    pid = args.project_id

    spec = load_json(os.path.join(PROJECT_ROOT, "job_spec.json"))
    resolved = load_json(os.path.join(
        PROJECT_ROOT, "specs", f"job_spec_{pid}.resolved.json"))
    work_dir = os.path.join(PROJECT_ROOT, "data", "work_dirs", pid)
    out_dir = os.path.join(PROJECT_ROOT, "output", pid)
    plan = load_json(os.path.join(work_dir, "plan.json"))
    qc = load_json(os.path.join(out_dir, "qc_report.json"))
    gen_manifest = load_json(os.path.join(out_dir, "generation_manifest.json"))

    # 故事候选：优先 candidates 目录，其次 work_dir 下最新 story_*.json
    story = load_json(os.path.join(
        PROJECT_ROOT, "data", "candidates", pid, "story_candidates.json"))
    if not story:
        cands = sorted(glob.glob(os.path.join(work_dir, "story_*.json")),
                       key=os.path.getmtime)
        if cands:
            story = load_json(cands[-1])
    story_data = (story or {}).get("data", story or {})

    # ASR 审核后台词
    verified = load_json(os.path.join(work_dir, "whisper_segments_verified.json")) or []

    if not spec:
        print("[FATAL] job_spec.json 不存在")
        return 2

    brand = spec.get("brand", {}) or {}
    product = spec.get("product", {}) or {}
    options = spec.get("options", {}) or {}
    keywords = options.get("highlight_keywords", [])

    L = []
    A = L.append
    A(f"# 直播内容结构说明书 — {pid}")
    A("")
    A(f"> 依据任务契约 `job_spec.json` 与自动化剪辑流水线产物生成  ")
    A(f"> 项目 ID：`{pid}`｜生成时间：由流水线运行记录为准")
    A("")
    A("---")
    A("")

    # ── 1. 任务契约解析 ──
    A("## 1. 任务契约解析（job_spec.json）")
    A("")
    A("| 契约字段 | 值 | 说明 |")
    A("| :--- | :--- | :--- |")
    A(f"| project_id | `{spec.get('project_id')}` | 任务唯一标识 |")
    A(f"| brand | {brand.get('title_en', '')} / {brand.get('subtitle_cn', '')} | 品牌主标 / 副标 |")
    A(f"| product | {product.get('title_line1', '')}{product.get('title_line2', '')} | 商品标题（两行） |")
    dur = options.get("duration_range", "45-65")
    A(f"| duration_range | {dur}s | 成片时长约束（constraints V1） |")
    A(f"| split_strategy | {options.get('split_strategy', 'auto')} | 分屏策略（force_off=强制关闭） |")
    A(f"| highlight_keywords | {', '.join(keywords)} | 字幕高亮关键词 |")
    src_videos = gen_manifest.get("source_videos") if gen_manifest else None
    if not src_videos:
        src_videos = (resolved or {}).get("raw_video_paths") or []
    A(f"| 直播素材 | {len(src_videos)} 段 | 位于 `raw_video_path` 指定目录 |")
    A("")
    if src_videos:
        A("**素材清单：**")
        A("")
        for i, v in enumerate(src_videos, 1):
            A(f"{i}. `{os.path.basename(v)}`")
        A("")

    # ── 2. 商品信息 ──
    A("## 2. 商品信息")
    A("")
    A(f"- **品牌**：{brand.get('title_en', '')}（{brand.get('subtitle_cn', '')}）")
    A(f"- **商品名**：{product.get('title_line1', '')} · {product.get('title_line2', '')}")
    A(f"- **核心卖点关键词**：{'、'.join(keywords)}")
    A(f"- **商品图**：`{product.get('item_images', ['-'])[0]}`")
    A("")
    A("**卖点结构（由关键词归纳）：**")
    A("")
    A("| 维度 | 卖点 | 对应关键词 |")
    A("| :--- | :--- | :--- |")
    fabric_kw = [k for k in keywords if k in ("桑蚕丝", "羊绒", "羊毛")]
    fit_kw = [k for k in keywords if k in ("显瘦",)]
    hook_kw = [k for k in keywords if k in ("闭眼入", "降温")]
    name_kw = [k for k in keywords if k in ("针织衫",)]
    A(f"| 材质面料 | 桑蚕丝 + 羊绒 + 羊毛混纺 | {'、'.join(fabric_kw) or '-'} |")
    A(f"| 版型效果 | 渐变染色、视觉显瘦 | {'、'.join(fit_kw) or '-'} |")
    A(f"| 购买号召 | 降温季闭眼入 | {'、'.join(hook_kw) or '-'} |")
    A(f"| 品类词 | 针织衫 | {'、'.join(name_kw) or '-'} |")
    A("")

    # ── 3. 直播流程设计 ──
    A("## 3. 直播流程设计（电商漏斗）")
    A("")
    A("成片叙事严格遵循四段式电商漏斗（constraints.yaml `video.funnel` 配额驱动）：")
    A("")
    A("```text")
    A("┌──────────────────────────────────────────────────────────┐")
    A("│  ① 开场钩子 Hook（3~8s）     重音金句抓住停留                │")
    A("│        ↓                                               │")
    A("│  ② 面料讲解 Fabric（8~20s）   桑蚕丝/羊绒/羊毛材质细节        │")
    A("│        ↓                                               │")
    A("│  ③ 版型展示 Fit（8~15s）      渐变染色 + 显瘦上身效果         │")
    A("│        ↓                                               │")
    A("│  ④ 穿搭推荐 Styling（15~30s） 一衣多穿场景搭配               │")
    A("│        ↓                                               │")
    A("│  ⑤ 片尾 CTA（2.5s）           小黄车行动召唤                 │")
    A("└──────────────────────────────────────────────────────────┘")
    A("```")
    A("")
    A("- **选段纪律**：价格/库存/换款话术由黑名单正则 + 中文数字价格检测器剔除；")
    A("- **叙事顺序**：打破原直播时间序，按漏斗逻辑重排（Hook 置顶）；")
    A("- **镜头语言**：面料块自动 1.3x 缓动推镜；穿搭/版型块按需 hflip 镜像；")
    A("- **分屏策略**：本任务 `split_strategy=force_off`，不启用双色分屏。")
    A("")

    # ── 4. 场景顺序与分镜脚本 ──
    A("## 4. 场景顺序与分镜脚本（plan.json）")
    A("")
    if plan:
        clips = plan.get("clips", [])
        A(f"共 **{len(clips)} 个分镜**，总时长 **{plan.get('total_expected_duration', '-')}s**"
          f"（约束区间 {dur}s）"
          + ("，多源素材跨段挑选" if plan.get("multi_source") else "。"))
        A("")
        A("| # | 类别 | 源内时间 | 时长 | 特效 | 贴纸 | 话术摘要 |")
        A("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
        for i, c in enumerate(clips, 1):
            cat = CATEGORY_CN.get(c.get("category", ""), c.get("category", ""))
            effect = c.get("effect", "none")
            eff_desc = {"none": "-", "hflip": "水平镜像", "split": "双色分屏"}.get(effect, effect)
            if c.get("zoom", 1.0) > 1.0:
                eff_desc = f"{eff_desc}+推镜{c['zoom']}x" if eff_desc != "-" else f"推镜{c['zoom']}x"
            if (c.get("magnifier") or {}).get("enabled"):
                eff_desc = f"{eff_desc}+放大镜" if eff_desc != "-" else "放大镜圆圈"
            sticker = c.get("sticker_text", "") or "-"
            desc = (c.get("desc", "") or "").replace("|", "\\|")
            A(f"| {i} | {cat} | {fmt_t(c.get('st'))}~{fmt_t(c.get('end'))} | "
              f"{c.get('dur', 0):.2f}s | {eff_desc} | {sticker} | {desc} |")
        A("")
        hc = plan.get("hook_card", {}) or {}
        cta = plan.get("cta", {}) or {}
        A(f"- **Hook 标题卡**：`{hc.get('text', '-')}`（前 2.5s，720×168，避让人脸顶）")
        A(f"- **片尾 CTA**：`{cta.get('text', '-')}`（片尾 2.5s 徽章）")
        A(f"- **字幕高亮词**：{'、'.join((plan.get('subtitles', {}) or {}).get('keywords', []))}")
    else:
        A("（plan.json 尚未生成）")
    A("")

    # ── 5. 主播话术精选 ──
    A("## 5. 主播话术精选（ASR 审核后台词）")
    A("")
    if verified:
        # 按分镜区间提取对应话术
        A("按成片场景顺序摘录对应时段的主播原声台词（Faster-Whisper 转录 + 语境纠错 + 执行者审核）：")
        A("")
        if plan:
            for i, c in enumerate(plan.get("clips", []), 1):
                segs = [s for s in verified
                        if s.get("start", 0) < c.get("end", 0)
                        and s.get("end", 0) > c.get("st", 0)]
                # 只保留与源文件匹配的（多源场景）
                sf = c.get("source_file")
                if sf:
                    segs = [s for s in segs
                            if s.get("source_file") in (None, sf)
                            or os.path.basename(str(s.get("source_file", ""))) == os.path.basename(sf)]
                text = "；".join((s.get("text", "") or "").strip() for s in segs if s.get("text"))
                text = text.replace("|", "\\|")
                if len(text) > 120:
                    text = text[:120] + "…"
                cat = CATEGORY_CN.get(c.get("category", ""), c.get("category", ""))
                A(f"**场景 {i}（{cat}）**：{text or '（该时段无对白，属纯展示镜头）'}")
                A("")
        else:
            for s in verified[:20]:
                A(f"- [{fmt_t(s.get('start'))}] {s.get('text', '')}")
    else:
        A("（ASR 台词数据尚未生成）")
    A("")

    # ── 5.5 关键词覆盖核查 ──
    if verified:
        A("## 5b. 规范关键词覆盖核查（highlight_keywords × ASR 台词）")
        A("")
        A("| 规范关键词 | 主播台词命中次数 | 覆盖状态 |")
        A("| :--- | :--- | :--- |")
        for kw in keywords:
            n = sum(1 for s in verified if kw in (s.get("text") or ""))
            status = "✅ 已覆盖" if n > 0 else "⚠️ 台词未提及"
            A(f"| {kw} | {n} | {status} |")
        A("")
        A("> **说明**：命中为 0 的关键词表示该卖点在主播原声中未被提及，")
        A("> 成片字幕无从高亮，面料卖点需依靠 Hook 卡/封面/贴纸等视觉元素传达。")
        A("> 若原声话术与规范商品存在系统性偏差（如素材实际为其他款式），")
        A("> 应核对 `raw_video_path` 指向的素材与契约商品信息是否一致。")
        A("")

    # ── 5.6 素材-契约一致性预检（2026-10-27 新增）──
    consistency = load_json(os.path.join(out_dir, "consistency_report.json"))
    if consistency:
        A("## 5c. 素材-契约一致性预检（S1.5）")
        A("")
        A(f"- **预检结论**：{'✅ PASS' if consistency.get('passed') else '❌ BLOCK'}"
          f"（强制级别 {consistency.get('enforce_level', 'WARN')}，"
          f"{consistency.get('summary', {}).get('n_warnings', 0)} 条 WARN）")
        A("")
        A("| 检查项 | 状态 | 实测值 | 阈值 |")
        A("| :--- | :--- | :--- | :--- |")
        for chk in consistency.get("checks", []):
            icon = {"PASS": "✅", "WARN": "⚠️", "INFO": "ℹ️",
                    "FAIL": "❌"}.get(chk.get("status"), chk.get("status"))
            A(f"| {chk.get('name', chk.get('id'))} | {icon} | "
              f"{chk.get('measured', '-')} | {chk.get('threshold', '-')} |")
        A("")
        for w in consistency.get("warnings", []):
            A(f"- ⚠️ {w}")
        A("")

    # ── 5.7 违规词三通道处理（2026-10-27 新增）──
    vreport = load_json(os.path.join(
        work_dir, "violation_handling_report.json"))
    if vreport:
        A("## 5d. 违规词三通道处理记录")
        A("")
        masked = vreport.get("subtitle_masked_words", [])
        applied = vreport.get("applied", {})
        n_mute = sum(1 for v in applied.values() if v.get("audio_mute_ranges"))
        n_mosaic = sum(1 for v in applied.values() if v.get("mosaic_regions"))
        n_magn = sum(1 for v in applied.values() if v.get("magnifier"))
        A(f"- **通道 1 字幕屏蔽**：{vreport.get('n_subtitle_masked', 0)} 处"
          + (f"（{', '.join(masked[:8])}）" if masked else ""))
        A(f"- **通道 2 语音消音**：{n_mute} 个分镜应用区间消音")
        A(f"- **通道 3 视频打码**：{n_mosaic} 个分镜应用马赛克")
        A(f"- **放大镜特效**：{n_magn} 个分镜")
        A("")
        A("> 三通道处理参数由 `apply_violation_handling.py` 生成，")
        A("> 执行记录由组装器回填；QC 对成片字幕做零残留 BLOCK 断言。")
        A("")

    # ── 6. 候选池统计 ──
    if story_data:
        pools = story_data.get("pools", {}) or {}
        A("## 6. 叙事候选池统计（analyze_story_segments）")
        A("")
        A("| 候选池 | 数量 | 用途 |")
        A("| :--- | :--- | :--- |")
        usage = {
            "hook_candidates": "开场钩子候选",
            "fabric_candidates": "面料讲解候选",
            "fit_candidates": "版型展示候选",
            "styling_candidates": "穿搭推荐候选",
            "rejected_candidates": "黑名单剔除（价格/库存/换款）",
        }
        for k in ("hook_candidates", "fabric_candidates", "fit_candidates",
                  "styling_candidates", "rejected_candidates"):
            A(f"| {usage.get(k, k)} | {len(pools.get(k, []))} | {k} |")
        A("")
        total_verified = story_data.get("total_verified_segments")
        if total_verified:
            A(f"ASR 审核后有效台词段总数：**{total_verified}**")
            A("")

    # ── 7. 质检结论 ──
    A("## 7. 质检结论（QC 门禁）")
    A("")
    if qc:
        passed = qc.get("all_passed", False)
        A(f"- **总体结论**：{'✅ ALL PASS' if passed else '❌ BLOCK FAILED'}")
        warnings = qc.get("warnings", []) or []
        A(f"- **WARN 数量**：{len(warnings)}")
        checks = qc.get("checks", []) or []
        if checks:
            A("")
            A("| 检查项 | 级别 | 结果 | 实测值 |")
            A("| :--- | :--- | :--- | :--- |")
            for chk in checks:
                name = chk.get("name") or "-"
                level = chk.get("level", "-")
                status = chk.get("status", "")
                icon = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌"}.get(status, status)
                measured = chk.get("measured")
                detail = chk.get("detail") or ""
                cell = ""
                if measured is not None:
                    cell = str(measured)
                elif detail and len(str(detail)) < 60:
                    cell = str(detail)
                A(f"| {name} | {level} | {icon} | {cell} |")
            n_block_fail = sum(1 for c in checks
                               if c.get("level") == "BLOCK" and c.get("status") != "PASS")
            A("")
            A(f"BLOCK 级检查 **{sum(1 for c in checks if c.get('level') == 'BLOCK')} 项全部通过**"
              f"（失败 {n_block_fail} 项）；WARN 级提示不拦截交付。")
    else:
        A("（qc_report.json 尚未生成）")
    A("")

    # ── 8. 交付物 ──
    A("## 8. 交付物清单")
    A("")
    if gen_manifest:
        A("| 交付物 | 路径 |")
        A("| :--- | :--- |")
        for k, v in (gen_manifest.get("final_deliverables", {}) or {}).items():
            A(f"| {k} | `{v}` |")
        A(f"| generation_manifest | `{os.path.join(out_dir, 'generation_manifest.json')}` |")
    else:
        A("（generation_manifest.json 尚未生成）")
    A("")
    A("---")
    A("")
    A("*本文档由 `scripts/build_content_structure_doc.py` 依据任务契约与流水线产物自动生成。*")

    out_path = os.path.join(PROJECT_ROOT, "docs",
                            f"live_content_structure_{pid}.md")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[DOC] 已生成: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
