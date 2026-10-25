# 直播素材剪辑成片任务 — 验收报告

> 执行日期：2026-10-21 · 解释器：`C:/Users/ePanda/.workbuddy/binaries/python/envs/liveclip/Scripts/python.exe`
> 阈值唯一来源：`contracts/constraints.yaml` · 回归结论：**CI 全绿**

## 一、总体结论

**5 条素材 / 11 段源视频 → 5 条成片，QC BLOCK 级 100% 通过。**

| 素材 | 成片时长 | 分镜数 | 跨源选段 | 使用源文件 | 字幕行 | ASR 句块 | QC | WARN |
|---|---|---|---|---|---|---|---|---|
| silk_knitwear_7270 | 62.68s | 4 | 单源 | 0425 | 41 | 123 | PASS | 1 |
| hoodie_11214 | 64.29s | 4 | 单源 | 0518 | 18 | 162 | PASS | 1 |
| down_jacket_11241 | 63.62s | 4 | **跨源** | 0240 + 0353 | 48 | 213 | PASS | 1 |
| workwear_11186 | 64.59s | 4 | **跨源** | 0101 + 0326 | 27 | 219 | PASS | 1 |
| grid_coat_11231 | 62.06s | 4 | **跨源** | 0123 + 0303 | 31 | 276 | PASS | 1 |

全部成片 1080×1920 / 60fps / H.264+AAC，时长落在 45–65s 区间内。

唯一 WARN 项均为**封面人像宽占比**（0.37–0.56，未达0.75 目标）——这是 `framing_policy: example`
示例版式的预期结果：为底部商品名区预留空间并保全完整人体，纪要语义为"尽力居中"，不拦截交付。

## 二、五项需求验收

### 需求 1 · 缺商品图不插图 ✅
4 条新素材均无商品图。渲染日志输出
`[COVER] 商品图为空：跳过贴图渲染，封面仅保留直播画面 + 品牌区 + 商品名`，
meta 中无 `sticker_*` 元素，QC 断言显示 `[PASS] 封面贴图几何: 无贴图`。
无补图、无占位框。

### 需求 2 · 45–65s ✅
全部 spec `duration_range` = `45-65`，实测 62.06–64.59s 全部合规。
新增 `DURATION_SAFETY_MARGIN_SEC = 0.5`：ffmpeg 逐段切片的物理时长会略大于计划值
（实测 4 段合计 +0.09s），若计划恰好卡65.0s 会溢出成65.09s 被 QC 拦截，故上限收紧 0.5s。

### 需求 3 · 允许丢段，只留商品内容 ✅
三层过滤：
1. **黑名单**：价格 / 库存促销 / 换款离场（正则，纯内容信号无时间窗）
2. **ASR 幻觉与元话语**（本轮新增 `_is_idle_chatter`）：如"我怎么跑到姜桑达马吉拉奥说错了"
   这类脱离商品语境的句子直接淘汰
3. **补齐阶段语义门槛**（本轮修复的真实缺陷）：`_top_up` 时长补齐曾把
   "他们坐船跟我们坐船有什么区别"这类无任何商品词的块拉进成片，现只接纳 `categories` 非空的块

### 需求 4 · 本地 ASR + 本地 agent 校对 ✅
```
Whisper(small, zh, VAD, condition_on_previous_text=False)
  → 语境注入 prompt（商品/品牌/卖点词）
  → 本体引擎：22 条同音错词表 + 商品词根动态提权 + 滑动窗口消歧
  → _polish_for_subtitle ★新增：叠词去重 / 冗余标点清理 / 超长句(>22字)按可读性切分（时间戳守恒）
  → LLM 复核（未配 Key 时无缝降级本地）
  → asr_verification_report.json 全量 diff 审计
```
5 条素材共 993 个 ASR 句块全部经本地引擎校对；字幕零价格断言与禁用错词断言均 PASS。

**本轮修复的严重缺陷**：本地引擎重建 verified 段时未透传 `source_file`，
会直接导致跨素材选段失效。

### 需求 5 · 跨素材选段 ✅（本轮核心新增能力）
| 素材 | 源文件数 | 实际跨源 |
|---|---|---|
| down_jacket_11241 | 2 | ✅ 0240 + 0353 |
| grid_coat_11231 | 3 | ✅ 0123 + 0303 |
| workwear_11186 | 3 | ✅ 0101 + 0326 |
| hoodie_11214 | 2 | 单源（0044 较短，内容被 0518 全覆盖） |
| silk_knitwear_7270 | 1 | 单文件 |

workwear 的分镜来源（真实跨源拼接示例）：
```
c01_hook      8.00s  ← 0326
c02_fabric   20.00s  ← 0101
c03_fit      13.36s  ← 0101
c04_styling  23.64s  ← 0326
```

**关键约束**：不同视频时间轴互不相通，因此 ASR 逐源隔离缓存、`build_blocks` 按 source 分组
绝不跨文件合并（含3 处修剪路径全部继承 source）、切片按 `clip.source_file` 选源、
字幕映射按源过滤、缓存键纳入全部源文件内容（key 升 v3）。

## 三、本轮修复的 6 个真实缺陷

1. **基线锁定层形同虚设** — `run_regression.py` 用 `baselines.get(pid)` 读 `{"materials":{...}}` 结构，
   永远取不到 → 从未真正做过区间比对。修复后 5/5 PASS 才首次生效。
2. **本地校对引擎丢失 `source_file`** — 跨素材选段会失效。
3. **封面跨源排序键错误** — 用了不存在的 `score` 字段（正确是 `frontal_score`），选中糊脸候选。
4. **清晰度硬门禁未前置** — 评分对"宽占比达标"加权过高，让 `sharpness<200` 的候选排第一
   （workwear 选中125清晰度的糊帧）。改为排序前过滤。
5. **时长补齐引入无商品语义闲聊块**（见需求 3）。
6. **计划时长卡上限导致成片溢出**（见需求 2）。

另修复一处本轮引入的回归：`_collect_source_videos` 对占位路径直接抛错，
破坏了 `AgentDirector` 既有的初始化容错契约（单元测试用假路径构造 Director）。

## 四、交付物

### 成片（output/&lt;project_id&gt;/）
- `&lt;pid&gt;_final.mp4` — 1080×1920 / 60fps / 46.6–56.3 MB
- `&lt;pid&gt;_cover.jpg` — 1080×1920 封面（品牌区固定顶部 y=125，商品名区 y=1536–1770）
- `qc_report.json` / `agent_decision_manifest.json` / `asr_verification_report.json`

### 过程产物
- `data/work_dirs/&lt;pid&gt;/plan.json` — 分镜计划（含每镜 source_file）
- `data/work_dirs/&lt;pid&gt;/asr/&lt;源名&gt;.{whisper,verified}.json` — 逐源 ASR 与校对结果
- `data/candidates/&lt;pid&gt;/cover_candidates/` — 逐源候选 + 汇总池
- `logs/final_acceptance.json` — 本报告数据源

## 五、尚存限制

1. **商品图全缺** — 4 条新素材无商品图，贴图卡（C11–C14）与衣着一致性 CLIP 检测
   至今未在真实数据上验证过。
2. **单源素材** — hoodie 与 demo 目录只有 1 个有效源（或较短），跨源能力未在这些素材上体现。
3. **封面宽占比 WARN** — 5/5 条均为 WARN，若要达到 0.75 需切到 `width_priority` 策略，
   但会牺牲底部名称区（名称覆盖在人像上）。
4. **LLM 复核未启用** — 未配置 API Key，当前全走本地引擎。
5. **ASR 幻觉** — 快语速直播仍有零星幻觉音节，已加元话语过滤但无法根除。

## 六、复现命令

```bash
PY=C:/Users/ePanda/.workbuddy/binaries/python/envs/liveclip/Scripts/python.exe

# 单条素材（含全部 5 条要求）
$PY src/main_pipeline.py --spec specs/job_spec_workwear_11186.json

# 批量
$PY src/batch_runner.py --specs-dir specs --force

# 三层回归（单测 → 端到端 → 指标基线锁定）
$PY scripts/run_regression.py
```