# ADR-002: V3.3/V3.4 视觉导演觉醒、混合视觉感知与灰区有条件放权决策

- **日期**: 2026-11-24
- **状态**: ACCEPTED（已与项目负责人逐项确认并冻结）
- **继承基线**: [ADR-001](2026-11-21-adr-001-agent-first-architecture.md) · PR #16 (`451eb80`)
- **配套文档**:
  - 总方案设计: [`docs/dy-video-v3.3-v3.4-architecture-upgrade-plan.md`](../../dy-video-v3.3-v3.4-architecture-upgrade-plan.md)
  - 可执行任务卡: [`docs/dy-video-v3.3-v3.4-task-cards.md`](../../dy-video-v3.3-v3.4-task-cards.md)

---

## 1. 背景与核心矛盾

PR #16（V3.2）已完成 **ADR-001 控制权反转**：Windows EXE 退化为纯任务交接器，外部通用 Agent（AGY / Codex / Claude Code）担任剪辑总导演，Python CLI（`python -m src.cli`）承担确定性媒体计算与防篡改哈希链门禁。

但在真实女装直播素材（`project-3`，3 源视频、797 个候选）验收与代码深度审计后，暴露出 **“安全审计防线极强，但导演视觉感知与剪辑可控性偏弱”** 的结构性矛盾：

1. **Agent 处于“半盲导片”状态**：797 条候选 JSON 导致上下文膨胀；缺少可视化分镜联系表（Contact Sheet）与成片接缝回看图；`compile_v3_plan()` 硬编码替 Agent 决定了推镜、镜像、贴纸与切点微调。
2. **本地视觉后端退化为全图 HSV 直方图**：`src/core/vision_backend.py` 未接入仓库既有的 Pose 躯干 ROI 裁切与白底剔除（`src/tools/detect_garment_match.py`），在同色系不同款、暖光反光与杂乱背景场景下极易误杀或漏判。
3. **V2 精品模块与 V3.2 主链路断链**：`ContinuityAuditor`、`CutOptimizer`、`apply_violation_handling.py`、`slicing_rules.py` 与 `cut_style_rules.py` 尚未真正接入 `src/runtime/stage_services.py` 闭环。

---

## 2. 冻结的核心架构决策

### 决策 D1：文档与交付节奏 —— 「PR #16 先清债合入 + V3.3 核心落地 + V3.4 接口预留」
- **Phase 1（PR #16 合入前必改）**：修补既有断链（`ContinuityAuditor` 接入 `stage_qc`）、消除代码重复行、将 Pose 躯干 ROI + 白底剔除接入 `vision_backend.py`、清理仓库根目录临时产物与 `README.md` 冲突表述。
- **Phase 2（V3.3 核心版本）**：围绕 **视觉导演套件** 与 **混合视觉感知** 全面升级，使外部多模态 Agent 真正“睁开眼导片、可控微调、可回看自修”。
- **Phase 3（V3.4 演进预留）**：在 V3.3 架构中为薄层 MCP Server、HTML/Remotion 透明动效叠加层、剪映草稿导出（`pyJianYingDraft`）、A/B Hook 投流矩阵预留标准扩展接口，但不阻塞 V3.3 主线交付。

### 决策 D2：视觉感知混合策略 —— 「本地 Pose 躯干 ROI + 轻量 ONNX 视觉向量 + 外部多模态 Agent 灰区终审」
- 严守 **零外部视觉 API 费用** 原则，不在 Python 侧引入付费云端视觉模型调用。
- 本地视觉后端升级为两级结构：
  1. **前景聚焦层**：强制优先提取主播躯干衣着 ROI（复用并增强 `torso_roi_from_pose`）与商品参考图去白底前景（`_mask_white_background`），隔离直播间货架、肤色与背景噪声。
  2. **表征打分层**：默认加载轻量本地 ONNX 视觉模型（约 300–600MB，如 MobileCLIP / SigLIP-base ONNX，自动适配 CPU/CUDA），计算结构/版型/纹理语义余弦相似度；当权重缺失或离线环境不可用时，显式降级为躯干 ROI HSV 并记录 `vision_engine: "hsv_fallback"` 与 `vision_confidence_tier: "weak"`，绝不伪造高置信度。
- 相似度划定三区间（具体阈值由 `contracts/constraints.yaml` 统一管理）：
  - **高置信区（`>= high_threshold`）**：自动判定 `SAME_SKU` / `SAME_SKU_VARIANT`；
  - **灰区（`low_threshold <= score < high_threshold`）**：判定 `UNCERTAIN`，标明 `visual_gray_zone=true` 并自动优先编入 Contact Sheet，交由外部多模态 Agent 肉眼复核；
  - **明确异款区（`< low_threshold` 且结合换款/品类负向词）**：判定 `OTHER_SKU`。

### 决策 D3：严格模式（`strict`）下的灰区有条件放权与防篡改边界
为平衡“绝不串款”与“不因保守误杀黄金片段”，在 `quality_policy=strict` 下确立严格的 **灰区有条件放权协议**：
- **不可逾越的红线（永远硬阻断，Agent 无权覆盖）**：
  1. `OTHER_SKU`（他款/换款/品类负向主推）；
  2. 违规极限词 BLOCK 项（未经三通道处理）与价格表达；
  3. 非白名单源视频、同源时间重叠、超出硬时长上限、历史成片污染；
  4. 无任何有效帧证据（`frame_refs` 为空或指向非本次 `analyze` 生成的帧文件）。
- **允许有条件覆盖的唯一范围**：仅限本地视觉判定为 `UNCERTAIN`（且非 `OTHER_SKU`、无换款词/负向品类冲突）的候选片段。
- **放权审计硬条件**：外部 Agent 必须在 `sequence_proposal.json` 的 `visual_overrides` 字段中显式声明：
  - `candidate_id`（必须属于当前冻结 `candidate_pool_hash`）；
  - `inspected_frame_refs`（必须指向 `work/<run_id>/candidate_frames/` 或 `storyboards/` 内的真实帧/拼图文件，由 `validate-plan` 校验文件存在且哈希可追溯）；
  - `override_relation`（仅允许 `SAME_SKU` / `SAME_SKU_VARIANT` / `ACCESSORY_OR_STYLING`）；
  - `visual_evidence_note`（具体的肉眼视觉特征描述，如领型/纹理/纽扣/版型吻合依据，禁止空字符串或模板废话）；
  - `agent_name`（审计标识）。
- **审计留痕**：所有 `visual_overrides` 必须完整写入 `plan_audit.json`、`editorial_qc.json` 与最终 `delivery_manifest.json`（标记 `has_agent_visual_overrides: true` 及覆盖清单），供人类运营抽检追溯。

### 决策 D4：V3.3 绝对核心聚焦 —— 视觉导演套件 4 件套
V3.3 集中全部研发精力打透以下 4 个直接决定成片质量的闭环能力：
1. **Director Brief（导演简报）**：将数百条原始滑窗候选按语义簇去重、按漏斗分组压缩为 Top 40–60 精选目录（`analysis/director_brief.md` + `analysis/director_brief.json`），配套 `inspect-candidate` 按需钻取命令。
2. **Contact Sheet（视觉联系表）**：自动生成分镜 4×4 拼图（每候选 3 点采样 25%/50%/75% 或主帧+角标）与封面候选对比拼板（`analysis/storyboards/*.jpg`），让外部多模态 Agent 看图决策。
3. **Proposal v3.3 导演微调协议**：扩展 `sequence_proposal.json`，允许 Agent 在硬约束边界内显式控制单段入出点微调（`cut_adjust_ms`，默认上限 ±500ms 且禁止倒切吞字）、景别特效（`zoom`/`hflip`/`none`）、贴纸文案（受防造假事实校验）、Hook/CTA 定制与 `visual_overrides`。
4. **成片接缝回看 Critic Loop（`inspect-render`）**：在 `render` 后自动生成全部 N-1 个接缝点的前/中/后 3 帧对照拼图（`seams_review.jpg`），并串联 `ContinuityAuditor`、`CutOptimizer` 与成片 ASR 复检，支持 Agent 做“外科手术式局部修复”后再签署交付。

### 决策 D5：业务聚焦女装直播带货，架构预留品类插件位
- 短期（1–2 个月）深耕**女装直播切片带货**（针织衫/毛衣、外套/羽绒服、连衣裙、裤装），把女装高频痛点（换款密集、同色串款、面料细节展示、全身版型构图、小黄车安全区、爆款快慢节奏）做到商业可用极致。
- 同时将当前散落在 `sku_tracker.py`、`candidate_ranker.py`、`plan_compiler.py` 中的女装硬编码词表、漏斗角色权重与面料/版型特征提取，抽离为数据驱动的 **品类档案配置（`contracts/profiles/womenswear.yaml`）**，通过统一的 `CategoryProfile` 加载器注入，未来扩展美妆/食品/3C 只需新增 YAML 档案而无需改动核心引擎。

---

## 3. 安全与兼容性底线（延续 ADR-001）

1. **向后兼容 v3.2 提案**：`validate-plan` 必须同时兼容 v3.2 最小提案格式与 v3.3 扩展提案格式；未填写 v3.3 可选微调字段时平滑沿用确定性默认策略。
2. **哈希链零弱化**：`candidate_pool_hash -> proposal_hash -> validated_plan_hash -> render_manifest -> dual_qc -> delivery_manifest` 链路完全保留，新增的 `visual_overrides`、`cut_adjust_ms` 与 `storyboards` 均纳入审计哈希覆盖范围。
3. **CI 零素材、零大模型权重强依赖**：GitHub Actions 快速单元测试层（`python -u scripts/run_regression.py --quick`）不得因未下载 ONNX 权重或缺少真实直播视频而失败，所有新视觉模块必须配备确定性 mock / 合成夹具回归测试。
