# 切片规则检查项清单（2026-11-01）

> 对应规则：`workflows/standard_pipeline_v2.md` §13　阈值：`contracts/constraints.yaml` → `slicing_rules`
> 证据：`docs/reference_cut_analysis_SSY410163.md`
> 自动化：`python -u scripts/verify_slicing_rules.py --video <参考成片>` / `--plan <plan.json>`

---

## 一、复现性检查（规则是否成立）

| # | 检查项 | 判定标准 | 工具 | 实测结果 |
| :-- | :--- | :--- | :--- | :--- |
| C1 | 参考成片自身能否通过 R2~R5 | **全部 PASS** | `verify_slicing_rules.py --video` | ✅ ALL PASS（全身 100%、角度 8 种/10 段、密度 96%） |
| C2 | 切点与 ASR 句末的偏移分布 | 中位 ≤0.1s、最大 ≤0.5s | ASR + 切点检测 | ✅ 中位 0.03s、最大 0.37s |
| C3 | 句内视觉切点占比 | ≤30%（参考片 2/9=22%） | `check_intra_sentence_ratio` | ✅ 22% |
| C4 | 片段时长分布 | 主体 5~10s，中位 ≈6.3s，范围 [1.7,13.0] | `check_clip_duration` | ✅ 60% 落在 5~10s |
| C5 | 全身景别占比 | ≥70%（参考片 100%） | `check_full_body_ratio` | ✅ 100% |
| C6 | 展示角度数 / 连续同角度 | ≥3 种且同角度不连续 | `check_shot_variety` | ✅ 8 种、连续同角度 1 段 |

**C1 是最关键的一条**：如果参考成片自己都过不了，说明规则方向错了。

---

## 二、可判定条件（阈值的单一事实源）

| 键 | 值 | 含义 | 消费点 |
| :--- | :--- | :--- | :--- |
| `cut_on_speech_pause.snap_tolerance_sec` | 0.45 | 句末吸附容差（**原 2.0s**） | `build_plan._snap_end_to_sentence` |
| `cut_on_speech_pause.max_offset_from_sentence` | 0.45 | 气口相对句末最大偏移 | `snap_cut_to_pause` |
| `cut_on_speech_pause.search_window_sec` | 0.40 | 气口搜索窗口 | `snap_cut_to_pause` |
| `cut_on_speech_pause.dip_threshold_db` | 8.0 | 相对凹陷最小深度 | `find_relative_dips` |
| `cut_on_speech_pause.absolute_db` | −60.0 | 绝对静音阈值（仅数字静音素材有效） | `adaptive_silence_db` |
| `cut_on_speech_pause.floor_margin_db` | 6.0 | 自适应阈值相对底噪的余量 | `adaptive_silence_db` |
| `cut_on_speech_pause.intra_sentence_cut_max_ratio` | 0.30 | 句内切点占比上限 | `check_intra_sentence_ratio` |
| `clip_duration.min_sec` / `target_sec` / `max_sec` | 1.7 / 6.3 / 13.0 | 片段时长区间 | `check_clip_duration` |
| `shot_framing.min_body_height_ratio` | 0.45 | span 全身门槛 | `classify_framing` |
| `shot_framing.target_body_height_ratio` | 0.60 | span 中位下限 | `evaluate_plan_slicing` |
| `shot_framing.full_body_min_ratio` | 0.70 | 全身段占比下限 | `check_full_body_ratio` |
| `shot_variety.min_distinct_angles` | 3 | 最少角度数 | `check_shot_variety` |
| `shot_variety.max_same_angle_consecutive` | 1 | 同角度最大连续段数 | `check_shot_variety` |
| `speech_density.min_ratio` | 0.85 | 语音密度下限 | `check_speech_density` |
| `funnel_order_overridable` / `default_funnel_order` | true / hook,fabric,fit,styling | 漏斗顺序可覆写 | `resolve_funnel_order` |

---

## 三、执行步骤检查（素材池检索与匹配）

### S1 候选池
- [ ] `detect_scenes.py` 产出镜头边界（候选段时间轴骨架）
- [ ] `analyze_story_segments.py` 产出 ASR 段 + 漏斗类目标签
- [ ] `detect_speech_pauses.py` 为**每个源素材**产出气口表
      → 校验：`relative 气口数 > 0`（直播素材底噪高，绝对静音会命中 0 处）
- [ ] **气口表随源缓存**（`work_dir/pauses/*.pauses.json`），避免重复解码

### S2 分镜编排
- [ ] 选段后调用 `_snap_end_to_sentence(segs, hard_end, pauses=…)`
      → 校验：`snap_tolerance_sec` 生效于 `meta["notes"]`
- [ ] **超长块按句末拆分**（`_split_long_block`，R2 主动生效）
      → 拆分点必须落在句末；尾段 0.8~1.7s 独立成段（不并回，否则破上限）
      → 校验：拆分后每段 ≤ `max_sec`，总时长缺口 ≤5%（消耗的是句间停顿）
- [ ] `extract_shot_features.annotate_clips` 补齐每个分镜的
      `span / shoulder_w / face_area / fabric_block / framing`
      → 校验：`pose_ok` 为真的分镜数 ≥ 分镜总数的 80%
- [ ] `resolve_funnel_order(spec)` 按品类取顺序
- [ ] `evaluate_plan_slicing` 写 `plan["slicing_rules"]`
      → **必须在画面特征补齐之后**重算（否则 R3/R4 拿到空数据会误报 0% 全身）
      → `meta.notes` 中 build_plan 阶段的过期"切片规则"提示需清除

### S5 QC 门禁
- [ ] 读取 `plan["slicing_rules"]`，逐项输出 R2~R6 的 WARN 级判定
- [ ] WARN 必须写进 `agent_decision_manifest.json`，供人工复核

---

## 四、反向验证检查（门禁必须能拦住真问题）

> 依据 `workflows/standard_pipeline_v2.md` §11.2 #8「门禁有效性必须反向验证」

| # | 毒化输入 | 期望 | 实测 |
| :-- | :--- | :--- | :--- |
| V1 | 片段时长 20s（超上限） | R2 FAIL | ✅ |
| V2 | 片段时长 0.5s（低于下限） | R2 FAIL | ✅ |
| V3 | 全部片段同一角度 | R4 FAIL | ✅ |
| V4 | 连续 3 段同一角度 | R4 FAIL | ✅ |
| V5 | 语音密度 58% | R5 FAIL | ✅ |
| V6 | 全部非全身景别 | R3 FAIL | ✅ |
| V7 | 指定超范围候选做封面审计 | 仍 BLOCK | ✅（缺陷 1 修复的回归） |
| V8 | 指定合规候选做封面审计 | 不 BLOCK | ✅ |

---

## 五、已知的坑（勿重复踩）

1. **不要用绝对静音阈值找气口**——直播素材底噪 −42~−53dB，
   `< −60dB` 命中 **0 处**；必须用相对凹陷。
2. **不要把参考成片切点处的 −120dB 当成选段信号**——那是剪辑软件切音轨
   留下的拼接缝；源侧同一时刻并无静音，源侧凹陷与随机点**无富集**。
3. **景别判据用 span（头顶→踝），不用 body_h（肩→踝）**——后者受站姿影响
   波动 ±0.08，会把 3/10 段误判为非全身。
4. **改配置前先确认键的归属段与消费方**（`yaml.safe_load` + 死键审计）。
   `scripts/run_regression.py --quick` 会在 `test_requirements_audit` 里
   报出未接线的配置键。
5. **CRLF 文件的多行编辑**：本仓库多数文件是 CRLF 行尾，Edit 工具多行匹配
   会失败；用 Python `open(newline="")` 读写处理。
6. **不要用像素统计判定"有无文字图层"**——直播画面偏亮（白墙/浅地板）与
   高纹理（衣架/地毯/窗帘）会让两种启发式都 100% 误报。改为目视复核。
7. **不要用"前段电平高 X dB"判定 Boom 音效**——主播正常大声说话同样满足。
   用频谱窄带性：`E(45~90Hz) / mean(E(20~45Hz), E(90~150Hz)) > 3.0`
   （实测参考片 0.77 vs 我们旧成片 25.30）。
8. **把配置项从"永远开启"改成"可关闭"= 第一次执行它的旁路代码**——
   旁路代码往往最缺测试覆盖。本仓库的 `use_trans=False` 分支就因此
   潜伏着"只接第 1 段、其余分镜全丢"的缺陷（见工作流 §14.2）。

---

## 五之二、风格类检查项（R7~R10，2026-11-02 新增）

> 工具：`python -u scripts/verify_cut_style.py --video <成片> --cuts <边界> --segments <asr.json>`

### 转场（R7）
- [ ] 硬切模式：实测叠化数 = **0**
- [ ] 每个切点的 Δα ≥ 0.5（单帧跳变，非渐变爬坡）
- [ ] 音频侧无 acrossfade 残留（切点应为硬接）
- [ ] 切点仍落在 ASR 句末 ±0.45s 内（R1 未被破坏）

### 音频（R8）
- [ ] 无 Boom 音效（低频窄带比 < 3.0）
- [ ] 无 BGM（低中频比 ≤ −18dB）
- [ ] 综合响度 −15.0 ± 1.5 LUFS
- [ ] LRA ≤ 4.0 LU
- [ ] 语音占比 ≥ 85%

### 调色（R9）
- [ ] R/G、B/G 段间变异 ≤ 8%
- [ ] 饱和度变异 ≤ 12% ｜亮度变异 ≤ 8% ｜对比度变异 ≤ 12%
- [ ] ⚠️ 人工确认：跨段商品颜色观感一致（卡其/奶白不串色）

### 节奏（R10）
- [ ] 钩子区(0~10s)切点数 ∈ [2, 4]
- [ ] 句长中位 ∈ [1.2, 3.0]s
- [ ] 语速 ∈ [300, 480] 字/分
- [ ] 后 1/3 句长 / 前 1/3 句长 ≤ 0.85（后段收紧）
- [ ] 中段(25%~70%)最长单段 ≤ 13.0s

### 跨片一致性
- [ ] 同一批次成片的 R7~R10 口径一致（不得一批硬切一批叠化）
- [ ] 换风格必须在 spec/constraints 中显式声明，不靠默认值漂移

### 时长结构（2026-11-03 补充，来自外部评审的 C6/C7 指标）
- [ ] 切片时长标准差 **σ ≥ 2.2s**（参考片 3.55；仅"每段落在区间内"不足以刻画节奏层次）
- [ ] 最长/最短切片比 **≥ 4.0**（参考片 8.58）
- [ ] 存在 **≥1 个 1.5~2.8s 的"提神快切"**（参考片 2 个；当前我方为 0，属已知缺口）
- [ ] R2 时长下限已按 11 段重新标定为 **1.5s**（参考片最短段 1.51s）

---

## 六、已知未覆盖项

- **R3/R4 目前只做"标记 + 自检"，未参与选段淘汰**：画面特征在 plan 生成
  **之后**才计算，故规则不能回头改变选段。要让它真正影响选段，
  需要把特征提取前移到 `build_blocks` 阶段（成本：每候选块 3 帧 Pose 推理）。
  （R2 是例外——超长块拆分已在 build_plan 内主动生效。）
- **R4「同角度不得连续」目前只判定不调整**：plan 里出现 2 段相邻同角度时，
  自检报 WARN 但不重排。要自动消解需在编排阶段做一次相邻异质重排。
- **R5 语音密度需要成片级数据**：当前以 plan 段落时长近似，
  精确值应由成片 ASR 复检（`verify_final_video_asr`）提供。
- **R6 顺序覆写尚未有真实案例验证**：参考片的 `hook→fit→fabric→styling`
  来自半身裙品类，其他品类是否同理需要更多样本。
- **R9 只检测不修正**：跨机位素材的段间色调跳变会被检出，但**不会自动校正**
  （需引入 `colorbalance`/`curves`/LUT）。
- ~~**R10 只在成片级检测**~~：✅ **已修复**——钩子区短段切分已下沉到 `build_plan`
  （`pacing_curve.hook_segment_sec`=4.0 / `hook_target_sec`=10.0），
  `video.funnel.hook` 上限同步 8→12。首轮实测钩子区只有 1 个切点（R10 FAIL），
  修正后应达到 2~4 个。
- **R7/R8 是"方向选择"而非"参数调优"**：硬切 vs 叠化、有无 Boom 属
  剪辑语言的分歧，须由业务拍板。当前配置按参考片取 `hard_cut` + 无 Boom。
