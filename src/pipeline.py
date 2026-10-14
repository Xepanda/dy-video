#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Live E-Commerce Short Video Automation Pipeline
一站式直播带货短视频全自动/半自动工业化剪辑流水线
"""

import os
import sys
import json
import wave
import subprocess
import numpy as np
from PIL import Image, ImageDraw, ImageFont

class LiveVideoPipeline:
    def __init__(self, project_dir):
        self.project_dir = os.path.abspath(project_dir)
        self.video_dir = os.path.join(self.project_dir, 'video')
        self.temp_dir = os.path.join(self.project_dir, 'temp')
        self.output_dir = os.path.join(self.project_dir, 'output')
        self.stickers_dir = os.path.join(self.temp_dir, 'stickers')
        self.clips_dir = os.path.join(self.temp_dir, 'clips')
        
        for d in [self.temp_dir, self.output_dir, self.stickers_dir, self.clips_dir]:
            os.makedirs(d, exist_ok=True)
            
        self.font_heavy = 'C:/Windows/Fonts/simhei.ttf' if os.name == 'nt' else '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
        self.font_msyh = 'C:/Windows/Fonts/msyhbd.ttc' if os.name == 'nt' else self.font_heavy

    def run_cmd(self, cmd, cwd=None):
        if cwd is None:
            cwd = self.temp_dir
        print(f"[RUN] {cmd} (cwd: {cwd})")
        res = subprocess.run(cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8')
        if res.returncode != 0:
            print(f"[ERROR] returncode {res.returncode}:\n{res.stderr}")
            raise RuntimeError(res.stderr)
        return res.stdout

    def transcribe_audio(self, model_size="small", device="cuda"):
        """使用 Faster-Whisper 进行毫秒级语音转录与断句"""
        from faster_whisper import WhisperModel
        print(f"[ASR] Initializing Faster-Whisper model ({model_size})...")
        try:
            model = WhisperModel(model_size, device=device, compute_type="float16")
        except Exception:
            print("[ASR] Fallback to CPU float32...")
            model = WhisperModel(model_size, device="cpu", compute_type="int8")

        all_results = {}
        for fname in sorted(os.listdir(self.video_dir)):
            if fname.endswith(('.mp4', '.mov', '.mkv')):
                fpath = os.path.join(self.video_dir, fname)
                print(f"[ASR] Transcribing {fname}...")
                segments, info = model.transcribe(fpath, beam_size=5, language="zh")
                seg_list = []
                for s in segments:
                    seg_list.append({
                        "start": round(s.start, 2),
                        "end": round(s.end, 2),
                        "text": s.text.strip()
                    })
                all_results[fname] = seg_list

        out_json = os.path.join(self.temp_dir, "whisper_segments.json")
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(all_results, f, ensure_ascii=False, indent=2)
        print(f"[ASR] Segments saved to {out_json}")
        return all_results

    def generate_sub_bass_boom(self, filename="impact_hit.wav", duration=1.2, sample_rate=48000):
        """合成 45Hz~90Hz 带阻尼衰减的影视级 Sub-Bass 重低音 Boom 冲击音效"""
        out_path = os.path.join(self.temp_dir, filename)
        t = np.linspace(0, duration, int(sample_rate * duration), False)
        freq = np.linspace(90, 45, len(t))
        waveform = np.sin(2 * np.pi * freq * t)
        envelope = np.exp(-4.5 * t)
        audio = (waveform * envelope * 32767 * 0.9).astype(np.int16)
        with wave.open(out_path, 'w') as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            stereo = np.column_stack((audio, audio)).flatten()
            wf.writeframes(stereo.tobytes())
        print(f"[AUDIO] Boom acoustic generated at {out_path}")
        return out_path

    def render_hook_card(self, title="别的棉服都被它比下去！", sub="★ 生日专场只推这一款 · 闭眼入 ★"):
        """绘制黄金前3秒超大醒目视觉钩子卡片（绝对避开人物面部）"""
        img = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        # y: 150 ~ 430 安全避障区
        draw.rounded_rectangle([40, 150, 1040, 430], radius=30, fill=(215, 20, 35, 245), outline=(255, 225, 110, 255), width=6)
        f_h1 = ImageFont.truetype(self.font_heavy, 66)
        f_sub = ImageFont.truetype(self.font_msyh, 36)
        draw.text((540, 245), title, font=f_h1, fill=(255, 240, 85), anchor="mm", stroke_width=4, stroke_fill=(110, 10, 10))
        draw.rounded_rectangle([140, 335, 940, 395], radius=24, fill=(18, 18, 22, 230), outline=(255, 215, 0, 220), width=3)
        draw.text((540, 365), sub, font=f_sub, fill=(255, 255, 255), anchor="mm")
        
        out_path = os.path.join(self.temp_dir, "golden3s_hook_card.png")
        img.save(out_path)
        print(f"[VISUAL] Hook card generated at {out_path}")
        return out_path

    def render_default_cover(self, clean_frame_path, output_filename="cover_default.jpg"):
        """生成默认的高点击率 Option 1 爆款大促型封面"""
        base = Image.open(clean_frame_path).convert('RGBA')
        overlay = Image.new('RGBA', base.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        
        # 1. 顶部大标题
        draw.rounded_rectangle([40, 150, 1040, 430], radius=30, fill=(215, 20, 35, 245), outline=(255, 225, 110, 255), width=6)
        f_h1 = ImageFont.truetype(self.font_heavy, 66)
        f_sub = ImageFont.truetype(self.font_msyh, 36)
        draw.text((540, 245), '别的棉服都被它比下去！', font=f_h1, fill=(255, 240, 85), anchor='mm', stroke_width=4, stroke_fill=(110, 10, 10))
        draw.rounded_rectangle([140, 335, 940, 395], radius=24, fill=(18, 18, 22, 230), outline=(255, 215, 0, 220), width=3)
        draw.text((540, 365), '★ 3M新雪丽防寒夹棉 · 假两件连帽爆款 ★', font=f_sub, fill=(255, 255, 255), anchor='mm')
        
        # 2. 底部价格福利腰封
        draw.rectangle([0, 1550, 1080, 1780], fill=(12, 12, 16, 230))
        f_tag = ImageFont.truetype(self.font_heavy, 46)
        f_price = ImageFont.truetype(self.font_heavy, 70)
        f_desc = ImageFont.truetype(self.font_msyh, 34)
        draw.rounded_rectangle([60, 1585, 330, 1665], radius=16, fill=(230, 30, 45, 255))
        draw.text((195, 1625), '生日专场', font=f_tag, fill=(255, 255, 255), anchor='mm')
        draw.text((365, 1625), '到手只要 300+ ！！', font=f_price, fill=(255, 235, 60), anchor='lm', stroke_width=3, stroke_fill=(150, 20, 20))
        draw.text((540, 1720), '★ 高级哑光面料   ★ 立体显瘦捏褶   ★ 可拆卸内胆连帽', font=f_desc, fill=(255, 235, 120), anchor='mm')
        
        final = Image.alpha_composite(base, overlay).convert('RGB')
        out_path = os.path.join(self.output_dir, output_filename)
        final.save(out_path, quality=95)
        print(f"[COVER] Default cover saved to {out_path}")
        return out_path

if __name__ == '__main__':
    pipeline = LiveVideoPipeline("D:/dy-video/project1")
    print("LiveVideoPipeline helper initialized successfully.")
