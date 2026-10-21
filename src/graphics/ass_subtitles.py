#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ass_subtitles.py - 抖音安全视域动态变色 ASS 字幕生成器
======================================================
1. MarginV = 240px，居中对齐，严格避开底部小黄车与账号文案；
2. 核心卖点关键词亮黄发光高亮: {\\c&H0000E6FF&}；
3. 输出带 UTF-8 BOM 的 .ass 文件，防止 FFmpeg 中文乱码。
"""

import os
from typing import List, Dict, Any

ASS_HL_YELLOW = "&H0000E6FF&"   # 亮黄变色高亮 (BGR)
ASS_WHITE = "&H00FFFFFF&"

def format_ass_timestamp(seconds: float) -> str:
    """转换秒数为 ASS 格式: H:MM:SS.cs"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"

def highlight_keywords_in_text(text: str, keywords: List[str]) -> str:
    """对文本中的核心卖点词进行黄色高亮着色"""
    result = text
    for kw in keywords or []:
        kw = kw.strip()
        if kw and kw in result:
            result = result.replace(kw, f"{{\\c{ASS_HL_YELLOW}}}{kw}{{\\c{ASS_WHITE}}}")
    return result

def generate_ass_subtitles(
    events: List[Dict[str, Any]],
    output_ass_path: str,
    keywords: List[str] = None
) -> str:
    """生成合规的 .ass 竖屏字幕文件"""
    os.makedirs(os.path.dirname(os.path.abspath(output_ass_path)), exist_ok=True)
    
    header = [
        "[Script Info]",
        "Title: Douyin E-Commerce Subtitles",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "PlayResX: 1080",
        "PlayResY: 1920",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: DouyinSubtitle,Microsoft YaHei,50,&H00FFFFFF,&H000000FF,&H0015151A,&H80000000,-1,0,0,0,"
        "100,100,1,0,1,4.5,2.0,2,40,40,240,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    
    dialogues = []
    for ev in events:
        t_start = format_ass_timestamp(ev["t_start"])
        t_end = format_ass_timestamp(ev["t_end"])
        colored_text = highlight_keywords_in_text(ev["text"], keywords)
        dialogues.append(
            f"Dialogue: 0,{t_start},{t_end},DouyinSubtitle,,0,0,0,,{colored_text}"
        )
        
    full_content = "\n".join(header + dialogues) + "\n"
    with open(output_ass_path, "w", encoding="utf-8-sig") as f:
        f.write(full_content)
        
    print(f"[SUBTITLE] 已生成 {len(events)} 行变色字幕: {output_ass_path}")
    return output_ass_path
