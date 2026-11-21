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
| **违规词检测** | `python src/tools/detect_violation.py --ass subtitles.ass` | 抖音极限词库（`contracts/violation_words.yaml`，飞书《违规整合》镜像）全量检测：ASR 台词 / 成片字幕 / 画面 OCR 三类输入 | **反馈 2026-10-27**：词库数据驱动，增删词条不改代码 |
| **违规词三通道处理** | `python src/tools/apply_violation_handling.py --plan plan.json --segments ... ` | 命中词三通道彻底处理：字幕屏蔽（`***`）+ 语音消音（区间 volume 表达式）+ 视频打码（OCR 定位像素化）；产出 `violation_handling.json` | **反馈 2026-10-27**：QC 对成片字幕零残留 BLOCK 断言 |
| **完整对话脚本** | `python src/tools/build_dialogue_script.py --spec job_spec.json --segments ...` | ASR 段合并为完整语句（utterance），标注句末完整标记/漏斗类目/违规命中，作为剪辑唯一剧本 | **反馈 2026-10-27**：句子边界吸附 + acrossfade 转场配套 |
| **一致性预检** | `python src/tools/check_spec_material_consistency.py --spec job_spec.json --segments ...` | 素材-契约一致性：关键词覆盖率/商品名词覆盖率/衣着一致性/违规词扫描，防"挂羊头卖狗肉" | **反馈 2026-10-27**：2026-10-26 马甲/针织衫错配教训的产物 |
| **叠加层人脸安全** | `python src/tools/check_overlay_face_safety.py --video final.mp4 --overlay-meta ...` | 贴纸/Hook 卡/字幕带/放大镜等叠加元素与模特面部的几何交叠审核（压脸 BLOCK） | **反馈 2026-10-27**：特效不得影响模特主体（尤其是面部） |
| **成片 ASR 复检** | `python src/tools/verify_final_video_asr.py --video final.mp4 --ass subtitles.ass --work-dir ...` | 对拼接后完整视频重新转录，五项检查：V1 字幕-原声一致性 / V2 语音违规词零残留 / V3 语音价格零残留 / V4 识别密度健全性 / V5 消音执行核查（核心区实测静音） | **2026-10-27 闭环**：素材 ASR 三重校验 + 成片 ASR 复检，双向闭环"物理咬合原声"承诺 |
| **商品图封面适配** | `python src/tools/adapt_product_cover.py --image product.png --out cover.jpg --mode extend` | 商品图直接作封面的自然过渡：背景延展（镜像/replicate）/ 边缘羽化（距离变换）/ 渐变融合（去晕）/ 阴影 / 圆角，四模式（card/blend/extend/collage）× 任意尺寸背景 × 强度可配 | **2026-10-28**：`verify_cover_adaptation.py` 六项量化验证（羽化剖面/无硬边界/防 halo/阴影/留白）；`remove_white_background` 白底去除（flood fill 连通域保护主体）已集成为封面右侧贴图默认样式（`sticker.sticker_style: cutout`） |
| **切片规则引擎** | `python -m src.tools.slicing_rules --spec job_spec.json` | 把参考成品实测的切片规律变成**可判定**规则（R1 切点落 ASR 句末 / R2 片段时长区间 / R3 全身景别 / R4 展示角度多样性 / R5 语音密度 / R6 漏斗顺序可按品类覆写），阈值唯一事实源 = `contracts/constraints.yaml` 的 `slicing_rules` 段 | **2026-11-01**：证据见 `docs/reference_cut_analysis_SSY410163.md`；配套 `detect_speech_pauses.py`（语音气口，**相对凹陷**判据——直播底噪 −42~−53dB，绝对阈值命中 0 处）+ `extract_shot_features.py`（画面特征 span/肩宽/人脸/织物）；规则细节见 `workflows/standard_pipeline_v2.md` §13，检查项见 `docs/slicing_rules_checklist.md` |
| **剪辑风格校验** | `python -u scripts/verify_cut_style.py --video 成片.mp4 --cuts 1.0,2.0 --segments asr.json` | 对成片做**转场/音频/调色/节奏**四项风格的同口径实测与判定（R7 硬切 vs 叠化 / R8 Boom·BGM·响度·LRA / R9 段间调色变异 / R10 钩子密度·句长·语速·收紧比），可与参考成片直接对照 | **2026-11-02**：证据见 `docs/reference_cut_style_analysis.md`；实测参考片 **9/9 全硬切**（单帧 Δα 0.41~0.90、零叠化）、**零音效零 BGM**（低中频比 −21.2dB）、段间调色变异 0.8%~7.8%、节奏为「钩子快→中段慢→尾段再起」；规则细节见 `workflows/standard_pipeline_v2.md` §14，检查项见 `docs/slicing_rules_checklist.md` |

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
启动任务配置器.exe  # 或 SpecCreator.exe
├── requirements.txt             # 依赖清单
└── .gitignore                   # 严格排除大体积素材与运行期缓存
```

---

## 🚀 Agent 协同剪辑操作流程 (Workflow)

> **2026-10-27 工作流重构**（反馈驱动）：封面全幅底图、ASR 驱动完整对话剪辑、
> 转场/放大镜特效集成、违规词三通道处理、全阶段审核机制。
> 完整阶段宪法见 [`workflows/standard_pipeline_v2.md`](workflows/standard_pipeline_v2.md)。

## 🚀 V3.2 Agent-First 协同剪辑操作流程 (Workflow)

> **2026-11-21 V3.2 架构重整（ADR-001）**：外部 Agent（AGY / Codex / Claude Code）作为工作流执行者与总导演；Windows EXE 仅用于配置项目与生成任务交接；通用确定性 CLI（`python -m src.cli`）执行媒体处理与双层质量验收硬门禁。
> 详见 [`docs/superpowers/plans/2026-11-21-adr-001-agent-first-architecture.md`](docs/superpowers/plans/2026-11-21-adr-001-agent-first-architecture.md)。

### 0. 准备素材与任务交接 (Windows EXE 向导)
用户只需完成基本点选操作，无需在 EXE 内选择/登录/启动 Agent：
1. 双击运行根目录的：
   ```bash
   启动任务配置器.exe  # 或 SpecCreator.exe
   ```
2. 在可视化向导中点选或新建项目目录、勾选原始直播录像素材、添加商品平铺参考图、确认商品事实与风格。
3. 点击 **「生成任务并交给 Agent」**：
   - 自动生成原子配置 `素材/job_spec.json`；
   - 自动在项目根目录生成 `AGENT_TASK.md` 任务指南；
   - 提供一键复制的标准任务指令，方便直接粘贴给外部 Agent。

---

### 1. 外部 Agent 通用 CLI 阶段化流水线 (Universal CLI Pipeline)

外部 Agent 获取项目交接后，在终端按以下阶段依次调度 CLI：

#### 阶段 1: 准备环境与资产白名单 (`prepare`)
```bash
python -m src.cli prepare --spec "<path_to_job_spec.json>"
```
- 严格解析并登记用户确认的源视频入库 `input_manifest.json`；
- 隔离所有历史成片与系统导出文件，绝对避免旧文件二次污染；
- 冻结 `spec_snapshot.json` 并生成 `work/<run_id>/` 隔离工作区。

#### 阶段 2: 多模态候选池分析与哈希冻结 (`analyze`)
```bash
python -m src.cli analyze --run "<work_dir>"
```
- 分段执行 ASR 转录、停顿气口对齐、镜头边界切点与最佳封面候选提取；
- 依据商品参考图与 SkuTracker 判定商品相关性（`SAME_SKU`, `ACCESSORY`, `OTHER_SKU`, `UNCERTAIN`）；
- 导出 `analysis/candidates.json`，并由工具计算不可篡改的 `candidate_pool_hash` 冻结候选池。

#### 阶段 3: Agent 审美裁决与编排提案 (Directorial Decision)
外部 Agent 审阅候选清单与多模态证据，选定镜头并在 `<work_dir>/decisions/sequence_proposal.json` 提交提案：
```json
{
  "schema_version": "3.2",
  "run_id": "<run_id>",
  "candidate_pool_hash": "<candidate_pool_hash>",
  "proposal_id": "agent_proposal_01",
  "ordered_candidate_ids": ["c_src_01_001", "c_src_02_005"],
  "chosen_cover_id": "cand_01",
  "style": "balanced",
  "decision_reasons": ["开场 2.5s 痛点抓取完整", "核心羊毛混纺质感展示充分", "行动号召语义完整"]
}
```

#### 阶段 4: 提案审计与计划校验 (`validate-plan`)
```bash
python -m src.cli validate-plan --run "<work_dir>" --proposal "<work_dir>/decisions/sequence_proposal.json"
```
- 硬门禁校验：候选 ID 白名单校验、同源镜头时间戳防重叠校验、总时长合法性 [45, 65]s 校验、封面门禁校验；
- 校验通过后编译出冻结的 `validated_plan.json`。

#### 阶段 5: 物理混流装配与封面渲染 (`render`)
```bash
python -m src.cli render --run "<work_dir>"
```
- FFmpeg 物理音画 PTS 锁定装配，统一 60FPS (1080×1920) 高清输出；
- 烧录动态发光字幕、Hook 标题卡 (720×168) 与挂角贴纸；
- 先在临时文件渲染，校验完整性后原子移动至 `output/<run_id>/final.mp4` 与 `cover.jpg`。

#### 阶段 6: 双层质量验收门禁 (`qc`)
```bash
python -m src.cli qc --run "<work_dir>"
```
- **工程技术 QC**：黑帧、静音、响度（-14 LUFS）、字幕零价格、封面几何与清晰度复测；
- **内容 Editorial QC**：违规极限词扫描、原声口播完整度、卖点真实性。

#### 阶段 7: 正式出库交付签署 (`deliver`)
```bash
python -m src.cli deliver --run "<work_dir>"
```
- 硬核准则：**只有技术 QC PASS 且内容 QC PASS 且任务指纹与文件真实有效时，才允许签发出库**；
- 生成 `output/<run_id>/delivery_manifest.json`，并原子更新 `output/latest.json`。

---

## 🎯 交付成果说明

流程执行完成后，系统会在 `output/<run_id>/` 目录下生成：
- `final.mp4`：1080×1920 60FPS 高清竖屏成片（45~65s，纯产品介绍，100% 物理咬合原声，−14 LUFS 响度归一）；
- `cover.jpg`：MIXCITY 风格高点击率品牌封面（水平 1:6:1 构图 + 金色渐变商品名）；
- `copywriting.md`：自动生成的带货文案与核心卖点提炼；
- `technical_qc.json`：constraints 驱动的工程技术量化质检报告；
- `editorial_qc.json`：内容合规与语境事实审核报告；
- `delivery_manifest.json`：带有全局数字指纹的双层质检交付清单。
