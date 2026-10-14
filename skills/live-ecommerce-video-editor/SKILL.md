---
name: live-ecommerce-video-editor
description: Use when turning raw livestream or long e-commerce video recordings into high-converting 45-65s short videos (Douyin/TikTok/Reels/Shorts) with Golden 3-Second hooks, automated Whisper ASR transcription, detail punches, mirror flipping with text-occlusion badges, symmetrical color/style split comparisons, dynamic keyword-highlight ASS subtitles, and high-CTR marketing covers.
---

# Live E-Commerce Video Editor (直播带货短视频全流程剪辑与工业化封包)

## Overview
本 Skill 总结并固化了一套成熟的**直播带货素材全流程工业化短视频剪辑标准与实战规范**。
将数小时的漫长直播录像提炼为 **45~65 秒** 高停留、高转化、视觉节奏紧凑的爆款短视频（含黄金 3 秒法则、细节特写推镜、文字防反镜像贴纸、对称同比例双色/款式对比、发光变色字幕、尾帧强转化 CTA、以及高点击率封面设计矩阵）。

---

## 核心流程全景 (Pipeline Workflow)

```
[原始直播素材录像]
       │
       ▼ (1. 毫秒级语音转录与台词时间戳定位)
[Faster-Whisper ASR JSON (Segment / Word Timestamps)]
       │
       ▼ (2. 黄金 3 秒法则重组 & 核心利益点切片)
[精选 8~12 个关键 Clips (总时长严格控制在 45s~65s)]
       │
       ├─► 镜像翻转片段 (hflip) ──► 动态标签遮挡反向文字 (Mirror Mask)
       ├─► 做工细节片段 (Zoom)  ──► 1.25x~1.35x 局部放大推镜 (Detail Punch)
       ├─► 多款式对比 (Split)   ──► 50:50 等比分屏 + 模特同比例上身 + 居中发光分割线
       │
       ▼ (3. 视觉与听觉钩子增强)
[黄金3秒超大震撼标题卡 + Sub-Bass Boom 冲击音效 + 6款阶段式利益点挂角徽标]
       │
       ▼ (4. 动态发光同步字幕烧录)
[.ass 格式字幕，核心关键词亮黄色发光变色，规避下挂购物车盲区]
       │
       ▼ (5. 尾部 CTA 转化闭环与高清混流)
[尾部 2~3s 下单引导气泡/提示 + 成片 1080x1920 60FPS]
       │
       ▼ (6. 爆款高点击率封面矩阵制作)
[默认首推：Option 1 爆款大促型封面（实心金星排版、模特原身利落穿搭、底部300+价格腰封）]
```

---

## 1. 黄金 3 秒法则 (The Golden 3-Second Rule)

短视频完播率与播放量的 80% 决定于前 3 秒。
1. **打破素材线性时间顺序 (Re-ordering Hook)**：
   - 绝不从“大家好”、“今天给大家看”开始。
   - 提取整场直播中**最极端、最煽动、最具对比性**的一句话（如：“别的棉服都被它比下去！”、“今年生日专场只排了这一个！”）强行剪切到 0.0s~2.5s。
2. **超大视觉标题卡 (Visual Punch Hook)**：
   - 宽度控制在 940~980px（几乎占满横向安全区），高度 160~180px。
   - **安全避障法则**：标题定位于 `y = 150 ~ 430` 区间（覆盖背景顶部杂乱店名招牌），必须严格与人物帽子/头顶（`y >= 460`）保持安全距离，**绝对不能遮挡模特五官与眼神**。
   - 搭配立体加粗金黄发光大字 + 醒目副标胶囊（“★ 生日专场只推这一款 · 闭眼入 ★”）。
3. **音频冲击混音 (Acoustic Hit)**：
   - 0.0s 处混入 45Hz~90Hz 带阻尼衰减的影视级 Sub-Bass 重低音 Boom 冲击音效，放大语言冲击力。

---

## 2. 镜头语言与特效剪辑规范

### 2.1 镜像翻转 (Horizontal Flip) 与文字防反遮罩
- **问题**：在将部分片段镜像（`hflip`）打破站位单一感时，背景中的招牌文字、LOGO、衣架标识也会跟着被反向镜像，极度违和。
- **解决方案**：
  - 凡是带有文字背景的画面在执行 `hflip` 时，**必须使用大尺寸营销贴纸、品质标签或标题框精确覆盖反向文字区域**。
  - 滤镜管线示例：`[v]hflip[vf]; [vf][sticker]overlay=x=50:y=200[outv]`。

### 2.2 细节推镜放大特写 (Detail Punch-in Zoom)
- **应用场景**：讲解面料材质（如 3M 新雪丽、羊绒、真丝）、走线缝头、防风内胆门襟、立体捏褶、袖口做工。
- **参数标准**：
  - 放大倍率：`1.25x ~ 1.35x`。
  - 锚点居中：聚焦在衣物展示部位（通常位于模特胸腹区 `y = 0.35h ~ 0.65h`）。
  - FFmpeg 实现公式：
    ```bash
    scale=1.3*1080:-1,crop=1080:1920:(in_w-1080)/2:(in_h-1920)/2
    ```

### 2.3 多款式/颜色对比展示 (Symmetrical Split Comparison)
- **核心原则**：
  1. **左右大小严格一致**：双屏各占 540x1920，主体人物在各自半屏中居中，肩宽与全身比例需通过缩放或微调裁剪严格平衡。
  2. **必须是不同款式/不同颜色的真实上身对比**：切忌一边是穿上身，一边是单纯用手提着衣服。左右必须均展现模特试穿效果（如：左边藏青色 + 右边米白色）。
  3. **视觉分界与专属标签**：
     - 中缝添加 6~10px 宽度金色发光垂直分割线。
     - 左右两侧各附带独立胶囊标签（如左边：`帅气藏青 · 显瘦利落`；右边：`气质米白 · 温柔百搭`）。

---

## 3. 字幕系统与安全视域规范 (.ass)

1. **避免 FFmpeg Windows 路径转义 Bug**：
   - 在 Windows 环境下，`subtitles='D:/path'` 会因为冒号解析失败引发 `original_size` 报错。
   - **最佳实践**：在字幕所在的当前目录运行 FFmpeg（`cwd="temp_dir"`），使用相对路径 `subtitles=douyin_subtitles.ass`。
2. **抖音/视频号/快手 安全视域规范**：
   - 底部 `0 ~ 200px` 为平台账号名字、文案、小黄车/商品锚点区域。
   - 右侧 `880 ~ 1080px` 为点赞、评论、分享转评赞互动挂件。
   - **字幕安全区**：字幕 `MarginV` 设为 `240`，文字居中（`Alignment=2`），字号 `48~52pt`，完美避开小黄车遮挡。
3. **高转化字幕样式规范**：
   - 字体：微软雅黑粗体 / 黑体。
   - 颜色设计：主体文字为白底黑边（阴影深度 2px），核心卖点词（如“棉服”、“3M新雪丽”、“保暖”、“300多块”、“显瘦”）使用样式覆盖代码 `{\c&H003CEBFF&}` 替换为**亮黄色发光强调**。

---

## 4. 爆款视频封面设计矩阵 (High-CTR Covers)

短视频发布必须配备 1080x1920 高清封面。**首选默认采用 Option 1（爆款大促促销型）**：

### 4.1 核心推荐：Option 1 爆款促销型（转化率最高）
- **视觉构成**：
  1. **底图选择**：模特自然穿搭、眼神交流自信、衣物版型展示完整的**纯净无字正帧**（如米白款全身/半身）。
  2. **顶部主标**：`y = 150 ~ 430`，大红底搭配亮金字「**别的棉服都被它比下去！**」，副标黑金胶囊「**★ 3M新雪丽防寒夹棉 · 假两件连帽爆款 ★**」，彻底覆盖背景杂乱店名，且距头顶有充足净空。
  3. **底部福利腰封**：`y = 1550 ~ 1780`，左侧红色「**生日专场**」圆角标签 + 亮黄色加粗大字「**到手只要 300+ ！！**」，下方附带 3 大核心利益点：
     `★ 高级哑光面料   ★ 立体显瘦捏褶   ★ 可拆卸内胆连帽`（注：统一采用通用支持的实心星号 `★`，避免符号缺字方框）。

### 4.2 备选风格矩阵：
- **Option 2: 双色对比型**：左右 50:50 藏青 vs 米白同时上身对比 + 顶部“今冬闭眼入”横幅（适合用户纠结选色场景）。
- **Option 3: 穿搭种草型**：模特自然生活感穿搭 + 顶部紧凑黑金轻奢标题（`y=110~370`）+ 悬浮细节标签气泡（适合轻奢小资受众）。

---

## 5. 尾部 CTA 转化闭环 (Call to Action)

带货视频最后 2~3 秒必须有促单收口，切忌声音戛然而止：
- **音频**：收录主播“姐妹们赶紧冲”、“生日福利只有这一次，拍下立减”等收单金句。
- **画面**：最后 2 秒在左下角或右上角浮现动态指向提示（如：“👉 戳左下方小黄车领券抢购”、“仅此一批 · 售完无补”），直接承接平台小黄车挂载逻辑。

---

## 6. Python 工业化自动化脚本代码模板

### 6.1 爆款促销封面自动绘制脚本
```python
from PIL import Image, ImageDraw, ImageFont

def render_promotion_cover(clean_frame_path, output_path):
    base = Image.open(clean_frame_path).convert('RGBA')
    overlay = Image.new('RGBA', base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    
    font_heavy = 'C:/Windows/Fonts/simhei.ttf'
    font_msyh = 'C:/Windows/Fonts/msyhbd.ttc'
    
    # 1. 顶部大标题 (y=150~430，完全覆盖背景，与头顶保持安全距离)
    draw.rounded_rectangle([40, 150, 1040, 430], radius=30, fill=(215, 20, 35, 245), outline=(255, 225, 110, 255), width=6)
    f_h1 = ImageFont.truetype(font_heavy, 66)
    f_sub = ImageFont.truetype(font_msyh, 36)
    draw.text((540, 245), '别的棉服都被它比下去！', font=f_h1, fill=(255, 240, 85), anchor='mm', stroke_width=4, stroke_fill=(110, 10, 10))
    draw.rounded_rectangle([140, 335, 940, 395], radius=24, fill=(18, 18, 22, 230), outline=(255, 215, 0, 220), width=3)
    draw.text((540, 365), '★ 3M新雪丽防寒夹棉 · 假两件连帽爆款 ★', font=f_sub, fill=(255, 255, 255), anchor='mm')
    
    # 2. 底部价格福利腰封 (y=1550~1780)
    draw.rectangle([0, 1550, 1080, 1780], fill=(12, 12, 16, 230))
    f_tag = ImageFont.truetype(font_heavy, 46)
    f_price = ImageFont.truetype(font_heavy, 70)
    f_desc = ImageFont.truetype(font_msyh, 34)
    draw.rounded_rectangle([60, 1585, 330, 1665], radius=16, fill=(230, 30, 45, 255))
    draw.text((195, 1625), '生日专场', font=f_tag, fill=(255, 255, 255), anchor='mm')
    draw.text((365, 1625), '到手只要 300+ ！！', font=f_price, fill=(255, 235, 60), anchor='lm', stroke_width=3, stroke_fill=(150, 20, 20))
    draw.text((540, 1720), '★ 高级哑光面料   ★ 立体显瘦捏褶   ★ 可拆卸内胆连帽', font=f_desc, fill=(255, 235, 120), anchor='mm')
    
    final = Image.alpha_composite(base, overlay).convert('RGB')
    final.save(output_path, quality=95)
```

### 6.2 低音冲击波合成 (Acoustic Boom)
```python
import numpy as np, wave

def generate_sub_bass_boom(filename="impact_hit.wav", duration=1.2, sample_rate=48000):
    t = np.linspace(0, duration, int(sample_rate * duration), False)
    freq = np.linspace(90, 45, len(t))
    waveform = np.sin(2 * np.pi * freq * t)
    envelope = np.exp(-4.5 * t)
    audio = (waveform * envelope * 32767 * 0.9).astype(np.int16)
    with wave.open(filename, 'w') as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        stereo = np.column_stack((audio, audio)).flatten()
        wf.writeframes(stereo.tobytes())
```

---

## 7. 避坑指南 (Common Pitfalls)

| 陷阱 (Pitfall) | 潜在危害 | 规范解法 |
| :--- | :--- | :--- |
| **`hflip` 未做文字遮挡** | 背景文字倒置，破坏高端品牌质感 | 使用大底色卖点徽章贴纸覆盖背景反字 |
| **分屏模特大小失衡** | 左右对比失调，视觉焦点被一边吞噬 | 左右两路统一裁剪 `(270, 0, 810, 1920)`，确保等身比例 |
| **大标题盖脸** | 遮挡主播颜值与眼神互动，掉停留率 | 测定头顶坐标（`y≈460`），标题框底边控制在 `y<=430`（保留净空） |
| **封面字形缺失渲染方框** | Unicode 特殊符号未匹配字体变为 `□` | 统一采用通用支持的实心星号 `★` 或代码绘制图形 |
| **字幕压小黄车挂件** | 用户点小黄车时手滑遮挡关键利益点 | 字幕 `MarginV` 设为 `240`，留出底部安全视域 |
| **单图 `-loop 1` 挂起死锁** | FFmpeg 以后台模式持续运行不终止 | 必须追加 `-t [视频实际时长]` 或 `-shortest` 参数 |
