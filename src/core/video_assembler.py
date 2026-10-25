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
from typing import Dict, Any, List, Tuple
from PIL import Image, ImageDraw, ImageFont
from src.graphics.sfx_generator import generate_sub_bass_boom
from src.graphics.ass_subtitles import generate_ass_subtitles
from src.graphics.video_overlays import (
    generate_platform_bar, generate_stage_sticker,
    generate_occlusion_patch, generate_split_labels,
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
        os.makedirs(self.clips_dir, exist_ok=True)

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
        upper = [r for r in rects
                 if r["y0"] < OUT_H * 0.42
                 and (r["x1"] - r["x0"]) * (r["y1"] - r["y0"]) >= min_area]
        if not upper:
            print(f"[ASSEMBLER] 镜像分镜 {clip['id']}：上半屏无背景文字，无需遮挡")
            self.occlusion_log[clip["id"]] = {
                "applied": True, "regions": 0, "reason": "上半屏无背景文字（镜像无违和点）"}
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

    def render_clips(self, plan: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """批量提取并切片视频镜头（物理音画强锁，杜绝历史缓存串音串画）

        多源支持（2026-10-21）：clip.source_file 存在时按该素材文件切片，
        缺省回退 plan.raw_video_path（单源行为不变）。
        字幕映射同样按源过滤，避免跨源时间轴错位。
        """
        default_src = os.path.abspath(plan["raw_video_path"])

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

        for c in plan["clips"]:
            # —— 多源：每个分镜按其来源文件切片 ——
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
                zoom_c = self.constraints.get("zoom_effect", {}) or {}
                vf = build_clip_filter(c, zoom_c)
                af = "asetpts=PTS-STARTPTS"
                # 物理强锁：-ss 放在 -i 之前秒级Seek + -t 放在 -i 之后 + -avoid_negative_ts make_zero
                run_cmd([
                    "ffmpeg", "-y",
                    "-ss", f"{c['st']:.3f}",
                    "-i", src,
                    "-t", f"{c['dur']:.3f}",
                    "-avoid_negative_ts", "make_zero",
                    "-vf", vf,
                    "-af", af,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                    out_clip
                ], desc=f"切片抽取 {c['id']} ({c['dur']}s, st={c['st']:.2f}s)")

                # —— 镜像分镜：遮挡反向的背景文字（SKILL 2.1，2026-10-24）——
                # 镜像后招牌/LOGO 文字会反向，必须用贴纸精确覆盖，
                # 否则比不做镜像更违和。这里用 OCR 检测文字框定位遮挡区域。
                if c.get("effect") == "hflip":
                    self._apply_mirror_occlusion(c, out_clip)
            
            # 回测实际物理时长
            actual_dur = ffprobe_get_duration(out_clip)
            timeline[c["id"]] = {
                "start": round(accumulated_start, 3),
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
                            "t_start": round(accumulated_start + rel_st, 3),
                            "t_end": round(accumulated_start + rel_end, 3),
                            "text": s["text"]
                        })

            accumulated_start += actual_dur

        # 保存精确时间轴
        tl_path = os.path.join(self.work_dir, "timeline.json")
        with open(tl_path, "w", encoding="utf-8") as f:
            json.dump(timeline, f, ensure_ascii=False, indent=2)
            
        print(f"[ASSEMBLER] 切片抽取完毕，实际总物理时长: {accumulated_start:.2f}s, 映射字幕事件: {len(sub_events)}条")
        return timeline, sub_events

    def assemble_final_video(
        self,
        plan: Dict[str, Any],
        output_mp4_path: str
    ) -> Tuple[str, float]:
        """总装：切片拼接 + 毫秒级发光字幕 + 黄金Hook卡片 + 片尾CTA贴片 + Boom 重音混流"""
        timeline, sub_events = self.render_clips(plan)
        
        # 1. 生成 concat 列表
        concat_txt = os.path.join(self.work_dir, "concat_list.txt")
        with open(concat_txt, "w", encoding="utf-8") as f:
            for c in plan["clips"]:
                clip_path = os.path.join(self.clips_dir, f"{c['id']}.mp4").replace(os.sep, "/")
                f.write(f"file '{clip_path}'\n")

        # 2. 生成对应时间的变色字幕
        ass_path = os.path.join(self.work_dir, "subtitles.ass")
        keywords = plan.get("subtitles", {}).get("keywords", [])
        generate_ass_subtitles(sub_events, ass_path, keywords=keywords)

        # 3. 合成 Boom 冲击音
        boom_wav = os.path.join(self.work_dir, "boom.wav")
        generate_sub_bass_boom(boom_wav, duration=1.2)

        # 4. 生成 Hook 卡片与 CTA 贴片
        hook_text = plan.get("hook_card", {}).get("text", "极简松弛感 · 显瘦显气质")
        hook_y = int(plan.get("hook_card", {}).get("y", 280))  # 动态避让人脸顶（B10）
        hook_png = os.path.join(self.work_dir, "hook_card.png")
        hook_c = self.constraints.get("hook_card", {}) or {}
        generate_hook_card(hook_text, hook_png,
                           width=int(hook_c.get("width", 720) or 720),
                           height=int(hook_c.get("height", 168) or 168))

        cta_text = plan.get("cta", {}).get("text", "戳左下方小黄车查看上身搭配")
        cta_png = os.path.join(self.work_dir, "cta_badge.png")
        generate_cta_badge(cta_text, cta_png)

        # 4.5 顶部平台标识条（2026-10-24 新增，常驻水印区）
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
                    print(f"[ASSEMBLER] 顶部平台标识条: 店铺「{shop_name}」 账号「{account}」")
                except Exception as e:
                    print(f"[WARN] 平台标识条生成失败（不阻断）: {e}")
                    bar_png = ""

        # 4.6 挂角利益点贴纸（2026-10-24 新增，按分镜时序出现/消失）
        #     贴纸时机由分镜在**成片时间轴**上的位置决定：
        #       出现 = 分镜起点 + delay_ratio×分镜时长
        #       消失 = 出现 + hold_ratio×分镜时长
        #     淡入/淡出用 fade 滤镜实现，避免"啪"地闪现。
        sticker_c = self.constraints.get("video_sticker", {}) or {}
        sticker_pngs: List[Tuple[str, float, float]] = []   # (png, t_in, t_out)
        if sticker_c.get("enabled", True):
            fade_in = float(sticker_c.get("fade_in_sec", 0.35) or 0.35)
            fade_out = float(sticker_c.get("fade_out_sec", 0.35) or 0.35)
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
                # 保证淡入淡出不越界
                t_out = min(t_out, t0 + dur - 0.05)
                if t_out - t_in < fade_in + fade_out + 0.1:
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

        # 计算总时长
        last_clip = plan["clips"][-1]
        total_dur = timeline[last_clip["id"]]["start"] + timeline[last_clip["id"]]["dur"]
        
        # 5. 音频响度两遍归一化预测量（V4）
        #    单遍动态 loudnorm 不保证 TP（实测 hoodie 素材 TP=-0.4 > -0.5 上限被 QC 拦截）；
        #    先对混合音频做一遍测量，正式总装用 linear 模式 + alimiter 兜底。
        measured = self._measure_loudness(concat_txt, boom_wav, total_dur)

        # 6. FFmpeg 滤镜复合网络编排 (字幕 + Hook卡片 + CTA贴片 + 音频混流 + 响度归一化)
        # 响度归一（EBU R128）：−14 LUFS 综合响度 + True Peak ≤ −0.5 dBTP（V4）
        ass_rel = os.path.basename(ass_path)
        hook_rel = os.path.basename(hook_png)
        cta_rel = os.path.basename(cta_png)

        loudnorm_params = "I=-14:TP=-0.5:LRA=11"
        # alimiter 上限由 constraints 的真峰值目标推导：
        # alimiter 限制的是**采样峰**，真峰值（帧间峰）还会高出 0.3~0.6 dB，
        # 故留 0.9 dB 余量（实测 demo：limit=0.92 时 TP 仍到 -0.4 > -0.5 上限）
        audio_c = self.constraints.get("audio", {}) or {}
        tp_max = float(audio_c.get("true_peak_max_dbtp", -0.5))
        limiter_limit = round(10 ** ((tp_max - 0.9) / 20.0), 3)
        if measured:
            # 两遍归一化：先判断 linear 模式是否可行（所需增益不冲高真峰值），
            # 可行则用 linear（纯增益、音质最好）；不可行（安静素材需大增益）
            # 则用动态模式（压缩动态范围同时满足响度与 TP 双目标）。
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
        # —— 视频画面叠加链（2026-10-24 扩展）——
        # 顺序：字幕 → 挂角贴纸(时序淡入淡出) → 顶部平台标识条 → Hook卡片 → CTA
        # 说明：
        #   · 挂角贴纸在字幕之下、标识条之上，避免与底部字幕打架；
        #   · 顶部标识条是常驻水印区，铺在最上层不被任何特效遮挡；
        #   · 贴纸淡入淡出用 fade 滤镜（对 PNG 序列做时间轴淡变），
        #     配合 overlay 的 enable 区间实现"出现→停留→消失"的完整时序。
        extra_inputs: List[str] = []
        fc: List[str] = []
        # 输入序号：0=concat视频 1=boom音频，PNG 叠加层从 2 开始顺序排布
        # （extra_inputs 的追加顺序必须与 in_idx 严格一致，否则 ffmpeg 报
        #  "Invalid file index N in filtergraph description"）
        in_idx = 1

        # 4.6 挂角贴纸（每个分镜一个，按成片时间轴排布）
        cur_label = "vsub"
        sticker_size = int(sticker_c.get("size", 220) or 220)
        sticker_mx = int(sticker_c.get("margin_x", 40) or 40)
        sticker_my = int(sticker_c.get("margin_y", 300) or 300)
        sticker_side = sticker_c.get("side", "right")
        fade_in = float(sticker_c.get("fade_in_sec", 0.35) or 0.35)
        fade_out = float(sticker_c.get("fade_out_sec", 0.35) or 0.35)
        for (png, t_in, t_out) in sticker_pngs:
            in_idx += 1
            rel = os.path.basename(png)
            # ⚠️ 必须用 `-loop 1 -t <总时长>` 把静态 PNG 变成**连续流**：
            #    单帧 PNG 的时间戳恒为 0，fade 的 st=9.85 会让它永远保持透明，
            #    贴纸根本不会出现在画面上（实测踩到）。
            extra_inputs += ["-loop", "1", "-framerate", str(FPS),
                             "-t", f"{total_dur:.2f}", "-i", rel]
            # 贴纸自身做淡入淡出（fade 对连续流按时间轴淡变）
            fc.append(
                f"[{in_idx}:v]format=rgba,"
                f"fade=t=in:st={t_in:.3f}:d={fade_in:.3f}:alpha=1,"
                f"fade=t=out:st={max(t_in, t_out - fade_out):.3f}:d={fade_out:.3f}:alpha=1"
                f"[stk{ in_idx }]"
            )
            sx = (OUT_W - sticker_size - sticker_mx) if sticker_side == "right" else sticker_mx
            nxt = f"vstk{in_idx}"
            fc.append(
                f"[{cur_label}][stk{in_idx}]overlay={sx}:{sticker_my}"
                f":enable='between(t,{t_in:.3f},{t_out:.3f})':alpha=premultiplied[{nxt}]"
            )
            cur_label = nxt

        # 4.5 顶部平台标识条（常驻）——同样用连续流，保证覆盖整片时长
        if bar_png:
            in_idx += 1
            rel = os.path.basename(bar_png)
            extra_inputs += ["-loop", "1", "-framerate", str(FPS),
                             "-t", f"{total_dur:.2f}", "-i", rel]
            fc.append(f"[{in_idx}:v]format=rgba[bar{in_idx}]")
            nxt = f"vbar{in_idx}"
            fc.append(f"[{cur_label}][bar{in_idx}]overlay=0:0:alpha=premultiplied[{nxt}]")
            cur_label = nxt

        # Hook 卡片（0~2.5s）
        in_idx += 1
        hook_in = in_idx
        extra_inputs += ["-i", hook_rel]
        fc.append(f"[{cur_label}][{hook_in}:v]overlay=(W-w)/2:{hook_y}"
                  f":enable='between(t,0,2.5)':alpha=premultiplied[vhook]")
        cur_label = "vhook"

        # CTA 贴片（末 2.5s）
        in_idx += 1
        cta_in = in_idx
        extra_inputs += ["-i", cta_rel]
        fc.append(f"[{cur_label}][{cta_in}:v]overlay=40:1440"
                  f":enable='gte(t,{max(0.0, total_dur - 2.5):.2f})':alpha=premultiplied[vout]")

        fc.insert(0, f"[0:v]subtitles={ass_rel}[vsub]")
        fc += [
            f"[1:a]volume=0.45[boom_hit]",
            f"[0:a][boom_hit]amix=inputs=2:duration=first:weights='1.0 1.0'[amix]",
            f"[amix]loudnorm={loudnorm_params}[aloud]",
            # alimiter 兜底：动态模式罕见过冲时硬限幅（采样峰，留 0.9dB 帧间峰余量）
            f"[aloud]alimiter=limit={limiter_limit}:level=false[alim]",
            f"[alim]afade=t=out:st={max(0.0, total_dur - 0.25):.2f}:d=0.25[aout]"
        ]

        os.makedirs(os.path.dirname(os.path.abspath(output_mp4_path)), exist_ok=True)

        run_cmd([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", concat_txt,
            "-i", boom_wav,
        ] + extra_inputs + [
            "-filter_complex", ";".join(fc),
            "-map", "[vout]", "-map", "[aout]",
            "-t", f"{total_dur:.2f}",
            "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart",
            output_mp4_path
        ], cwd=self.work_dir, desc=f"最终总装导出 ({total_dur:.1f}s)")

        # 叠加元素清单（2026-10-24）：供 QC 独立复测"该有的特效有没有真的加上"
        zoom_c_meta = self.constraints.get("zoom_effect", {}) or {}
        overlay_meta = {
            "platform_bar": {
                "applied": bool(bar_png),
                "height": int(bar_c.get("height", 88) or 88) if bar_png else 0,
                "top": int(bar_c.get("top", 0) or 0),
                "shop_name": shop_name if bar_png else "",
                "account": account if bar_png else "",
            } if bar_png else {"applied": False},
            "sticker_events": [
                {
                    "clip_id": os.path.basename(png).replace("_sticker.png", ""),
                    "png": os.path.basename(png),
                    "t_in": round(t_in, 3),
                    "t_out": round(t_out, 3),
                    "fade_in_sec": fade_in,
                    "fade_out_sec": fade_out,
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
            "mirror_clips": [c["id"] for c in plan["clips"]
                             if c.get("effect") == "hflip"],
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

        print(f"[ASSEMBLER] 成片导出成功: {output_mp4_path} ({total_dur:.2f}s)")
        return output_mp4_path, total_dur
