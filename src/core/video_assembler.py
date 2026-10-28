#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
video_assembler.py - FFmpeg 音画物理强锁组装引擎与动态图层合成
===============================================================
1. 物理强锁：视频与音频共享相同 PTS 时间戳，杜绝口型偏差；
2. 零缓存污染：每次组装自动清空旧切片缓存，杜绝历史片段串音串画；
3. 动态时间轴实测：ffprobe 回测实际切片时长，杜绝时钟漂移；
4. 滤镜复合混流：
   - 毫秒精准 ASS 发光变色字幕烧录；
   - 0.0s~2.5s 极简黄金 Hook 悬浮卡片贴片；
   - 片尾 2.5s 行动召唤 (CTA) 搭配推荐贴片；
   - 0.0s Boom 45-90Hz 重音冲击波混音。
"""

import os
import sys
import json
import subprocess
from typing import Dict, Any, List, Tuple, Optional
from PIL import Image, ImageDraw, ImageFont
from src.graphics.sfx_generator import generate_sub_bass_boom
from src.graphics.ass_subtitles import generate_ass_subtitles
from src.graphics.video_overlays import (
    generate_platform_bar, generate_stage_sticker,
    generate_occlusion_patch, generate_split_labels,
    generate_magnifier_mask, generate_magnifier_ring,
)


def _hex_to_rgb(hex_color: str, default=(255, 255, 255)) -> Tuple[int, int, int]:
    """#RRGGBB → (r,g,b)。"""
    h = (hex_color or "").strip().lstrip("#")
    if len(h) == 6:
        try:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
        except ValueError:
            return default
    return default

OUT_W, OUT_H, FPS = 1080, 1920, 60
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc" if os.name == "nt" else "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def run_cmd(cmd: List[str], cwd: str = None, desc: str = "") -> str:
    """运行子进程并实时捕获输出"""
    print(f"[FFMPEG] {desc or cmd[0]} ...", flush=True)
    res = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd
    )
    if res.returncode != 0:
        err_msg = res.stderr[-1500:] if res.stderr else "未知错误"
        raise RuntimeError(f"FFmpeg 执行失败 ({desc}):\n{err_msg}")
    return res.stdout


def ffprobe_get_duration(path: str) -> float:
    """使用 ffprobe 测定文件精确物理时长"""
    out = run_cmd([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path
    ], desc=f"测定时长 {os.path.basename(path)}")
    return float(out.strip())


def build_clip_filter(clip: Dict[str, Any], zoom_c: Dict[str, Any] = None) -> str:
    """构建单切片滤镜链：可选镜像 → 居中裁切 1080x1920 → 定时缓动推镜 → 帧率锁定 + PTS归零。

    支持的 clip 效果参数（plan.schema.json）：
      effect=hflip : 水平镜像（打破站位单一感）
      zoom>1.0     : 局部推镜特写（1.25~1.35x，锚点 y_anchor）
      effect=split : 由 render_clips 特殊处理（双输入 hstack），不经过本函数

    放大特效（2026-10-24 升级）：原先是一次性静态裁切（推镜立刻到位、无过渡），
    现改为**分镜内定时缓动推镜**——到达触发点后按 smoothstep 曲线推到目标倍率：
      触发点 = zoom_effect.trigger_sec（从分镜起点算）
      过渡   = zoom_effect.transition_sec（smoothstep 缓入缓出）
      锚点   = y_anchor（0.35h~0.65h，保证推镜时画面主体不跑位）
    触发点之前的画面保持原速静止构图，避免"一开始就动"抢注意力。
    """
    zoom_c = zoom_c or {}
    z = float(clip.get("zoom", 1.0) or 1.0)
    # 上限钳制：zoom_effect.max_zoom 是渲染与 QC 的共同硬上限。
    # 2026-10-25 接线：此前 max_zoom 只写在 constraints.yaml 里没有任何代码消费
    # （死键），改 yaml 不会改变行为；现在渲染端真实读取并钳制。
    z_max = float(zoom_c.get("max_zoom", 1.35) or 1.35)
    if z > z_max:
        z = z_max
    ya = float(clip.get("y_anchor", zoom_c.get("y_anchor", 0.35)) or 0.35)
    effect = clip.get("effect", "none")

    parts = []
    if effect == "hflip":
        parts.append("hflip")
    parts.append(f"crop={OUT_W}:{OUT_H}:(iw-{OUT_W})/2:0")
    parts.append(f"fps={FPS}")

    zoom_on = bool(zoom_c.get("enabled", True))
    if z > 1.0 and zoom_on:
        trig_f = int(round(float(zoom_c.get("trigger_sec", 0.8)) * FPS))
        trans_f = max(1, int(round(float(zoom_c.get("transition_sec", 1.2)) * FPS)))
        easing = str(zoom_c.get("easing", "smoothstep"))
        k = f"min(1,max(0,(on-{trig_f})/{trans_f}))"
        ease = f"pow({k},2)*(3-2*{k})" if easing == "smoothstep" else k
        # zoompan：d=1 保证 1 输入帧 = 1 输出帧，on 为本分镜内的输出帧号
        # x/y 公式保证 y_anchor 处的点在推镜过程中保持不动（主体不跑位）
        parts.append(
            f"zoompan=z='1+({z}-1)*{ease}'"
            f":x='iw/2-(iw/2/zoom)'"
            f":y='ih*{ya}-(ih*{ya}/zoom)'"
            f":d=1:s={OUT_W}x{OUT_H}:fps={FPS}"
        )
    elif z > 1.0:
        # 关闭放大特效时回退到静态裁切（保持旧行为）
        parts.append(
            f"crop=w=iw/{z}:h=ih/{z}:x=(iw-iw/{z})/2:y=(ih-ih/{z})*{ya},"
            f"scale={OUT_W}:{OUT_H}"
        )

    parts.append("setsar=1,setpts=PTS-STARTPTS")
    return ",".join(parts)


def generate_hook_card(text: str, out_path: str,
                       width: int = 720, height: int = 168) -> str:
    """生成前 2.5 秒黄金 Hook 悬浮卡片 (香槟金细边框 + 半透明深褐底)

    尺寸说明（2026-10-25 全局统一 720）：
      SKILL.md §1.2 原写"超大视觉标题卡"宽 940~980px、高 160~180px。
      2026-10-25 决策**全局统一为 720px 宽**（不再保留 940~980 口径）：
      1080 画幅上 940~980 只剩左右各 50px 边距，且与顶部平台标识条（高 88px）
      争抢顶部空间。高度沿用 168px，仍落在 SKILL 的 160~180 区间内，
      宽度足以承载 10~12 字卖点且不压边；纵向位置由 plan.hook_card.y 动态
      给出，装配前已自动避让人物头顶（实测 y=310 → 占据 310~478）。

      尺寸来源：contracts/constraints.yaml 的 `hook_card.width/height`
      （由 VideoAssembler 注入，改 yaml 即改画面）。
    """
    w, h = int(width), int(height)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 半透明深褐胶囊底色
    d.rounded_rectangle([0, 0, w, h], radius=26, fill=(35, 20, 15, 220), outline=(226, 200, 142, 255), width=3)
    f_card = ImageFont.truetype(FONT_BOLD, 40)
    bbox = f_card.getbbox(text)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((w - tw) // 2, (h - th) // 2 - 4), text, font=f_card, fill=(255, 235, 170, 255))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    img.save(out_path, "PNG")
    return out_path


def generate_cta_badge(text: str, out_path: str) -> str:
    """生成片尾 2.5 秒行动召唤 (CTA) 搭配推荐胶囊贴片"""
    w, h = 480, 100
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 高转化活力暖红底色
    d.rounded_rectangle([0, 0, w, h], radius=20, fill=(230, 45, 40, 235), outline=(255, 240, 200, 255), width=3)
    f_cta = ImageFont.truetype(FONT_BOLD, 28)
    bbox = f_cta.getbbox(text)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((w - tw) // 2, (h - th) // 2 - 2), text, font=f_cta, fill=(255, 255, 255, 255))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    img.save(out_path, "PNG")
    return out_path


class VideoAssembler:
    def __init__(self, work_dir: str, constraints: Dict[str, Any] = None):
        self.work_dir = os.path.abspath(work_dir)
        self.clips_dir = os.path.join(self.work_dir, "clips")
        self.constraints = constraints or {}
        self.occlusion_log: Dict[str, Any] = {}
        self.mirror_abandoned: set = set()
        os.makedirs(self.clips_dir, exist_ok=True)
        # 违规词三通道处理参数（apply_violation_handling 产出；缺失则直通）
        self.violation_handling = self._load_violation_handling()
        self.violation_report: Dict[str, Any] = {"applied": {}}
        # 放大镜素材缓存（clip_id -> (mask_png, ring_png, ring_size)）
        self._magnifier_assets: Dict[str, Any] = {}

    def _load_violation_handling(self) -> Dict[str, Any]:
        hp = os.path.join(self.work_dir, "violation_handling.json")
        if os.path.exists(hp):
            try:
                with open(hp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                s = data.get("summary", {})
                print(f"[VIOLATION] 已加载三通道处理参数: ASR命中 {s.get('n_asr_hits', 0)} 处 | "
                      f"消音分镜 {s.get('n_clips_with_mute', 0)} | "
                      f"打码分镜 {s.get('n_clips_with_mosaic', 0)}")
                return data
            except Exception as e:
                print(f"[WARN] violation_handling.json 加载失败（跳过违规处理）: {e}")
        return {"clips": {}, "summary": {}}

    # ------------------------------------------------ 2026-10-27 新增：违规处理辅助

    @staticmethod
    def _mute_filter(a: float, b: float, fade: float = 0.08) -> str:
        """生成单区间语音消音 volume 表达式（80ms 淡入出防爆音）。"""
        a, b, f = float(a), float(b), float(fade)
        expr = (
            f"if(between(t,{a:.3f},{a + f:.3f}),(t-{a:.3f})/{f:.3f},"
            f"if(between(t,{b - f:.3f},{b:.3f}),({b:.3f}-t)/{f:.3f},"
            f"if(between(t,{a + f:.3f},{b - f:.3f}),0,1)))"
        )
        return f"volume=volume='{expr}':eval=frame"

    def _get_magnifier_assets(self, clip: Dict[str, Any],
                              magn_c: Dict[str, Any]) -> Optional[Tuple[str, str, int]]:
        """生成（或复用）放大镜掩码 + 金属环 PNG。"""
        cid = clip["id"]
        if cid in self._magnifier_assets:
            return self._magnifier_assets[cid]
        m = clip.get("magnifier") or {}
        if not m.get("enabled"):
            return None
        r = int(m.get("r", magn_c.get("radius", 130)) or 130)
        cx = int(m.get("cx", OUT_W - int(magn_c.get("margin_x", 60)) - r))
        cy = int(m.get("cy", int(OUT_H * float(magn_c.get("anchor_y_ratio", 0.42)))))
        cx = max(r + 8, min(OUT_W - r - 8, cx))
        cy = max(r + 100, min(OUT_H - r - 260, cy))   # 避让顶部标识条与底部字幕区
        mask_png = os.path.join(self.work_dir, f"{cid}_magnifier_mask.png")
        ring_png = os.path.join(self.work_dir, f"{cid}_magnifier_ring.png")
        try:
            generate_magnifier_mask(mask_png, OUT_W, OUT_H, cx, cy, r)
            ring_png, ring_size = generate_magnifier_ring(
                ring_png, r,
                ring_width=int(magn_c.get("ring_width", 8) or 8),
                ring_color=str(magn_c.get("ring_color", "#E8A91E")))
        except Exception as e:
            print(f"[WARN] 放大镜素材生成失败 {cid}（跳过放大镜）: {e}")
            return None
        assets = (mask_png, ring_png, ring_size)
        self._magnifier_assets[cid] = assets
        return assets

    # ------------------------------------------------ 2026-10-24 新增：特效辅助

    def _make_split_labels(self, clip: Dict[str, Any],
                           split_c: Dict[str, Any]) -> str:
        """生成分屏左右两侧的独立胶囊标签 PNG（SKILL 2.3）。"""
        left = clip.get("split_label_left") or ""
        right = clip.get("split_label_right") or ""
        if not (left or right):
            return ""
        out = os.path.join(self.work_dir, f"{clip['id']}_split_labels.png")
        try:
            generate_split_labels(out, left, right, split_c,
                                  half_w=OUT_W // 2, height=OUT_H)
            return out
        except Exception as e:
            print(f"[WARN] 分屏标签生成失败（不阻断）: {e}")
            return ""

    def _apply_mirror_occlusion(self, clip: Dict[str, Any], clip_path: str) -> None:
        """镜像分镜：遮挡反向的背景文字（SKILL 2.1）。

        流程：抽一帧 → OCR 检测文字框 → 生成深色遮挡贴纸 → overlay 回该分镜。
        检测不到足够文字时不遮挡（保持画面干净），但会在日志里提示——
        决策层据此可在下一轮放弃该分镜的镜像。

        min_occlusion_ratio（2026-10-25 接线，此前是死键）：
          口径 = 镜像分镜**上半屏**可遮挡文字面积 / 上半屏画面面积。
          占比 **≥ 阈值** 判定为"背景文字密集"——镜像后大量店招/LOGO 反向，
          违规观感强，此时**放弃该分镜的镜像**（把画面翻转回原方向，不做遮挡贴纸）；
          占比 < 阈值说明背景文字零散，镜像后反向文字面积小，保留镜像并用贴纸精确遮挡。
        """
        mirror_c = self.constraints.get("mirror_effect", {}) or {}
        if not mirror_c.get("enabled", True):
            return
        try:
            import cv2
            from src.tools.detect_ocr import detect_text_boxes
        except Exception as e:
            print(f"[WARN] 镜像遮挡：OCR 模块不可用（跳过遮挡）: {e}")
            self.occlusion_log[clip["id"]] = {
                "applied": False, "error": "OCR 模块不可用"}
            return

        # 多帧采样（最多 3 帧，均匀分布）：单帧可能错过片中后段才出现的店招
        try:
            cap = cv2.VideoCapture(clip_path)
            n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
            sample_idx = ([0, n_frames // 2, n_frames - 1] if n_frames > 10 else [0])
            frames = []
            for idx in sample_idx:
                cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, idx))
                ok, fr = cap.read()
                if ok and fr is not None:
                    frames.append(fr)
            cap.release()
        except Exception as e:
            print(f"[WARN] 镜像遮挡：读帧失败（跳过）: {e}")
            self.occlusion_log[clip["id"]] = {
                "applied": False, "error": f"读帧失败: {str(e)[:80]}"}
            return

        if not frames:
            self.occlusion_log[clip["id"]] = {
                "applied": False, "error": "未能读取任何帧"}
            return

        ocr_c = self.constraints.get("ocr", {}) or {}
        boxes_raw = []
        try:
            for fr in frames:
                boxes_raw.extend(detect_text_boxes(
                    fr, engine=ocr_c.get("engine", "rapidocr"),
                    min_conf=float(ocr_c.get("min_confidence", 0.5))) or [])
        except Exception as e:
            print(f"[WARN] 镜像遮挡：文字检测失败（跳过）: {e}")
            self.occlusion_log[clip["id"]] = {
                "applied": False, "error": f"文字检测失败: {str(e)[:80]}"}
            return

        if not boxes_raw:
            print(f"[ASSEMBLER] 镜像分镜 {clip['id']}：未检出背景文字，无需遮挡贴纸")
            self.occlusion_log[clip["id"]] = {
                "applied": True, "regions": 0, "reason": "未检出背景文字（镜像无违和点）"}
            return

        # detect_text_boxes 返回 {"box": [[x,y]x4], "text", "score"}
        # 先算出外接矩形，再筛上半屏 + 面积门槛
        rects = []
        for b in boxes_raw:
            poly = b.get("box") or []
            if len(poly) < 2:
                continue
            xs = [float(p[0]) for p in poly]
            ys = [float(p[1]) for p in poly]
            rects.append({
                "x0": min(xs), "y0": min(ys),
                "x1": max(xs), "y1": max(ys),
                "text": b.get("text", ""),
            })
        if not rects:
            print(f"[ASSEMBLER] 镜像分镜 {clip['id']}：未检出背景文字，无需遮挡贴纸")
            self.occlusion_log[clip["id"]] = {
                "applied": True, "regions": 0, "reason": "未检出背景文字（镜像无违和点）"}
            return

        min_area = float(ocr_c.get("min_box_area_px", 400))
        # 只遮挡画面上半部分（背景店招/LOGO 区域），人物身上的印花不遮挡
        eligible = [r for r in rects
                    if (r["x1"] - r["x0"]) * (r["y1"] - r["y0"]) >= min_area]
        upper = [r for r in eligible if r["y0"] < OUT_H * 0.42]
        if not upper:
            print(f"[ASSEMBLER] 镜像分镜 {clip['id']}：上半屏无背景文字，无需遮挡")
            self.occlusion_log[clip["id"]] = {
                "applied": True, "regions": 0, "reason": "上半屏无背景文字（镜像无违和点）"}
            return

        # —— min_occlusion_ratio 门禁（2026-10-25 接线，此前是死键）——
        # 口径：**可遮挡文字面积 / 全部有效文字面积**（遮挡覆盖率）。
        # 覆盖率过低说明镜面后的大部分反向文字位于可遮挡范围之外（如下半身服饰印花、
        # 字幕带），贴纸遮不全 → 保留镜像必然露出反向文字，此时**放弃该分镜的镜像**
        # （把画面翻转回原方向），而不是贴一个遮不全的补丁。
        min_ratio = float(mirror_c.get("min_occlusion_ratio", 0.25) or 0.25)
        total_area = sum((r["x1"] - r["x0"]) * (r["y1"] - r["y0"]) for r in eligible)
        upper_area = sum((r["x1"] - r["x0"]) * (r["y1"] - r["y0"]) for r in upper)
        coverage = (upper_area / total_area) if total_area > 0 else 1.0
        if coverage < min_ratio:
            reason = (f"遮挡覆盖率 {coverage:.2f} < 阈值 {min_ratio:.2f}"
                      f"（反向文字遮不全，放弃镜像）")
            print(f"[ASSEMBLER] 镜像分镜 {clip['id']}：{reason}")
            self._undo_mirror(clip, clip_path)
            self.occlusion_log[clip["id"]] = {
                "applied": False, "regions": 0,
                "occlusion_coverage": round(coverage, 4),
                "min_occlusion_ratio": min_ratio,
                "reason": reason}
            self.mirror_abandoned.add(clip["id"])
            return

        # 归一化为 (x0,y0,x1,y1) 并适度外扩，覆盖文字描边
        boxes = []
        for r in upper:
            pad = 10
            boxes.append((max(0, r["x0"] - pad), max(0, r["y0"] - pad),
                          min(OUT_W, r["x1"] + pad), min(OUT_H, r["y1"] + pad)))

        patch = os.path.join(self.work_dir, f"{clip['id']}_occlusion.png")
        try:
            generate_occlusion_patch(patch, OUT_W, OUT_H, boxes, mirror_c)
        except Exception as e:
            print(f"[WARN] 镜像遮挡：贴纸生成失败（跳过）: {e}")
            return

        tmp_out = clip_path + ".occ.mp4"
        try:
            run_cmd([
                "ffmpeg", "-y",
                "-i", clip_path,
                "-i", patch,
                "-filter_complex", "[0:v][1:v]overlay=0:0:alpha=premultiplied[v]",
                "-map", "[v]", "-map", "0:a",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "copy",
                tmp_out,
            ], desc=f"镜像文字遮挡 {clip['id']}（{len(boxes)} 处）")
            os.replace(tmp_out, clip_path)
            print(f"[ASSEMBLER] 镜像分镜 {clip['id']}：已遮挡 {len(boxes)} 处背景文字")
            self.occlusion_log[clip["id"]] = {
                "applied": True, "regions": len(boxes),
                "boxes": [list(b) for b in boxes],
            }
        except Exception as e:
            print(f"[WARN] 镜像遮挡：叠加失败（保留原片）: {e}")
            if os.path.exists(tmp_out):
                try:
                    os.remove(tmp_out)
                except OSError:
                    pass
            self.occlusion_log[clip["id"]] = {"applied": False, "error": str(e)[:120]}

    def _undo_mirror(self, clip: Dict[str, Any], clip_path: str) -> None:
        """把已渲染的镜像分镜翻转回原方向（放弃镜像）。

        原理：滤镜链 hflip → crop(居中) → zoompan(x 居中) 在水平方向对称，
        而 hflip 是对合变换（做两次等于没做），因此对成片再补一次 hflip
        即可得到与"从未镜像"完全一致的画面，无需回源重切。
        """
        tmp_out = clip_path + ".unflip.mp4"
        try:
            run_cmd([
                "ffmpeg", "-y",
                "-i", clip_path,
                "-vf", "hflip",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "copy",
                tmp_out,
            ], desc=f"放弃镜像 {clip['id']}（翻转回原方向）")
            os.replace(tmp_out, clip_path)
            print(f"[ASSEMBLER] 镜像分镜 {clip['id']}：已放弃镜像，翻转回原方向")
        except Exception as e:
            print(f"[WARN] 放弃镜像失败（保留镜像画面）: {e}")
            if os.path.exists(tmp_out):
                try:
                    os.remove(tmp_out)
                except OSError:
                    pass

    def _measure_loudness(self, concat_txt: str, boom_wav: str,
                          total_dur: float) -> dict:
        """两遍 loudnorm 的第一遍：测量混合音频的 EBU R128 参数。

        返回 loudnorm print_format=json 的测量字典；失败返回 {}（调用方降级单遍）。
        """
        import json as _json
        import re as _re
        fc_m = (
            "[0:a][1:a]amix=inputs=2:duration=first:weights='1.0 1.0'[amix];"
            "[amix]loudnorm=I=-14:TP=-0.5:LRA=11:print_format=json[aout]"
        )
        try:
            res = subprocess.run(
                ["ffmpeg", "-hide_banner", "-nostats",
                 "-f", "concat", "-safe", "0", "-i", concat_txt,
                 "-i", boom_wav,
                 "-filter_complex", fc_m,
                 "-map", "[aout]", "-f", "null", "-"],
                cwd=self.work_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", timeout=120)
            m = _re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", res.stderr, _re.DOTALL)
            if not m:
                print("[LOUDNORM] 测量输出解析失败，降级单遍动态模式")
                return {}
            measured = _json.loads(m.group(0))
            print(f"[LOUDNORM] 两遍归一化测量: I={measured.get('input_i')} LUFS, "
                  f"TP={measured.get('input_tp')} dBTP, LRA={measured.get('input_lra')}")
            return measured
        except Exception as e:
            print(f"[LOUDNORM] 测量异常（{e}），降级单遍动态模式")
            return {}

    def _extract_clip(self, c: Dict[str, Any], src: str,
                      out_clip: str) -> None:
        """抽取单个分镜：主滤镜链 + 放大镜 + 违规消音 + 违规打码。

        简单路径（无放大镜/打码）：-vf 主链 + -af 消音链（旧行为兼容）。
        复合路径：-filter_complex（掩码 alphamerge + 像素化 overlay）。
        """
        dur = float(c.get("dur", 0))
        magn_c = self.constraints.get("magnifier_effect", {}) or {}
        handling = ((self.violation_handling.get("clips", {}) or {})
                    .get(c["id"], {}) or {})
        mute_ranges = handling.get("audio_mute", []) or []
        mosaic_regions = handling.get("video_mosaic", []) or []

        magnifier_on = bool(magn_c.get("enabled", True)) and bool(
            (c.get("magnifier") or {}).get("enabled"))
        assets = self._get_magnifier_assets(c, magn_c) if magnifier_on else None
        if magnifier_on and assets is None:
            magnifier_on = False

        # —— 音频链：消音表达式（相对分镜本地时间）——
        af_chain = ["asetpts=PTS-STARTPTS"]
        for (a, b) in mute_ranges:
            af_chain.append(self._mute_filter(a, b))

        # —— 简单路径 -—
        if not magnifier_on and not mosaic_regions:
            vf = build_clip_filter(c, self.constraints.get("zoom_effect", {}) or {})
            run_cmd([
                "ffmpeg", "-y",
                "-ss", f"{c['st']:.3f}",
                "-i", src,
                "-t", f"{dur:.3f}",
                "-avoid_negative_ts", "make_zero",
                "-vf", vf,
                "-af", ",".join(af_chain),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                out_clip
            ], desc=f"切片抽取 {c['id']} ({dur}s, st={c['st']:.2f}s)")
            self.violation_report["applied"][c["id"]] = {
                "audio_mute_ranges": mute_ranges, "mosaic_regions": 0,
                "magnifier": False,
            }
            return

        # —— 复合路径：filter_complex -—
        main_vf = build_clip_filter(c, self.constraints.get("zoom_effect", {}) or {})
        fc: List[str] = [f"[0:v]{main_vf}[base]"]
        cur = "base"
        idx = 0

        # 违规打码（像素化）：crop → scale 缩小 → scale 放大(neighbor) → overlay 回原位
        for reg in mosaic_regions:
            idx += 1
            x0, y0, x1, y1 = [int(v) for v in reg["box"][:4]]
            w, h = max(8, x1 - x0), max(8, y1 - y0)
            t0, t1 = float(reg["t0"]), float(reg["t1"])
            fc.append(f"[{cur}]split=2[mo{idx}a][mo{idx}b]")
            fc.append(
                f"[mo{idx}b]crop=w={w}:h={h}:x={x0}:y={y0},"
                f"scale=18:18,scale={w}:{h}:flags=neighbor[mbl{idx}]")
            fc.append(
                f"[mo{idx}a][mbl{idx}]overlay={x0}:{y0}"
                f":enable='between(t,{t0:.3f},{t1:.3f})'[mo{idx}o]")
            cur = f"mo{idx}o"

        # 放大镜圆圈
        magnifier_applied = False
        if magnifier_on and assets:
            mask_png, ring_png, ring_size = assets
            m = c["magnifier"]
            r = int(m.get("r", magn_c.get("radius", 130)) or 130)
            cx = int(m.get("cx", 0))
            cy = int(m.get("cy", 0))
            cx = max(r + 8, min(OUT_W - r - 8, cx))
            cy = max(r + 100, min(OUT_H - r - 260, cy))
            z = float(m.get("zoom", magn_c.get("zoom", 1.8)) or 1.8)
            t_in = float(m.get("t_in", 0.3))
            t_out = float(m.get("t_out", max(t_in + 0.5, dur - 0.3)))
            fade = float(magn_c.get("fade_in_sec", 0.35) or 0.35)
            ax = cx / float(OUT_W)
            ay = cy / float(OUT_H)
            in_mask, in_ring = 1, 2   # 输入序号：0=源视频，1=掩码，2=金属环
            fc.append(f"[{cur}]split=2[mgA][mgB]")
            fc.append(
                f"[mgB]crop=w='iw/{z:.3f}':h='ih/{z:.3f}'"
                f":x='(iw-iw/{z:.3f})*{ax:.5f}':y='(ih-ih/{z:.3f})*{ay:.5f}',"
                f"scale={OUT_W}:{OUT_H}:flags=lanczos[mgz]")
            fc.append("[mgz]format=rgba[mgzr]")
            fc.append(
                f"[{in_mask}:v]format=rgba,"
                f"fade=t=in:st={t_in:.3f}:d={fade:.3f}:alpha=1,"
                f"fade=t=out:st={max(t_in, t_out - fade):.3f}:d={fade:.3f}:alpha=1[mgmask]")
            fc.append("[mgzr][mgmask]alphamerge[mgm]")
            fc.append(
                f"[mgA][mgm]overlay=0:0"
                f":enable='between(t,{t_in:.3f},{t_out:.3f})'[mgA2]")
            fc.append(
                f"[{in_ring}:v]format=rgba,"
                f"fade=t=in:st={t_in:.3f}:d={fade:.3f}:alpha=1,"
                f"fade=t=out:st={max(t_in, t_out - fade):.3f}:d={fade:.3f}:alpha=1[mgring]")
            ring_x = max(0, cx - ring_size // 2)
            ring_y = max(0, cy - ring_size // 2)
            fc.append(
                f"[mgA2][mgring]overlay={ring_x}:{ring_y}"
                f":enable='between(t,{t_in:.3f},{t_out:.3f})'[mgout]")
            cur = "mgout"
            magnifier_applied = True

        fc.append(f"[0:a]{','.join(af_chain)}[aout]")
        # ⚠️ -t 位置：输入侧 -ss/-t 必须在 -i 之前（默认属于该输入）；
        # 输出侧 -t 放在所有输入之后。若把输出侧 -t 写在下一个 -i 之前，
        # 它会被当作那个输入的选项（已实测：对本切片无效，对掩码 PNG 生效）。
        cmd = ["ffmpeg", "-y",
               "-ss", f"{c['st']:.3f}", "-t", f"{dur:.3f}",
               "-i", src,
               "-avoid_negative_ts", "make_zero"]
        if magnifier_applied:
            cmd += ["-loop", "1", "-framerate", str(FPS),
                    "-t", f"{dur:.3f}", "-i", mask_png,
                    "-loop", "1", "-framerate", str(FPS),
                    "-t", f"{dur:.3f}", "-i", ring_png]
        cmd += ["-filter_complex", ";".join(fc),
                "-map", "[mgout]" if magnifier_applied else f"[{cur}]",
                "-map", "[aout]",
                "-t", f"{dur:.3f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                out_clip]
        run_cmd(cmd, desc=f"复合特效抽取 {c['id']} "
                          f"(放大镜={magnifier_applied}, 打码={len(mosaic_regions)})")
        self.violation_report["applied"][c["id"]] = {
            "audio_mute_ranges": mute_ranges,
            "mosaic_regions": len(mosaic_regions),
            "magnifier": magnifier_applied,
        }

    def render_clips(self, plan: Dict[str, Any],
                     transition_sec: float = 0.0) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """批量提取并切片视频镜头（物理音画强锁，杜绝历史缓存串音串画）

        多源支持（2026-10-21）：clip.source_file 存在时按该素材文件切片，
        缺省回退 plan.raw_video_path（单源行为不变）。
        字幕映射同样按源过滤，避免跨源时间轴错位。

        transition_sec（2026-10-27）：切片间转场时长。时间轴与字幕事件按
        「每个后续分镜提前 transition_sec」折算，与 xfade/acrossfade 链对齐。
        """
        default_src = os.path.abspath(plan["raw_video_path"])
        trans = max(0.0, float(transition_sec or 0.0))

        # 1. 彻底清空切片目录，杜绝缓存 ID 碰撞导致的内容错位
        for f in os.listdir(self.clips_dir):
            try:
                os.remove(os.path.join(self.clips_dir, f))
            except Exception:
                pass

        # 优先加载经过智能体验证校正后的字幕，杜绝错别字
        asr_cache = os.path.join(self.work_dir, "whisper_segments_verified.json")
        if not os.path.exists(asr_cache):
            asr_cache = os.path.join(self.work_dir, "whisper_segments.json")
        all_segs = []
        if os.path.exists(asr_cache):
            with open(asr_cache, "r", encoding="utf-8") as f:
                all_segs = json.load(f)

        timeline = {}
        sub_events = []
        accumulated_start = 0.0

        for ci, c in enumerate(plan["clips"]):
            # —— 多源：每个分镜按其来源文件切片 -—
            src = os.path.abspath(c.get("source_file") or default_src)
            if not os.path.exists(src):
                raise FileNotFoundError(f"分镜 {c['id']} 的源素材不存在: {src}")
            out_clip = os.path.join(self.clips_dir, f"{c['id']}.mp4")

            if c.get("effect") == "split":
                # 双色对比分屏：左半屏用本 clip，右半屏用 split_pair_st 指定的片段
                pair_st = c.get("split_pair_st", c["st"])
                half_w, half_h = OUT_W // 2, OUT_H
                left_raw = os.path.join(self.clips_dir, f"{c['id']}_L.mp4")
                right_raw = os.path.join(self.clips_dir, f"{c['id']}_R.mp4")
                for raw_path, st in ((left_raw, c["st"]), (right_raw, pair_st)):
                    run_cmd([
                        "ffmpeg", "-y",
                        "-ss", f"{st:.3f}", "-i", src,
                        "-t", f"{c['dur']:.3f}",
                        "-avoid_negative_ts", "make_zero",
                        "-vf", f"crop={half_w}:{half_h}:(iw-{half_w})/2:0,fps={FPS},setsar=1,setpts=PTS-STARTPTS",
                        "-af", "asetpts=PTS-STARTPTS",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                        raw_path
                    ], desc=f"分屏半场抽取 {os.path.basename(raw_path)} (st={st:.2f}s)")
                # hstack 合成（音频取左半场）
                # 2026-10-24 补 SKILL 2.3：中缝金色发光分割线 + 左右独立胶囊标签
                split_c = self.constraints.get("split_effect", {}) or {}
                div_w = int(split_c.get("divider_width", 8) or 8)
                div_rgb = _hex_to_rgb(split_c.get("divider_color", "#E8A91E"))
                # drawbox 的颜色必须是 0xRRGGBB 形式（不是 R@G@B，后者会被
                # 解析成 "Invalid 0xRRGGBB[AA] color string"）
                div_hex = "0x%02X%02X%02X" % div_rgb
                label_png = None
                if split_c.get("enabled", True):
                    label_png = self._make_split_labels(c, split_c)
                vc = (f"[0:v]scale={half_w}:{half_h}[v0];"
                      f"[1:v]scale={half_w}:{half_h}[v1];"
                      f"[v0][v1]hstack=inputs=2[vs];"
                      f"[vs]drawbox=x={half_w - div_w // 2}:y=0:w={div_w}:h={half_h}"
                      f":color={div_hex}:t=fill[vdiv]")
                maps = ["-map", "[vdiv]"]
                if label_png:
                    vc += (f";[vdiv][2:v]overlay=0:0:alpha=premultiplied[vout2]")
                    maps = ["-map", "[vout2]"]
                    label_in = ["-i", label_png]
                else:
                    vc += "[vdiv]"
                    label_in = []
                run_cmd([
                    "ffmpeg", "-y",
                    "-i", left_raw, "-i", right_raw,
                ] + label_in + [
                    "-filter_complex", vc,
                ] + maps + ["-map", "0:a",
                    "-t", f"{c['dur']:.3f}",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                    out_clip
                ], desc=f"分屏合成 {c['id']}")
                for tmp in (left_raw, right_raw):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
            else:
                self._extract_clip(c, src, out_clip)

                # —— 镜像分镜：遮挡反向的背景文字（SKILL 2.1，2026-10-24）——
                # 镜像后招牌/LOGO 文字会反向，必须用贴纸精确覆盖，
                # 否则比不做镜像更违和。这里用 OCR 检测文字框定位遮挡区域。
                if c.get("effect") == "hflip":
                    self._apply_mirror_occlusion(c, out_clip)

            # 回测实际物理时长
            actual_dur = ffprobe_get_duration(out_clip)
            # 转场折算：第 i 个分镜（i>=1）在成片中提前 i*trans 秒
            eff_start = max(0.0, accumulated_start - ci * trans)
            timeline[c["id"]] = {
                "start": round(eff_start, 3),
                "dur": round(actual_dur, 3),
                "desc": c.get("desc", "")
            }

            # 精准映射当前镜头内的每一句原声语音字幕，保证口型与字幕毫秒级咬合
            # 多源：仅取与该分镜同源的 segments（不同文件时间轴互不相通）
            b_end = c.get("end", c["st"] + c["dur"])
            for s in all_segs:
                s_src = os.path.abspath(s["source_file"]) if s.get("source_file") else default_src
                if s_src != src:
                    continue
                if s["start"] >= c["st"] - 0.2 and s["end"] <= b_end + 0.2:
                    rel_st = max(0.0, s["start"] - c["st"])
                    rel_end = min(c["dur"], s["end"] - c["st"])
                    if rel_end > rel_st:
                        sub_events.append({
                            "t_start": round(eff_start + rel_st, 3),
                            "t_end": round(eff_start + rel_end, 3),
                            "text": s["text"]
                        })

            accumulated_start += actual_dur

        # 保存精确时间轴
        tl_path = os.path.join(self.work_dir, "timeline.json")
        with open(tl_path, "w", encoding="utf-8") as f:
            json.dump(timeline, f, ensure_ascii=False, indent=2)

        print(f"[ASSEMBLER] 切片抽取完毕，硬拼接总长 {accumulated_start:.2f}s"
              + (f"（转场折算后约 {accumulated_start - (len(plan['clips']) - 1) * trans:.2f}s）"
                 if trans and len(plan["clips"]) > 1 else "")
              + f", 映射字幕事件: {len(sub_events)}条")
        return timeline, sub_events

    def assemble_final_video(
        self,
        plan: Dict[str, Any],
        output_mp4_path: str
    ) -> Tuple[str, float]:
        """总装：切片转场拼接 + 毫秒级发光字幕 + 黄金Hook卡片 + 片尾CTA贴片 + Boom 重音混流

        2026-10-27 重构（反馈驱动）：
          - 转场：切片间 xfade 视频叠化 + acrossfade 音频交叉淡化（transition_effect）；
          - 违规字幕屏蔽：ASS 生成前对命中词替换为 ***（violation_words 通道 1）；
          - 头尾淡入淡出（fade_effect）；
          - 放大镜/消音/打码在 render_clips 阶段按分镜执行（通道 2/3）。
        """
        # —— 转场配置 -—
        trans_c = self.constraints.get("transition_effect", {}) or {}
        trans_on = (bool(trans_c.get("enabled", True))
                    and str(trans_c.get("type", "fade")) != "none")
        trans_d = float(trans_c.get("duration_sec", 0.25) or 0.25) if trans_on else 0.0
        audio_xfade = bool(trans_c.get("audio_crossfade", True)) if trans_on else False
        fade_c = self.constraints.get("fade_effect", {}) or {}
        fade_in = float(fade_c.get("fade_in_sec", 0.3) or 0.0) if fade_c.get("enabled", True) else 0.0
        fade_out = float(fade_c.get("fade_out_sec", 0.3) or 0.0) if fade_c.get("enabled", True) else 0.0

        n_clips = len(plan["clips"])
        timeline, sub_events = self.render_clips(plan, transition_sec=trans_d)

        # —— 违规词通道 1：字幕屏蔽 -—
        vw_c = self.constraints.get("violation_words", {}) or {}
        handling_c = vw_c.get("handling", {}) or {}
        masked_words: List[str] = []
        if handling_c.get("subtitle_mask", True):
            from src.tools.detect_violation import ViolationDetector
            det = ViolationDetector()
            masked_events = []
            for ev in sub_events:
                text, hits = det.mask_text(ev.get("text", "") or "")
                if hits:
                    masked_words.extend(h["word"] for h in hits)
                masked_events.append({**ev, "text": text})
            sub_events = masked_events
            if masked_words:
                print(f"[VIOLATION] 字幕屏蔽 {len(masked_words)} 处: "
                      f"{', '.join(sorted(set(masked_words))[:8])}")

        # 1. 生成对应时间的变色字幕
        ass_path = os.path.join(self.work_dir, "subtitles.ass")
        keywords = plan.get("subtitles", {}).get("keywords", [])
        generate_ass_subtitles(sub_events, ass_path, keywords=keywords)

        # 2. 合成 Boom 冲击音
        boom_wav = os.path.join(self.work_dir, "boom.wav")
        generate_sub_bass_boom(boom_wav, duration=1.2)

        # 3. 生成 Hook 卡片与 CTA 贴片
        hook_text = plan.get("hook_card", {}).get("text", "今晚必看爆款")
        hook_y = int(plan.get("hook_card", {}).get("y", 280))  # 动态避让人脸顶（B10）
        hook_png = os.path.join(self.work_dir, "hook_card.png")
        hook_c = self.constraints.get("hook_card", {}) or {}
        generate_hook_card(hook_text, hook_png,
                           width=int(hook_c.get("width", 720) or 720),
                           height=int(hook_c.get("height", 168) or 168))

        cta_text = plan.get("cta", {}).get("text", "戳左下方小黄车查看上身搭配")
        cta_png = os.path.join(self.work_dir, "cta_badge.png")
        generate_cta_badge(cta_text, cta_png)

        # 3.5 顶部平台标识条（2026-10-24 新增，常驻水印区）
        bar_c = self.constraints.get("platform_bar", {}) or {}
        bar_png = ""
        shop_name = ""
        account = ""
        if bar_c.get("enabled", True):
            brand = plan.get("brand", {}) or {}
            shop_name = bar_c.get("shop_name") or brand.get("title_en") or ""
            account = bar_c.get("account") or (
                f"@{brand.get('subtitle_cn')}" if brand.get("subtitle_cn") else "")
            if shop_name or account:
                bar_png = os.path.join(self.work_dir, "platform_bar.png")
                try:
                    generate_platform_bar(bar_png, bar_c, shop_name=shop_name,
                                          account=account)
                    print(f"[ASSEMBLER] 顶部平台标识条: 店铺《{shop_name}》 账号《{account}》")
                except Exception as e:
                    print(f"[WARN] 平台标识条生成失败（不阻断）: {e}")
                    bar_png = ""

        # 3.6 挂角利益点贴纸（按分镟时序出现/消失）
        sticker_c = self.constraints.get("video_sticker", {}) or {}
        sticker_pngs: List[Tuple[str, float, float]] = []   # (png, t_in, t_out)
        if sticker_c.get("enabled", True):
            fade_in_s = float(sticker_c.get("fade_in_sec", 0.35) or 0.35)
            fade_out_s = float(sticker_c.get("fade_out_sec", 0.35) or 0.35)
            hold_ratio = float(sticker_c.get("hold_ratio", 0.72) or 0.72)
            delay_ratio = float(sticker_c.get("delay_ratio", 0.10) or 0.10)
            for c in plan["clips"]:
                text = (c.get("sticker_text") or "").strip()
                if not text:
                    continue
                tl = timeline.get(c["id"]) or {}
                t0 = float(tl.get("start", 0.0))
                dur = float(tl.get("dur", 0.0))
                if dur <= 0.2:
                    continue
                t_in = t0 + dur * delay_ratio
                t_out = t0 + dur * (delay_ratio + hold_ratio)
                t_out = min(t_out, t0 + dur - 0.05)
                if t_out - t_in < fade_in_s + fade_out_s + 0.1:
                    continue
                png = os.path.join(self.work_dir, f"{c['id']}_sticker.png")
                try:
                    generate_stage_sticker(text, png, sticker_c)
                except Exception as e:
                    print(f"[WARN] 挂角贴纸生成失败 {c['id']}（跳过）: {e}")
                    continue
                sticker_pngs.append((png, t_in, t_out))
            if sticker_pngs:
                print(f"[ASSEMBLER] 挂角利益点贴纸: {len(sticker_pngs)} 个（带淡入淡出时序）")

        # 计算总时长（转场折减）
        last_clip = plan["clips"][-1]
        total_dur = timeline[last_clip["id"]]["start"] + timeline[last_clip["id"]]["dur"]

        # 4. 韵序预测（保留 concat demuxer 测量，转场对响度影响可忽）
        concat_txt = os.path.join(self.work_dir, "concat_list.txt")
        with open(concat_txt, "w", encoding="utf-8") as f:
            for c in plan["clips"]:
                clip_path = os.path.join(self.clips_dir, f"{c['id']}.mp4").replace(os.sep, "/")
                f.write(f"file '{clip_path}'\n")
        measured = self._measure_loudness(concat_txt, boom_wav, total_dur)

        # 5. FFmpeg 输入与滤镜网络编排
        loudnorm_params = "I=-14:TP=-0.5:LRA=11"
        audio_c = self.constraints.get("audio", {}) or {}
        tp_max = float(audio_c.get("true_peak_max_dbtp", -0.5))
        limiter_limit = round(10 ** ((tp_max - 0.9) / 20.0), 3)
        if measured:
            try:
                gain_needed = -14.0 - float(measured["input_i"])
                tp_after_gain = float(measured["input_tp"]) + gain_needed
                linear_safe = tp_after_gain <= -0.5
            except (KeyError, ValueError):
                linear_safe = False
            loudnorm_params += (
                f":measured_I={measured['input_i']}"
                f":measured_TP={measured['input_tp']}"
                f":measured_LRA={measured['input_lra']}"
                f":measured_thresh={measured['input_thresh']}"
                f":offset={measured['target_offset']}"
            )
            loudnorm_params += ":linear=true" if linear_safe else ":linear=false"
            print(f"[LOUDNORM] 模式选择: {'linear（纯增益）' if linear_safe else 'dynamic（动态压缩，安静素材）'}"
                  f"（需增益 {gain_needed:.1f}dB, 增益后 TP 预估 {tp_after_gain:.1f}dBTP）")

        # 输入：0..n-1 切片，n=Boom，之后 PNG 叠加层
        inputs: List[str] = []
        for c in plan["clips"]:
            clip_path = os.path.join(self.clips_dir, f"{c['id']}.mp4").replace(os.sep, "/")
            inputs += ["-i", clip_path]
        boom_idx = n_clips
        inputs += ["-i", boom_wav]

        fc: List[str] = []
        # —— 5.1 转场链（xfade / acrossfade）——
        use_trans = trans_on and n_clips >= 2
        if use_trans:
            c0, c1 = plan["clips"][0]["id"], plan["clips"][1]["id"]
            fc.append(f"[0:v][1:v]xfade=transition=fade:duration={trans_d:.3f}"
                      f":offset={timeline[c1]['start']:.3f}[vx1]")
            if audio_xfade:
                fc.append(f"[0:a][1:a]acrossfade=d={trans_d:.3f}:c1=tri:c2=tri[ax1]")
            else:
                fc.append(f"[0:a][1:a]concat=n=2:v=0:a=1[ax1]")
            for i in range(2, n_clips):
                cid = plan["clips"][i]["id"]
                fc.append(f"[vx{i - 1}][{i}:v]xfade=transition=fade:duration={trans_d:.3f}"
                          f":offset={timeline[cid]['start']:.3f}[vx{i}]")
                if audio_xfade:
                    fc.append(f"[ax{i - 1}][{i}:a]acrossfade=d={trans_d:.3f}:c1=tri:c2=tri[ax{i}]")
                else:
                    fc.append(f"[ax{i - 1}][{i}:a]concat=n=2:v=0:a=1[ax{i}]")
            cur_v, cur_a = f"vx{n_clips - 1}", f"ax{n_clips - 1}"
            print(f"[ASSEMBLER] 转场链: {n_clips} 切片 xfade/acrossfade "
                  f"{trans_d:.2f}s（音频交叉淡化={'on' if audio_xfade else 'off'}）")
        else:
            fc.append("[0:v]null[vbase]")
            fc.append("[0:a]null[abase]")
            cur_v, cur_a = "vbase", "abase"

        # —— 5.2 字幕烷馇 -—
        ass_rel = os.path.basename(ass_path)
        fc.append(f"[{cur_v}]subtitles={ass_rel}[vsub]")
        cur_v = "vsub"

        # —— 5.3 叠加层（贴纸 → 标识条 → Hook卡 → CTA）——
        in_idx = boom_idx
        sticker_size = int(sticker_c.get("size", 220) or 220)
        sticker_mx = int(sticker_c.get("margin_x", 40) or 40)
        sticker_my = int(sticker_c.get("margin_y", 300) or 300)
        sticker_side = sticker_c.get("side", "right")
        fade_in_s = float(sticker_c.get("fade_in_sec", 0.35) or 0.35)
        fade_out_s = float(sticker_c.get("fade_out_sec", 0.35) or 0.35)
        for (png, t_in, t_out) in sticker_pngs:
            in_idx += 1
            rel = os.path.basename(png)
            inputs += ["-loop", "1", "-framerate", str(FPS),
                       "-t", f"{total_dur:.2f}", "-i", rel]
            fc.append(
                f"[{in_idx}:v]format=rgba,"
                f"fade=t=in:st={t_in:.3f}:d={fade_in_s:.3f}:alpha=1,"
                f"fade=t=out:st={max(t_in, t_out - fade_out_s):.3f}:d={fade_out_s:.3f}:alpha=1"
                f"[stk{in_idx}]"
            )
            sx = (OUT_W - sticker_size - sticker_mx) if sticker_side == "right" else sticker_mx
            nxt = f"vstk{in_idx}"
            fc.append(
                f"[{cur_v}][stk{in_idx}]overlay={sx}:{sticker_my}"
                f":enable='between(t,{t_in:.3f},{t_out:.3f})':alpha=premultiplied[{nxt}]"
            )
            cur_v = nxt

        if bar_png:
            in_idx += 1
            rel = os.path.basename(bar_png)
            inputs += ["-loop", "1", "-framerate", str(FPS),
                       "-t", f"{total_dur:.2f}", "-i", rel]
            fc.append(f"[{in_idx}:v]format=rgba[bar{in_idx}]")
            nxt = f"vbar{in_idx}"
            fc.append(f"[{cur_v}][bar{in_idx}]overlay=0:0:alpha=premultiplied[{nxt}]")
            cur_v = nxt

        # Hook 卡片（0~2.5s）
        in_idx += 1
        hook_in = in_idx
        hook_rel = os.path.basename(hook_png)
        inputs += ["-i", hook_rel]
        fc.append(f"[{cur_v}][{hook_in}:v]overlay=(W-w)/2:{hook_y}"
                  f":enable='between(t,0,2.5)':alpha=premultiplied[vhook]")
        cur_v = "vhook"

        # CTA 贴片（末 2.5s）
        in_idx += 1
        cta_in = in_idx
        cta_rel = os.path.basename(cta_png)
        inputs += ["-i", cta_rel]
        fc.append(f"[{cur_v}][{cta_in}:v]overlay=40:1440"
                  f":enable='gte(t,{max(0.0, total_dur - 2.5):.2f})':alpha=premultiplied[vcta]")
        cur_v = "vcta"

        # —— 5.4 全片头尾淡入淡出 -—
        if fade_in > 0 or fade_out > 0:
            fades = []
            if fade_in > 0:
                fades.append(f"fade=t=in:st=0:d={fade_in:.3f}")
            if fade_out > 0:
                fades.append(f"fade=t=out:st={max(0.0, total_dur - fade_out):.3f}:d={fade_out:.3f}")
            fc.append(f"[{cur_v}]{','.join(fades)}[vfinal]")
            cur_v = "vfinal"

        # —— 5.5 音频混流 + 响度归一化 -—
        fc += [
            f"[{boom_idx}:a]volume=0.45[boom_hit]",
            f"[{cur_a}][boom_hit]amix=inputs=2:duration=first:weights='1.0 1.0'[amix]",
            f"[amix]loudnorm={loudnorm_params}[aloud]",
            f"[aloud]alimiter=limit={limiter_limit}:level=false[alim]",
            f"[alim]afade=t=out:st={max(0.0, total_dur - 0.25):.2f}:d=0.25[aout]",
        ]

        os.makedirs(os.path.dirname(os.path.abspath(output_mp4_path)), exist_ok=True)
        run_cmd([
            "ffmpeg", "-y",
        ] + inputs + [
            "-filter_complex", ";".join(fc),
            "-map", f"[{cur_v}]", "-map", "[aout]",
            "-t", f"{total_dur:.2f}",
            "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart",
            output_mp4_path
        ], cwd=self.work_dir, desc=f"最终总装导出 ({total_dur:.1f}s)")

        # —— 5.6 叠加元素清单（QC 复测依据） -—
        zoom_c_meta = self.constraints.get("zoom_effect", {}) or {}
        magn_c_meta = self.constraints.get("magnifier_effect", {}) or {}
        magnifier_events = []
        for c in plan["clips"]:
            m = c.get("magnifier") or {}
            if not m.get("enabled"):
                continue
            r = int(m.get("r", magn_c_meta.get("radius", 130)) or 130)
            cx = max(r + 8, min(OUT_W - r - 8, int(m.get("cx", 0))))
            cy = max(r + 100, min(OUT_H - r - 260, int(m.get("cy", 0))))
            magnifier_events.append({
                "clip_id": c["id"],
                "cx": cx, "cy": cy, "r": r,
                "zoom": float(m.get("zoom", magn_c_meta.get("zoom", 1.8)) or 1.8),
                "t_in": float(m.get("t_in", 0.3)),
                "t_out": float(m.get("t_out", 0.0)),
                "box": [cx - r, cy - r, cx + r, cy + r],
            })
        overlay_meta = {
            "platform_bar": {
                "applied": bool(bar_png),
                "height": int(bar_c.get("height", 88) or 88) if bar_png else 0,
                "top": int(bar_c.get("top", 0) or 0),
                "shop_name": shop_name if bar_png else "",
                "account": account if bar_png else "",
            } if bar_png else {"applied": False},
            "hook_card": {
                "applied": True,
                "x": (OUT_W - int(hook_c.get("width", 720) or 720)) // 2,
                "y": hook_y,
                "w": int(hook_c.get("width", 720) or 720),
                "h": int(hook_c.get("height", 168) or 168),
                "t_in": 0.0, "t_out": 2.5,
                "text": hook_text,
            },
            "cta_badge": {
                "applied": True,
                "x": 40, "y": 1440, "w": 480, "h": 100,
                "t_in": max(0.0, total_dur - 2.5), "t_out": total_dur,
                "text": cta_text,
            },
            "sticker_events": [
                {
                    "clip_id": os.path.basename(png).replace("_sticker.png", ""),
                    "png": os.path.basename(png),
                    "t_in": round(t_in, 3),
                    "t_out": round(t_out, 3),
                    "fade_in_sec": fade_in_s,
                    "fade_out_sec": fade_out_s,
                    "size": sticker_size,
                    "side": sticker_side,
                    "x": (OUT_W - sticker_size - sticker_mx) if sticker_side == "right" else sticker_mx,
                    "y": sticker_my,
                }
                for (png, t_in, t_out) in sticker_pngs
            ],
            "zoom_events": [
                {
                    "clip_id": c["id"],
                    "zoom": c.get("zoom", 1.0),
                    "trigger_sec": float(zoom_c_meta.get("trigger_sec", 0.8)),
                    "transition_sec": float(zoom_c_meta.get("transition_sec", 1.2)),
                    "easing": str(zoom_c_meta.get("easing", "smoothstep")),
                    "y_anchor": c.get("y_anchor", 0.35),
                }
                for c in plan["clips"] if float(c.get("zoom", 1.0) or 1.0) > 1.0
            ],
            # 2026-10-27 新增：放大镟 / 转场 / 淡入淡出 / 违规处理
            "magnifier_events": magnifier_events,
            "transition": {
                "applied": use_trans,
                "type": "fade" if use_trans else "none",
                "duration_sec": round(trans_d, 3) if use_trans else 0.0,
                "audio_crossfade": audio_xfade if use_trans else False,
                "n_clips": n_clips,
            },
            "fade": {
                "applied": bool(fade_in > 0 or fade_out > 0),
                "fade_in_sec": round(fade_in, 3),
                "fade_out_sec": round(fade_out, 3),
            },
            "violation_handling": {
                "subtitle_masked_words": sorted(set(masked_words)),
                "n_subtitle_masked": len(masked_words),
                "applied": self.violation_report.get("applied", {}),
            },
            "mirror_clips": [c["id"] for c in plan["clips"]
                             if c.get("effect") == "hflip"
                             and c["id"] not in self.mirror_abandoned],
            "mirror_abandoned": sorted(self.mirror_abandoned),
            "occlusion_applied": {
                cid: v.get("applied", False)
                for cid, v in (self.occlusion_log or {}).items()
            },
            "occlusion_detail": self.occlusion_log or {},
            "split_clips": [c["id"] for c in plan["clips"]
                            if c.get("effect") == "split"],
            "total_duration": round(total_dur, 3),
        }
        meta_path = os.path.join(self.work_dir, "video_overlay_meta.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(overlay_meta, f, ensure_ascii=False, indent=2)
        print(f"[ASSEMBLER] 叠加元素清单已落盘: {meta_path}")

        # 违规处理执行报告（QC 审计依据）
        vreport = {
            "subtitle_masked_words": sorted(set(masked_words)),
            "n_subtitle_masked": len(masked_words),
            "applied": self.violation_report.get("applied", {}),
            "handling_summary": (self.violation_handling.get("summary", {}) or {}),
        }
        vreport_path = os.path.join(self.work_dir, "violation_handling_report.json")
        with open(vreport_path, "w", encoding="utf-8") as f:
            json.dump(vreport, f, ensure_ascii=False, indent=2)

        print(f"[ASSEMBLER] 成片导出成功: {output_mp4_path} ({total_dur:.2f}s)")
        return output_mp4_path, total_dur
