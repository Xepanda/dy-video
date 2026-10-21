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
| **封面候选提取** | `python src/tools/extract_cover_candidates.py` | 严格正面（双肩深度差 ≤0.055 + **人脸偏航角 ≤12°**）+ 人脸表情门禁（EAR/MAR/清晰度）+ **水平 1:6:1 构图**（人像宽占画布 75% 居中，身高 ≤88% 保全完整人体）+ 衣着一致性检测；输出 `cover_candidates.json`（含全部 metrics） | **Agent 审美裁决**：评估多张候选帧中模特的正面表情与精神风貌，挑选最佳底图（如 `cand_01`） |
| **叙事分镜分析** | `python src/tools/analyze_story_segments.py` | Faster-Whisper 转录 + ASR 语境智能体质检（自动纠正行业错词），按电商漏斗打标分类（Hook、面料、版型、穿搭、黑名单），输出 `story_candidates.json` | **Agent 内容编排**：决定黄金 3 秒开场钩子、产品核心做工与穿搭推荐的组合，剔除价格库存信息 |
| **泛化分镜编译** | `python src/tools/build_plan.py` | **零硬编码**分镜编译：漏斗配额（Hook/面料/版型/穿搭）+ 时长约束 [45,65] + 中文数字价格检测 + 库存/换款黑名单，编译生成 `plan.json` | **替代旧 plan_compiler 的硬编码方案**，可泛化到任意新素材 |
| **镜头边界检测** | `python src/tools/detect_scenes.py` | OpenCV 帧间差分自适应阈值，输出 shot 边界与区间 | 替代旧版硬编码时间窗黑名单 |
| **价格检测器** | `python src/tools/detect_price.py` | 中文数字价格检测（chinese_numeral_v1）：数字串/货币符号/中文数字+块米元/到手价等；码数尺码白名单 | **决策 3 三级防护**第一级：选段排除 + 字幕过滤 + QC 断言 |
| **人脸质量检测** | `python src/tools/detect_face.py` | MediaPipe FaceLandmarker + 姿态引导 ROI：偏航角（solvePnP）/EAR/MAR/人脸清晰度/面积占比 | 补齐封面"主播表情正常"量化门禁 |
| **衣着一致性检测** | `python src/tools/detect_garment_match.py` | 封面人像躯干 ROI vs 商品图：CLIP（可选）/ HSV 直方图（降级） | 纪要"模特身着衣服和商品图片一致" |
| **品牌封面渲染** | `python src/tools/render_cover_tool.py` | MIXCITY 高定 3D 金色浮雕主标 + **金色渐变商品名（决策 2）** + 自动换行 + 居中圆角卡片（左右可配）+ 渲染几何 meta 输出 | **Agent 视觉确认**：验证商品文案、品牌标识与排版效果 |
| **音画强锁组装** | `python src/tools/assemble_video_tool.py` | 依据 `plan.json` 原子化执行物理切片、双轨 PTS 归零、小黄车安全区避让、发光字幕烧录与 Sub-Bass Boom 合成；支持 zoom 推镜 / hflip 镜像 / split 分屏算子 | **Agent 执行调度**：下发组装指令，获取导出视频物理时长与元数据 |
| **自动化质检门禁** | `python src/qc/qc_auditor.py` | constraints.yaml 驱动全项量化断言：时长/分辨率/帧率 30/**响度 −14 LUFS**/**黑帧**/**字幕零价格**/MarginV/封面几何与金色色相/宽占比复测；BLOCK/WARN 分级 | **Agent 验收审计**：检查质检门禁各项指标，全绿则签署交付 |
| **任务契约创建** | `python src/tools/spec_creator.py` | 免安装 GUI 填表小工具，点选原始视频与商品图，配置品类与关键词，输出 `job_spec.json` | **Agent/人工初始化**：装配剪辑任务的基准上下文数据 |
| **封面 OCR 纯净度** | `python src/tools/detect_ocr.py` | rapidocr-onnxruntime（PP-OCR ONNX 轻量版）检测封面背景文字（店招/背景板）面积占比 ≤2%；模板自有元素（品牌/商品名/贴图）豁免 | **V7 封面纯净**：候选淘汰 + QC 双源复测 |
| **决策约束网关** | `python src/agent/decision_gateway.py --demo` | Agent 每次选择（封面/分镜/Hook 卡）先过 constraints BLOCK 校验，不合格拒绝重选；全部决策携带 metrics 快照落册 | **B12**：外部干预同样校验，构造不合格候选 100% 拦截 |
| **阶段缓存原语** | `src/tools/stage_cache.py` | sha256(视频+spec+constraints+核心代码指纹) 阶段产物缓存；6 阶段断点续跑 | **B13**：中断重跑不重算；代码变更自动失效缓存 |
| **批量运行器** | `python src/batch_runner.py --specs-dir specs` | 多素材批量执行 + 已完成素材跳过 + 汇总报告 `logs/batch_report.json` | **B13**：批量 10 条/晚 |
| **LLM 字幕审核** | `src/core/asr_agent_verifier.py`（LLM 分支） | OpenAI 兼容 LLM 上层复核：严格 JSON 契约 + 时间戳绝对守恒 + 零价格护栏 + 两引擎 diff 全量审计 + 不可用自动降级 | **B15**：环境变量 LLM_BASE_URL/LLM_API_KEY/LLM_MODEL 配置 |
| **OpenChatCut 桥接** | `python src/tools/chatcut_bridge.py` | plan.json ↔ OpenChatCut 风格多轨时间线双向转换；人工精修（trim/删除/重排）回注后可重新组装 | **B14**：`--plan x.json --out timeline.json` 导出；`--timeline t.json --plan-out p.json` 回注 |
| **回归测试体系** | `python scripts/run_regression.py` | 三层回归：单元测试（5 套）→ 素材端到端（fixtures 5 条）→ 指标区间锁定比对；`--update-baselines` 显式更新基线 | **B16**：CI 全绿才允许合入；阈值变更必须附回归结果 |

---

## 📁 目录结构

```text
dy-live-ecommerce-editor/
├── contracts/                  # 量化约束契约（constraints.yaml + 4 份 JSON Schema + 纪要映射表）
├── config/                      # 品牌样式预设与抖音安全视域配置
├── data/
│   ├── raw_samples/             # 真实素材测试样例
│   └── work_dirs/               # 运行时切片与过程缓存（自动忽略）
├── output/                      # 交付成品（成片、高定封面、QC报告、Agent决策清单）
├── scripts/                     # 辅助测试工具与探索脚本
├── src/
│   ├── agent/                   # Agent 决策中枢与协同调度层
│   │   └── agent_director.py    # 剪辑总导演中枢 (审视候选池并仲裁决策)
│   ├── core/                    # ASR、漏斗编排、视频组装核心算法
│   │   ├── asr_engine.py        # Faster-Whisper 转录打点
│   │   ├── asr_agent_verifier.py# 语境感知 ASR 字幕智能校验体
│   │   ├── plan_compiler.py     # 兼容壳（已委托 tools/build_plan.py 泛化实现）
│   │   └── video_assembler.py   # FFmpeg 物理音画强锁混流引擎（zoom/hflip/split 算子）
│   ├── graphics/                # MIXCITY 封面排版与重音合成
│   │   ├── cover_renderer.py    # 品牌封面渲染器（金色渐变商品名+自动换行+渲染 meta）
│   │   ├── ass_subtitles.py     # 荧光变色发光字幕生成
│   │   └── sfx_generator.py     # 45~90Hz Sub-Bass Boom 合成
│   ├── tools/                   # 面向 Agent 与 CLI 的原子化确定性工具集
│   │   ├── spec_creator.py      # 免安装 GUI 任务契约填表工具
│   │   ├── detect_price.py      # 中文数字价格检测器（决策 3）
│   │   ├── detect_face.py       # 人脸偏航角/表情/清晰度检测（姿态引导 ROI）
│   │   ├── detect_scenes.py     # 镜头边界检测
│   │   ├── detect_garment_match.py # 衣着一致性检测（CLIP/直方图）
│   │   ├── extract_cover_candidates.py # 严格正面候选帧提取与水平 1:6:1 构图
│   │   ├── analyze_story_segments.py   # 叙事分镜语义分类打标工具
│   │   ├── build_plan.py        # 泛化分镜编译器（零硬编码）
│   │   ├── render_cover_tool.py        # MIXCITY 高定封面参数化渲染工具（含 meta）
│   │   └── assemble_video_tool.py      # 视频物理强锁混流总装工具
│   ├── qc/
│   │   └── qc_auditor.py        # constraints 驱动全量自动化量化质检门禁
│   ├── main_pipeline.py         # Agent 总导演端到端调度入口
│   └── pipeline.py              # 兼容性导入重定向入口
├── tests/                       # 自动化单元测试套件
│   └── test_pipeline_units.py   # 覆盖 ASR 校验、语义分类、Agent 决策、构图算法
├── job_spec.json                # 当前任务契约配置
├── 一键启动填表小工具.bat       # 人工输入素材与商品信息的快捷交互入口
├── requirements.txt             # 依赖清单
└── .gitignore                   # 严格排除大体积素材与运行期缓存
```

---

## 🚀 Agent 协同剪辑操作流程 (Workflow)

### 0. 准备素材与商品信息 (生成任务契约)
剪辑前，用户需提供原始直播录像与商品信息以建立契约上下文。
双击运行根目录的：
```bash
一键启动填表小工具.bat
```
*(或在终端执行 `python src/tools/spec_creator.py`)*
系统将在浏览器自动打开可视化表单页面，点选视频素材、商品平铺图、填写品牌名与核心面料/版型卖点，点击“导出任务文件”即可生成标准 `job_spec.json`。
**Agent 将以此文件为依据，读取素材路径与商品领域指纹，启动后续的检测、判断与决策执行。**

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
- `*_final.mp4`：1080×1920 高清竖屏成片（45~65s，纯产品介绍，100% 物理咬合原声，−14 LUFS 响度归一）；
- `*_cover.jpg`：MIXCITY 风格高点击率品牌封面（水平 1:6:1 构图 + 金色渐变商品名）；
- `*_cover_render_meta.json`：封面渲染几何 meta（QC 像素级复测依据）；
- `qc_report.json`：constraints 驱动的全项量化质检报告（BLOCK/WARN 分级）；
- `asr_verification_report.json`：ASR 语境同音字校正详细审计报告；
- `agent_decision_manifest.json`：Agent 总导演全程决策记录清单（含选段审计与质检警告）。
