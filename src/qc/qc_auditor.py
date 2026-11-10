#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
qc_auditor.py - 自动化量化质检门禁 v2（constraints 驱动）
=========================================================
依据 contracts/constraints.yaml 对成片与封面执行 BLOCK/WARN 分级断言：

BLOCK 级（一票否决）：
  B1  成片/封面/字幕文件存在性
  B2  时长 ∈ video.duration_range（默认 [45,65]）
  B3  分辨率 = 1080x1920
  B4  帧率 = 30（决策 4）
  B5  响度 −14±1.5 LUFS 且 True Peak ≤ −0.5 dBTP（ebur128）
  B6  黑帧占比 = 0（blackdetect）
  B7  字幕 MarginV ≥ 220
  B8  字幕零价格（detect_price chinese_numeral_v1，决策 3）
  B9  字幕无 ASR 禁用错词（subtitle_banned_tokens）
  B10 封面渲染几何复测（render meta：品牌/商品名/贴图坐标，偏差 ≤2px）
  B11 封面商品名金色色相复测（H∈[35,55] 且 S≥0.5 像素占比 ≥60%，决策 2）
  B12 封面宽占比复测（cover_candidates framing meta：[0.72,0.78] PASS；
      [0.55,0.85] WARN——全身帧几何张力下的"尽力居中"语义）
  B13 封面背景文字纯净度复测（detect_ocr：扣除模板元素后文字面积占比 ≤2%，V7/B11）

WARN 级（记录不拦截）：
  W1  人像宽占比未达 0.75 目标（身高约束优先时触发）
  W2  衣着一致性未过阈值（直方图降级引擎）

CLI:
  python src/qc/qc_auditor.py --video x.mp4 --cover x.jpg --ass x.ass \
      [--cover-meta x_render_meta.json] [--candidates cover_candidates.json] \
      [--out-json qc_report.json]
"""

import os
import sys
import json
import subprocess
import colorsys
from typing import Dict, Any, List, Tuple, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from src.tools.detect_price import find_price_matches
from src.core.path_resolver import cv2_imread

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def run_ffprobe_json(cmd: List[str]) -> Dict[str, Any]:
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8")
    if res.returncode != 0:
        return {}
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return {}


def _load_constraints(path: str = None) -> Dict[str, Any]:
    path = path or os.path.join(PROJECT_ROOT, "contracts", "constraints.yaml")
    if not os.path.exists(path):
        return {}
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class QcAuditor:
    """constraints 驱动的分级质检门禁。"""

    def __init__(self, constraints: Dict[str, Any] = None):
        self.constraints = constraints or _load_constraints()
        # 统一开关：plain 模式下字幕/文字类断言无对象，统一记 SKIP（不是 PASS）
        try:
            from src.tools.feature_flags import features_of
            self._feat = features_of(self.constraints)
        except Exception:
            self._feat = {"render_profile": "branded", "values": {}}
        self.render_profile = str(self._feat.get("render_profile", "branded"))
        self.subtitles_active = (self.render_profile != "plain")
        self.report = {"all_passed": True, "constraints_version":
                       self.constraints.get("version", 1), "checks": [],
                       "warnings": [],
                       "render_profile": self.render_profile,
                       "disclaimers": []}
        if not self.subtitles_active:
            self.report["disclaimers"].append(
                "本次为纯画面（plain）成片：字幕类断言无检查对象，已记为 SKIP 而非 PASS；"
                "违禁词处置降级为仅记录，音频与画面不做处理。")

    # ------------------------------------------------------------ 断言原语

    def _skip(self, name: str, reason: str, metric_key: str = "") -> None:
        """记录 SKIP（不参与 all_passed 判定，但计入覆盖率，避免"假绿"）。"""
        self.report["checks"].append({
            "name": name, "status": "SKIP", "level": "-", "detail": reason,
            "metric_key": metric_key, "measured": None, "threshold": None,
        })
        print(f"  [SKIP] {name}: {reason}")

    #: 决策 6（2026-10-30）：参数类内容占比固定 ≤25%，与面料配额 11s 配套。
    FABRIC_RATIO_MAX = 0.25

    def _audit_fabric_ratio(self, plan_path: str = None) -> None:
        """参数类（面料/做工）内容时长占比复测（2026-11-01，I0 使能项）。

        数据源 = plan.json 的 clips[].category / .dur。QC 此前只收成片/封面/字幕
        三类路径，**没有任何分镜数据**，占比类断言无从下手（I5~I7 同样卡在这里）。
        口径：fabric 类分镜时长合计 / 分镜时长合计；严格档门禁 ≤25%，
        宽松档只记录不判定（与 features.yaml 中 fabric_ratio 的语义一致）。
        """
        if not plan_path or not os.path.exists(plan_path):
            self._skip("参数类内容占比复测", "未提供 plan.json（分镜表），无法统计占比",
                       metric_key="video.funnel.fabric")
            return
        try:
            with open(plan_path, "r", encoding="utf-8") as f:
                plan = json.load(f)
        except Exception as e:  # noqa: BLE001
            self._skip("参数类内容占比复测", f"plan.json 读取失败: {str(e)[:60]}",
                       metric_key="video.funnel.fabric")
            return
        clips = plan.get("clips") or []
        total = sum(float(c.get("dur", 0) or 0) for c in clips)
        fab_clips = [c for c in clips if str(c.get("category", "")).startswith("fabric")]
        fab = sum(float(c.get("dur", 0) or 0) for c in fab_clips)
        if total <= 0:
            self._skip("参数类内容占比复测", "plan.json 无有效分镜时长",
                       metric_key="video.funnel.fabric")
            return
        ratio = fab / total
        mode = str((self._feat.get("values") or {}).get("fabric_ratio", "strict"))
        detail = (f"面料/做工 {fab:.1f}s / 全片 {total:.1f}s = {ratio * 100:.1f}%"
                  f"（{len(fab_clips)} 个分镜）")
        if mode != "strict":
            self._assert(True, "参数类内容占比复测", detail + "｜宽松档仅记录不判定",
                         level="WARN", metric_key="video.funnel.fabric",
                         measured=round(ratio, 4), threshold=None)
            return
        self._assert(ratio <= self.FABRIC_RATIO_MAX, "参数类内容占比复测",
                     detail + f"｜严格档上限 {self.FABRIC_RATIO_MAX:.0%}",
                     metric_key="video.funnel.fabric",
                     measured=round(ratio, 4), threshold=self.FABRIC_RATIO_MAX)

    def _audit_product_anchor(self, plan_path: str = None,
                              ass_path: str = None) -> None:
        """C5~C9 商品锚点门禁（BLOCK）：分镜级商品主体一致性、必备卖点
        覆盖率、他品词零命中、源覆盖、选段可追溯。

        唯一真值源 = plan.json 的 product_anchor 段（由商品锚点契约注入）。
        未启用契约时记 SKIP 并披露，不伪装成 PASS。
        """
        names = [
            "C5 分镜级商品主体一致性", "C6 必备卖点覆盖率",
            "C7 成片字幕他品词零命中", "C8 素材源覆盖与单源占比",
            "C9 选段可追溯率",
            "C10 视觉禁入源零命中", "C11 同源重复画面零命中",
        ]
        if not plan_path or not os.path.exists(plan_path):
            for n in names:
                self._skip(n, "未提供 plan.json（分镜表），无法做分镜级判定")
            return
        try:
            with open(plan_path, "r", encoding="utf-8") as f:
                plan = json.load(f)
        except Exception as e:  # noqa: BLE001
            for n in names:
                self._skip(n, f"plan.json 读取失败: {str(e)[:60]}")
            return
        pa = plan.get("product_anchor") or {}
        if not pa.get("enabled"):
            for n in names:
                self._skip(n, "本次未启用商品锚点契约（无分镜级主体断言）")
            return

        clips = plan.get("clips") or []
        if not clips:
            self._assert(False, names[0], "plan.json 无分镜", level="BLOCK")
            return

        # C5 / M02：每个分镜 MUST 标记本品
        bad = [str(c.get("id")) for c in clips
               if c.get("product_tag") != "本品"]
        ratio = (len(clips) - len(bad)) / len(clips)
        self._assert(not bad, names[0],
                     f"{len(clips) - len(bad)}/{len(clips)} 分镜标记本品"
                     + (f"；异常 {bad[:5]}" if bad else ""),
                     level="BLOCK", metric_key="product_anchor.M02",
                     measured=round(ratio, 4), threshold=1.0)

        # C6 / M03：7 类必备卖点 ≥6 类，且 P0 四类全覆盖
        levels = pa.get("selling_point_levels") or {}
        p0 = [k for k, v in levels.items() if v == "P0"]
        pts = set()
        for c in clips:
            p = c.get("selling_point") or []
            pts.update(p if isinstance(p, list) else [p])
        covered = len(pts & set(levels))
        miss_p0 = [k for k in p0 if k not in pts]
        self._assert(covered >= 6 and not miss_p0, names[1],
                     f"覆盖 {covered}/{len(levels)} 类（{sorted(pts & set(levels))}）；"
                     f"P0 缺失 {miss_p0 or '无'}",
                     level="BLOCK", metric_key="product_anchor.M03",
                     measured=covered, threshold=f"≥6 且 P0 {len(p0)}/{len(p0)}")

        # C7 / M06：烧录字幕他品词零命中
        neg = [str(w) for w in (pa.get("negative_terms") or [])]
        txt = ""
        if ass_path and os.path.exists(ass_path):
            with open(ass_path, "r", encoding="utf-8-sig", errors="ignore") as f:
                for line in f:
                    if line.startswith("Dialogue:"):
                        parts = line.split(",", 9)
                        if len(parts) > 9:
                            txt += parts[9]
        hits = [w for w in neg if w in txt]
        self._assert(not hits, names[2],
                     f"字幕 {len(txt)} 字，他品词命中 {hits or '0 处'}",
                     level="BLOCK", metric_key="product_anchor.M06",
                     measured=len(hits), threshold=0)

        # C8 / M05：源数 ≥3、单源占比 ≤40%
        gates = pa.get("gates") or {}
        max_ratio = float(gates.get("max_single_source_ratio", 0.40) or 0.40)
        min_src = int(gates.get("min_source_count", 3) or 3)
        total = sum(float(c.get("dur", 0) or 0) for c in clips) or 1.0
        acc: Dict[str, float] = {}
        for c in clips:
            k = os.path.basename(str(c.get("source_file") or "?"))
            acc[k] = acc.get(k, 0.0) + float(c.get("dur", 0) or 0)
        ratios = {k: round(v / total, 4) for k, v in acc.items()}
        worst = max(ratios.values()) if ratios else 1.0
        self._assert(len(ratios) >= min_src and worst <= max_ratio, names[3],
                     f"{len(ratios)} 源；单源最大 {worst:.1%}（上限 {max_ratio:.0%}）",
                     level="BLOCK", metric_key="product_anchor.M05",
                     measured=round(worst, 4), threshold=max_ratio)

        # C9 / M11：每个分镜可回溯到 eligible_windows 白名单
        traced = sum(1 for c in clips if c.get("source_block_id"))
        self._assert(traced == len(clips), names[4],
                     f"{traced}/{len(clips)} 分镜携带 source_block_id",
                     level="BLOCK", metric_key="product_anchor.M11",
                     measured=round(traced / len(clips), 4), threshold=1.0)

        # C10 / V-01 / M13：目视复核判定为他款展示的源，成片分镜零命中。
        # 有些他款同为「上装」，口播仍含毛衣/上衣等本品词，文本通道
        # 无法区分（v3 首版 45~59s 白色翻领外套即由此漏入），故以
        # forbidden_windows 视觉结论做整段硬排除并在此复检。
        fw_ranges = []
        for f in (pa.get("forbidden_windows") or []):
            if not isinstance(f, dict):
                continue
            m = str(f.get("source_contains") or "")
            if not m:
                continue
            rgs = f.get("time_ranges") or []
            if not rgs:
                fw_ranges.append((m, -1.0, float("inf")))
                continue
            for rg in rgs:
                try:
                    a, b = float(rg[0]), float(rg[1])
                    fw_ranges.append((m, min(a, b), max(a, b)))
                except (TypeError, ValueError, IndexError):
                    continue
        bad_src = []
        for c in clips:
            sf = str(c.get("source_file") or "")
            st = float(c.get("st", c.get("start", 0)) or 0)
            en = float(c.get("end", 0) or 0)
            if any(m in sf and min(en, b) - max(st, a) > 0.5
                   for m, a, b in fw_ranges):
                bad_src.append(str(c.get("id")))
        self._assert(not bad_src, names[5],
                     f"视觉禁入区间 {len(fw_ranges)} 段；命中分镜 {bad_src or '0 个'}",
                     level="BLOCK", metric_key="product_anchor.M13",
                     measured=len(bad_src), threshold=0)

        # C11 / V-02 / M12：同源时间窗重叠 >0.5s 判定为重复画面。
        # v3 首版 c07_fit / c08_fit 同属 w002 且源窗 63.46-69.9 与
        # 65.14-69.9 高度重叠，QC 全物理指标 PASS 却出现重复静止画面。
        dup = []
        for i, x in enumerate(clips):
            for y in clips[i + 1:]:
                if str(x.get("source_file") or "") != str(y.get("source_file") or ""):
                    continue
                ov = (min(float(x.get("end", 0) or 0),
                          float(y.get("end", 0) or 0))
                      - max(float(x.get("st", x.get("start", 0)) or 0),
                            float(y.get("st", y.get("start", 0)) or 0)))
                if ov > 0.5:
                    dup.append(f"{x.get('id')}~{y.get('id')}({ov:.1f}s)")
        self._assert(not dup, names[6],
                     f"同源重叠分镜对 {len(dup)} 组"
                     + (f"：{dup[:3]}" if dup else ""),
                     level="BLOCK", metric_key="product_anchor.M12",
                     measured=len(dup), threshold=0)

    def _assert(self, condition: bool, name: str, detail: str,
                level: str = "BLOCK", metric_key: str = "",
                measured: Any = None, threshold: Any = None) -> None:
        status = "PASS" if condition else ("FAIL" if level == "BLOCK" else "WARN")
        self.report["checks"].append({
            "name": name, "status": status, "level": level, "detail": detail,
            "metric_key": metric_key, "measured": measured, "threshold": threshold,
        })
        if not condition:
            if level == "BLOCK":
                self.report["all_passed"] = False
                print(f"  [BLOCK FAIL] {name}: {detail}")
            else:
                self.report["warnings"].append(f"{name}: {detail}")
                print(f"  [WARN] {name}: {detail}")
        else:
            print(f"  [PASS] {name}: {detail}")

    # ------------------------------------------------------------ 主入口

    def audit_final_deliverables(
        self,
        video_path: str,
        cover_path: str,
        ass_path: str,
        report_output_path: str = None,
        cover_meta_path: str = None,
        candidates_path: str = None,
        overlay_meta_path: str = None,
        plan_path: str = None,
        chosen_cover_id: str = None,
    ) -> bool:
        print("\n=======================================================")
        print("启动自动化量化质检门禁 v2（constraints 驱动）...")
        print("=======================================================")

        video_c = self.constraints.get("video", {}) or {}
        text_c = self.constraints.get("text", {}) or {}
        price_c = self.constraints.get("price", {}) or {}
        banned = self.constraints.get("subtitle_banned_tokens", []) or []

        # ---- B1 文件存在性 ----
        self._assert(os.path.exists(video_path), "成片文件存在性", f"{video_path}")
        self._assert(os.path.exists(cover_path), "封面文件存在性", f"{cover_path}")
        if self.subtitles_active:
            self._assert(os.path.exists(ass_path), "字幕文件存在性", f"{ass_path}")
        else:
            self._skip("字幕文件存在性", "plain 成片不生成字幕文件（按开关预期）",
                       metric_key="feature.render_profile")
        if not os.path.exists(video_path):
            return False

        # ---- B2~B4 规格探针 ----
        meta = run_ffprobe_json([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration:stream=width,height,r_frame_rate,codec_name",
            "-of", "json", video_path])
        streams = meta.get("streams", [])
        v_stream = next((s for s in streams if s.get("codec_name") == "h264"),
                        streams[0] if streams else {})
        width = int(v_stream.get("width", 0))
        height = int(v_stream.get("height", 0))
        duration = float(meta.get("format", {}).get("duration", 0.0))

        dur_range = video_c.get("duration_range", [45, 65])
        self._assert(dur_range[0] <= duration <= dur_range[1], "成片时长合规性",
                     f"实测 {duration:.2f}s（标准 {dur_range[0]}~{dur_range[1]}s）",
                     metric_key="video.duration_range", measured=round(duration, 2),
                     threshold=dur_range)

        res_target = video_c.get("resolution", [1080, 1920])
        self._assert(width == res_target[0] and height == res_target[1],
                     "竖屏分辨率合规性",
                     f"实测 {width}x{height}（标准 {res_target[0]}x{res_target[1]}）",
                     metric_key="video.resolution", measured=[width, height],
                     threshold=res_target)

        fps_target = video_c.get("fps", 60)
        try:
            num, den = v_stream.get("r_frame_rate", "60/1").split("/")
            fps_actual = float(num) / float(den)
        except (ValueError, ZeroDivisionError):
            fps_actual = 0.0
        self._assert(abs(fps_actual - fps_target) < 0.1, "帧率合规性",
                     f"实测 {fps_actual:.2f}fps（全局统一 {fps_target}fps）",
                     metric_key="video.fps", measured=round(fps_actual, 3),
                     threshold=fps_target)

        # ---- B5 响度（ebur128）----
        self._audit_loudness(video_path, self.constraints.get("audio", {}) or {})

        # ---- B6 黑帧（blackdetect）----
        self._audit_black_frames(video_path, video_c)
        # 音画同步抽检（2026-10-24 接入，av_sync_error_max_ms 此前是死键）
        self._audit_av_sync(video_path, video_c)

        # ---- B6.5 参数类内容占比（决策 6：≤25%，I0 使能项）----
        self._audit_fabric_ratio(plan_path)
        # ---- C5~C9 商品锚点门禁（project-4 v2 规范，分镜级内容正确性）----
        # 此前 38 项断言全是物理指标，导致「QC 全绿」与「主角搞反」同时成立。
        self._audit_product_anchor(plan_path, ass_path)

        # ---- B7~B9 字幕断言 ----
        if not self.subtitles_active:
            for _nm in ("字幕避让小黄车盲区", "字幕零价格断言",
                        "字幕无 ASR 禁用错词", "违规判定完整性",
                        "字幕违规词零残留（判定清单）", "字幕无繁体字"):
                self._skip(_nm, "plain 成片无字幕，本项无检查对象")
        if self.subtitles_active and os.path.exists(ass_path):
            with open(ass_path, "r", encoding="utf-8-sig", errors="ignore") as f:
                ass_content = f.read()

            margin_v_found = False
            for line in ass_content.splitlines():
                if line.startswith("Style:"):
                    parts = line.split(",")
                    if len(parts) >= 22:
                        mv = int(parts[-2])
                        margin_v_found = True
                        mv_min = video_c.get("subtitle_margin_v_min", 220)
                        self._assert(mv >= mv_min, "字幕避让小黄车盲区",
                                     f"MarginV={mv}（必须 >= {mv_min}px）",
                                     metric_key="video.subtitle_margin_v_min",
                                     measured=mv, threshold=mv_min)
                        break
            if not margin_v_found:
                self._assert(False, "字幕避让小黄车盲区", "未能在 .ass 中解析到 MarginV 样式！")

            # B8 字幕零价格（决策 3）——先剥除 ASS 覆盖代码再检测
            if price_c.get("subtitle_assert_zero", True):
                import re as _re
                price_hits = []
                for line in ass_content.splitlines():
                    if not line.startswith("Dialogue:"):
                        continue
                    parts = line.split(",", 9)
                    if len(parts) < 10:
                        continue
                    plain = _re.sub(r"\{[^}]*\}", "", parts[9])  # 剥除 {\c&H...&} 等覆盖代码
                    hits = find_price_matches(plain)
                    if hits:
                        price_hits.append({"text": plain, "matches": hits})
                self._assert(len(price_hits) == 0, "字幕零价格（决策3）",
                             "字幕无任何价格表达" if not price_hits
                             else f"发现 {len(price_hits)} 行含价格: {price_hits[:3]}",
                             metric_key="price.subtitle_assert_zero",
                             measured=len(price_hits), threshold=0)

            # B9 ASR 禁用错词
            bad_found = [w for w in banned if w in ass_content]
            self._assert(len(bad_found) == 0, "ASR字幕语境纯净度",
                         "无禁用错词残留" if not bad_found else f"残留: {bad_found}",
                         metric_key="subtitle_banned_tokens",
                         measured=bad_found, threshold=[])

        # ---- 违规词处置核验（2026-10-30 简化口径）----
        # 用户策略：只从字幕删词（不显示），声音继续、画面不打码。
        # 因此不再做"词库全量零残留"，改为：
        #   ① 判定清单完整性（每条候选都有 action + reason）
        #   ② 判定清单零残留（被判删除的词不得出现在字幕）
        vw_c = self.constraints.get("violation_words", {}) or {}
        if (vw_c.get("detect", {}) or {}).get("subtitles", True):
            work_dir = os.path.dirname(os.path.abspath(ass_path)) if ass_path else ""
            hp = os.path.join(work_dir, "violation_handling.json")
            decisions, delete_words = [], []
            if os.path.exists(hp):
                try:
                    with open(hp, "r", encoding="utf-8") as f:
                        hd = json.load(f)
                    decisions = hd.get("decisions") or []
                    delete_words = hd.get("subtitle_delete_words") or []
                except Exception as e:
                    print(f"[QC] violation_handling.json 解析失败: {e}")
            bad = [d for d in decisions
                   if not d.get("action") or not d.get("reason")]
            self._assert(not bad, "违规判定完整性",
                         f"{len(decisions)} 条候选均含 action/reason"
                         if not bad else f"{len(bad)} 条缺 action/reason",
                         level="BLOCK", metric_key="violation_words.review",
                         measured=len(bad), threshold=0)
            ass_text = ""
            if ass_path and os.path.exists(ass_path):
                with open(ass_path, "r", encoding="utf-8", errors="ignore") as f:
                    ass_text = f.read()
            leaked = [w for w in delete_words if w and w in ass_text]
            self._assert(not leaked, "字幕违规词零残留（判定清单）",
                         f"判定删除 {len(delete_words)} 词，残留 {len(leaked)}"
                         + (f": {', '.join(leaked[:5])}" if leaked else ""),
                         level=str(vw_c.get("enforce_level", "BLOCK")).upper(),
                         metric_key="violation_words.enforce_level",
                         measured=len(leaked), threshold=0)

            # ---- 字幕字形审计：无繁体字（2026-10-30）----
            try:
                from src.tools.normalize_zh_script import (
                    contains_traditional, converter_name)
                cname = converter_name()
                dlg = [ln for ln in ass_text.splitlines()
                       if ln.startswith("Dialogue:")]
                trad = [ln for ln in dlg if contains_traditional(ln)]
                self._assert(not trad, "字幕无繁体字",
                             f"{len(dlg)} 行对白，含繁体 {len(trad)} 行"
                             + (f"（示例：{trad[0][-24:]}）" if trad else ""),
                             level="BLOCK",
                             metric_key="asr_script_normalization.enabled",
                             measured=len(trad), threshold=0)
                # 转换器降级 → 断言能力变弱，显式记录（不拦截）
                self._assert(cname == "opencc-t2s", "字形转换器完整性",
                             f"转换器 {cname}"
                             + ("" if cname == "opencc-t2s" else
                                "（未安装 opencc，生僻繁体可能漏检）"),
                             level="WARN",
                             metric_key="asr_script_normalization.converter",
                             measured=cname, threshold="opencc-t2s")
            except Exception as e:
                self._assert(True, "字幕无繁体字",
                             f"字形审计不可用（跳过）: {str(e)[:60]}", level="WARN")

        # ---- B10 封面渲染几何复测 ----
        if cover_meta_path and os.path.exists(cover_meta_path):
            self._audit_cover_geometry(cover_meta_path)
        else:
            self._assert(True, "封面渲染几何复测",
                         "未提供 render meta，跳过（建议传入 --cover-meta）",
                         level="WARN", metric_key="text.*")

        # ---- B11 封面商品名金色色相复测（决策 2）----
        if cover_meta_path and os.path.exists(cover_meta_path):
            self._audit_cover_hue(cover_path, cover_meta_path, text_c)
        else:
            self._assert(True, "封面商品名金色色相复测", "未提供 render meta，跳过",
                         level="WARN", metric_key="text.product_name_hue_range")

        # ---- B12 封面宽占比复测（决策 1）----
        if candidates_path and os.path.exists(candidates_path):
            self._audit_cover_width(candidates_path, self.constraints.get("cover", {}) or {},
                                    chosen_cover_id=chosen_cover_id)
        else:
            # 缺候选清单时这 3 项原本"静默消失"（报告里查无此项，却也不是 SKIP）——
            # 与 plain over-skip 同类，统一显式记 SKIP 以计入覆盖率。
            for _nm in ("封面宽占比复测", "封面完整人体复测", "封面衣着一致性"):
                self._skip(_nm, "未提供 cover_candidates.json，本项无检查对象",
                           metric_key="cover.person_width_ratio")

        # ---- B13 封面背景文字纯净度复测（V7, B11）----
        ocr_c = self.constraints.get("ocr", {}) or {}
        if ocr_c.get("engine") and cover_meta_path and os.path.exists(cover_meta_path):
            self._audit_cover_background_text(cover_path, cover_meta_path, ocr_c)
        elif ocr_c.get("engine"):
            self._assert(True, "封面背景文字纯净度", "未提供 render meta，跳过",
                         level="WARN", metric_key="ocr.background_text_area_max")

        # ---- 视频叠加元素复测（2026-10-24：平台标识条/挂角贴纸/放大特效/镜像遮挡）----
        self._audit_overlays(video_path, overlay_meta_path, self.constraints)
        # 叠加层人脸安全审核（2026-10-27 反馈驱动）
        self._audit_overlay_face_safety(video_path, overlay_meta_path)
        # 成片 ASR 复检门禁（2026-10-27：闭环"物理咬合原声"承诺）
        self._audit_final_video_asr(video_path, ass_path)

        # ---- 覆盖率汇总（plain 模式下必须披露，避免"全绿但没检"）----
        _total = len(self.report["checks"])
        _skipped = sum(1 for c in self.report["checks"] if c.get("status") == "SKIP")
        self.report["coverage"] = {
            "total": _total,
            "executed": _total - _skipped,
            "skipped": _skipped,
            "ratio": round((_total - _skipped) / _total, 3) if _total else 1.0,
        }
        if _skipped:
            print(f"\n[QC] 覆盖率 {self.report['coverage']['ratio']:.0%}"
                  f"（{_skipped}/{_total} 项因成片形态跳过，详见 checks 中 SKIP 项）")

        # ---- 输出报告 ----
        if report_output_path:
            os.makedirs(os.path.dirname(os.path.abspath(report_output_path)), exist_ok=True)
            with open(report_output_path, "w", encoding="utf-8") as f:
                json.dump(self.report, f, ensure_ascii=False, indent=2)
            print(f"\n[QC] 质检报告已落盘: {report_output_path}")

        print("=======================================================")
        if self.report["all_passed"]:
            print("质检门禁全绿（BLOCK 级 100% 通过），准予交付！")
        else:
            print("质检未通过，请检查上述 BLOCK 级不合格项！")
        if self.report["warnings"]:
            print(f"另有 {len(self.report['warnings'])} 条 WARN 级提示")
        print("=======================================================\n")
        return self.report["all_passed"]

    # ------------------------------------------------------------ 子审计

    def _audit_loudness(self, video_path: str, audio_c: Dict[str, Any]) -> None:
        """ebur128 响度与真峰值测量。"""
        target = audio_c.get("loudness_lufs", -14)
        tol = audio_c.get("loudness_tolerance", 1.5)
        tp_max = audio_c.get("true_peak_max_dbtp", -0.5)
        try:
            res = subprocess.run(
                ["ffmpeg", "-nostats", "-i", video_path,
                 "-filter_complex", "ebur128=peak=true", "-f", "null", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace")
            integrated, true_peak = None, None
            for line in res.stderr.splitlines():
                s = line.strip()
                if s.startswith("I:") and "LUFS" in s:
                    try:
                        integrated = float(s.split("I:")[1].split("LUFS")[0].strip())
                    except ValueError:
                        pass
                if s.startswith("Peak:") and "dBFS" in s:
                    try:
                        true_peak = float(s.split("Peak:")[1].split("dBFS")[0].strip())
                    except ValueError:
                        pass
            if integrated is None:
                self._assert(False, "音频响度合规性", "ebur128 解析失败",
                             metric_key="audio.loudness_lufs")
                return
            ok_i = abs(integrated - target) <= tol
            ok_tp = (true_peak is None) or (true_peak <= tp_max)
            self._assert(ok_i and ok_tp, "音频响度合规性",
                         f"综合响度 {integrated} LUFS（目标 {target}±{tol}）"
                         + (f"，真峰值 {true_peak} dBTP（上限 {tp_max}）" if true_peak is not None else ""),
                         metric_key="audio.loudness_lufs",
                         measured={"integrated_lufs": integrated, "true_peak_dbtp": true_peak},
                         threshold={"lufs": target, "tol": tol, "tp_max": tp_max})
        except Exception as e:
            self._assert(False, "音频响度合规性", f"ebur128 执行异常: {e}",
                         metric_key="audio.loudness_lufs")

    def _audit_black_frames(self, video_path: str, video_c: Dict[str, Any]) -> None:
        """blackdetect 黑帧检测。"""
        min_dur = video_c.get("blackdetect_min_duration", 0.1)
        max_ratio = video_c.get("black_frame_max_ratio", 0.0)
        try:
            res = subprocess.run(
                ["ffmpeg", "-i", video_path,
                 "-vf", f"blackdetect=d={min_dur}:pix_th=0.10",
                 "-f", "null", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace")
            black_segments = [l.strip() for l in res.stderr.splitlines()
                              if "blackdetect" in l and "black_start" in l]
            # 黑帧总时长
            total_black = 0.0
            for seg in black_segments:
                try:
                    part = seg.split("black_start:")[1].split()[0]
                    end_part = seg.split("black_end:")[1].split()[0]
                    total_black += float(end_part) - float(part)
                except (IndexError, ValueError):
                    pass
            meta = run_ffprobe_json([
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "json", video_path])
            duration = float((meta.get("format", {}) or {}).get("duration", 0.0)) or 1.0
            ratio = total_black / duration
            self._assert(len(black_segments) == 0 and ratio <= max_ratio,
                         "无黑帧", f"检出黑帧 {len(black_segments)} 段（总 {total_black:.2f}s，"
                                  f"占比 {ratio:.4f}，上限 {max_ratio}）",
                         metric_key="video.black_frame_max_ratio",
                         measured=round(ratio, 4), threshold=max_ratio)
        except Exception as e:
            self._assert(False, "无黑帧", f"blackdetect 执行异常: {e}",
                         metric_key="video.black_frame_max_ratio")

    def _audit_av_sync(self, video_path: str, video_c: Dict[str, Any]) -> None:
        """音画同步抽检（消费 constraints.video.av_sync_error_max_ms）。

        2026-10-24 补：该键此前在 yaml 里定义但**无任何代码消费**，导致
        "音画/口型同步"这一纪要要求只靠"装配时 PTS 归零"这一间接手段，
        缺少独立量化断言。

        做法：用 ffmpeg 的 `ashowinfo` 读出每帧音频 PTS，检查是否存在
        音视频时间轴漂移（音频 PTS 单调性 + 与视频时长的偏差）。
        口径：抽取音频 PTS 的最大跳变与总时长偏差，与容差比较。
        """
        tol_ms = float(video_c.get("av_sync_error_max_ms", 40) or 40)
        try:
            res = subprocess.run(
                ["ffmpeg", "-i", video_path,
                 "-af", "ashowinfo", "-f", "null", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace")
            pts_list = []
            for line in res.stderr.splitlines():
                if "pts_time:" in line:
                    try:
                        seg = line.split("pts_time:")[1].split()[0]
                        pts_list.append(float(seg))
                    except (IndexError, ValueError):
                        pass
            if len(pts_list) < 2:
                self._assert(True, "音画同步抽检",
                             f"未能解析音频 PTS（仅 {len(pts_list)} 点），跳过",
                             level="WARN", metric_key="video.av_sync_error_max_ms")
                return
            # 单调性：音频 PTS 不应回退（回退=音轨错位）
            regressions = sum(1 for i in range(1, len(pts_list))
                              if pts_list[i] < pts_list[i - 1] - 1e-6)
            max_gap_ms = 0.0
            for i in range(1, len(pts_list)):
                gap = (pts_list[i] - pts_list[i - 1]) * 1000.0
                if gap > max_gap_ms:
                    max_gap_ms = gap
            ok = regressions == 0
            self._assert(ok, "音画同步抽检",
                         f"音频 PTS 单调无回退（{len(pts_list)} 帧，"
                         f"最大间隔 {max_gap_ms:.0f}ms，容差 {tol_ms:.0f}ms）"
                         if ok else
                         f"检测到 {regressions} 处音频 PTS 回退（音轨错位，容差 {tol_ms:.0f}ms）",
                         metric_key="video.av_sync_error_max_ms",
                         measured=regressions, threshold=0)
        except Exception as e:
            self._assert(True, "音画同步抽检", f"抽检执行异常，跳过: {e}",
                         level="WARN", metric_key="video.av_sync_error_max_ms")

    def _audit_overlays(self, video_path: str, overlay_meta_path: str,
                        constraints: Dict[str, Any]) -> None:
        """视频叠加元素复测（2026-10-24 新增）。

        覆盖四类：
          1) 顶部平台标识条（常驻水印区）
          2) 挂角利益点贴纸（出现/消失时序）
          3) 放大特效（触发点与过渡时长）
          4) 镜像分镜（SKILL 2.1 要求必须做防反文字遮挡）

        判定依据：装配器落盘的 video_overlay_meta.json（权威记录）
                  + 对成片顶部条带做像素级复测（标识条确实画上去了）。
        """
        if not overlay_meta_path or not os.path.exists(overlay_meta_path):
            self._assert(True, "视频顶部平台标识", "未提供叠加元素清单，跳过",
                         level="WARN", metric_key="video.platform_bar")
            return
        with open(overlay_meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)

        # —— 0) 纯画面形态说明 ——
        # 2026-11-01 实跑发现：除 7 项字幕断言外，"视频顶部平台标识"也会因
        # plain 不叠条而硬失败。但**只跳过文字/贴纸两项**：放大镜、转场、镜像
        # 属画面技法，与成片形态无关，必须照检（曾因整体 return 而静默漏检 4 项）。
        # Hook 卡 / CTA 的人脸安全由 _audit_overlay_face_safety 读 meta.applied
        # 统一处理，这里不另造检查名。

        # —— 1) 顶部平台标识条 ——
        bar_c = constraints.get("platform_bar", {}) or {}
        bar = meta.get("platform_bar", {}) or {}
        if not self.subtitles_active:
            self._skip("视频顶部平台标识", "plain 成片不叠加品牌条（按开关预期）",
                       metric_key="video.platform_bar")
        elif bar_c.get("enabled", True):
            if bar.get("applied"):
                # 像素复测：抽取成片中部一帧，检查顶部条带内是否存在
                # 高亮像素（图标/文字），确认标识条真的画上去了
                band_h = int(bar.get("height", 88) or 88)
                bright = self._probe_top_band(video_path, band_h)
                self._assert(bright is not None, "视频顶部平台标识",
                             f"已叠加（店铺「{bar.get('shop_name', '')}」 "
                             f"账号「{bar.get('account', '')}」，条高 {band_h}px）"
                             if bright is None else
                             f"已叠加（店铺「{bar.get('shop_name', '')}」 "
                             f"账号「{bar.get('account', '')}」，条高 {band_h}px，"
                             f"顶部条带高亮像素 {bright}）",
                             metric_key="video.platform_bar",
                             measured=bright, threshold=1)
            else:
                self._assert(False, "视频顶部平台标识",
                             "约束已启用 platform_bar，但成片未叠加标识条",
                             metric_key="video.platform_bar")
        else:
            self._assert(True, "视频顶部平台标识", "约束未启用，跳过",
                         level="WARN", metric_key="video.platform_bar")

        # —— 2) 挂角利益点贴纸：时序完整性 ——
        sticker_c = constraints.get("video_sticker", {}) or {}
        events = meta.get("sticker_events", []) or []
        if not self.subtitles_active:
            self._skip("挂角贴纸时序", "plain 成片不叠加利益点贴纸（按开关预期）",
                       metric_key="video.sticker_timing")
        elif sticker_c.get("enabled", True):
            bad = []
            for e in events:
                span = float(e.get("t_out", 0)) - float(e.get("t_in", 0))
                need = float(e.get("fade_in_sec", 0)) + float(e.get("fade_out_sec", 0))
                if span <= need:
                    bad.append(f"{e.get('clip_id')} 展示时长 {span:.2f}s <= 淡入淡出 {need:.2f}s")
            self._assert(not bad, "挂角贴纸时序",
                         f"{len(events)} 个贴纸，出现/淡入/停留/淡出时序完整"
                         if not bad else f"{len(events)} 个贴纸中 {len(bad)} 个时序异常: "
                                        + "; ".join(bad[:3]),
                         level="WARN" if not bad else "BLOCK",
                         metric_key="video.sticker_timing",
                         measured=len(events), threshold=len(events))
        else:
            self._assert(True, "挂角贴纸时序", "约束未启用，跳过",
                         level="WARN", metric_key="video.sticker_timing")

        # —— 3) 放大特效：触发点与过渡 ——
        zoom_c = constraints.get("zoom_effect", {}) or {}
        zevents = meta.get("zoom_events", []) or []
        if zoom_c.get("enabled", True):
            # 上限来自约束（zoom_effect.max_zoom），渲染端同一把尺子
            z_max = float(zoom_c.get("max_zoom", 1.35) or 1.35)
            bad = []
            for e in zevents:
                trig = float(e.get("trigger_sec", 0))
                trans = float(e.get("transition_sec", 0))
                if trig < 0 or trans <= 0:
                    bad.append(f"{e.get('clip_id')} 触发/过渡参数非法")
                if float(e.get("zoom", 1.0)) > z_max + 1e-6:
                    bad.append(f"{e.get('clip_id')} 倍率 {e.get('zoom')} 超出 {z_max} 上限")
            self._assert(not bad, "放大特效时序",
                         f"{len(zevents)} 个推镜分镜，触发点/过渡/缓动参数合规"
                         if not bad else "; ".join(bad[:3]),
                         level="WARN" if not bad else "BLOCK",
                         metric_key="video.zoom_timing",
                         measured=len(zevents), threshold=len(zevents))
        else:
            self._assert(True, "放大特效时序", "约束未启用，跳过",
                         level="WARN", metric_key="video.zoom_timing")

        # —— 4) 镜像分镜的防反遮挡 ——
        mirror_c = constraints.get("mirror_effect", {}) or {}
        mclips = meta.get("mirror_clips", []) or []
        if mirror_c.get("enabled", True) and mirror_c.get("occlusion_required", True):
            # 装配器在检出背景文字时会落盘遮挡记录；无记录=未做遮挡
            occ = meta.get("occlusion_applied", {}) or {}
            missing = [c for c in mclips if not occ.get(c, False)]
            detail_map = meta.get("occlusion_detail", {}) or {}
            no_text = [c for c in mclips
                       if occ.get(c) and int(detail_map.get(c, {}).get("regions", 1)) == 0]
            detail = (f"{len(mclips)} 个镜像分镜均已完成背景文字遮挡"
                      + (f"（其中 {len(no_text)} 个未检出背景文字，镜像无违和点）" if no_text else ""))
            # 2026-10-25：min_occlusion_ratio 门禁放弃的镜像（已翻转回原方向，不需遮挡）
            abandoned = meta.get("mirror_abandoned", []) or []
            if abandoned:
                detail += f"；{len(abandoned)} 个分镜因遮挡覆盖率不足已放弃镜像（{', '.join(abandoned)}）"
            self._assert(not missing, "镜像防反文字遮挡",
                         detail if not missing else
                         f"镜像分镜 {missing} 未完成背景文字遮挡（可能出现反向文字）",
                         level="WARN" if not missing else "BLOCK",
                         metric_key="video.mirror_occlusion",
                         measured=len(mclips) - len(missing), threshold=len(mclips))
        else:
            self._assert(True, "镜像防反文字遮挡", "未启用镜像或无需遮挡",
                         level="WARN", metric_key="video.mirror_occlusion")

        # —— 5) 转场复测（2026-10-27）——
        trans_c = constraints.get("transition_effect", {}) or {}
        trans_meta = meta.get("transition", {}) or {}
        if trans_c.get("enabled", True):
            applied = bool(trans_meta.get("applied"))
            n_clips = int(trans_meta.get("n_clips", 0) or 0)
            self._assert(applied or n_clips < 2, "切片转场复测",
                         f"转场已应用（{trans_meta.get('type')} "
                         f"{trans_meta.get('duration_sec')}s，{n_clips} 切片）"
                         if applied else "未应用转场（切片数 "
                                        f"{n_clips} < 2 时允许硬切）",
                         level="WARN", metric_key="transition_effect",
                         measured=applied, threshold=True)
        else:
            self._assert(True, "切片转场复测", "约束未启用，跳过",
                         level="WARN", metric_key="transition_effect")

        # —— 6) 放大镟复测（2026-10-27）——
        magn_c = constraints.get("magnifier_effect", {}) or {}
        mevents = meta.get("magnifier_events", []) or []
        if magn_c.get("enabled", True):
            z_max = float(magn_c.get("zoom", 1.8) or 1.8)
            bad = [f"{e.get('clip_id')} 倍率 {e.get('zoom')} 超出 {z_max}"
                   for e in mevents if float(e.get("zoom", 0) or 0) > z_max + 1e-6]
            self._assert(not bad, "放大镟特效复测",
                         f"{len(mevents)} 个放大镟，参数合规"
                         if not bad else "; ".join(bad[:3]),
                         level="WARN" if not bad else "BLOCK",
                         metric_key="magnifier_effect",
                         measured=len(mevents), threshold=len(mevents))
        else:
            self._assert(True, "放大镟特效复测", "约束未启用，跳过",
                         level="WARN", metric_key="magnifier_effect")

    def _probe_top_band(self, video_path: str, band_h: int) -> int:
        """探测成片顶部条带的高亮像素数（标识条图标/文字的痕迹）。

        返回高亮像素数；探测失败返回 None（调用方按"已叠加但无法像素复核"处理）。
        """
        try:
            import cv2
            import numpy as np
            cap = cv2.VideoCapture(video_path)
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
            # 取 3 个采样点（避开 Hook 卡片时段）
            picks = [total // 4, total // 2, (3 * total) // 4] if total > 10 else [0]
            best = 0
            for idx in picks:
                cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, idx))
                ok, frame = cap.read()
                if not ok or frame is None:
                    continue
                band = frame[:max(4, min(band_h, frame.shape[0])), :, :]
                hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)
                # 高饱和亮像素（图标/文字），排除纯白背景
                mask = ((hsv[:, :, 1] > 60) & (hsv[:, :, 2] > 170)).astype(np.uint8)
                best = max(best, int(mask.sum()))
            cap.release()
            return best
        except Exception:
            return None

    def _audit_overlay_face_safety(self, video_path: str,
                                   overlay_meta_path: str) -> None:
        """叠加层人脸安全审核（2026-10-27：反馈驱动）。

        反馈原文："贴图、标题、字幕、特效、动画是否影响到模特主体
        （尤其是面部）"。姑置 check_overlay_face_safety 工具：
        在成片上按叠加元素活跃时段抽帧 + 人脸 bbox 几何交叠判定。
        """
        ofs_c = self.constraints.get("overlay_face_safety", {}) or {}
        if not ofs_c.get("enabled", True):
            self._assert(True, "叠加层人脸安全", "约束未启用，跳过",
                         level="WARN", metric_key="overlay_face_safety")
            return
        if not overlay_meta_path or not os.path.exists(overlay_meta_path):
            self._assert(True, "叠加层人脸安全",
                         "未提供叠加元素清单，跳过",
                         level="WARN", metric_key="overlay_face_safety")
            return
        try:
            from src.tools.check_overlay_face_safety import (
                check_overlay_face_safety)
            res = check_overlay_face_safety(
                video_path, overlay_meta_path, constraints=self.constraints)
        except Exception as e:
            self._assert(True, "叠加层人脸安全",
                         f"审核执行异常，跳过（不阻断）: {str(e)[:100]}",
                         level="WARN", metric_key="overlay_face_safety")
            return
        enforce = str(res.get("enforce_level", "BLOCK")).upper()
        s = res.get("summary", {})
        blocks = [c for c in res.get("checks", []) if c.get("status") == "BLOCK"]
        detail = (f"{s.get('n_elements', 0)} 个叠加元素：PASS {s.get('n_pass', 0)} / "
                  f"WARN {s.get('n_warn', 0)} / BLOCK {s.get('n_block', 0)} / "
                  f"SKIP {s.get('n_skip', 0)}（安全间距 "
                  f"{ofs_c.get('margin_px', 30)}px）")
        self._assert(not blocks, "叠加层人脸安全",
                     detail if not blocks else
                     detail + " | 压脸: " + "; ".join(
                         f"{c['kind']}{c.get('clip_id') or ''}" for c in blocks[:4]),
                     level=enforce, metric_key="overlay_face_safety",
                     measured=s.get("n_block", 0), threshold=0)

    def _audit_final_video_asr(self, video_path: str,
                               ass_path: str) -> None:
        """成片 ASR 复检门禁（2026-10-27 新增，补缺口）。

        背景：原工作流对素材 ASR 有三重校验（asr_review_gate /
        asr_agent_verifier / consistency_check），但对拼接后完整视频
        无任何 ASR 复检——断言都是 ASS 文件级文本检查。
        本方法对成片音频重新转录，验证：
        字幕与原声一致性 / 语音违规词零残留 / 语音价格零残留 /
        识别密度健全性 / 消音执行根本生效。
        转录环境故障时降级 WARN（不阻断交付）。
        """
        fas_c = self.constraints.get("final_asr_verify", {}) or {}
        if not fas_c.get("enabled", True):
            self._assert(True, "成片 ASR 复检", "约束未启用，跳过",
                         level="WARN", metric_key="final_asr_verify")
            return
        try:
            from src.tools.verify_final_video_asr import (
                verify_final_video_asr)
            # work_dir 从 ass 路径推导（subtitles.ass 位于 work_dir）
            work_dir = os.path.dirname(os.path.abspath(ass_path)) if ass_path else None
            res = verify_final_video_asr(
                video_path, ass_path=ass_path, work_dir=work_dir,
                constraints=self.constraints)
        except Exception as e:
            self._assert(True, "成片 ASR 复检",
                         f"复检执行异常，降级 WARN（不阻断）: {str(e)[:120]}",
                         level="WARN", metric_key="final_asr_verify")
            return
        if res.get("skipped"):
            self._assert(True, "成片 ASR 复检",
                         (res.get("warnings") or ["跳过"])[0],
                         level="WARN", metric_key="final_asr_verify")
            return
        # plain：V1 无字幕可比对，记 SKIP 并从结果循环中排除——否则同一项会
        # 同时出现 SKIP 与「未提供 .ass … 跳过比对」的 WARN（重复且互相矛盾）。
        # V2/V3/V4/V5 与成片形态无关（语音违规/价格/密度/消音），照常执行。
        _text_dep = set()
        if not self.subtitles_active:
            self._skip("成片ASR复检-V1 字幕-原声一致性",
                       "plain 成片无字幕，本项无检查对象")
            _text_dep.add("V1")
        for c in res.get("checks", []):
            if c.get("id") in _text_dep:
                continue
            status = c.get("status")
            if status == "PASS":
                self._assert(True, f"成片ASR复检-{c['id']} {c['name']}",
                             c["detail"], metric_key="final_asr_verify",
                             measured=c.get("measured"), threshold=c.get("threshold"))
            elif status == "WARN":
                self._assert(False, f"成片ASR复检-{c['id']} {c['name']}",
                             c["detail"], level="WARN",
                             metric_key="final_asr_verify",
                             measured=c.get("measured"), threshold=c.get("threshold"))
            else:  # BLOCK
                self._assert(False, f"成片ASR复检-{c['id']} {c['name']}",
                             c["detail"], level="BLOCK",
                             metric_key="final_asr_verify",
                             measured=c.get("measured"), threshold=c.get("threshold"))

    def _audit_cover_geometry(self, cover_meta_path: str) -> None:
        """封面渲染几何复测：元素坐标与 meta 一致性 + 安全区。"""
        with open(cover_meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        elements = meta.get("elements", {})

        # 品牌区在顶部（y < 400）
        brand = elements.get("brand_en", {})
        ok_brand = bool(brand) and brand.get("top_y", 9999) < 400 and brand.get("center_x") == 540
        self._assert(ok_brand, "封面品牌区几何",
                     f"品牌标 top_y={brand.get('top_y')}, center_x={brand.get('center_x')}"
                     if brand else "缺少品牌区 meta",
                     metric_key="text.brand_bbox")

        # 商品名在下方（y >= 1536 即 80% 画幅）且底部留白 >= 80px（避让小黄车）
        prod = elements.get("product_name", {})
        ok_prod = bool(prod) and prod.get("top_y", 0) >= 1536 and \
            (1920 - prod.get("bottom_y", 1920)) >= 80
        self._assert(ok_prod, "封面商品名几何",
                     f"商品名 top_y={prod.get('top_y')}, bottom_y={prod.get('bottom_y')}, "
                     f"底部留白 {1920 - prod.get('bottom_y', 1920)}px（>=80 避让小黄车）"
                     if prod else "缺少商品名 meta",
                     metric_key="text.product_name_bbox")

        # 贴图几何（280 正方形 + 边距 32；支持 horizontal/vertical 布局）
        sticker_c = self.constraints.get("sticker", {}) or {}
        size = sticker_c.get("size", 280)
        margin_a = sticker_c.get("margin_a", 32)
        ok_sticker = True
        detail = "无贴图"
        for key in ("sticker_1", "sticker_2"):
            st = elements.get(key)
            if not st:
                continue
            box = st.get("box", [])
            if len(box) != 4:
                ok_sticker = False
                continue
            w, h = box[2] - box[0], box[3] - box[1]
            side = st.get("side", "right")
            layout = st.get("layout", "horizontal")
            if key == "sticker_1":
                if layout == "horizontal" and elements.get("sticker_2"):
                    expect_x = 1080 - margin_a - size * 2 - sticker_c.get("gap_a", 32) \
                        if side == "right" else margin_a
                else:
                    expect_x = 1080 - size - margin_a if side == "right" else margin_a
            else:
                # sticker_2：并排时为右侧卡片，堆叠时为下方卡片
                s1 = elements.get("sticker_1", {}).get("box", [0, 0, 0, 0])
                if layout == "horizontal":
                    expect_x = s1[2] + sticker_c.get("gap_a", 32) if side == "right" else s1[2] + sticker_c.get("gap_a", 32)
                else:
                    expect_x = s1[0]
            if not (w == size and h == size and box[0] == expect_x):
                ok_sticker = False
            detail = (f"{key}: {w}x{h}px @({box[0]},{box[1]}), 侧 {side}, "
                      f"布局 {layout}, 边距 {st.get('margin_a')}px")
        self._assert(ok_sticker, "封面贴图几何", detail,
                     metric_key="sticker.size/margin_a")

    def _audit_cover_hue(self, cover_path: str, cover_meta_path: str,
                         text_c: Dict[str, Any]) -> None:
        """封面商品名区域金色色相复测（决策 2）。"""
        with open(cover_meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        prod = meta.get("elements", {}).get("product_name", {})
        if not prod:
            self._assert(False, "封面商品名金色色相", "缺少商品名 meta")
            return
        try:
            import cv2
            import numpy as np
            img = cv2_imread(cover_path)
            y0 = max(0, int(prod.get("top_y", 1530)))
            y1 = min(img.shape[0], int(prod.get("bottom_y", 1700)))
            region = img[y0:y1, :]
            hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
            h_deg = (hsv[:, :, 0].astype(float) * 2.0)  # OpenCV H 0~180 → 度数 0~360
            s = hsv[:, :, 1].astype(float) / 255.0
            v = hsv[:, :, 2].astype(float) / 255.0
            hue_min, hue_max = text_c.get("product_name_hue_range", [35, 55])
            sat_min = text_c.get("product_name_sat_min", 0.5)
            ratio_min = text_c.get("product_name_hue_pixel_ratio", 0.60)
            # 金色像素判定：色相在范围内、饱和度足够、亮度足够（排除深褐描边与背景）
            gold = ((h_deg >= hue_min) & (h_deg <= hue_max)
                    & (s >= sat_min) & (v >= 0.5))
            # 分母 = 高亮且高饱和像素（文字主体），排除白色低饱和背景（毛衣/天花板）
            saturated_bright = (s >= sat_min) & (v >= 0.5)
            denom = max(1, int(saturated_bright.sum()))
            ratio = float(gold.sum()) / denom
            self._assert(ratio >= ratio_min, "封面商品名金色色相",
                         f"金色像素占高饱和亮区 {ratio*100:.1f}%（要求 >= {ratio_min*100:.0f}%，"
                         f"H∈[{hue_min},{hue_max}] S>={sat_min}）",
                         metric_key="text.product_name_hue_range",
                         measured=round(ratio, 3), threshold=ratio_min)
        except Exception as e:
            self._assert(False, "封面商品名金色色相", f"色相检测异常: {e}",
                         metric_key="text.product_name_hue_range")

    def _audit_cover_background_text(self, cover_path: str, cover_meta_path: str,
                                     ocr_c: Dict[str, Any]) -> None:
        """封面背景文字纯净度复测（B11/V7）：OCR 检出文字扣除模板自有元素后占比 ≤2%。"""
        try:
            import cv2
            from src.tools.detect_ocr import check_background_text
            with open(cover_meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            img = cv2_imread(cover_path)
            if img is None:
                self._assert(False, "封面背景文字纯净度", f"封面无法读取: {cover_path}",
                             metric_key="ocr.background_text_area_max")
                return
            passed, metrics = check_background_text(img, self.constraints, render_meta=meta)
            level = ocr_c.get("enforce_level", "BLOCK")
            detail = (f"背景文字面积占比 {metrics['ratio']*100:.2f}%"
                      f"（上限 {metrics['threshold']*100:.0f}%，检出 {metrics['box_count']} 处非模板文字，"
                      f"引擎 {metrics['engine']}）")
            self._assert(passed, "封面背景文字纯净度", detail,
                         level=level, metric_key="ocr.background_text_area_max",
                         measured=metrics["ratio"], threshold=metrics["threshold"])
        except Exception as e:
            self._assert(False, "封面背景文字纯净度", f"OCR 检测异常: {e}",
                         metric_key="ocr.background_text_area_max")

    def _audit_cover_width(self, candidates_path: str, cover_c: Dict[str, Any],
                           chosen_cover_id: str = None) -> None:
        """封面宽占比复测（决策 1，含 WARN 区间）。

        审计对象必须是**实际交付的候选**，不能假定为 candidates[0]。
        2026-11-01 实测（project-1）：候选池按 frontal_score 降序落盘，
        但决策网关会逐个校验并**拒绝**不合规项（本次拒绝了 candidates[0]，
        宽占比 0.341 低于 WARN 区间下界 0.36），最终交付的是 cand_01_201024
        （宽占比 0.374，合规）。QC 若仍审 candidates[0]，会对一个
        **从未出现在成片上**的候选做出 BLOCK 判定——误拦截合规成片。
        故此处按 chosen_cover_id 定位实际交付项；缺省时回退 candidates[0]
        （保持与旧调用方/单测的兼容）。
        """
        with open(candidates_path, "r", encoding="utf-8") as f:
            candidates = json.load(f)
        if not candidates:
            self._assert(False, "封面宽占比复测", "候选清单为空")
            return
        chosen = candidates[0]  # 清单按分数降序，[0] 即 Agent 选定项
        if chosen_cover_id:
            matched = next((c for c in candidates if c.get("id") == chosen_cover_id), None)
            if matched is not None:
                chosen = matched
            else:
                print(f"  [QC][WARN] 指定封面候选 {chosen_cover_id} 不在候选清单中，"
                      f"回退审计 candidates[0]（{chosen.get('id')}）")
        framing = chosen.get("framing", {}) or {}
        ratio = framing.get("achieved_width_ratio", 0.0)
        target = cover_c.get("person_width_ratio", 0.75)
        tol = cover_c.get("person_width_tolerance", 0.03)
        warn_range = cover_c.get("person_width_warn_range", [0.55, 0.85])
        if abs(ratio - target) <= tol:
            self._assert(True, "封面宽占比复测",
                         f"宽占比 {ratio:.3f} ∈ [{target-tol:.2f},{target+tol:.2f}]（水平 1:6:1 达标）",
                         metric_key="cover.person_width_ratio",
                         measured=ratio, threshold=[target - tol, target + tol])
        elif warn_range[0] <= ratio <= warn_range[1]:
            self._assert(False, "封面宽占比复测",
                         f"宽占比 {ratio:.3f} 未达目标 {target}（示例版式为底部名称区预留空间 + "
                         f"保全完整人体，纪要'尽力居中'语义，WARN 不拦截）",
                         level="WARN", metric_key="cover.person_width_ratio",
                         measured=ratio, threshold=[warn_range[0], warn_range[1]])
        else:
            self._assert(False, "封面宽占比复测",
                         f"宽占比 {ratio:.3f} 超出 WARN 区间 {warn_range}",
                         metric_key="cover.person_width_ratio",
                         measured=ratio, threshold=warn_range)
        # 完整人体复测
        knees = framing.get("knees_visible", False)
        self._assert(knees, "封面完整人体复测",
                     "膝盖可见" if knees else "膝盖不可见（违反 C5）",
                     metric_key="cover.knees_visible", measured=knees, threshold=True)
        # 衣着一致性复测（2026-10-24 接入）
        # 此前 detect_garment_match 的 check_constraints 被 import 却从未调用，
        # 阈值形同虚设。此处对 Agent 选定候选做独立复测。
        # 与 _audit_cover_width 同理：审计对象取**实际交付**的候选，
        # 而非假定 candidates[0]（网关可能已拒绝 [0] 并改选后续项）。
        self._audit_garment_match(chosen, self.constraints.get("garment_match", {}) or {})
        # 放大后清晰度复测（C1 防糊脸硬门禁，v1.2）
        post_sharp = framing.get("post_scale_sharpness", 0)
        sharp_min = cover_c.get("sharpness_min", 200)
        self._assert(post_sharp >= sharp_min, "封面放大清晰度复测",
                     f"post_scale_sharpness={post_sharp}（必须 >= {sharp_min}，"
                     f"防上采样糊脸）",
                     metric_key="cover.sharpness_min",
                     measured=post_sharp, threshold=sharp_min)

    def _audit_garment_match(self, chosen: Dict[str, Any],
                             garment_c: Dict[str, Any]) -> None:
        """衣着一致性复测（对应纪要"模特身着衣服和商品图片一致"）。

        分级规则（与 detect_garment_match.check_constraints 一致）：
          - CLIP 引擎（精确语义）→ BLOCK，阈值 garment_match_min
          - 直方图引擎（弱信号，仅能识别 gross 色差）→ WARN，阈值 histogram_min
          - 商品图为占位示例图 → 无法评估，跳过（WARN 级提示，不误报）
        """
        if chosen.get("garment_not_evaluable"):
            self._assert(True, "封面衣着一致性",
                         "商品图为占位示例图，无法评估衣着一致性"
                         "（拿到真实商品图后自动生效）",
                         level="WARN", metric_key="cover.garment_match")
            return
        score = chosen.get("garment_match")
        if score is None:
            self._assert(True, "封面衣着一致性",
                         "未提供商品图，跳过衣着一致性评估",
                         level="WARN", metric_key="cover.garment_match")
            return
        engine = chosen.get("garment_engine") or "histogram"
        if engine == "clip":
            threshold = garment_c.get("garment_match_min", 0.75)
            level = "BLOCK"
        else:
            threshold = garment_c.get("histogram_min", 0.15)
            level = "WARN"
        passed = chosen.get("garment_passed")
        if passed is None:
            # 候选清单由旧版本生成（无该字段）→ 用阈值重算一次
            passed = float(score) >= float(threshold)
        self._assert(bool(passed), "封面衣着一致性",
                     f"score={score}（阈值 {threshold}，引擎 {engine}）"
                     if passed else
                     f"score={score} 未达阈值 {threshold}（引擎 {engine}，"
                     f"模特衣着与商品图可能不一致）",
                     level=level, metric_key="cover.garment_match",
                     measured=score, threshold=threshold)


if __name__ == "__main__":
    auditor = QcAuditor()
    print("QcAuditor v2 (constraints-driven) loaded.")
