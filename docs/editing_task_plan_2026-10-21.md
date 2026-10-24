# 直播素材剪辑成片任务 — 完整处理方案

> 版本：2026-10-21 · 阈值唯一来源：`contracts/constraints.yaml`

## 一、需求与落地对照

| # | 需求 | 落地方式 | 状态 |
|---|---|---|---|
| 1 | 缺商品图则**自动补占位示例图**（原为"不补图不占位"，2026-10-24 需求变更） | `make_placeholder_cards.py` 生成灰蓝斜纹占位图并登记 `product.item_images` + `placeholder_images=True`；封面走标准贴图渲染路径（C11~C14 几何断言与 QC「封面贴图几何」得以真实验证）。**注意：占位图会被衣着匹配跳过**（灰蓝斜纹 vs 直播暖色必然 0 分），拿到真实商品图后自动生效 | ✅ |
| 2 | 成片 45–65s，区间内浮动，优先内容完整 | spec `duration_range` 统一 `45-65`；`build_plan` 漏斗配额贪心 + 补齐 + 上限修剪；新增 0.5s 切片安全余量（防ffmpeg 切片物理时长溢出） | ✅ |
| 3 | 允许丢段，只留商品介绍/卖点/演示 | 三层过滤：① 黑名单（价格/库存/换款离场）② `_is_idle_chatter` ASR 幻觉与元话语 ③ `_top_up` 补齐阶段只接纳 `categories` 非空的语义块 | ✅ |
| 4 | 本地 ASR + 本地 agent 校对，无错字/无断句错误 | Faster-Whisper 转录 → `AsrContextVerifier` 本体引擎（错词表+滑动窗口消歧）+ `_polish_for_subtitle`（叠词去重、冗余标点清理、超长句按可读性切分且时间戳守恒） | ✅ |
| 5 | 同目录多视频跨素材挑选最优片段 | **本轮新增能力**，见第三节 | ✅ |

## 二、架构（Agent 决策中枢 + 确定性工具链）

```
Phase 1 确定性扫描（多源并行）
  ├─ detect_scenes          逐源镜头边界
  ├─ extract_cover_candidates 逐源封面候选 → 清晰度硬门禁前置 → 按 frontal_score 全局排序
  └─ analyze_story_segments逐源 Whisper ASR → 本地语境校对 → segments 携带 source_file
Phase 2 Agent 决策（全部过 DecisionGateway 约束网关）
  ├─ 封面优选  取首个过约束候选
  ├─ build_plan  漏斗四类配额贪心 + 时长收敛 + 黑名单/闲聊过滤 + 跨源不合并
  └─ Hook 卡避让  在首分镜所属源上取人脸顶
Phase 3 渲染总装
  ├─ render_cover_with_meta   品牌区固定顶部 y=125 + 金色商品名 + 可选贴图
  └─ assemble_video_tool      按 clip.source_file 逐源切片 → 拼接 → ASS 字幕 → Hook/CTA 贴片 → 两遍 loudnorm
Phase 4 QcAuditor 量化门禁（24 项断言，2026-10-24 起新增视频叠加元素复测）
```

## 三、跨素材选段（需求 5 核心实现）

### 源素材发现
`AgentDirector._collect_source_videos()`
- 优先读 `spec.raw_video_paths` 显式列表
- 否则扫描 `raw_video_path` 所在目录全部视频（`.mp4/.mov/.mkv/.avi/.flv/.m4v/.ts`）
- 按文件名稳定排序 → 保证缓存键可复现
- 单文件 spec 完全向后兼容

### 时间轴隔离（关键约束）
不同视频的时间轴互不相通，因此：

| 环节 | 处理 |
|---|---|
| ASR | 逐源独立转录，缓存隔离到 `work_dir/asr/<源名>.*.json` |
| 上下文引擎 | 逐源独立校对（滑动窗口不跨文件） |
| `build_blocks` | 多源时按 `source_file` 分组，**绝不跨文件合并** |
| 修剪路径 | 3 处 block 构造（关键词窗口/余料/段对齐）全部继承 `source_file` |
| 切片 | `render_clips` 按 `clip.source_file` 选源，缺省回退 `plan.raw_video_path` |
| 字幕映射 | 仅取与该分镜同源的 segments，避免跨源时间轴错位 |
| 缓存键 | `compute_pipeline_key` 支持视频列表，全部源内容参与哈希（key 升 v3） |

### 跨源选段实测（hoodie_11214，2 源）
```
c01_hook     7.94s   ← 0518源
c02_fabric  20.00s   ← 0518 源
c03_fit     14.80s   ← 0518 源
c04_styling 21.46s   ← 0518 源
workwear_11186（3 源）真正跨源：
c01_hook     8.00s   ← 0326 源
c02_fabric  20.00s   ← 0101 源
c03_fit     13.36s   ← 0101 源
c04_styling 23.64s   ← 0326 源
```

## 四、字幕校对流水线（需求 4）

```
Whisper(small, zh, VAD, condition_on_previous_text=False)
   ↓  ⚠️ 不注入 initial_prompt（2026-10-24 修正）
   ↓     实测：注入后模型会把 prompt 文本当成音频内容识别，挤占开头约 30s
   ↓     真实语音（6 号素材：前 90s 仅 348 字 vs 无 prompt 的 495 字，-30%）；
   ↓     8/11 份素材首段都是纯回声。领域词纠错改由下方本地本体引擎负责，
   ↓     不依赖 Whisper 的 prompt 偏置。审核门禁新增 A10 检出回声。
本地本体引擎（AsrContextVerifier）
   ├─ 静态同音错词表（22 条：食物→实物、半导棉→半岛棉、卡马→卡码、马术→码数…）
   ├─ 商品专项词根动态提权
   ├─ 滑动窗口语境消歧（不要老钱 → 显老钱）
   └─ _polish_for_subtitle ★本轮新增
        ├─ 叠词去重（这个这个→这个）
        ├─ 冗余标点/首尾清理
        └─ 超长句（>22字）切分为多条，按字数比例分配时长（时间戳守恒）
   ↓
LLM 复核（配置 API Key 时启用，否则无缝降级本地引擎）
   ↓  护栏：严格 JSON / 时间戳绝对守恒 / 零价格硬约束
ass_verification_report.json 全量diff 审计落盘
```

## 五、本轮修复的真实缺陷

1. **基线锁定层形同虚设**：`run_regression.py` 用 `baselines.get(pid)` 读`{"materials": {...}}` 结构，永远取不到 → 从未做过区间比对。已修复。
2. **本地校对引擎丢失 `source_file`**：重建 verified 段时未透传来源标记，会让跨素材选段失效。已修复。
3. **封面候选跨源排序键错误**：用了不存在的 `score` 字段，导致选中糊脸候选。正确键是 `frontal_score`。已修复。
4. **清晰度硬门禁未前置**：`frontalscore` 对"宽占比达标"加权过高，可能让 `sharpness<200` 的候选排第一。已改为排序前过滤。
5. **时长补齐引入无商品语义闲聊块**："他们坐船跟我们坐船有什么区别"这类内容被拉进成片。已加语义门槛。
6. **计划时长卡上限导致成片溢出**：ffmpeg 逐段切片物理时长略大于计划值（4 段合计 +0.09s），计划 65.0s → 成片 65.09s 被 QC 拦截。已加 0.5s 安全余量。

## 六、验收结果

见 `docs/delivery_report_2026-10-21.md`。