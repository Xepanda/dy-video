# dy-live-ecommerce-editor (抖音带货直播切片自动化剪辑系统)

一套高确定性、工业级、零口型漂移、零外部 API 费用的**抖音带货短视频 AI Agent 剪辑工具链与高定封面系统**。
专为女装带货短视频打造，提炼数小时直播素材为 **45~65 秒** 黄金高转化带货成片，并提供自动化质量门禁与高精度排版封面。

---

## 🤖 核心架构设计：Agent 参与决策与操作，脚本作为确定性原子工具 (Agent-in-the-Loop Architecture)

传统全自动剪辑方案往往试图用一段死板、黑盒的单体脚本（如 bat/单一 Python 脚本）走到底，极易引发分镜突兀截断、价格与换装杂质外泄、封面模特神态尴尬或姿态歪斜等致命缺陷。

本仓库坚决摒弃死板单一脚本执行模式，采用 **Agent-in-the-Loop（智能体总导演中枢 + 确定性原子工具链）** 体系：
- **底层工具层（Tools as Deterministic Primitives）**：提供纯粹、高确定性、可独立调用的原子 CLI 工具，执行繁重且确定性的音视频计算（CV 骨骼提取、ASR 毫秒级打点、FFmpeg 物理音画锁合、ASS 动态渲染、量化指标质检），并输出结构化 JSON 候选清单；
- **Agent 决策层（Agent as Creative Director）**：由 Agent（或人工运营操作）担任剪辑总导演，对中间候选池行使**主观审美与内容裁决权**：
  1. **封面优选决策**：Agent 审视提取出的候选正身帧，评估模特神态是否自然挺拔，裁决最优封面底图；
  2. **分镜编排决策**：Agent 审阅已消除同音错词的台词候选池，决策讲款节奏与逻辑线，剔除价格/库存/换衣杂质，编排生成 `plan.json`；
  3. **质量验收签署**：Agent 审核 ASR 纠错记录与 QC 审计报告，签字交付或针对未达标项动态重试。

---

## 🛠️ 确定性原子工具链 (Deterministic Toolset)

| 工具模块 | 命令行入口 | 核心职责 | Agent 决策与操作点 |
| :--- | :--- | :--- | :--- |
| **封面候选提取** | `python src/tools/extract_cover_candidates.py` | 严格正面 3D 姿态筛选（双肩深度差 $\le 0.05$），并将模特全身比例固定归一化至画布 **74%**（头顶留白 16%），输出 `cover_candidates.json` 与候选图片 | **Agent 审美裁决**：评估多张候选帧中模特的正面表情与精神风貌，挑选最佳底图（如 `cand_01`） |
| **叙事分镜分析** | `python src/tools/analyze_story_segments.py` | Faster-Whisper 转录 + ASR 语境智能体质检（自动纠正行业错词），按电商漏斗打标分类（Hook、面料、版型、穿搭、黑名单），输出 `story_candidates.json` | **Agent 内容编排**：决定黄金 3 秒开场钩子、产品核心做工与穿搭推荐的组合，剔除价格库存信息，生成 `plan.json` |
| **品牌封面渲染** | `python src/tools/render_cover_tool.py` | MIXCITY 高定 3D 金色浮雕主标 + 动感斜体 + 居中圆角卡片参数化排版渲染 | **Agent 视觉确认**：验证商品文案、品牌标识与排版效果 |
| **音画强锁组装** | `python src/tools/assemble_video_tool.py` | 依据 `plan.json` 原子化执行物理切片、双轨 PTS 归零、小黄车安全区避让、发光字幕烧录与 Sub-Bass Boom 合成 | **Agent 执行调度**：下发组装指令，获取导出视频物理时长与元数据 |
| **自动化质检门禁** | `python src/qc/qc_auditor.py` | 全项量化断言（成片存在、时长 45~65s、1080×1920、字幕安全边距、ASR 纯净度、无黑帧），输出 `qc_report.json` | **Agent 验收审计**：检查质检门禁各项指标，全绿则签署交付 |
| **任务契约创建** | `python src/tools/spec_creator.py` | 免安装 GUI 填表小工具，点选原始视频与商品图，配置品类与关键词，输出 `job_spec.json` | **Agent/人工初始化**：装配剪辑任务的基准上下文数据 |

---

## 📁 目录结构

```text
dy-live-ecommerce-editor/
├── config/                      # 品牌样式预设与抖音安全视域配置
├── data/
│   ├── raw_samples/             # 真实素材测试样例
│   └── work_dirs/               # 运行时切片与过程缓存（自动忽略）
├── output/                      # 交付成品（成片、高定封面、QC报告、Agent决策清单）
├── scripts/                     # 辅助测试工具与探索脚本
├── src/
│   ├── agent/                   # Agent 决策中枢与协同调度层
│   │   └── agent_director.py    # 剪辑总导演中枢 (审视候选池并仲裁决策)
│   ├── core/                    # ASR、智能选帧、漏斗编排、视频组装核心算法
│   │   ├── asr_engine.py        # Faster-Whisper 转录打点
│   │   ├── asr_agent_verifier.py# 语境感知 ASR 字幕智能校验体
│   │   ├── frame_picker.py      # MediaPipe 姿态拓扑智能选帧
│   │   ├── plan_compiler.py     # 纯产品介绍漏斗编排器
│   │   └── video_assembler.py   # FFmpeg 物理音画强锁混流引擎
│   ├── graphics/                # MIXCITY 封面排版与重音合成
│   │   ├── cover_renderer.py    # 品牌封面渲染器
│   │   ├── ass_subtitles.py     # 荧光变色发光字幕生成
│   │   └── sfx_generator.py     # 45~90Hz Sub-Bass Boom 合成
│   ├── tools/                   # 面向 Agent 与 CLI 的原子化确定性工具集
│   │   ├── spec_creator.py      # 免安装 GUI 任务契约填表工具
│   │   ├── extract_cover_candidates.py # 严格正面候选帧提取与比例标准化工具
│   │   ├── analyze_story_segments.py   # 叙事分镜语义分类打标工具
│   │   ├── render_cover_tool.py        # MIXCITY 高定封面参数化渲染工具
│   │   └── assemble_video_tool.py      # 视频物理强锁混流总装工具
│   ├── qc/
│   │   └── qc_auditor.py        # 全量自动化量化质检门禁
│   ├── main_pipeline.py         # Agent 总导演端到端调度入口
│   └── pipeline.py              # 兼容性导入重定向入口
├── tests/                       # 自动化单元测试套件
│   └── test_pipeline_units.py   # 覆盖 ASR 校验、语义分类、Agent 决策、构图算法
├── job_spec.json                # 当前任务契约配置
├── requirements.txt             # 依赖清单
└── .gitignore                   # 严格排除大体积素材与运行期缓存
```

---

## 🚀 Agent 协同剪辑操作流程 (Workflow)

### 1. 环境准备
确保 Python 3.10+ 及 FFmpeg（已加入系统环境变量 PATH）：
```bash
pip install -r requirements.txt
```

### 2. 运行模式

#### 模式一：Agent 自主协同调度 (Autonomous Agent Mode)
Agent 直接调用调度层，依据内置电商决策模型自主完成候选审阅、分镜规划与组装：
```bash
python src/main_pipeline.py --spec job_spec.json
```

#### 模式二：Agent 细粒度干预执行 (Interactive / Override Mode)
Agent 亦可显式介入每一处决策：
1. **生成并审视封面候选**：
   ```bash
   python src/tools/extract_cover_candidates.py --video data/raw_samples/sample.mp4 --out-dir data/candidates/cover_candidates
   ```
   *Agent 查看 `cover_candidates.json` 并选定如 `cand_01`。*

2. **生成并审视台词叙事池**：
   ```bash
   python src/tools/analyze_story_segments.py --video data/raw_samples/sample.mp4 --spec job_spec.json --out-json data/candidates/story_candidates.json
   ```
   *Agent 审阅分镜池，剔除价格信息，调整分镜编排生成 `my_plan.json`。*

3. **指定 Agent 决策执行总装**：
   ```bash
   python src/main_pipeline.py --spec job_spec.json --cover-id cand_01 --plan my_plan.json
   ```

---

## 🎯 交付成果说明

流程执行完成后，系统会在 `output/<project_id>/` 目录下生成：
- `*_final.mp4`：1080×1920 高清竖屏成片（45~65s，纯产品介绍，100% 物理咬合原声）；
- `*_cover.jpg`：MIXCITY 风格高点击率品牌封面（74% 统一模特比例）；
- `qc_report.json`：全项量化质检通过报告；
- `asr_verification_report.json`：ASR 语境同音字校正详细审计报告；
- `agent_decision_manifest.json`：Agent 总导演全程决策记录清单。
