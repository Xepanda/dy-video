# 终审裁决与决策清单（2026-10-30）

> **角色**：独立终审 agent（不参与 A~E 任一份的撰写）
> **输入**：A `docs/prompt_review_2026-10-30.md`、B `docs/workflow_enhancement_proposal_2026-10-30.md`、C `docs/workflow_enhancement_proposals_2026-10-30.md`、D `docs/prompt_review_unified_2026-10-30.md`、E `C:\Users\ePanda\Desktop\agent-tmp.txt`
> **裁决基准**：仓库实测（`git` / `contracts/constraints.yaml` / `src/` / `output/*/qc_report.json` / `data/work_dirs/*/plan.json`）
> **裁决原则**：凡 A~E 与实测冲突，一律以实测为准；凡分歧可由计算或命令消解的，不投票、直接算。

---

## 0. 终审摘要

### 0.1 实测复核（本次新增证据，A~E 均未提供）

| # | 复核对象 | 实测结果 | 对裁决的影响 |
| :--- | :--- | :--- | :--- |
| E1 | **7 条历史成片的 fabric 占比**（`data/work_dirs/*/plan.json` → `selection_meta.funnel`） | 33.6% / 27.4% / 31.9% / 26.3% / 28.3% / 33.3% / 28.0%，**中位 28.3%，7/7 全部 > 25%** | "fabric 上限偏松"从定性判断升级为**已发生的 7/7 破线**；这条是当前成片可量化缺陷里最硬的一条 |
| E2 | `plan.subtitles.keywords` 实际值 | `10-29-毛衣` 成片为 `[]`（`job_spec.options.highlight_keywords` 为空即常态） | A/B/C 的"从 `plan.subtitles.keywords` 取商品关键词"**口径不成立**，主题款/贵材质断言会永久空跑或误报 |
| E3 | QC 报告结构 | `qc_report.json` 共 **34 条 checks**（key 是 `checks` 不是 `assertions`），第 3 条「字幕文件存在性」`level=BLOCK` | 纯画面模式下 ASS 不生成时，第一条撞的就是这条 BLOCK——**这是"7 类"而非"6 类"字幕断言** |
| E4 | `audit_final_deliverables()` 数据源 | 入参只有 video/cover/ass/report/cover_meta/candidates/overlay_meta；**不读 plan.json，也不读 spec** | 任何"统计类"断言（fabric 占比、styling 段数、核心词）都缺数据源，必须先加 `plan_path` 入参 |
| E5 | git 真实状态 | 当前分支 **`master`**；`6bb0fc1 Merge PR #9`、`46864ff Merge PR #10` 均已在 master 上；`git branch` 无本地特性分支 | 与"事实 7"（master 尚无上述能力、工作区在 #10）**不符**，见【纠正 1】 |
| E6 | 回归基线 | `scripts/run_regression.py` 的 `UNIT_TESTS` 已有 **17 个**测试文件（= #9 的 16 + #10 的 `test_desktop_spec_creator.py`） | 基线应为 **17/17**，不是 15/15 或 16/16，见【纠正 1】 |
| E7 | `constraints.yaml` 自标注死键 | **10 处**（7 处 ⚠️未接线 + 3 处 ❌未实现） | "死键纪律"不是抽象治理，是 10 个已在注释里认罪的键 |
| E8 | `HOOK_KEYWORDS` 构成 | 48 词，强卖点仅 `最显瘦` 1 个（约 2%），其余为 `绝了/超级/好看/种草` 等情绪泛词 | Hook 加权不是"优化体验"，是 2% 有效信号对 98% 噪声 |
| E9 | `atempo` / `asetrate` | `src/` 全库 **0 处** | 语速是从零新增能力，不是参数调整 |
| E10 | `fabric` 配额的真实约束力 | `build_plan.py:617-646` 按 `q_max` 硬截断，但 `_top_up()`（:660 起）从剩余池补齐时**不校验类目配额** | "只改 `fabric.max`"不足以保证占比，见【纠正 5】 |

### 0.2 六项残留争议的最终裁决

| 争议 | D（统一稿） | E（交叉比对稿） | **终审裁决** | 依据 |
| :--- | :--- | :--- | :--- | :--- |
| **① fabric.max** | 11 | 12 | **取 11**，断言 `≤0.25` **不加容差** | 12 在 45s = 26.7%、46s = 26.1%，与自身断言冲突；11 在 45s = 24.4%（余量 0.6pp，对"整秒选段"足够）。若取 12 则断言必须写成 `min(0.25, 12/total)`——**这等于让断言随总时长漂移，失去门禁意义，否决**。且 11 与 12 的业务差只有 1s，不值得为它把断言做成函数 |
| **② 批次划分** | 5 批 | 4 批 | **4 批**，但**批内强制串行小 PR** | E 的"5 批过碎"论据一半已失效（master 已合并 #9/#10，长活分支不存在，合并开销不再是主矛盾）；但"风险隔离"仍然成立。正确做法是：**批次 = 合并单元（4 个），风险隔离 = 批内 PR 粒度 + 每 PR 一次 `--quick`**，而不是靠增加批次数。第 3 批（形态）内部顺序固定为 plain → 语速 → 时长 |
| **③ 福利词** | 只拦 福利价/福利秒杀/福利链接/福利到 | M6 写"排除福利款、**福利价**" | **E 错误必须纠正：福利价仍拦**。D 的清单也**不可直接采用**（会打破现有回归用例，见【纠正 3】）。**采用"白名单剥离"方案**：`福利(款\|感)` 先剥离再跑原黑名单 |
| **④ 纯画面档位** | 需决策 | 第二批（2~3 天） | **两者都对，但说的是两件事**：**"建能力"可进第 2 批**（2~3 天，最小可用版见 Q1）；**"启用形态"必须等用户确认**。第 2 批产出的是开关与 SKIP 机制，默认 `branded`，不产出任何 plain 成片 |
| **⑤ 40–44s** | 需决策 | 未提 | **不支持 40–44s**。理由是可计算的连锁：40s 时 fabric 25% 线 ⇒ `fabric.max ≤ 10`，与 `fabric.min: 8` 挤成 8–10 的 2s 窗口，fabric 类目近乎不可选；42s ⇒ 10.5。保 fabric 语义就得破线，保线就得废 fabric |
| **⑥ 颜色跨源** | 否决全局禁跨源，改局部同源连续 | 未处理 | **确认 D 的裁决**。多源跨素材是 `plan.clips[i].source_file` + `render_clips` 的核心能力，全局禁跨源 = 退化为单素材剪辑。改为：**换色引导段与其承接段必须同源且时间连续（允许 ≤0.25s 转场），成片其他镜头照常跨源** |

---

## 第一部分：值得纳入工作流的具体内容

> 分组口径：**可立即做** = 不需产品决策即可开工；**需先决策** = 需用户拍板口径才能开工；**探索** = 先影子模式，只加权不拦截。
> 工作量单位：人日（单人）。

### 1.1 可立即做（12 项）

| 编号 | 事项 | 为什么值得（量化收益） | 落点（文件/键/函数） | 验收标准 | 工作量 | 风险 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **I0** | **QC 增加 `plan_path` 入参与 `plan.product_ctx`**（使能项，I2/I5/I6/I7 全部依赖它） | 现状 QC **零**数据源可做统计类断言（E4）。加这一项后，后续 4 条断言才可能落地，否则只能靠"读 ass 文本猜" | ① `src/qc/qc_auditor.py:101 audit_final_deliverables()` 增 `plan_path: str = None`，内部加载 `plan`，写进 `self.plan`；② `src/agent/agent_director.py:719` 调用点补 `plan_path=os.path.join(self.work_dir, "plan.json")`；③ `src/tools/build_plan.py:834` 附近的 plan 组装处新增 `selection_meta.product_ctx = {"core_words": [...], "material_words": [...], "is_top": bool}`（复用 `_extract_product_context` 的返回值）；④ 阶段缓存 key 纳入 plan 路径 | 7 条历史 plan 重跑：`qc_report.json` 中新增字段可读；branded 成片 34 条 checks 的 PASS/WARN 分布与改动前**逐条一致**（零漂移） | 1 | 低。唯一风险是缓存 key 变更导致首跑全量重算（一次性成本） |
| **I1** | **福利词放行（白名单剥离，不删原模式）** | 实测「这个是福利款，价格特别优惠」被 `.*福利.*` **整句拦截**，「性价比超高的一件」「买了超级划算」放行 → 拦截由"福利"二字单独触发，与价格无关。女装直播"福利款/福利感"是高频卖点，被拦=该块从 hook/fit 候选池整块消失。不改则提示词"保留福利款/很划算/性价比高"永远做不到 | ① `src/tools/build_plan.py:158 is_blacklisted()` 内新增 `_ALLOW_PROMO = re.compile(r"福利(款|感)")`，对 `inv_patterns` 匹配改用 `probe = _ALLOW_PROMO.sub("", text)`；② `INVENTORY_PATTERNS:84` 的 `.*福利.*` **保持原样**（不要替换成 D 的四个模式，见【纠正 3】） | `tests/test_blacklist_precision.py`：`MUST_ALLOW_CONTENT` 新增「这个是福利款，价格特别优惠」「这件很福利」「性价比超高的一件」；`MUST_BLOCK_BLACKLIST` **保留**「你们今天福利来啦」，新增「福利价九十九」「抢福利了宝子们」「福利名额就五个」。`run_regression --quick` 全绿 | 0.5 | 低。混句"福利款和福利价"剥离后会剩"福利价"仍被拦（已推演正确）；"福利款九十九"由 `detect_price` 兜住（实测命中"价格表达"） |
| **I2** | **`funnel.fabric.max` 20 → 11 + 占比断言 + `_top_up` 补丁** | 实测 **7/7 历史成片破线**（中位 28.3%，最高 33.6%，见 E1）。改 11 后：45s→24.4%、59.19s→18.6%。这是当前唯一有 7 条实测报废证据的观感缺陷 | ① `contracts/constraints.yaml:276` `fabric: {min: 8, max: 11}`；② `src/tools/build_plan.py` `_top_up()`（:660 起）入参增加 `quota_max`，补齐时按 `_quota_cat` 累计上限过滤——**不加这条补丁，短素材上补齐阶段会把 fabric 块重新拉进来，配额形同虚设**（E10）；③ `src/qc/qc_auditor.py` 新增 check「fabric 时长占比」，`measured = plan.selection_meta.funnel.fabric / total`，`threshold = 0.25`，`metric_key = "video.funnel.fabric"`，level **WARN**（沿用 `funnel_enforce_level: WARN`，不升级为 BLOCK） | ① 7 条历史素材重跑，fabric 占比全部 ≤25%（预期 18–24%）；② 边界单测：fabric=11、total=45 → 24.4% PASS；fabric=11.5 → FAIL；③ 总时长仍 ≥45s 且 ≤64.5s | 1 | 中低。8–11s 窗口很窄，类目稀薄素材 `funnel.notes` 的"配额不足"会变多——但 `enforce_level: WARN` 保证不拦交付 |
| **I3** | **句级跨段赘述去重** | 现有只有 `_clean_oral_fillers` 的**字符级**叠词（真的真的/好看好看），跨句"黑色一套，黑色一套我给大家穿了看一下"完全没处理。按 ASR 段长估，单次重复铺垫占 2–5s ≈ 成片的 4–8% | `src/tools/build_plan.py:208 build_blocks()` 内、`_trim_block_to_duration` 之前新增 `_dedup_adjacent(segments)`：相邻 utterance `difflib.SequenceMatcher(None,a,b).ratio() >= 0.8` 且 `abs(len(a)-len(b))/max(len) <= 0.3` 时，保留"关键词命中更多 / 更长"的一条；**只在 utterance 边界删，不在时间戳中点切** | 单测：「黑色一套，黑色一套我给大家穿了看一下」只留一条；「件件都好看」不被误删；「这个版型显瘦」→「这个版型特别显瘦」**保留两条**（语义递进，须加"差异词"例外：若两者差集含卖点词则不去重）；7 条素材重跑总时长不跌破 `dur_min` | 1 | 中。0.8 阈值在语义递进句上会误删，必须带"差集含卖点词"例外；需 5 条素材回放确认 |
| **I4** | **Hook 组合加权 + 单段封顶** | `HOOK_KEYWORDS` 48 词中强卖点仅 `最显瘦` 1 个（E8），`_segment_score` 是 `hits*2.0` → "绝了"与"显白"等权。女装前 5s 的留人靠显白/显瘦/小个子/反差，不靠"绝了" | ① `src/tools/build_plan.py` `HOOK_KEYWORDS` 由 `List[str]` 改 `Dict[str,float]`（强 3.0：显白/显瘦/小个子/遮胯/显高/反差；中 1.5：高级感/气质/版型；泛 1.0：其余）；② `:180 _segment_score()` 改 `sum(weight.get(k,1.0) for k in ...)` 并对 hook 类别封顶 `min(score, 12.0)`；③ 核心词命中改**乘性** `×3.0`（"讲的是不是这款"是必要条件），主题款指代（这件/这条+核心词）**加性 +3.0**；④ `:636` `prefer_keywords=HOOK_KEYWORDS` 与 `:311 _trim_block_to_duration()` 的 `k in wtext` 需兼容 dict | 5 条历史素材：hook 段前 5s 文本必含 ≥1 个"强卖点"词，或 `selection_meta.notes` 明确记"素材未提及强卖点"；单测覆盖权重累加与 12.0 封顶；总时长不劣化 | 1 | 中低。可能把"显瘦"长段顶到开场、压掉有情绪的开场——封顶 + 情绪词保底权重缓解 |
| **I5** | **主题款明确断言（前 5s）** | 无字幕档位下唯一能把"这次讲的是哪件"讲清的机制；女装误认款直接掉转化。且这是 A/B/C 三方唯一全票的"真空白" | ① 数据源用 **I0 的 `plan.selection_meta.product_ctx.core_words`**（**不是** `plan.subtitles.keywords`，实测恒空，见【纠正 2】）；② `src/qc/qc_auditor.py` 新增 check：`final_asr_segments.json` 中 `st <= 5.0` 的文本命中「(这件\|这条\|这双\|这个\|这款) + 核心词」或直接命中核心词；level **WARN**；③ `spec.options.skip_topic_anchor=true` 时 SKIP | 7 条历史成片不误报；构造 2 条反例（开场只说"太显瘦了""绝了姐妹们"）必须命中；`core_words` 为空时记 SKIP 并注明"spec 标题未提供核心词" | 1.5 | 中低。核心词从 `title_line1/2` 抽取，标题不规范（实测有"渐变针织衫贸易"）会把"贸易"当核心词 → 需停用词表清洗 |
| **I6** | **贵材质 ≥1 处强调** | `highlight_keywords` 只用于字幕高亮，**零反向校验**：针织衫素材台词 0 次提"桑蚕丝"仍静默 PASS。贵材质是价格支撑点 | ① `build_plan` 在 `product_ctx.material_words` 里落 羊绒/羊毛/桑蚕丝/真丝/精纺/羊驼；② `qc_auditor` 新增 check：`final_asr_segments.json` 全文命中 `material_words` ≥1 次，否则 **WARN**；③ 纯棉/化纤类商品（`material_words` 为空）→ SKIP | 针织衫素材正确 WARN（不是静默 PASS）；羊绒素材 PASS；`material_words` 为空的素材显示 SKIP | 1 | 低（依赖 I0 字段） |
| **I7** | **搭配镜头数上限 2–5** | `styling` 配额 15–30s、**无镜头数上限**；30s 上限下若每段 5–6s 即 5–6 段，主款曝光被稀释 | ① `build_plan.build_plan()` 配额循环（:616-646）对 `cat == "styling"` 加 `max_blocks=5` 判断；② `qc_auditor` 新增 check：`len([c for c in plan.clips if c.category=="styling"]) ∈ [2,5]`，越界 WARN | 多搭配素材回跑 styling ≤5 段；总时长仍 ≥45；不足 2 段时 WARN 不拦 | 0.5 | 低 |
| **I8** | **死键审计并入 CI（基线 diff 口径）** | `constraints.yaml` 已有 **10 处自标注死键**（E7）；`audit_requirements.py --only-dead` 有能力但**不在 CI**（`.github/workflows/regression.yml` 仅跑 `run_regression --quick`）。本轮还要新增 `render_profile`/`time_stretch`/`funnel.fabric` 等键，不锁住必然复现 | ① `src/tools/audit_requirements.py` 增 `--fail-on-new-dead --baseline <json>`：与基线 diff，**只对"新增死键"返回非零**；② 生成 `contracts/dead_keys_baseline.json`（把现有 10 个登记进去）；③ `.github/workflows/regression.yml` unit job 末尾加一行 `python -u src/tools/audit_requirements.py --only-dead --fail-on-new-dead --baseline contracts/dead_keys_baseline.json`；④ `scripts/run_regression.py` 的 `UNIT_TESTS` 已含 `test_dead_constraint_wiring.py`，扩为读基线文件 | 故意在 `constraints.yaml` 加一个无人读的键 → CI 变红；删掉 → 变绿；现有 10 个死键不导致红 | 1 | 中。若做成"全量死键即失败"，第一次接入就全红 → 必须基线 diff（这也是我建议与 A/B 的"直接阻塞"不同的地方） |
| **I9** | **agent 回写必达（WARN + 报告顶部提示）** | `violation_words.review.fallback_hard_categories` 只有 `medical_claim`/`false_absolutism` 2 类走兜底，agent 不回写时其余类目判定质量静默下降，报告里看不出来 | ① `qc_auditor` 新增 check：读 `agent_decision_manifest.json` 的 `review_engine`，`startswith("fallback")` → WARN；② `qc_report.json` 增顶层 `summary.disclaimers: []`，把该提示写进去（纯画面也用这个字段，见 Q1） | 不回写 → WARN + 顶部提示；回写 → 两者都消失 | 0.5 | 低 |
| **I10** | **交付审核包（contact sheet）** | A 的 34 项自检里 **13 项只能人判**；现在人判要拖 60s 时间轴，一张 8–12 格图能把审核压到 10s 内 | 新增 `src/tools/build_review_sheet.py`：读 `plan.json`，每 clip 取 `st + dur/2` 抽 1 帧，拼 4×3 网格，叠加 `时间码 / 镜号 / category`，输出 `output/<pid>/review_sheet.jpg`；`agent_director` S6 段写进 `generation_manifest.json` 的 `final_deliverables` | 一张图能分辨每镜在讲什么；branded/plain 均不报错；素材缺失帧时降级为占位灰块而非崩溃 | 1.5 | 低（纯增量产物，不参与 QC 判定） |
| **I11** | **多色阶段一：清单 + 偏好色配额（ASR 口径）** | 仓库**完全无颜色概念**；这是女装切片最高频场景，且偏好色占比是可量化硬规则（2 色 60–70% / 3 色 45–55%）。ASR 侧零模型依赖 | ① `job_spec`：`product.colors[]`、`options.color_preference`；② `src/tools/desktop_spec_creator.py` UI 加"多色输入框 + 偏好色下拉"；③ `build_plan` 新增 `_color_of(text)` 用颜色词表（奶白/奶咖/黑/白/黄/蓝/红/灰/卡其/杏/燕麦…）给 block 打 `color` 标；配额循环里对偏好色做软倾斜（WARN）；④ `qc_auditor` 新增 check：偏好色 clip 时长占比 vs 目标区间，`metric_key: "product.color_ratio"` | 双色素材（奶白/奶咖）偏好色占比落 60–70%±5%；无法标色的 clip 单列 `unlabeled` 计数并在 report 披露 | 2 | 中。"这个色/这个"无颜色词的段无法标注，统计口径会失真——必须把 `unlabeled` 比例作为断言的一部分（>30% 则 SKIP 而非 FAIL） |

### 1.2 需先决策（5 项）

| 编号 | 事项 | 为什么值得（量化收益） | 落点（文件/键/函数） | 验收标准 | 工作量 | 风险 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Q1** | **纯画面档位 `plain`（能力先建、形态后启用）** | 覆盖"无文字压画面"发布形态；当前 **5 处**无条件执行（不是 4 处）：`generate_ass_subtitles(:841)`、`generate_hook_card(:852)`、`generate_cta_badge(:858)`、`subtitles=` 过滤器(:984)，**加第 5 处「字幕文件存在性」QC 断言（BLOCK）**——ASS 不生成时它先炸（E3） | ① `contracts/constraints.yaml`：`hook_card.enabled`、`cta.enabled`、`subtitles.enabled` 三个键（**都要默认 true**）；② `src/core/video_assembler.py` 上述 4 处加 `if enabled:` 分支，未启用时 `ass_path` 传空串且在 overlay_meta 记 `disabled`；③ `src/qc/qc_auditor.py` 新增 `skip(name, reason)` 方法写 `status="SKIP"`；把「字幕文件存在性」+ 6 条字幕类断言共 **7 条**改为 `if not subtitles_enabled: self.skip(...)`；④ `qc_report.json` 增 `coverage: {total, executed, skipped, ratio}` | ① branded 零漂移：7 条历史成片 34 条 checks 的 status **逐条一致**；② plain 报告顶部 `summary.disclaimers` 有"字幕防护已旁路，违规词仅 ASR 侧记录"；③ `coverage.ratio` 在 plain 下 <1.0 且被显式打印 | 2–3（最小可用版）<br>4–5（含逐条 SKIP 分级与 SKIP 原因枚举） | **高**。QC 覆盖率实质下降（7/34 变 SKIP）；违规词"只删字幕"在 plain 下**无对象**，退化为 S1 的 ASR 扫描 + WARN |
| **Q2** | **语速三档（1.0 / 1.15 / 1.2）** | 完播率提升；`atempo` 全库 0 处（E9），属从零新增。70s 素材 ×1.15 → 61s，可落进 45–65 区间 | ① `constraints` 新增 `time_stretch: {enabled: false, rate: 1.0}`；② `video_assembler._extract_clip` 加 `[0:v]setpts=PTS/{rate}[v];[0:a]atempo={rate}[a]`；③ **所有时间戳统一 ×1/rate**：ASS 事件、消音窗口、`xfade` offset、`plan.clips[i].dur`、`duration_range` 判定、`transition_effect.duration_sec`；④ `qc_auditor` 增「音画偏移 ≤ `video.av_sync_error_max_ms`」实测断言（该键当前是死键，正好一并接线） | 1.2x 成片：V1 一致性 ≥0.7；总时长回落区间；音画偏移 ≤40ms；主观听测无齿音/爆破音恶化（1 条 60s 片段 A/B） | 3–4 | **高**。全链时间戳换算，任何一处漏乘都会静默错位；建议先在 plain 上验证（无 ASS 负担），但 plain 依赖 Q1 |
| **Q3** | **时长下限是否放到 40–44s** | 文档要求 40–70s；当前 `[45, 65]` | 若支持：`constraints.yaml:262 duration_range: [40,65]` + `build_plan` `dur_min` + `qc_auditor` 时长断言同步；**且必须同步下调 `fabric.max`**（见裁决⑤） | 40s 成片：时长 PASS 且 fabric 占比 ≤25%（⇒ `fabric.max ≤ 10`） | 0.5（改键）+ 2（重定所有断言口径） | 中。**结论：不支持**。fabric 窗口被压到 8–10s，等价于废掉参数类内容这个类目 |
| **Q4** | **17% 构图与"清晰度换放大"的取舍** | 视频分镜端**零构图约束**（封面有 `head_top_min_ratio: 0.13`，视频没有）；C 的公式 `S=(1-0.17)/(1-y_head)` 数学正确（1920×0.17=326.4px，已验算） | ① 每 clip 抽 1 帧跑 Pose 取 `y_head`，写 `plan.clips[i].framing_scale = max(1.0, S)`（`S<1` 钳制为 1）；② `src/core/video_assembler.py:74 build_clip_filter()` 在 `crop` 前插入 `scale=w=iw*S:h=ih*S` 并把 crop 的 y 改为 `ih-1920`（底边不动、左右对称）；③ `qc_auditor` 复用 `cover.sharpness_min: 200` 对放大分镜复测 | 成片抽帧头顶 ≤17%；放大分镜 `post_scale_sharpness ≥ 200`；**不满足则回退该 clip 的 `framing_scale=1.0`** | 3–5 | 中高。二次放大必然掉清晰度；在 22fps 源、1080×1920 上 S=1.2 即掉约 30% 有效像素 |
| **Q5** | **多色配额的统计口径（达人身穿 vs 手持）** | 文档明确"只按达人身穿计入，手持/助播拿着不算"。这决定 I11 是"ASR 口径可上"还是"必须等视觉标注" | 若严格按上身：阶段一必须引入人体 bbox 内主色统计（MediaPipe Pose 已有，可复用 `detect_garment_match` 的躯干区域），1 人日；若放宽为 ASR 口径：I11 按原样 2 人日 | 严格口径：双色素材上身主色占比与人工标注偏差 ≤10pp；放宽口径：`unlabeled` 比例 ≤30% | +1（严格） | 中。严格口径把 I11 从"零模型"变成"依赖 Pose"，且颜色判定在暖光直播间偏色严重 |

### 1.3 探索（5 项，先影子模式，只加权不拦截）

| 编号 | 事项 | 为什么值得（量化收益） | 落点（文件/键/函数） | 验收标准 | 工作量 | 风险 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **X1** | **颜色转场强锁（阶段二，局部同源连续）** | 杜绝"说奶咖、画面奶白"的穿帮；这是女装多色切片最容易被用户一眼看出的错误 | `build_plan` 建引导语正则 `(再看\|另外\|这是\|换成\|试一下).{0,3}色`；命中后给该 block 打 `color_lead`，选段时强制"承接 block 与引导 block 同 `source_file` 且 `abs(gap) <= 0.25s`"；`qc_auditor` 校验 `plan.clips` 中该 pair 的同源连续性 | 构造"说奶咖→接奶白"反例必被拦；跨源成片（7 条历史）不受影响，零误伤 | 3（依赖视觉颜色标注） | 中高。颜色标注准确率是瓶颈；建议先只对"显式颜色词"的段生效，`这个色` 不触发 |
| **X2** | **情绪瞬间（音频 RMS 峰值）** | 真实情绪是高完播最强信号，当前选段**完全是文本驱动**，音频信息未参与 | `build_plan` 选段前对候选 block 跑 `ffmpeg astats`（或直接读 `pcm` 算 RMS），写 `block.audio_peak`；`_segment_score` 对 hook/fit 加 +2.0（仅打平局时生效） | 影子模式跑 5 条成片，记录"hook 段是否变为高 RMS 段"，不改变任何 QC 结果 | 2 | 中。"喊麦式"高音量段可能被选中；黑名单兜底不够（黑名单是文本维度） |
| **X3** | **动态优先（帧差运动能量）** | 走动/转身/拉扯 > 坐着纯讲；当前零画面信息参与选段 | 抽帧 `cv2.absdiff` 均值写 `block.motion_score`；`_segment_score` +1.0，仅用于同分打平 | 5 条成片"静态讲解段占比"下降；面料细节特写（干货段）不被误伤 | 2 | 中。镜头运动（推镜）会被误判为"动态"，需先扣除全局位移 |
| **X4** | **多色遮挡优先** | 单独讲某色时被外套盖住 = 看不清商品本体 | 复用 `detect_garment_match` 的躯干区域 + 衣着一致性分数，低于阈值的 block 降权 20% | 有外套遮挡的素材中，纯色讲解段优先选到无遮挡片段 | 2（依赖 X1 的颜色标注） | 中 |
| **X5** | **输出到素材目录 / 目录名预填 spec** | 省一次手动拷贝与手打；但属于方便性收益，不是质量收益 | `scripts/generate_from_job_spec.py` 末尾加 `--copy-to-source`；`desktop_spec_creator.py` 加"从文件夹名正则抽 `【…】`/款号预填" | 开关关闭时零副作用（逐字节比对产物路径列表） | 1 | 低。会污染素材目录，默认必须关 |

### 1.4 剔除项（明确不做，附理由）

| 编号 | 剔除项 | 理由 |
| :--- | :--- | :--- |
| **R1** | **助播说话人分离（pyannote diarization）** | 素材以主播为主（文档自己也承认"刚好主播缺失这一两小句"）；diarization 在单麦直播混响下的错误率会同时造成"误删主播"与"漏删助播"两类错误，两种都比现状差。投入 2 周+，收益不可量化 |
| **R2** | **用 LLM 生成/改写卖点话术**（含"卖点翻译成用户收益""提前解决痛点""颜色适合人群的表达"） | 直接违反硬原则①「只用真实素材原声，绝不 TTS、不生成/改写口播」。这不是成本问题，是原则问题——一旦允许改写，口型与语音的物理咬合保证全部失效 |
| **R3** | **把 34 项自检全做成门禁** | 其中 **13 项是主观判断**（A 已分层）。硬做会造成大量误报 → 阈值被反复放宽 → 门禁整体失效。仓库已有先例：违规词"全库零残留"就是因为误报太多被推翻，改成"只删字幕"。主观项走 I10 审核包 + 人工抽样登记 |
| **R4** | **换色段"不得跨源拼接"（全局）** | 多源跨素材是核心能力（`plan.clips[i].source_file` + `render_clips` 按源取流）。全局禁跨源 = 退化为单素材剪辑，等于砍掉"5 段素材合成一条"的既有价值。正确做法见裁决⑥与 X1：**只锁"引导段↔承接段"这一对** |
| **R5** | **输出文本剪辑指令/时间线（替代结构化产物）** | `plan.json` / `timeline.json` / `dialogue_script.json` / `asr_subtitle_review.json` 已经是**机器可读的决策链超集**，退回文本是降低可执行性 |
| **R6** | **`afftdn` 轻降噪** | ① 现有 `loudnorm -14 LUFS + alimiter` 已统一响度；② `afftdn` 会伤齿音/气声，而"齿音是否恶化"**无法量化验收**，只能主观听测——与"可再生验证"原则冲突；③ 无任何实测证明直播间底噪是完播的瓶颈 |
| **R7** | **变速的"音高补偿"** | `atempo` 是时域拉伸，**本身不改音高**；音高补偿只在 `asetrate`（改采样率、必然变调）场景才需要。C 的表述是概念误用，不实现 |

### 1.5 【纠正】（与 A~E 或给定事实冲突的实测结论）

- **【纠正 1】git 事实 7 已过期，批次划分的前提一变。** 实测：当前分支是 `master`，`6bb0fc1 Merge PR #9`、`46864ff Merge PR #10` 均已在 master；`git branch` 无本地特性分支；`UNIT_TESTS` 已列 **17** 个测试文件，因此基线是 **17/17**，不是"#10 的 15/15 / #9 的 16/16"。E 的 `16/16` 与"必须保证 16/16"也是过期数字。**影响**：E 用来反对 5 批的"合并开销"论据有一半失效（不再有长活分支）；但"风险隔离"论据仍成立，故裁决为"4 批 + 批内串行小 PR"。
- **【纠正 2】"从 `plan.subtitles.keywords` 取商品关键词"不成立。** `plan.subtitles.keywords` 直接来自 `options.highlight_keywords`（`build_plan.py:834`），而 `job_spec.json` 的 `highlight_keywords` 为 `[]`，实测 `10-29-毛衣/plan.json` 里就是 `{"keywords": []}`。**任何依赖它的断言（A 的 P0-4 主题款、B 的 1.3 贵材质）都会永久 SKIP 或误报**。正确口径：由 `build_plan` 把 `_extract_product_context()` 的结果写进 `selection_meta.product_ctx`，QC 经 I0 的 `plan_path` 读取。
- **【纠正 3】福利词收窄：E 有实质错误，D 的清单也不可直接用。** ① E 的 M6 写"排除福利款、**福利价**"——**福利价必须仍被拦截**（实测「这件是福利价九十九」命中价格表达，且"福利价"是最典型的诱导价话术）；② D 的 `福利价|福利秒杀|福利链接|福利到` 四模式会让现有回归用例 **「你们今天福利来啦」从"必拦"变成"放行"**（`tests/test_blacklist_precision.py:68` 实测 old=True→new=False），并且漏掉"发个福利/抢福利/福利名额"；③ "福利秒杀"本来就已被 `.*秒杀.*` 兜住，"福利到"是生造词。**正确方案**：白名单剥离 `福利(款|感)` 后仍跑原 `.*福利.*`，零回归风险。
- **【纠正 4】纯画面需要处理的是 7 条断言，不是 6 条，且最容易炸的那条没人提。** `qc_report.json` 第 3 条是「字幕文件存在性」，`level=BLOCK`。ASS 不生成时它**不是 SKIP 而是直接 BLOCK 失败**，任何"只旁路渲染、QC 加 profile 感知"的方案都会在第一条硬门禁上炸掉。A/B/D/E 的 6 类清单全部漏了它。
- **【纠正 5】`fabric.max` 单独改不足以控住占比。** `build_plan.py` 的 `_top_up()`（:660 起）从剩余块补齐时长时只按 `styling+fabric` 得分排序，**不校验类目配额**，会把 fabric 块重新拉进成片。只改 `constraints.yaml:276` 会在"素材总时长偏短、需要补齐"的场景下随机破线。必须同时给 `_top_up` 加类目上限过滤。

---

## 第二部分：需要仓库主人（用户本人）确认的细节清单

> 每项给"可选值 / 背景 / 影响 / 我的建议"。**建议默认值可直接采纳，即视为确认。**

| 编号 | 待确认问题（含可选值与建议默认） | 背景（为什么会出现这个选择） | 影响（选错的后果） | 我的建议 |
| :--- | :--- | :--- | :--- | :--- |
| **1** | **时长区间是否支持 40–44s？**<br>可选：(a) 维持 `[45,65]`（默认）(b) 放开到 `[40,65]` (c) 收窄到 `[48,55]` | 提示词要 40–70s、优先 45–60；仓库 `constraints.yaml:262` 是 `[45,65]`，spec 只能在此区间内收窄。A/B 把它列为**唯一没人处理的硬冲突**（E 完全未提） | 选 (b)：fabric 25% 线 ⇒ `fabric.max` 必须 ≤10，与 `min:8` 挤成 8–10s 窗口，**参数类内容这个类目基本废掉**；且 I2 的断言口径要重写、7 条历史成片全部重跑重定基线。选 (c)：成片更紧凑，但短于 45s 的素材无法交付（当前不是问题）。选 (a)：零返工 | **选 (a)，不支持 40–44s**。理由是可算的连锁代价，不是保守。若确有 40s 档需求，用 spec 的 `options.duration_range` 收窄到 `48-55` 满足主流诉求即可，不需要动下限 |
| **2** | **纯画面档位的 QC 覆盖率下降如何对外披露？**<br>可选：(a) 逐条 SKIP（每条断言单独标 SKIP + 原因）(b) 报告顶部统一声明 (c) 两者都要（默认建议 c 的轻量版） | 7 条字幕类断言在 plain 下不适用。A 的核心诉求是"SKIP ≠ PASS，不能假绿"；C/B 只想到"标记 SKIP 避免阻塞"，没提"让报告使用者知道哪些没检" | 选 (a)：报告最真实，但要给 7 条断言各写 SKIP 原因，且 `qc_report.json` 结构变化会影响下游消费（当前 `checks[].status` 只有 PASS/FAIL/WARN）。选 (b)：改动最小，但单条断言看不出是"通过"还是"没测"，容易假绿。选 (c)：最完整，但工作量翻倍（2–3 → 4–5 人日） | **选 (c) 的轻量版**：`checks[].status` 增加 `SKIP` + `skip_reason` 字段（逐条），同时在 `summary.disclaimers` 写一条总声明 + `coverage: {total:34, executed:27, ratio:0.79}`。不做"SKIP 原因枚举体系"（那是 4–5 人日版本） |
| **3** | **语速档位取值与首批验证场景？**<br>可选：(a) 1.0/1.15/1.2（默认建议）(b) 只有 1.15 (c) 1.0/1.1/1.2。验证场景：(i) 先在 plain 验证（默认建议）(ii) 直接在有字幕档验证 | 文档要"1.1–1.2，优先接近 1.2"；C 主张 1.1–1.15（更自然），A/B 主张到 1.2。E 建议"先在 plain 验证，此时无字幕时间戳负担" | 选 (b)：少一个档位，遇到"素材本身语速就快"无退路。选 (ii)：一次改动同时动"时间戳换算"与"字幕烧录"两个变量，出问题无法二分定位。**额外风险：`atempo` 全库 0 处，属从零新增** | **选 (a) + (i)**：三档、默认 1.0 不启用；1.15 标"推荐"、1.2 标"极限"。验证顺序：plain（无字幕，只看音画同步）→ branded（校 ASS 时间轴）。注意 (i) 依赖 Q1 先落地 |
| **4** | **`fabric.max` 取 11 还是 12？**<br>可选：(a) 11 + 断言 `≤0.25` 无容差（默认建议）(b) 12 + 断言 `min(0.25, 12/total)` (c) 12 + 断言 `≤0.25`（直接破线） | 文档"参数类占 4~5 分之一"；`fabric.min:8` 是下限。A/B/C 都提"12 或 14"，但**都没算短时长边界** | 选 (b)：断言变成随总时长漂移的函数，45s 时容许 26.7%，门禁失去"占比上限"的语义，以后所有占比类断言都得这么写，标准先例就坏了。选 (c)：45s 成片**必然**断言失败，等于交付不出去。选 (a)：45s 上余量仅 0.6pp，但选段是整秒级，可接受 | **选 (a)，11**。11 与 12 的业务差只有 1s，不值得为它污染断言体系。补一条纪律：**占比类断言一律用固定比值阈值，不写含变量的阈值** |
| **5** | **福利词放行边界：哪些"福利X"必须仍拦？**<br>可选：(a) 白名单剥离 `福利(款\|感)`，其余全拦（默认建议）(b) 只放行 `福利款` 一个词 (c) 全放行（除金额） | 实测 `.*福利.*` 会整句拦「这个是福利款，价格特别优惠」；但"你们今天福利来啦"是现有回归的必拦项，业务上属促销紧迫话术而非卖点 | 选 (b)：`很福利`/`福利感`/`福利款` 之外，达人真实表达"这个真的很福利"仍被删，覆盖不全。选 (c)：`福利价九十九`/`抢福利`/`福利名额` 全部放行 → 交易与诱导话术入侵成片，违反"删交易叫卖"的既有原则。选 (a)：只放行"福利款/福利感"两类名词性表达，动词性/价格性/名额性全保留拦截 | **选 (a)**。必须仍拦的清单：`福利价`、`福利链接`、`福利来`、`发个福利`、`抢福利`、`福利名额`、`福利放送`、`福利秒杀`（已被 `秒杀` 兜住）。可放行：`福利款`、`福利感`、`很福利` |
| **6** | **多色配额是否只按"达人身穿"计入？**<br>可选：(a) 严格：人体 bbox 内主色（默认建议，但把 I11 推到 3 人日）(b) 放宽：ASR 颜色词口径 + `unlabeled` 披露（2 人日）(c) 两者混用 | 提示词明确"只按达人身着计入，手持/助播拿着不算"。但仓库**无颜色概念**，阶段一原计划走 ASR 文本侧（零模型）。严格口径必须引入视觉主色统计 | 选 (b)：达人拿在手上的颜色也会被计入，占比统计与业务定义不符，但成本最低、可当周上线。选 (a)：暖光直播间偏色严重（实测素材暖黄光），颜色判定误差可能 >20%，比"口径不严"更糟。选 (c)：两套数字对不上，报告难解释 | **选 (b)，但把 `unlabeled` 比例设为强制披露项**：`unlabeled > 30%` 时该断言记 SKIP（不是 FAIL）。理由：先有可信的口径一致的初版，再谈严格；视觉口径留给 X1 阶段二一起做 |
| **7** | **17% 构图是否允许"以轻微清晰度损失换放大"？**<br>可选：(a) 不允许，跌破 200 就放弃放大（默认建议）(b) 允许，为放大把阈值降到 150 (c) 分档：S≤1.15 保 200，S>1.15 保 150 | 视频端目前**零构图约束**，但 `cover.sharpness_min: 200` 是 C1 的 BLOCK 门禁。二次放大必然掉清晰度，与门禁天然冲突 | 选 (b)：把 BLOCK 门禁从 200 降到 150，等于**全局削弱 C1**（封面也受影响，7 条历史成片可能重新判定）。选 (c)：多一档逻辑，但正好对应"轻微 vs 明显"两个档，且不污染封面口径。选 (a)：最保守，代价是远机位素材**永远放不大**（因为放大后必破 200） | **选 (a) 作默认，但实现上按 clip 回退**：`framing_scale` 计算后先试放大，QC 复测不达 200 则该 clip 回退 `1.0` 并在 report 记 WARN"该分镜因清晰度放弃构图"。**不建议动 200 这个全局阈值**——它是封面的 C1 门禁，为了单个分镜去改全局门禁是典型的"为局部放宽标准" |
| **8** | **主观项（13 项）的抽样登记责任归属与频率？**<br>可选：(a) 每条成片都登记（默认建议：交付前 100% 登记）(b) 每 5 条抽 1 条 (c) 不登记，只出 contact sheet | A 的 34 项里 13 项机器判不了（情绪/话术-画面匹配/搭配完整等）。B 建议 `manual_review.md` 模板；A 建议"连续 5 条完整登记"。但**谁填**没说——这是流程能否持续的真正瓶颈 | 选 (b)：样本太少，统计不出"哪项长期不达标"。选 (c)：等于放弃这 13 项，回到"人看 60s 全片"。选 (a)：每条登记 13 项约 3–5 分钟，若交付频率是每天 1–3 条则每天 5–15 分钟，**可持续但有摩擦** | **选 (a) 但只填"不通过项"**：`output/<pid>/manual_review.md` 默认全部预填"未发现问题"，审核人只勾选/备注异常项（正常情况 30 秒内完成）。责任人：**成片交付人（不是使用者）**；频率：**每条成片交付前**。I10 的 contact sheet 与它一起交付 |
| **9** | **死键审计是否升级为 CI 阻塞门禁（会拦 PR）？**<br>可选：(a) 阻塞，但用"基线 diff"口径（默认建议）(b) 阻塞，全量死键即失败 (c) 不阻塞，仅报告 | 现有 **10 个**自标注死键（7 ⚠️未接线 + 3 ❌未实现）；`audit_requirements --only-dead` 有能力但不在 CI。本轮还要新增 `render_profile`/`time_stretch`/`funnel.fabric` 等键 | 选 (b)：**第一次接入就全红**，10 个存量死键无人清理，结果只能是被加进白名单或直接关掉 CI 步骤——反而破坏纪律。选 (c)：等于没有，历史上贴图四键被错放在 `split_effect` 段就是这么发生的。选 (a)：只对"新增死键"失败，存量进基线，**可以立刻接入且永远有效** | **选 (a)**。落地：`contracts/dead_keys_baseline.json` 登记现有 10 键（每条写"已知/待实现"）；PR 模板加"新增键已在代码消费点 grep 到"勾选项；存量死键每季度清一次，只减不增 |

### 2.1 阻塞项判定（不确认就无法开工）

按"批次"划分，而非笼统列一串：

- **阻塞第 1 批开工：#4（fabric 取值）、#5（福利放行边界）**
  第 1 批的第一项（I1）依赖 #5 的放行清单，第二项（I2）依赖 #4 的取值。这两项不确认，写完就要返工重跑基线。
- **阻塞第 2 批开工：#2（plain 覆盖率披露口径）、#6（多色统计口径）、#9（死键审计是否阻塞）**
  #2 决定 `qc_report.json` 是否要新增 `SKIP`/`coverage` 字段——**这是全局报告结构改动，先做后改的返工成本最高**；#6 决定 I11 是 2 人日还是 3 人日、要不要引入 Pose；#9 决定 I8 是"基线 diff 失败"还是"仅报告"，脚本参数与验收口径完全不同。
- **不阻塞开工、但要尽早定的：#1（时长下限）、#3（语速档位）、#7（清晰度取舍）、#8（抽样登记）**
  第 1、2 批不碰这四项；#3 最早在第 3 批才需要，但**#3 与 #2 有依赖顺序**（语速先在 plain 验证 ⇒ Q1 必须先于 Q2）。

---

## 附：本次裁决用到的实测命令与关键输出

```
# 1. git 真实状态（纠正 E5/E6）
git branch -a ; git log --oneline -5
→ * master ; 46864ff Merge PR #10 ; b3c6e75 ... ; 6bb0fc1 Merge PR #9
→ scripts/run_regression.py 的 UNIT_TESTS 共 17 项

# 2. fabric 占比（E1）
for plan in data/work_dirs/*/plan.json: selection_meta.funnel
→ 10-29-毛衣 19.87/59.19=33.6% ; down_jacket 19.40/70.84=27.4% ; grid_coat 19.64/61.64=31.9%
   hoodie 18.88/71.66=26.3% ; silk_knit 20.00/70.78=28.3% ; silk_7270 19.56/58.78=33.3%
   workwear 19.60/69.96=28.0%  → 7/7 > 25%

# 3. 福利黑名单（事实 1 复核 + 纠正 3）
'这个是福利款，价格特别优惠' → 命中 (.*福利.*)
'性价比超高的一件' / '买了超级划算' → 放行
'这件是福利价九十九' → 命中（价格表达）
收窄为 福利价|福利秒杀|福利链接|福利到 后：
'你们今天福利来啦' → old=True, new=False   ← 打破 tests/test_blacklist_precision.py:68

# 4. QC 报告（E3/E4）
output/10-29-毛衣/qc_report.json → 34 checks；第 3 条「字幕文件存在性」level=BLOCK
audit_final_deliverables() 签名无 spec、无 plan

# 5. plan 取词口径（纠正 E2）
data/work_dirs/10-29-毛衣/plan.json → subtitles.keywords = [] ；selection_meta 无 product_ctx

# 6. 变速能力（E9）
grep -rn "atempo|asetrate" --include=*.py src/ → 0 处

# 7. 死键（E7）
grep -n "未接线|未实现" contracts/constraints.yaml | wc -l → 10（7 ⚠️ + 3 ❌）
