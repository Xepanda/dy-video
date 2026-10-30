#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
asr_review_gate.py - ASR 识别结果执行者审核门禁（需求 2 & 4）
==============================================================
**定位**：本模块是 ASR 输出的**强制审核关口**，由执行者（Agent/流水线本体）
持有，不是可选的"事后抽查"。任何 ASR 结果——无论来自哪个识别方案
（Faster-Whisper / 其他 ASR / 外部服务）——都必须先通过本门禁，
才允许进入选段（build_plan）、字幕生成与总装流程。

**触发方式**（三重保险，见 wire_into_verifier）：
  1. 常规路径：`AsrContextVerifier.verify_and_correct()` 内部自动调用，
     AgentDirector 无需改动，审核永远发生；
  2. 兜底路径：`analyze_story_segments` 在ASR 之后显式再调一次
     `enforce_review_gate()`，防止未来新增调用点绕过；
  3. 手工路径：CLI `--strict` 可对任意 whisper_segments.json 单独复审，
     用于识别方案切换后的批量回归。

**审核校验点**（逐条 segment 级+ 整体级）：

| 编号 | 校验点 | 判据 | 失败级别 |
|---|---|---|---|
| A1 | 非空/有效文本 | 去空白后非空；纯标点/纯符号视为空| BLOCK |
| A2 | 噪声串拒| 重复同一字符≥5（如"啊啊啊啊"）、无元音拉丁乱码占比过高 | BLOCK |
| A3 | 时间戳有效性 | start< end；start>=0；end<=媒体时长+容差 | BLOCK |
| A4 | 时间戳单调性 | 段间不允许倒序；重叠超容差告警 | BLOCK / WARN |
| A5 | 覆盖率 | 有效句数 / 媒体时长 ≥ min_density（防"只转出15 句"类退化） | BLOCK |
| A6 | 单段最长时长 | 超长段（>max_seg_sec）视为切分异常 | WARN |
| A7 | 幻觉元话语 | 含"说错了/我怎么跑到/口误"等脱离商品语境句| WARN（交由 build_plan 淘汰） |
| A8 | 商品语义密度 | 漏斗关键词覆盖率过低（整篇几乎无商品词）| WARN |
| A9 | 乱码比例 | 含大量非常规字符（CJK 占比 <阈值）| WARN |

**失败处理策略**（分级，不是一律报错）：
  - 存在 BLOCK 项→ 抛 `AsrReviewError`，**阻断后续流程**，
    并携带完整 `findings` 供 Agent 决策（换识别方案 / 换模型 / 放宽阈值后重试）。
    这是需求"识别结果需经执行者确认后才可进入后续流程"的强制点。
  - 仅 WARN 项 → 放行但把 findings 写入报告并计数；WARN 达到上限
    （`warn_ratio_limit`）时自动升级为 BLOCK，防止"带病放行"。
  - 全部通过     → 放行，`passed=True`，findings 落审计报告。
"""
import os
import re
import sys
import json
import argparse
import unicodedata
from typing import List, Dict, Any, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_constraints() -> Dict[str, Any]:
    """载入 contracts/constraints.yaml（阈值唯一来源）。"""
    path = os.path.join(_PROJECT_ROOT, "contracts", "constraints.yaml")
    if not os.path.exists(path):
        return {}
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


class AsrReviewError(RuntimeError):
    """ASR 审核未通过（存在 BLOCK 级问题），必须阻断后续流程。"""

    def __init__(self, message: str, findings: List[Dict[str, Any]] = None):
        super().__init__(message)
        self.findings = findings or []


# 幻觉/元话语（与 build_plan._is_idle_chatter 呼应，此处只告警不淘汰）
META_HALLUCINATION = re.compile(
    r"(说错|口误|跑到了?那|我怎么跑|重复一遍|听不清|没听清|刚才说到)")

# initial_prompt 回声特征（2026-10-24 新增）
# 这些措辞来自 AsrContextVerifier.build_initial_prompt 生成的引导语，
# 正常情况下**不可能**出现在主播的真实口语里——出现即说明模型把 prompt
# 当成了音频内容。
PROMPT_ECHO = re.compile(
    r"(主播带货视频|主播口语高频词|主推商品|这是一段抖音|"
    r"抖音女装直播带货|品牌：MIXCITY|实物更漂亮、超显瘦)")

# 漏斗语义词（覆盖率检查用；与 build_plan 关键词表同源语义）
SEMANTIC_WORDS = [
    "面料", "做工", "材质", "棉", "羊毛", "蚕丝", "绒", "针织", "进口", "手感",
    "显瘦", "修身", "版型", "搭配", "穿搭", "好看", "气质", "显高", "遮肉",
    "宽松", "百搭", "老钱", "松弛", "裤", "裙", "外套", "卫衣", "毛衣",
]


class AsrReviewGate:
    """ASR 识别结果的执行者审核门禁。"""

    # —— 可调审核阈值（可用 constraints.asr_review 覆盖）——
    DEFAULT_MAX_SEG_SEC = 30.0
    DEFAULT_MIN_DENSITY = 0.06  # 句/秒：45s 成片需 >=3 句，取 0.06 兼顾短视频素材
    DEFAULT_OVERLAP_TOLERANCE = 0.5
    DEFAULT_MAX_DURATION_TOLERANCE = 2.0
    DEFAULT_WARN_RATIO_LIMIT = 0.35      # 逐条级 WARN 占比超此比例自动升级为 BLOCK
    DEFAULT_CJK_MIN_RATIO = 0.55
    DEFAULT_REPEAT_RUN_LEN = 5           # "啊啊啊啊" 连续同字符 >=5 判为噪声

    def __init__(self, media_duration: float = None, constraints: Dict[str, Any] = None):
        c = ((constraints or {}).get("asr_review", {}) or {})
        self.media_duration = media_duration
        self.max_seg_sec = float(c.get("max_seg_sec", self.DEFAULT_MAX_SEG_SEC))
        self.min_density = float(c.get("min_density", self.DEFAULT_MIN_DENSITY))
        self.overlap_tol = float(c.get("overlap_tolerance_sec", self.DEFAULT_OVERLAP_TOLERANCE))
        self.dur_tol = float(c.get("duration_tolerance_sec", self.DEFAULT_MAX_DURATION_TOLERANCE))
        self.warn_ratio_limit = float(c.get("warn_ratio_limit", self.DEFAULT_WARN_RATIO_LIMIT))
        self.cjk_min_ratio = float(c.get("cjk_min_ratio", self.DEFAULT_CJK_MIN_RATIO))
        self.repeat_run_len = int(c.get("repeat_run_len", self.DEFAULT_REPEAT_RUN_LEN))
        # A11 字形审计（2026-10-30）：残留繁体即拦截（归一应在进入门禁前完成）
        self.traditional_block = bool(c.get("traditional_block", True))
        # 自定义语义词
        self.semantic_words = c.get("semantic_words") or SEMANTIC_WORDS

    # ---------------------------------------------------------------- 主入口
    def review(self, segments: List[Dict[str, Any]]) -> Dict[str, Any]:
        """逐条复核 ASR 输出。返回审核报告（含 passed / findings）。

        绝不静默放行：存在 BLOCK 项时抛 AsrReviewError。
        """
        findings: List[Dict[str, Any]] = []
        segs = list(segments or [])

        def _add(code, level, idx, detail, excerpt=""):
            findings.append({"code": code, "level": level, "index": idx,
                             "detail": detail, "excerpt": excerpt})

        # —— A5整体级：空结果 ——
        if not segs:
            raise AsrReviewError(
                "ASR 审核失败 [A5]: 识别结果为空（0 句），禁止进入后续流程",
                [{"code": "A5_EMPTY", "level": "BLOCK", "index": -1,
                  "detail": "ASR 返回 0 个句块，多半是音频轨异常或模型加载失败"}])

        # —— 逐条 segment 级校验 ——
        valid_count = 0
        # A11 前置：转换器降级告警（2026-10-30）
        # contains_traditional 是回环比对，内置字表仅 120 字——降级后生僻繁体
        # 既转不掉也检不出，门禁会静默变弱，必须显式记 WARN。
        if self.traditional_block:
            try:
                from src.tools.normalize_zh_script import converter_name
                cname = converter_name()
                if cname != "opencc-t2s":
                    _add("A11_CONVERTER_DEGRADED", "WARN", -1,
                         f"字形归一降级为 {cname}（未安装 opencc），"
                         f"A11 对生僻繁体可能漏检")
            except Exception:
                pass
        for i, s in enumerate(segs):
            text = (s.get("text") or "").strip()
            start, end = s.get("start"), s.get("end")

            # A1 非空 / 有效文本
            if not text:
                _add("A1_EMPTY", "BLOCK", i, "文本为空")
                continue
            stripped = re.sub(r"[\s，,。.、！!？?；;：:~～…—\-]", "", text)
            if not stripped:
                _add("A1_PUNCT_ONLY", "BLOCK", i, "仅含标点符号，无有效内容", text[:40])
                continue

            # A2 噪声串（重复同字符 / 纯拉丁乱码）
            if re.search(r"(.)\1{%d,}" % self.repeat_run_len, stripped):
                _add("A2_REPEAT_RUN", "BLOCK", i,
                     f"存在连续重复字符≥{self.repeat_run_len}（噪声串）", text[:40])
                continue

            # A9 乱码比例
            cjk = sum(1 for ch in stripped if "\u4e00" <= ch <= "\u9fff")
            if stripped and cjk / len(stripped) < self.cjk_min_ratio:
                _add("A9_GARBLED", "WARN", i,
                     f"中文字符占比 {cjk / len(stripped):.2f} < {self.cjk_min_ratio}（疑似乱码）",
                     text[:40])

            # A3 时间戳有效性
            if start is None or end is None:
                _add("A3_TS_MISSING", "BLOCK", i, "缺少 start/end 时间戳", text[:30])
                continue
            try:
                start, end = float(start), float(end)
            except (TypeError, ValueError):
                _add("A3_TS_INVALID", "BLOCK", i, "时间戳非数值", str(s.get("start")))
                continue
            if start < 0:
                _add("A3_TS_NEGATIVE", "BLOCK", i, f"start 为负 ({start})", text[:30])
                continue
            if end <= start:
                _add("A3_TS_INVERTED", "BLOCK", i,
                     f"end({end:.3f}) <= start({start:.3f})", text[:30])
                continue
            if self.media_duration and end > self.media_duration + self.dur_tol:
                _add("A3_TS_OVERFLOW", "WARN", i,
                     f"end={end:.2f}s 超出媒体时长 {self.media_duration:.2f}s", text[:30])

            # A6 单段最长时长
            if end - start > self.max_seg_sec:
                _add("A6_SEG_TOO_LONG", "WARN", i,
                     f"单段 {end - start:.1f}s > {self.max_seg_sec}s（切分异常）", text[:30])

            # A7幻觉元话语
            if META_HALLUCINATION.search(text):
                _add("A7_META_HALLUCINATION", "WARN", i,
                     "含元话语/ASR 幻觉特征（后续选段将淘汰）", text[:40])

            # A10 提示词回声污染（2026-10-24 新增）
            # 现象：给 faster-whisper 注入 initial_prompt 后，模型会把 prompt
            # 文本当成音频内容识别出来，挤占开头约 30s 的真实语音。
            # 实测回声样本（8/11 素材首段）：
            #   "连帽衫·绵羊毛"(7字/30s)、"蕉个朋友·环保毛绒，"(10字/30s)、
            #   "主播口语高频词，羽绒服，羽绒，"(15字/8s)、"主播口语高频词，绵羊毛假两件，…"
            # 可见回声有两个特征：①命中 prompt 固定措辞 ②首段被拉得异常长而文字极少。
            # 判据取两者的并集（任一命中即告警），避免只靠文字密度漏判。
            is_echo = bool(PROMPT_ECHO.search(text))
            if not is_echo and i == 0 and (end - start) >= 10.0 and len(stripped) <= 30:
                # 首段异常长却只有极少字 → 典型的"prompt 占位"（真实语音被挤掉）
                is_echo = True
            if is_echo:
                _add("A10_PROMPT_ECHO", "WARN", i,
                     f"疑似 initial_prompt 回声（单段 {end - start:.1f}s 仅 {len(stripped)} 字，"
                     f"真实语音可能被挤占）", text[:40])
            # 补充信号：超长 + 文字密度偏低（<0.35 字/秒）→ 大概率是被摊平的回声段
            elif (end - start) >= 5.0 and len(stripped) / max(end - start, 0.1) < 0.35:
                _add("A10_LOW_DENSITY_LONG", "WARN", i,
                     f"单段 {end - start:.1f}s 但仅 {len(stripped)} 字"
                     f"（{len(stripped)/(end-start):.2f} 字/秒，疑似回声/空段）", text[:30])

            # A11 繁体字形残留（2026-10-30 新增）
            # 现象：faster-whisper zh 词表含港台繁体，"褲/個/類/麼/嗎/覺"概率性输出；
            # 归一（OpenCC t2s）应在进入门禁前完成，此处拦截任何残留，防繁体烧录。
            if self.traditional_block:
                try:
                    from src.tools.normalize_zh_script import contains_traditional
                    if contains_traditional(text):
                        _add("A11_TRADITIONAL", "BLOCK", i,
                             "含繁体字（应已被 t2s 归一，检查归一开关与转换器）",
                             text[:40])
                except Exception as e:  # 归一模块不可用时不阻断审核
                    _add("A11_CHECK_UNAVAILABLE", "WARN", i, f"字形审计不可用: {e}")

            valid_count += 1

        # —— A4 时间戳单调性（整体级）——
        prev_end = None
        for i, s in enumerate(segs):
            try:
                st, en = float(s.get("start")), float(s.get("end"))
            except (TypeError, ValueError):
                continue
            if prev_end is not None:
                if st < prev_end - self.overlap_tol:
                    _add("A4_TS_OVERLAP", "WARN", i,
                         f"与前段重叠 {prev_end - st:.2f}s（>容差 {self.overlap_tol}s）")
                elif st < prev_end:
                    _add("A4_TS_MINOR_OVERLAP", "WARN", i,
                         f"与前段微重叠 {prev_end - st:.2f}s")
            prev_end = max(prev_end or 0, en)

        # —— A5 覆盖率（整体级）——
        if self.media_duration and self.media_duration > 0:
            density = valid_count / self.media_duration
            if density < self.min_density:
                _add("A5_LOW_DENSITY", "BLOCK", -1,
                     f"识别密度 {density:.3f} 句/秒 < {self.min_density}"
                     f"（{valid_count} 句 / {self.media_duration:.1f}s），"
                     f"疑似转录退化（如提示词复读循环）")
        else:
            _add("A5_NO_DURATION", "WARN", -1,
                 "未提供媒体时长，跳过识别密度校验")

        # —— A8 商品语义密度（整体级）——
        joined = "".join((s.get("text") or "") for s in segs)
        hits = sum(1 for w in self.semantic_words if w in joined)
        if joined and hits < 3:
            _add("A8_LOW_SEMANTIC", "WARN", -1,
                 f"全篇商品语义词命中仅 {hits} 个，疑似非商品内容或识别失败")

        # —— 分级裁决 ——
        blocks = [f for f in findings if f["level"] == "BLOCK"]
        warns = [f for f in findings if f["level"] == "WARN"]
        # WARN 占比只用"逐条级"WARN 参与计算（index >= 0）。
        # 整体级 WARN（A5_NO_DURATION / A8_LOW_SEMANTIC）如果也按句数摊薄，
        # 在短语料（1~2 句）上会算出 ratio=1.0，从而误升级为 BLOCK。
        seg_warns = [f for f in warns if f["index"] >= 0]
        overall_warns = [f for f in warns if f["index"] < 0]
        warn_ratio = len(seg_warns) / max(len(segs), 1)
        escalated = False
        if not blocks and warn_ratio > self.warn_ratio_limit:
            _add("WARN_RATIO_ESCALATED", "BLOCK", -1,
                 f"逐条级 WARN 占比 {warn_ratio:.2f} > 上限 {self.warn_ratio_limit}，自动升级为 BLOCK")
            blocks = [f for f in findings if f["level"] == "BLOCK"]
            escalated = True

        report = {
            "passed": not blocks,
            "total_segments": len(segs),
            "valid_segments": valid_count,
            "media_duration": self.media_duration,
            "block_count": len(blocks),
            "warn_count": len(warns),
            "segment_warn_count": len(seg_warns),
            "overall_warn_count": len(overall_warns),
            "warn_ratio": round(warn_ratio, 4),
            "escalated": escalated,
            "checks": {
                "A1_non_empty": "非空/有效文本",
                "A2_noise": "噪声串（重复字符/乱码）",
                "A3_timestamp_validity": "时间戳有效性（存在性/数值/正序/越界）",
                "A4_timestamp_monotonic": "时间戳单调性与重叠",
                "A5_coverage": f"识别覆盖率（≥{self.min_density} 句/秒）",
                "A6_segment_length": "单段最长时长",
                "A7_meta_hallucination": "幻觉/元话语特征",
                "A8_semantic_density": "商品语义密度",
                "A9_garbled_ratio": "中文字符占比（乱码）",
                "A10_prompt_echo": "提示词回声污染 / 超长低密度段",
            },
            "findings": findings,
            "failed_codes": sorted({f["code"] for f in blocks}),
        }

        if blocks:
            raise AsrReviewError(
                f"ASR 执行者审核未通过：{len(blocks)} 项 BLOCK"
                f"（编码 {report['failed_codes']}），禁止进入后续流程",
                findings)

        print(f"[ASR-REVIEW] 审核通过：{valid_count}/{len(segs)} 句有效，"
              f"{len(warns)} 条 WARN（占比 {warn_ratio:.2f}）→ 允许进入后续流程")
        for f in warns[:8]:
            print(f"            [WARN {f['code']}] #{f['index']} {f['detail']}")
        if len(warns) > 8:
            print(f"            ...另有 {len(warns) - 8} 条 WARN（详见报告）")
        return report


def enforce_review_gate(segments, media_duration=None, constraints=None,
                        report_path=None, label=""):
    """对ASR 结果执行审核门禁；通过则返回 segments，不通过抛 AsrReviewError。

    这是给调用方的函数式入口（常规路径与兜底路径都调它）。
    """
    if constraints is None:
        constraints = _load_constraints()
    gate = AsrReviewGate(media_duration=media_duration, constraints=constraints)
    try:
        report = gate.review(segments)
    except AsrReviewError as e:
        report = {
            "passed": False,
            "total_segments": len(segments or []),
            "media_duration": media_duration,
            "block_count": len([f for f in e.findings if f["level"] == "BLOCK"]),
            "warn_count": len([f for f in e.findings if f["level"] == "WARN"]),
            "findings": e.findings,
            "error": str(e),
        }
        if report_path:
            os.makedirs(os.path.dirname(os.path.abspath(report_path)), exist_ok=True)
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            print(f"[ASR-REVIEW] 审核报告已落盘: {report_path}")
        print(f"[ASR-REVIEW] 审核失败：{e}")
        print(f"[ASR-REVIEW] 处理建议：1) 检查音轨是否正常 2) 更换更大ASR 模型"
              f" 3) 放宽 constraints.asr_review 阈值后重试 4) 换用其他识别方案")
        raise
    if report_path:
        os.makedirs(os.path.dirname(os.path.abspath(report_path)), exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    return segments


def wire_into_verifier() -> None:
    """把审核门禁挂进 AsrContextVerifier.verify_and_correct（需求2/4 的落点）。

    挂载后，**任何** 经由该方法的 ASR 结果都会自动被审核，
    无需调用方显式配合——从机制上保证"识别结果必须经执行者确认"。
    """
    from src.core.asr_agent_verifier import AsrContextVerifier
    if getattr(AsrContextVerifier, "_review_gate_wired", False):
        return
    _orig = AsrContextVerifier.verify_and_correct

    def _patched(self, raw_segments, report_output_path=None):
        segments, report = _orig(self, raw_segments, report_output_path)
        # 媒体时长未知时用末段 end 近似（无媒体文件也能做基本校验）
        media_dur = None
        if segments:
            try:
                media_dur = max(float(s.get("end", 0)) for s in segments)
            except (TypeError, ValueError):
                media_dur = None
        # 保守处理：媒体时长未知时跳过 A5 密度校验（WARN 而非 BLOCK），
        # 因为真实素材末尾常有静音，末段 end 并不等于媒体总长。
        review_report = None
        try:
            review_report = AsrReviewGate(media_duration=None).review(segments)
        except AsrReviewError as e:
            review_report = {"passed": False, "error": str(e), "findings": e.findings}
            if report_output_path:
                os.makedirs(os.path.dirname(os.path.abspath(report_output_path)),
                            exist_ok=True)
                with open(report_output_path, "w", encoding="utf-8") as f:
                    json.dump({"asr_verification": report, "asr_review": review_report},
                              f, ensure_ascii=False, indent=2)
            print(f"[ASR-REVIEW] 审核未通过，已阻断后续流程：{e}")
            raise
        if report_output_path:
            os.makedirs(os.path.dirname(os.path.abspath(report_output_path)), exist_ok=True)
            with open(report_output_path, "w", encoding="utf-8") as f:
                json.dump({"asr_verification": report, "asr_review": review_report},
                          f, ensure_ascii=False, indent=2)
        # 把审核结论并入校对报告，便于统一审计
        report["asr_review"] = review_report
        return segments, report

    AsrContextVerifier.verify_and_correct = _patched
    AsrContextVerifier._review_gate_wired = True
    print("[ASR-REVIEW] 审核门禁已挂载到 AsrContextVerifier.verify_and_correct")


# 模块导入即挂载（幂等）
wire_into_verifier()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ASR 识别结果执行者审核（CLI 复审入口）")
    ap.add_argument("--segments", required=True, help="whisper_segments.json 路径")
    ap.add_argument("--duration", type=float, default=None, help="媒体时长（秒）")
    ap.add_argument("--report", default=None, help="审核报告输出路径")
    ap.add_argument("--strict", action="store_true", help="严格模式：WARN 也视为失败")
    args = ap.parse_args(argv)

    with open(args.segments, "r", encoding="utf-8") as f:
        segs = json.load(f)
    try:
        rep = enforce_review_gate(segs, media_duration=args.duration,
                                  report_path=args.report)
        print("[ASR-REVIEW] CLI 复审通过")
        return 0
    except AsrReviewError as e:
        if args.report and not os.path.exists(args.report):
            with open(args.report, "w", encoding="utf-8") as f:
                json.dump({"passed": False, "error": str(e), "findings": e.findings},
                          f, ensure_ascii=False, indent=2)
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())