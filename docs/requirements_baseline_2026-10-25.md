# 既定需求审计基线（第 30 步产出）

> 版本：2026-10-25 · 机器可读基线：`contracts/requirements.yaml`
> 审计引擎：`src/tools/audit_requirements.py` · 回归测试：`tests/test_requirements_audit.py`

## 一、这一步解决什么问题

本仓库的"既定需求"此前散落在三处文档里：

| 来源 | 文件 | 内容形态 |
| :-- | :-- | :-- |
| PLAN | `docs/editing_task_plan_2026-10-21.md` | 5 条编号需求 + 架构 + 字幕校对流水线 |
| SKILL | `skills/live-ecommerce-video-editor/SKILL.md` | 7 大节制作规范（黄金 3 秒 / 特效 / 字幕 / 封面 / CTA / 代码模板 / 避坑） |
| CON | `contracts/constraints.yaml` + `contracts/constraints_map.md` | C1~C17 / V1~V8 量化约束 |

问题在于：**"是否满足"从来没有可复核的判据**。人工读文档 + 人工读代码下结论，导致本仓库反复出现同一类缺陷：

> **声明与实现脱节** —— yaml 里声明了键，代码根本不读（"死键"），文档里却标成 ✅。

这直接违反 `contracts/constraints.yaml` 头部自己写下的纪律：

> 规则：阈值只改本文件，不改代码；本文件版本进入缓存键与 QC 报告。

一旦出现死键，这条纪律就被无声破坏——改 yaml 不改行为，而没有任何机制会报错。

第 30 步的产出就是把这个判据**机器化**：

1. `contracts/requirements.yaml` —— 43 条既定需求的结构化基线（每条带证据锚点与实现状态）
2. `src/tools/audit_requirements.py` —— AST 级审计引擎，自动判定"yaml 键是否真被消费"
3. `tests/test_requirements_audit.py` —— 回归守卫，防止基线自身腐化

## 二、审计引擎怎么判定"是否接线"

核心是**段路径（path）模型**。把代码里的读取表达式还原成"约束段路径"，再与 yaml 的键路径做匹配：

```
constraints.get("video", {}).get("funnel", {})   →  ("video", "funnel")
q = funnel_c.get(cat, {})                         →  ("video", "funnel", "*")
q.get("min")                                      →  ("video", "funnel", "*", "min")

yaml 键 video.funnel.hook.min  ↔  ("video","funnel","hook","min")
                                 ↑ "*" 匹配任意动态段 → 判定为已接线
```

引擎（AST 级，不用正则）处理了这些真实代码形态：

- **跨函数透传**：`bar_c = constraints.get("platform_bar", {})` → `generate_platform_bar(bar_c, ...)` → 函数体内 `bar_c.get("font_size")`
- **dict 合并**：`{**DEFAULTS["text"], **(constraints.get("text", {}) or {})}`
- **动态键**：`funnel_c.get(cat, {})`（cat 为运行期变量）
- **多级链式**：`constraints.get("video").get("funnel")`
- **作用域隔离**：同名变量在不同函数绑定不同段时不串味（宁可漏判也不误判）

判定原则是**保守的**：宁可漏判一个死键（登记为"需人工复核"），也不把"已接线"错报成"死键"——因为后者会引发无谓的返工。

## 三、审计结果

```
约束键 151 个 · 死键 27 个 · QC 断言 27 个 · 需求基线 43 条（证据全部成立）
```

### 27 个死键（yaml 有声明、代码无消费者）

| # | 死键 | 性质 |
| :-- | :-- | :-- |
| 1 | `cover.ghosting_energy_max` | 未实现（重影能量比） |
| 2 | `cover.contour_completeness_min` | 未实现（人像掩码完整度） |
| 3 | `cover.landmark_visibility_min` | 未实现（关键点置信度下限） |
| 4 | `cover.knees_visible` | 已被 `cover.knee_y_max` 几何判据取代 |
| 5 | `cover.ankles_visible` | 已被 `cover.ankle_y_max` 几何判据取代 |
| 6 | `cover.horizontal_margin_ratio` | 1:6:1 语义由 `cover.person_width_ratio` 承载 |
| 7 | `cover.garment_match_min` | 重复键，生效的是 `garment_match.garment_match_min` |
| 8 | `cover.ocr_area_ratio_max` | 重复键，生效的是 `ocr.background_text_area_max` |
| 9 | `garment_match.clip_model` | open_clip 模型名硬编码 `ViT-B-32` |
| 10-16 | `text.product_name_gradient` / `product_name_stroke` / `brand_en_gradient` / `brand_en_fill` / `brand_en_stroke` / `brand_cn_fill` / `brand_cn_stroke` | 封面文字颜色类键全部无消费者，渲染用 `cover_renderer` 里的硬编码常量 |
| 17 | `zoom_effect.max_zoom` | 推镜倍率实际由 `build_plan` 硬编码 1.3 决定，1.35 上限写在 QC 里 |
| 18 | `mirror_effect.min_occlusion_ratio` | 镜像遮挡全程不读该阈值 |
| 19-22 | `split_effect.radius` / `border_color` / `border_width` / `vertical_center` | **键归属错位**：分屏路径不读，实际被 `sticker` 段消费 |
| 23 | `video.subtitle_safe_zone_px` | 与 `video.subtitle_margin_v_min` 重复，生效的是后者 |
| 24 | `audio.sample_rate` | 由 ffmpeg `-ar 48000` 硬编码 |
| 25-27 | `price.clip_exclude` / `price.detector` / `price.whitelist_units` | 价格选段排除在 `detect_price` 内部实现，白名单与检测器版本为模块常量 |

这些死键已**逐条登记**在 `contracts/requirements.yaml` 的 R-X01 ~ R-X10 条目中（`status: unimplemented`，`allow_dead: true`）。这不是"新增缺陷"，而是**把散落的实现缺口显性化**，让它们无法再被 ✅ 掩盖。

### 43 条需求的实现状态

| 分组 | 条数 | 说明 |
| :-- | :-- | :-- |
| R-P01 ~ R-P05 | 5 | PLAN 的 5 条编号需求 —— 全部 implemented |
| R-S101 ~ R-S702 | 16 | SKILL 7 大节 —— 3 条 partial（推镜上限、ASR 三件套、封面文字色），其余 implemented |
| R-C001 ~ R-C008 | 8 | 量化约束 —— 全部 implemented |
| R-V01 ~ R-V02 | 2 | 2026-10-24 新增视频叠加元素 —— 全部 implemented |
| R-X01 ~ R-X10 | 10 | 死键登记 —— 全部 unimplemented（如实标注） |

3 条 partial 的具体缺口：

| 需求 | 已实现 | 缺口 |
| :-- | :-- | :-- |
| R-S202 推镜 | 触发点 + 过渡 + smoothstep 缓动 + 锚点 | `max_zoom` 未接线 |
| R-S303 ASR 三件套 | 静态错词表 + 滑动窗口消歧 | prompt 注入（实测有害，2026-10-24 明确禁用）+ pypinyin 置换网未落地 |
| R-S403 封面文字色 | 斜体 + 色相/饱和度 QC 复测 | 渐变/描边/品牌色键全部无消费者 |

## 四、怎么用

```bash
# 逐条核对 + 死键清单
python -u src/tools/audit_requirements.py

# 只看死键
python -u src/tools/audit_requirements.py --only-dead

# JSON（CI 消费）
python -u src/tools/audit_requirements.py --json

# 回归层（已接入 scripts/run_regression.py 的 UNIT_TESTS）
python -u tests/test_requirements_audit.py
python -u scripts/run_regression.py --quick
```

### 变更纪律

| 场景 | 必须做的事 |
| :-- | :-- |
| 改了 `src/**` 的约束消费逻辑 | 跑审计；若死键集合变化，同步更新 `contracts/requirements.yaml` 的 R-X 条目与 `tests/test_requirements_audit.py` 的 `EXPECTED_DEAD_KEYS` |
| 修好了一个死键 | 从 `EXPECTED_DEAD_KEYS` 移除，并把对应 R-X 条目的 `status` 改为 `implemented`、去掉 `allow_dead` |
| 新增了既定需求 | 在 `contracts/requirements.yaml` 追加 R-xxx 条目，**必须**带可机器验证的证据锚点（`constraint_keys` / `qc_assertions` / `code_symbols` 三者至少其一） |
| 只想登记一个暂无实现的愿望 | 记为 `status: unimplemented` + `allow_dead: true`，**不要**写成 implemented |

最后一条是关键：基线的价值取决于它是否诚实。宁可标 unimplemented 也不标 implemented。

## 五、遗留与待决

1. ~~**SKILL.md 与项目决策冲突（未拍板）**~~ → **已拍板（2026-10-25）**：帧率全局统一 **60fps**。SKILL.md §5、`constraints.yaml` 的 `video.fps`、装配器 `FPS`、ChatCut 时间轴、QC 帧率断言五处全部改为 60。
2. ~~**Hook 卡宽度偏差**~~ → **已拍板（2026-10-25）**：标题卡全局统一 **720×168**（原 940~980 口径作废），并新增 `hook_card` 约束段（width/height/y_min/y_max/face_top_gap_px），决策网关与 Agent Director 均改为从约束读取，改 yaml 即改画面。
3. **`duration_tolerance_sec` 键不存在**：`constraints_map.md` 曾引用 `video.duration_tolerance_sec`，实际 yaml 里该键在 `asr_review` 段（`asr_review.duration_tolerance_sec`），且已被消费。文档笔误，本步已顺带核实。
4. **未接线的三处若要修复，建议单独开 PR**：本次第 30 步的目标是**建立审计基准**而非修改实现，故所有死键一律如实登记、未动实现代码。其中 `zoom_effect.max_zoom`、`mirror_effect.min_occlusion_ratio` 两项接线风险最低（纯阈值透传，不改默认行为），可作为后续独立改动。
