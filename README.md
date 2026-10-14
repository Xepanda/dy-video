# 抖音带货短视频工业化剪辑流水线 (Live E-Commerce Video Pipeline)

[![FFmpeg](https://img.shields.io/badge/FFmpeg-9.0.2-blue.svg)](https://ffmpeg.org)
[![Python](https://img.shields.io/badge/Python-3.10%2B-brightgreen.svg)](https://python.org)
[![Whisper](https://img.shields.io/badge/Whisper-Faster--Whisper-orange.svg)](https://github.com/SYSTRAN/faster-whisper)
[![Skill](https://img.shields.io/badge/Skill-live--ecommerce--video--editor-red.svg)](skills/live-ecommerce-video-editor/SKILL.md)

一套开箱即用、标准化、高转化率的**直播带货素材短视频全自动化剪辑工程仓库**。
专为抖音/TikTok/视频号/快手带货短视频打造，提炼数小时直播原素材为 **45~65 秒** 黄金短视频。

---

## 🌟 核心特色与剪辑规范

1. **黄金 3 秒法则（The Golden 3-Second Rule）**：
   - 非线性重组：将整场最极端、最煽动的重音金句切片前置到 0.0s~2.5s；
   - 视觉冲击大卡：980px 超大金红吸睛卡片，精确定位在人物帽子上方安全区，**绝不遮挡模特面部**；
   - 听觉冲击：0.0s 混入 45Hz~90Hz 带阻尼衰减的影视级 Sub-Bass 重低音 Boom 冲击音效。
2. **镜头语言与特效**：
   - **推镜特写（Detail Punch Zoom）**：核心材质与做工讲解处应用 1.3× 局部放大特写；
   - **镜像防反文字贴纸（Mirror Masking）**：`hflip` 翻转画面时，自动用营销徽章贴纸覆盖背景反字；
   - **对称双色真实对比（Symmetrical Split-Screen）**：左右 50:50 等比分屏，模特真实穿搭对比（藏青 vs 米白），中缝呼吸发光中线。
3. **动态发光字幕（Keyword Highlight ASS）**：
   - 30+ 条毫秒级对齐台词；核心卖点词（3M新雪丽、棉服、300多块）亮黄色发光高亮；底边距 `MarginV=240` 避开小黄车。
4. **高点击率封面设计矩阵（High-CTR Covers）**：
   - 默认采用 Option 1 爆款大促促销型封面（底图为纯净无字正帧 + 底部 300+ 价格腰封 + 原生实心星号排版）。

---

## 📁 仓库目录结构

```text
d:\dy-video/                    # 纯净代码与技能仓库（可推送到 GitHub/GitLab）
├── docs/                        # 项目说明文档与工具配置指南
│   ├── tools_setup.md           # 各种工具下载、安装、环境变量配置指南
│   └── knowledge_base/          # 核心知识库与经验沉淀
│       └── ai_video_engines_playbook.md # ASR、TTS、Google Flow、三大渲染引擎实测对比与排坑宝典
├── workflows/                   # 标准化作业流程 (SOP)
│   └── standard_pipeline.md     # 从原素材到成片的 6 步标准流水线
├── skills/                      # 自媒体/AI Agent 专属技能包
│   └── live-ecommerce-video-editor/
│       └── SKILL.md             # 完整的短视频剪辑技能定义与实战规则
├── src/                         # 工业化自动化处理脚本模块
│   └── pipeline.py              # ASR 转录、低音合成、大标题绘制、封面生成核心类
├── tools/                       # 辅助小工具与便捷脚本
├── .gitignore                   # 严格排除音视频、素材及过程临时文件
├── README.md                    # 仓库说明与上手指南
└── requirements.txt             # Python 依赖清单

d:\dy-video-projects/           # 本地独立视频工程工作区（不入库，仅本地高效管理）
├── _template_project/           # 新视频项目空白标准模板
├── README.md                    # 视频工程管理规范说明
└── project1_jacket/             # 本次实战项目（新雪丽小棉服带货短视频）
    ├── 01_raw_materials/        # 原始视频/音频/图片素材
    ├── 02_scripts_and_docs/     # Whisper 转录台词、时间戳、分镜策划文稿
    ├── 03_intermediate/         # 过程切片(clips)、贴纸(stickers)、ASS发光字幕
    └── 04_final_output/         # 63.8s 最终成片及封面设计矩阵
```

---

## 🚀 跨电脑快速上手指南 (Quick Start)

### 1. 准备环境 (耗时约 3~5 分钟)
1. 确保电脑已安装 `FFmpeg` 并加入系统环境变量（详细步骤见 [docs/tools_setup.md](docs/tools_setup.md)）。
2. 安装 Python 依赖：
   ```bash
   pip install -r requirements.txt
   ```

### 2. 运行示例或开始新项目
若要为新素材剪辑，仅需将录像存入 `project_name/video/`，并遵循 [workflows/standard_pipeline.md](workflows/standard_pipeline.md) 操作流程：

```python
from src.pipeline import LiveVideoPipeline

# 初始化项目
pipeline = LiveVideoPipeline("project1")

# 1. 毫秒级语音转录与台词定位
pipeline.transcribe_audio(model_size="small")

# 2. 生成低音冲击音效与黄金标题卡
pipeline.generate_sub_bass_boom()
pipeline.render_hook_card(title="别的棉服都被它比下去！", sub="★ 生日专场只推这一款 · 闭眼入 ★")

# 3. 产出默认高转化率封面
pipeline.render_default_cover("project1/temp/raw_white_worn.jpg")
```

---

## 🎯 交付成果验收 (Project 1)
- **成片文件**：[douyin_jacket_final.mp4](file:///D:/dy-video/project1/output/douyin_jacket_final.mp4)（时长 63.82s，1080x1920，60fps）
- **默认封面**：[cover_default.jpg](file:///D:/dy-video/project1/output/cover_default.jpg)
- **全流程文档**：
  - [标准作业流程 SOP](file:///D:/dy-video/workflows/standard_pipeline.md)
  - [工具安装配置指南](file:///D:/dy-video/docs/tools_setup.md)
  - [自媒体 Agent Skill](file:///D:/dy-video/skills/live-ecommerce-video-editor/SKILL.md)
