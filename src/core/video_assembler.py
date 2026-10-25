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

OUT_W, OUT_H, FPS = 1080, 1920, 30
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


def build_clip_filter(clip: Dict[str, Any]) -> str:
    """构建单切片滤镜链：居中裁切 1080x1920 + 可选推镜 + 帧率锁定 + PTS归零对齐"""
    z = clip.get("zoom", 1.0)
    ya = clip.get("y_anchor", 0.35)
    
    parts = [f"crop={OUT_W}:{OUT_H}:(iw-{OUT_W})/2:0"]
    if z and z > 1.0:
        parts.append(f"crop=w=iw/{z}:h=ih/{z}:x=(iw-iw/{z})/2:y=(ih-ih/{z})*{ya},scale={OUT_W}:{OUT_H}")
    parts.append(f"fps={FPS},setsar=1,setpts=PTS-STARTPTS")
    return ",".join(parts)


def generate_hook_card(text: str, out_path: str) -> str:
    """生成前 2.5 秒黄金 Hook 悬浮卡片 (香槟金细边框 + 半透明深褐底)"""
    w, h = 640, 130
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 半透明深褐胶囊底色
    d.rounded_rectangle([0, 0, w, h], radius=22, fill=(35, 20, 15, 220), outline=(226, 200, 142, 255), width=3)
    f_card = ImageFont.truetype(FONT_BOLD, 34)
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
    def __init__(self, work_dir: str):
        self.work_dir = os.path.abspath(work_dir)
        self.clips_dir = os.path.join(self.work_dir, "clips")
        os.makedirs(self.clips_dir, exist_ok=True)

    def render_clips(self, plan: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """批量提取并切片视频镜头（物理音画强锁，杜绝历史缓存串音串画）"""
        src = os.path.abspath(plan["raw_video_path"])
        
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
            out_clip = os.path.join(self.clips_dir, f"{c['id']}.mp4")
            vf = build_clip_filter(c)
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
            
            # 回测实际物理时长
            actual_dur = ffprobe_get_duration(out_clip)
            timeline[c["id"]] = {
                "start": round(accumulated_start, 3),
                "dur": round(actual_dur, 3),
                "desc": c.get("desc", "")
            }

            # 精准映射当前镜头内的每一句原声语音字幕，保证口型与字幕毫秒级咬合
            b_end = c.get("end", c["st"] + c["dur"])
            for s in all_segs:
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
        hook_png = os.path.join(self.work_dir, "hook_card.png")
        generate_hook_card(hook_text, hook_png)

        cta_text = plan.get("cta", {}).get("text", "戳左下方小黄车查看上身搭配")
        cta_png = os.path.join(self.work_dir, "cta_badge.png")
        generate_cta_badge(cta_text, cta_png)

        # 计算总时长
        last_clip = plan["clips"][-1]
        total_dur = timeline[last_clip["id"]]["start"] + timeline[last_clip["id"]]["dur"]
        
        # 5. FFmpeg 滤镜复合网络编排 (字幕 + Hook卡片 + CTA贴片 + 音频混流)
        ass_rel = os.path.basename(ass_path)
        hook_rel = os.path.basename(hook_png)
        cta_rel = os.path.basename(cta_png)

        fc = [
            f"[0:v]subtitles={ass_rel}[vsub]",
            f"[vsub][2:v]overlay=(W-w)/2:280:enable='between(t,0,2.5)':alpha=premultiplied[vhook]",
            f"[vhook][3:v]overlay=40:1440:enable='gte(t,{max(0.0, total_dur - 2.5):.2f})':alpha=premultiplied[vout]",
            f"[1:a]volume=0.45[boom_hit]",
            f"[0:a][boom_hit]amix=inputs=2:duration=first:weights='1.0 1.0'[amix]",
            f"[amix]afade=t=out:st={max(0.0, total_dur - 0.25):.2f}:d=0.25[aout]"
        ]

        os.makedirs(os.path.dirname(os.path.abspath(output_mp4_path)), exist_ok=True)
        
        run_cmd([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", concat_txt,
            "-i", boom_wav,
            "-i", hook_rel,
            "-i", cta_rel,
            "-filter_complex", ";".join(fc),
            "-map", "[vout]", "-map", "[aout]",
            "-t", f"{total_dur:.2f}",
            "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart",
            output_mp4_path
        ], cwd=self.work_dir, desc=f"最终总装导出 ({total_dur:.1f}s)")

        print(f"[ASSEMBLER] 成片导出成功: {output_mp4_path} ({total_dur:.2f}s)")
        return output_mp4_path, total_dur
