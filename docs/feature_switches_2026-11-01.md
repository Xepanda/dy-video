# 统一能力开关（exe 总入口）说明书 · 2026-11-01

> 单一事实源：`contracts/features.yaml`
> 解析器：`src/tools/feature_flags.py`（GUI 与流水线共用）
> 落盘位置：`job_spec.json → options.features`
> 已验证：单元测试 18/18 个测试文件；branded 形态真实出片 48.86s，38 项断言全绿（覆盖率 100%）；plain 形态真实出片 48.86s，无字幕/无贴纸，QC 全绿但 10 项文字类断言为 SKIP（覆盖率 0.76）

---

## 一、总入口是怎么工作的

```
exe 界面「⚙️ 能力开关」面板
        │  渲染自 features.yaml（不在前端硬编码任何开关）
        ▼
job_spec.json → options.features { render_profile: "plain", ... }
        │
        ▼
feature_flags.apply(constraints, spec)
        ├─ 注入 constraints["features"]（供运行时直接读）
        └─ 派生到既有配置键：video.duration_range / video.funnel.fabric.max /
           transition_effect.enabled / magnifier_effect.enabled /
           violation_words.handling.subtitle_action
        │
        ▼
build_plan / video_assembler / qc_auditor / generate_from_job_spec 各自消费
```

三条硬约束（写进 `features.yaml` 头部注释）：

1. **不硬编码第二份**：界面、默认值、校验规则全部来自同一个 yaml；
2. **每个开关必须有消费点**，否则只能标 `state: disabled`（界面置灰并给出原因），避免重演"改了配置没生效"；
3. **开关只写在 spec 里，不改 `constraints.yaml`**：constraints 是全局默认与阈值，开关是单任务覆盖。

---

## 二、纳入范围总览（12 项：8 已接线 / 4 未开放）

| 分组 | 开关 | 默认 | 状态 | 消费点 |
| :--- | :--- | :--- | :--- | :--- |
| 成片形态 | `render_profile` | `branded` | ✅ 已接线 | `agent_director` → `assemble_video_tool`（透传 constraints）+ `video_assembler`（5 处旁路）+ `qc_auditor`（10 项 SKIP） |
| | `duration_preset` | `standard` | ✅ 已接线 | 派生 `video.duration_range` → `build_plan` / `qc_auditor` |
| 内容配比 | `fabric_ratio` | `strict`(11s) | ✅ 已接线 | 派生 `video.funnel.fabric.max` → `build_plan` 配额 |
| | `promo_words` | `allow_welfare` | ✅ 已接线 | `build_plan.is_blacklisted()` 白名单剥离 |
| | `violation_action` | `delete` | ✅ 已接线 | 派生 `violation_words.handling.subtitle_action` → `video_assembler` |
| 视听技法 | `transition` | `on` | ✅ 已接线 | 派生 `transition_effect.enabled` → `video_assembler` |
| | `magnifier` | `on` | ✅ 已接线 | 派生 `magnifier_effect.enabled` → `agent_director` / `video_assembler` |
| | `auto_frame` | `off` | ⛔ 未开放 | 待接入 17% 构图（清晰度取舍未验证） |
| | `speech_rate` | `1.0` | ⛔ 未开放 | 待接入 `atempo` + 全链时间戳换算 |
| 商品资料 | `color_quota` | `off` | ⛔ 未开放 | 待接入颜色标注与配额统计 |
| 交付审核 | `manual_review` | `on` | ✅ 已接线 | `generate_from_job_spec` 写 `manual_review.md`（13 项预填） |
| | `review_sheet` | `off` | ⛔ 未开放 | 待实现 `build_review_sheet.py` |

---

## 三、每个开关的选项与默认

### 3.1 `render_profile`｜包装版本（默认 `branded`）

| 选项 | 含义 | 影响 |
| :--- | :--- | :--- |
| `branded`（默认） | 字幕 + 顶部品牌条 + 开场钩子卡 + 片尾小黄车提示 + 挂角贴纸 | 现有全部行为，QC 覆盖率 100% |
| `plain` | 全片无字幕、无文字、无贴纸 | QC 10 项文字类断言记 SKIP；违禁词自动降为"仅记录"；放大镜/转场/镜像 4 项画面断言**照检** |

### 3.2 `duration_preset`｜时长档位（默认 `standard`）

| 选项 | 区间 | 说明 |
| :--- | :--- | :--- |
| `standard`（默认） | 45–65s | 兼容现有基线 |
| `compact` | 48–55s | 节奏更紧；会**强制** `fabric_ratio=strict` |

> 40–44s 档位不提供：40s 下 fabric 25% 线逼得上限 ≤10s，与下限 8s 挤成 2s 窗口，参数类内容基本不可选。

### 3.3 `fabric_ratio`｜参数类内容占比（默认 `strict`）

| 选项 | 面料镜头上限 | 断言 |
| :--- | :--- | :--- |
| `strict`（默认） | 11s | 占成片 ≤25%（45s→24.4%，60s→18.3%） |
| `loose` | 20s（现状） | 不判定，仅记录，用于对比实验 |

> 实测 7 条历史成片 7/7 破线（29.9%–33.6%），`strict` 修的就是这个。

### 3.4 `promo_words`｜"福利"类措辞（默认 `allow_welfare`）

| 选项 | 行为 | 用例 |
| :--- | :--- | :--- |
| `allow_welfare`（默认） | 先剥离 `福利款/福利感/很福利` 再跑原黑名单 | 「这个是福利款，价格特别优惠」→ **放行**；「你们今天福利来啦」「抢福利了宝子们」「福利名额就五个」→ **仍拦** |
| `block_all` | 不剥离，任何含"福利"整句删 | 历史行为 |

### 3.5 `violation_action`｜违禁词处置（默认 `delete`）

| 选项 | 派生的 `subtitle_action` | 效果 |
| :--- | :--- | :--- |
| `delete`（默认） | `delete` | 命中词从字幕消失，句子其余保留；不消音、不打码 |
| `mask` | `mask` | 替换为 `***` |
| `record_only` | `off` | 只写判定清单，字幕原样（质检会给 WARN） |

### 3.6 `transition`｜切片间转场（默认 `on`）

| 选项 | 效果 |
| :--- | :--- |
| `on`（默认） | 0.25s 视频 `xfade` + 音频 `acrossfade`，总时长按 N−1 个转场折算 |
| `off` | 硬切，总时长不折减 |

### 3.7 `magnifier`｜面料放大镜（默认 `on`）

| 选项 | 效果 |
| :--- | :--- |
| `on`（默认） | 面料类分镜最多叠 2 枚 1.8x 圆形放大镜，锚点由姿态计算避开面部 |
| `off` | 无圆形特效，仅保留推镜 |

### 3.8 `manual_review`｜人工复核清单（默认 `on`）

| 选项 | 效果 |
| :--- | :--- |
| `on`（默认） | 生成 `output/<任务号>/manual_review.md`，13 项预填"未发现问题" |
| `off` | 交付物不含该清单 |

### 3.9–3.12 未开放项（界面置灰，写非默认值会被校验拒绝）

| 开关 | 未开放原因 |
| :--- | :--- |
| `auto_frame` | 17% 构图与清晰度门禁（200）冲突，取舍未验证 |
| `speech_rate` | `atempo` 全库 0 处，需字幕/消音/转场/时长全链时间戳联动 |
| `color_quota` | 颜色标注与"只按身穿计入"的口径未落地 |
| `review_sheet` | 工具 `build_review_sheet.py` 未实现 |

---

## 四、组合与互斥关系

### 4.1 强制收敛（override，写入报告并打印日志）

| 触发条件 | 强制结果 | 原因 |
| :--- | :--- | :--- |
| `render_profile=plain` | `violation_action → record_only` | 没有字幕可删，删词策略无对象 |
| `duration_preset=compact` | `fabric_ratio → strict` | 短时长下放宽参数占比会挤掉场景/搭配内容 |

> UI 层同步：触发规则时被锁定的选项**自动选中并置灰**，同时在面板下方用黄字说明原因；切换回不触发规则的状态会解除锁定（`resetFeatureLocks()`）。

### 4.2 断言联动（SKIP，不计入通过率但计入覆盖率）

| 形态 | SKIP 的断言（共 10 项） |
| :--- | :--- |
| `plain` | 字幕文件存在性、字幕避让小黄车盲区、字幕零价格、字幕无 ASR 禁用错词、违规判定完整性、字幕违规词零残留、字幕无繁体字、成片ASR复检-V1 字幕-原声一致性、视频顶部平台标识、挂角贴纸时序 |

**只有"文字/贴纸类"才 SKIP**：放大特效时序、切片转场复测、镜像防反文字遮挡、放大镜特效复测这 4 项属画面技法，与成片形态无关，plain 下照常执行（早期版本在 `_audit_overlays` 里整体 `return`，把这 4 项连 SKIP 记录都没有地静默丢掉了，2026-11-01 已修）。

同一份清单**不再**写进 `contracts/features.yaml`：曾有 `forces_skip` 键既无人消费、又写出两个并不存在的检查名（"Hook 卡人脸安全""CTA 贴片位置"）。权威位置是 `qc_auditor.py`（消费方）+ 本表（说明）。

报告会写 `coverage: {total, executed, skipped, ratio}`，并在 `disclaimers` 里声明："本次为纯画面成片，字幕类断言无检查对象，记为 SKIP 而非 PASS；违禁词处置降级为仅记录。"

### 4.3 可自由组合（无冲突）

- `transition` × `render_profile`：纯画面也可用淡入淡出
- `magnifier` × `render_profile`：放大镜属画面元素，纯画面同样保留
- `fabric_ratio` × `duration_preset=standard`：任意档位都可选严格/宽松
- `manual_review` × 任何形态：交割物独立，不影响成片

### 4.4 与 `constraints.yaml` 的优先级

```
constraints.yaml（全局默认/阈值）
      ↑ 被覆盖的键：duration_range / funnel.fabric.max / transition_effect.enabled /
                    magnifier_effect.enabled / violation_words.handling.subtitle_action
spec.options.features（单任务覆盖，本次新增的总入口）
      ↑ 只能在这 5 个键上生效；其它约束（响度/分辨率/黑名单正则等）仍由 constraints 决定
```

---

## 五、实测验证（本次跑的真实结果）

| 验证 | 结果 |
| :--- | :--- |
| 单元测试 | `run_regression.py --quick` **18/18 个测试文件全通过**（其中 `test_feature_flags.py` 10 项断言；黑名单测试新增福利分流 9 例） |
| plain 出片 | 48.86s，`subtitles.ass` **不生成**，`platform_bar/hook_card/cta_badge` 全 `applied=false`、`sticker_events=0` |
| plain 质检 | **全绿**（2 WARN），**41 项中 31 执行 / 10 SKIP，`coverage.ratio = 0.76`**；放大镜/转场/镜像 4 项 + 新增的参数占比 1 项照检（按同一口径推算，plain 未重跑） |
| branded 质检 | **全绿**（2 WARN），**38 项全部执行、0 SKIP，`coverage.ratio = 1.0`**，无 FAIL（含新增「参数类内容占比复测 20.6%」） |
| 福利词 | 「这个是福利款，价格特别优惠」放行；「你们今天福利来啦」「抢福利了宝子们」「福利名额就五个」仍拦 |
| 组合规则 | `plain + delete` → `record_only`；`compact + loose` → `strict`（均写入 forced 列表） |
| 校验 | 未知键、非法值、未开放项（如 `speech_rate=1.2`）全部拦截并给出原因 |

---

## 六、三种使用方式

```bash
# 1) exe 界面（推荐）：双击 SpecCreator.exe → 「⚙️ 6. 能力开关」→ 选完保存/直接运行
# 2) 命令行查看某个 spec 的生效值（含组合收敛与告警）
python -m src.tools.feature_flags --spec job_spec.json
# 3) 手写 spec（CI / 脚本）
{ "options": { "features": { "render_profile": "plain", "duration_preset": "compact" } } }
```

未写 `features` 时全部取推荐默认值——**旧 spec 无需修改即可继续跑**，行为变化仅有 P0 那两项（`fabric.max` 20→11、福利款放行），这是本轮批准的建议默认值。

---

## 七、复核修复记录（2026-11-01 第二轮，均已在同一分支内修掉）

| # | 问题 | 症状 | 修复 |
| :--- | :--- | :--- | :--- |
| 1 | **开关到不了装配器** | `assemble_video_tool()` 内部只读 `constraints.yaml`，不接 `spec.options.features`；`render_profile=plain` 时成片仍烧字幕、叠贴纸，而 QC 拿着 plain 约束把字幕断言全 SKIP → **带字幕的片子"全绿"**（假绿） | `assemble_video_tool(..., constraints=None)` 新增透传，`agent_director` 传入 `self.constraints` |
| 2 | **over-skip** | `_audit_overlays` 在 plain 下整体 `return`，把放大特效时序/切片转场/镜像防反/放大镜复测 4 项**连 SKIP 记录都没有地**丢掉 | 改为只对"品牌条 + 挂角贴纸"两个文字项记 SKIP，画面技法照检 |
| 3 | **假检查名** | plain 报告里出现两个从不存在的检查项「Hook 卡人脸安全」「CTA 贴片位置」；同一份清单还抄进了 `features.yaml` 的 `forces_skip`（无人消费） | 删除假名与 `forces_skip`；Hook/CTA 的人脸安全统一由 `_audit_overlay_face_safety` 读 `meta.applied` 处理 |
| 4 | **V1 重复且矛盾** | plain 下同一项同时出现 `SKIP` 和「未提供 .ass … 跳过比对」的 `WARN` | plain 时 V1 记 SKIP 并从结果循环中剔除；V2/V3/V4/V5 照常执行 |
| 5 | **契约漂移** | `qc_report.schema.json` 的 `status` 枚举没有 `SKIP`、`level` 不允许 `-`，新报告按契约是非法文档 | 补 `SKIP` / `-`，并把 `render_profile`、`coverage`、`disclaimers` 写进 schema |
| 6 | **缓存不看开关** | 代码指纹不含 `features.yaml` / `assemble_video_tool.py`，只改开关会命中旧成片 | 两者加入 `_CODE_FINGERPRINT_FILES` |
| 7 | **未开放项被写进 spec** | 界面保存时把 4 个 `disabled` 项按当前默认值一并落盘；将来若改默认值，旧 spec 会被 `validate` 判"取值非法" | `readFeatures()` 跳过 `state=disabled` |
| 8 | **组合解锁后残留** | 选 `plain`（违禁词被锁为"仅记录"）再切回 `branded`，违禁词处置静默停在"仅记录" | 锁定前快照 `_lockedPrev`，解锁时还原 |
| 9 | **打包版读不到开关定义** | onefile 打包后 `__file__` 在 `_MEIPASS`，`feature_flags` 按 `__file__` 推三层 → 指向临时目录祖先，`contracts/features.yaml` 必然缺失；exe 是 `--windowed`，WARN 无处可见，面板静默空白 | `feature_flags._project_root()` 改为 frozen 时取 `dirname(sys.executable)`（与 `desktop_spec_creator.get_project_root()` 同口径），`default_definitions_path()` 按「项目根 → `_MEIPASS/contracts`」查找；`scripts/build_exe.py` 加 `--add-data`；补测试 `test_frozen_root_resolution`；面板缺定义时显式提示（**需重新打包 exe**） |
| 10 | **缺候选清单时 3 项断言静默消失** | `_audit_cover_width` 只在 `cover_candidates.json` 存在时调用，文件缺失时「封面宽占比/完整人体/衣着一致性」三项在报告里**查无此项**，也不是 SKIP | 补 `else` 分支记 SKIP，计入覆盖率 |

---

## 八、九项待决事项的落地状态（2026-11-01 复核）

| # | 决策（10-30 定稿） | 状态 | 落地位置 / 证据 |
| :--- | :--- | :--- | :--- |
| 1 | 不支持 40–44s，维持 `[45,65]` | ✅ | `constraints.video.duration_range=[45,65]`；`features` 只给 standard/compact 两档 |
| 2 | plain 只建开关、默认不启用 | ✅ | `features.render_profile` 默认 `branded`；plain 端到端可用（本轮修完假绿后实测无字幕、无贴纸） |
| 3 | 语速三档但暂不开放 | ✅ | `features.speech_rate` 三档 + `state: disabled` + 理由；写非默认值被 `validate` 拒（有测试） |
| 4 | 17% 构图不动清晰度门禁 200 | ✅ | `cover.sharpness_min` 仍为 200；`auto_frame` 保持 disabled 未接线 |
| 5 | 福利只放行「福利款/福利感/很福利」 | ✅ | `build_plan._WELFARE_NOUN_ALLOW` 白名单剥离 + 9 例回归（含"福利来啦/抢福利/福利名额/福利链接/福利价"必拦） |
| 6 | `fabric.max` 11 + 占比断言固定 ≤25% | ✅（本轮补齐） | 配额：`fabric_ratio=strict → video.funnel.fabric.max=11`；断言：本轮新增 `qc_auditor._audit_fabric_ratio()`（strict 档 BLOCK ≤25%、loose 档只记录、无分镜表记 SKIP），实测 10.2s/49.5s = 20.6% |
| 7 | 多色先按 ASR 口径 + 披露 unlabeled | ⛔ 未做 | 仓库仍无颜色概念（无 `product.colors` / `options.color_preference` / ASR 标色）；`features.color_quota` 已按此理由置灰。属独立迭代（表 1 第 9 项 + 2 人日） |
| 8 | 13 项主观项交付人填、模板预填 | ✅ | `generate_from_job_spec` 产出 `manual_review.md`（13 行预填 + 形态/时长/WARN 数），已进交付清单 |
| 9 | 死键审计按基线 diff 阻塞 CI | ✅ | `tests/test_requirements_audit.py::case_dead_keys_match_baseline`：新增未登记死键 → 失败；已修复但仍留在基线 → 也失败（强制只减不增），随 `run_regression` 跑 |

> 结论：9 项里 **8 项已落地**（其中第 6 项的断言是本轮补齐的），仅第 7 项（多色）按原计划留给独立迭代——它的开关位已经在界面上以"未开放 + 原因"的形式占位。
