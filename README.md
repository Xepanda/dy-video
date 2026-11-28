# dy-live-ecommerce-editor (抖音带货直播切片 AI 视觉导演剪辑系统 V3.3)

一套高确定性、工业级、零口型漂移、零外部视觉 API 费用的**抖音带货短视频 AI Agent 视觉导演剪辑系统与高定封面工具链**。
聚焦女装直播切片带货，将多段数小时直播素材提炼为 **45~65 秒** 黄金高转化成片，配套完整防篡改哈希链、视觉联系表审片、成片接缝回看 Critic Loop 与双层质量硬门禁。

---

## 核心架构：Agent-First 视觉导演总控 + 确定性媒体工具链（ADR-001 & ADR-002）

* **Windows EXE（无 Agent 依赖配置器）**：只负责点选素材、配置商品事实与保存 `job_spec.json` + `AGENT_TASK.md`，不隐式启动任何 Agent 或渲染进程。
* **外部多模态 Agent（AGY / Codex / Claude Code = 剪辑总导演）**：阅读压缩后的 `director_brief.md`，直接看 **4×4 分镜联系表（Contact Sheets）** 与 **封面对比拼板（`cover_sheet.jpg`）** 用“眼睛”选片、微调切点/特效/贴纸、审阅灰区片段，并在渲染后通过 `inspect-render` 视觉回看接缝拼图（`seams_review.jpg`）进行局部自修复。
* **通用确定性 CLI（`python -m src.cli` = 媒体计算与防篡改硬门禁）**：执行 64 位源素材 SHA-256 白名单登记、ASR 与相对凹陷气口检测、顺序 3 点（25%/50%/75%）抽帧、Pose 躯干 ROI + 轻量 ONNX（自动降级 ROI HSV）混合视觉打分、FFmpeg 60FPS 物理音画锁合与双层 QC 门禁签署。

> 架构决策与升级文档：
> - [ADR-001: Agent-First 控制权架构](docs/superpowers/plans/2026-11-21-adr-001-agent-first-architecture.md)
> - [ADR-002: V3.3/V3.4 视觉导演、混合视觉感知与灰区放权](docs/superpowers/plans/2026-11-24-adr-002-visual-director-and-hybrid-perception.md)
> - [V3.3/V3.4 架构升级总方案](docs/dy-video-v3.3-v3.4-architecture-upgrade-plan.md) · [可执行任务卡](docs/dy-video-v3.3-v3.4-task-cards.md)

---

## V3.3 视觉导演全流程（Universal CLI Pipeline）

### 0. 素材配置与商品链接一键抓取（精简 4 步 Windows EXE 向导 / CLI）
1. 运行根目录 `启动任务配置器.exe`（或 `SpecCreator.exe`）：
   - **① 项目工作区**：选择已有项目（支持 `raw/`、`素材/`、`materials/`、`input/`）或新建项目；
   - **② 素材与商品链接**：直接粘贴抖音/抖店商品链接，点击 **「⚡ 一键解析商品」** 自动抓取商品名称、品牌、款号、品类、事实卖点与高清主图（下载至 `<project_root>/product_ref/`，彻底省去手动填写“商品与标签”步骤；需要微调时可展开折叠区）；勾选原始直播视频；
   - **③ 剪辑与风格**：确认时长、节奏、字幕与封面策略卡片；
   - **④ 生成任务与交付**：点击 **「📋 生成任务并交给 Agent」**，一键复制任务指令交给本地多模态 Agent（AGY / Codex / Claude Code）。
2. 或直接通过 CLI 一键从链接生成 `job_spec.json`：
```bash
python -m src.cli fetch-product --url "<抖音商品链接>" --project "<项目目录>" --spec-out "<job_spec.json输出路径>"
```
> 仅读取公开商品基础信息与主图，绝不绕过登录墙或验证码；遇到风控拦截会明确上报 `BLOCKED`。

### 1. 初始化环境与资产白名单（`prepare`）
```bash
python -m src.cli prepare --spec "<path_to_job_spec.json>"
```
- 登记显式确认的源视频与 64 位完整 SHA-256 到 `input_manifest.json`，自动隔离历史成片；
- 加载品类档案（默认 `contracts/profiles/womenswear.yaml`），冻结 `spec_snapshot.json` 与 `spec_fingerprint`。

### 2. 多模态分析、顺序 3 点抽帧与视觉导演简报（`analyze`）
```bash
python -m src.cli analyze --run "<work_dir>"
```
- 执行镜头边界检测、相对凹陷语音气口检测、ASR 分句对齐、单次打开源视频顺序 3 点（25%/50%/75%）批抽；
- 运行混合视觉引擎（Pose 躯干 ROI + 参考图去白底 + 轻量 ONNX 语义向量，无权重时显式降级为 ROI HSV + 3 点段内漂移检测）；
- 冻结全量 `analysis/candidates.json`（`candidate_pool_hash`）并自动生成：
  - `analysis/director_brief.md`（去重压缩为 Top 40–60 精选 + 最多 12 条灰区待复核候选）；
  - `analysis/storyboards/sheet_01.jpg ...`（4×4 高清分镜联系表）；
  - `analysis/storyboards/cover_sheet.jpg`（全部封面候选同屏对比板）。

### 3. 外部多模态 Agent 视觉审片与编排提案（v3.3）
- 阅读 `director_brief.md`，用多模态看图查看 `cover_sheet.jpg` 与 `sheet_*.jpg`；
- 按需钻取单条候选前后句上下文与 3 点原帧：
  ```bash
  python -m src.cli inspect-candidate --run "<work_dir>" --ids "c_src_01_001,c_src_01_009"
  ```
- 编写并保存 `<work_dir>/decisions/sequence_proposal.json`（支持 `clip_overrides` 微调 `cut_adjust_start_ms/end_ms`、`zoom`、`effect`、`sticker_text`（传空串 `""` 可显式隐藏该镜贴纸），支持 `hook_card_override`/`cta_override`、受审计的 `subtitle_replacements` 同音错字精校与 `highlight_keywords_override`，以及针对灰区 `UNCERTAIN` 带真实帧引用的 `visual_overrides`；`OTHER_SKU` 永不放权）。

### 4. 提案审计与计划冻结（`validate-plan`）
```bash
python -m src.cli validate-plan --run "<work_dir>" --proposal "<work_dir>/decisions/sequence_proposal.json"
```
- 校验候选池/打分/证据哈希防篡改、源白名单、同源防重叠、硬时长上限、切点微调边界（±500ms 且禁止倒切吞字）、贴纸防造假、以及 `visual_overrides` 证据路径与留痕；
- 可选启用 `violation_mitigation_mode: "auto_three_channel"` 自动生成 `violation_handling.json`；
- 编译冻结 `validated_plan.json` 与 `plan_audit.json`（含 `validated_plan_hash`）。

### 5. 物理音画锁合渲染（`render`）
```bash
python -m src.cli render --run "<work_dir>"
```
- 核验计划哈希与磁盘源视频完整 SHA-256，原子输出 1080×1920 60FPS `output/<run_id>/final.mp4`、`cover.jpg`、`copywriting.md` 与 `render_manifest.json`。

### 6. 成片接缝视觉回看 Critic Loop（`inspect-render`）
```bash
python -m src.cli inspect-render --run "<work_dir>"
```
- 抽取成片全部 N-1 个接缝 `[t-0.25s, t, t+0.25s]` 三帧对照拼图 `output/<run_id>/seams_review.jpg`，以及每段成片中点帧（含首秒 Hook 卡、贴纸、字幕、结尾 CTA）四列总览拼图 `output/<run_id>/overlay_review.jpg`，并运行 `ContinuityAuditor` + `CutOptimizer` 输出局部微调建议（只读不改文件）。

### 7. 双层质量门禁审计（`qc`）
```bash
python -m src.cli qc --run "<work_dir>"
```
- 工程技术 QC（`technical_qc.json`：时长/分辨率/60FPS/−14 LUFS 响度/黑帧/零价格/压脸安全/成片 ASR 复检）+ 接缝连续性审计（`continuity_review.json`）+ 内容 Editorial QC（`editorial_qc.json`，含 `SeamContinuityGate`）。

### 8. 正式不可变交付签署（`deliver`）
```bash
python -m src.cli deliver --run "<work_dir>"
```
- 强制校验 `proposal_hash -> validated_plan_hash -> render_manifest -> video_sha256 -> dual_qc (AND) -> delivery_manifest` 完整哈希链与 `agent_visual_overrides` 溯源，签发不可变 `delivery_manifest.json` 并更新 `output/latest.json`。

---

## 目录结构

```text
dy-live-ecommerce-editor/
├── .agents/skills/live-ecommerce-video-editor/SKILL.md  # 外部 Agent V3.3 视觉导演技能规范
├── contracts/
│   ├── constraints.yaml               # 量化约束唯一事实源（含 vision_v33 阈值）
│   ├── profiles/womenswear.yaml       # 女装品类档案（数据驱动词表/ROI策略/配额）
│   └── schemas/                       # job_spec / sequence_proposals_v33 等 JSON Schema
├── docs/                              # ADR-001/002、V3.3/V3.4 架构总方案与任务卡
├── src/
│   ├── cli.py                         # V3.3 统一阶段 CLI 入口
│   ├── runtime/
│   │   ├── stages/                    # prepare / analyze / inspect / validate_plan / render / qc_deliver / status / security
│   │   └── stage_services.py          # 向后兼容门面
│   ├── core/
│   │   ├── vision/                    # Pose 躯干 ROI + ONNX 视觉编码器 + ROI HSV 降级
│   │   ├── director_digest.py         # Director Brief + 4×4 Contact Sheets + Cover Sheet
│   │   ├── category_profile.py        # 品类档案插件加载器
│   │   ├── frame_provider.py          # 顺序 3 点批抽 + 500ms 桶去重
│   │   ├── sku_tracker.py / candidate_ranker.py / plan_compiler.py / video_assembler.py
│   ├── graphics/                      # 品牌封面渲染 / ASS 字幕 / 动态贴片
│   ├── qc/                            # qc_auditor / editorial_qc / continuity_qc
│   └── tools/                         # 底层确定性原子工具集（价格/违规/人脸/OCR/气口/切片规则/OpenChatCut桥接）
└── tests/                             # 42 套自动化回归测试（python -u scripts/run_regression.py --quick）
```

---

## 回归测试

```bash
python -u scripts/run_regression.py --quick
```

---

## 附录：底层确定性原子工具速查

| 能力 | 模块入口 | 核心职责 |
| :--- | :--- | :--- |
| **封面候选与构图** | `src/tools/extract_cover_candidates.py` | 人脸偏航角 ≤12° / EAR / MAR / 清晰度 / 构图比例 / OCR 背景纯净度 |
| **衣着躯干匹配** | `src/tools/detect_garment_match.py` | MediaPipe Pose 躯干 ROI 裁切 + 商品图去白底前景掩码 |
| **价格与违规三通道** | `src/tools/detect_price.py` · `detect_violation.py` · `apply_violation_handling.py` | 中文数字价格拦截、抖音极限词检测与字幕/语音/OCR 三通道处置 |
| **气口与切片风格** | `src/tools/detect_speech_pauses.py` · `extract_shot_features.py` · `slicing_rules.py` · `cut_style_rules.py` | 相对凹陷气口、全身景别/视角多样性、R1–R10 剪辑风格量化 |
| **成片 ASR 与压脸安全** | `src/tools/verify_final_video_asr.py` · `check_overlay_face_safety.py` | 成片原声二次转写复核、贴纸/Hook 卡防压脸几何检查 |
| **时间线双向桥接** | `src/tools/chatcut_bridge.py` | `plan.json` ↔ OpenChatCut 多轨时间线双向导出/回注 |
