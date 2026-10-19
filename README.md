# dy-live-ecommerce-editor (抖音带货直播切片自动化剪辑系统)

一套高确定性、工业级、零口型漂移、零外部 API 费用的**抖音带货短视频全自动化剪辑与高定封面生成系统**。
专为女装带货短视频打造，提炼数小时直播素材为 **45~65 秒** 黄金高转化带货成片，并提供自动化质量门禁与高精度排版封面。

---

## 🌟 核心特性与架构升级

1. **零门槛 GUI 填表交互 (`spec_creator.py`)**：
   - 支持双击运行，点选视频素材、商品平铺图，输入品牌与商品信息，自动生成标准任务契约 `job_spec.json`。
2. **语境感知 ASR 智能体质检验校 (`asr_agent_verifier.py`)**：
   - 前端 Prompt 动态注入：从任务契约自动装配商品属性注入 Whisper 解码先验；
   - 多句滑动语境分析（Discourse Context）：结合前后文消除语义矛盾（如“非常不要老钱 -> 非常显老钱”）；
   - 拼音同音词指纹置换（Pinyin Phonetic Map）：毫秒级自动修正行业错词（“实物/食物”、“半岛棉/半导棉”、“超显瘦/超选瘦”、“人瘦没了/瘦眉了”、“码数/马术”、“卡码/卡马”、“螺纹T/罗恩梯”、“YYDS/歪歪DS”）；
   - 时间戳物理守恒：仅纠错文字层，毫秒级打点时间绝对冻结，保证声画口型 100% 咬合。
3. **姿态感知 CV 智能寻优选帧 (`frame_picker.py`)**：
   - 基于 MediaPipe Pose 33 关键点拓扑空间感知；
   - 结合拉普拉斯边缘清晰度、手部抬升遮胸严厉扣分（-60分）、双脚踝落地立姿加分（+25分）、水平中心偏离约束，精准锁定全身居中无遮挡模特正身帧。
4. **MIXCITY 高定品牌封面参数化排版 (`cover_renderer.py`)**：
   - 1080×1920 竖屏高定封面；
   - 顶部金色 3D 立体渐变品牌主标，底部 Shear 动感右倾斜体，右侧商品圆角卡片居中排版。
5. **四段电商漏斗编排引擎 (`plan_compiler.py`)**：
   - 黄金 3 秒极致 Hook 挖掘 + 进口半岛棉浪漫工艺 + 修身版型展示 + 穿搭松弛老钱风；
   - 纯产品介绍严格过滤：正则阻断网剔除价格（309、调价、原价）与库存逼单（剩20件、最后三条）及配件转场杂质；
   - 连续大块镜头保护（Contiguous Block Preservation）：避免细碎硬切，物理杜绝音画不同步。
6. **FFmpeg 音画物理强锁与动态图层混流 (`video_assembler.py`)**：
   - 双轨 PTS 绝对归零 (`setpts=PTS-STARTPTS`, `asetpts=PTS-STARTPTS`)；
   - 自动原子清空切片缓存，根除缓存碰撞串画；
   - 复合滤镜：0~2.5s 悬浮 Hook 卡片 + 片尾 2.5s 小黄车行动召唤贴片 + 45~90Hz Sub-Bass Boom 重音冲击波 + 变色发光 ASS 字幕。
7. **全自动化量化质检门禁 (`qc_auditor.py`)**：
   - 严格断言：成片与封面存在性、时长（45~65s）、分辨率（1080×1920）、字幕安全视域（MarginV>=220 避让小黄车盲区）、ASR 语境纯净度（同音错词零外泄）。

---

## 📁 目录结构

```text
dy-live-ecommerce-editor/
├── config/                      # 品牌样式预设与抖音安全视域配置
├── data/
│   ├── raw_samples/             # 真实素材测试样例
│   └── work_dirs/               # 运行时切片与过程缓存（自动忽略）
├── output/                      # 交付成品（最终成片、高定封面、QC质检报告）
├── scripts/                     # 辅助脚本与测试工具
├── src/
│   ├── core/                    # ASR、智能选帧、漏斗编排、视频组装核心
│   │   ├── asr_engine.py        # Faster-Whisper 转录打点
│   │   ├── asr_agent_verifier.py# 语境感知 ASR 字幕智能校验体
│   │   ├── frame_picker.py      # MediaPipe 姿态拓扑智能选帧
│   │   ├── plan_compiler.py     # 纯产品介绍漏斗编排器
│   │   └── video_assembler.py   # FFmpeg 物理音画强锁混流引擎
│   ├── graphics/                # MIXCITY 封面排版与重音合成
│   │   ├── cover_renderer.py    # 品牌封面渲染器
│   │   ├── ass_subtitles.py     # 荧光变色发光字幕生成
│   │   └── sfx_generator.py     # 45~90Hz Sub-Bass Boom 合成
│   ├── tools/
│   │   └── spec_creator.py      # 免安装 GUI 填表小工具
│   ├── qc/
│   │   └── qc_auditor.py        # 全量自动化量化质检门禁
│   └── main_pipeline.py         # 端到端总控入口脚本
├── tests/                       # 测试套件
├── job_spec.json                # 当前任务契约配置
├── requirements.txt             # 依赖清单
├── .gitignore                   # 严格排除大体积素材与过程文件
├── 一键启动填表小工具.bat       # Windows 桌面一键交互入口
└── 一键运行全自动剪辑流水线.bat # Windows 桌面一键总装入口
```

---

## 🚀 快速上手 (Quick Start)

### 1. 准备环境
确保安装了 Python 3.10+ 及 FFmpeg（已加入系统环境变量 PATH）：
```bash
pip install -r requirements.txt
```

### 2. 方式 A：GUI 交互式一键生成
在 Windows 上双击运行：
```bash
一键启动填表小工具.bat
```
在弹出的界面中点选直播素材视频、商品图片，输入商品名称与面料关键词，点击“导出任务文件”，即可自动生成 `job_spec.json`。

### 3. 方式 B：全自动流水线一键运行
直接双击运行：
```bash
一键运行全自动剪辑流水线.bat
```
或通过命令行运行：
```bash
python src/main_pipeline.py --spec job_spec.json
```

---

## 🎯 交付成果说明
运行完成后，系统会在 `output/<project_id>/` 目录下生成：
- `*_final.mp4`：1080×1920 高清竖屏成片（45~65s，纯产品介绍，100% 物理咬合原声）；
- `*_cover.jpg`：MIXCITY 风格高点击率品牌封面；
- `qc_report.json`：全项质检通过量化报告；
- `asr_verification_report.json`：ASR 语境同音字校正详细审计报告。
