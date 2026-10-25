# 抖音直播剪辑能力成熟度评估报告

> 评估日期：2026-10-24
> 评估对象：`Xepanda/dy-video` @ `feature/asr-review-gate-multi-source` (bf4e362~c2c6c56)
> 评估方法：**逐环节读代码 + 查实际产物 + 注入配置验证 + 端到端实跑**（不含推测）
> 需求基线：`docs/editing_task_plan_2026-10-21.md`、`skills/live-ecommerce-video-editor/SKILL.md`、`contracts/constraints.yaml`

---

## 0. 总体成熟度结论

| 维度 | 评级 | 依据 |
|:--|:--|:--|
| **主干闭环**（素材→转写→选段→成片→QC） | 🟢 **生产可用** | 6 字段最小 spec 实跑 5m45s 产出成片+封面，QC 20 项断言全绿；5 条真实素材 5/5 通过 |
| **短视频工业化包装**（镜像/分屏/水印/贴纸） | 🔴 **未达标** | `水印` 全仓 0 命中；`effect` 被硬编码 `"none"`，镜像/分屏是死代码 |
| **可移植性 / 可维护性** | 🟡 **刚达标** | 本轮刚修完路径/缓存/依赖三类问题；仍缺 CI 端到端把关 |
| **需求一致性（文档 vs 实现）** | 🔴 **失同步** | 需求文档 3 处与实现相反（详见 §3） |

**一句话结论**：这是一条**"能稳定出片"的流水线**，但还不是 SKILL.md 描述的**"工业化封包"流水线**——
两者差距集中在视觉包装层（镜像/分屏/水印）与需求文档同步上，不在识别与选段主链路上。

---

## 1. 逐环节成熟度矩阵

### 环节 1：素材获取 / 解析

| 项 | 状态 | 依据 |
|:--|:--|:--|
| 素材**抓取/下载**（抖音/网络） | ❌ **未实现** | 全仓无 `yt-dlp`/`requests`/下载器；`urllib` 仅用于 LLM API（`asr_agent_verifier.py:35`）。素材须用户手动放置 |
| 素材**发现**（同目录多视频） | ✅ 已实现 | `agent_director.py:_collect_source_videos()` 扫描 `raw_video_path` 同目录 `.mp4/.mov/.mkv/.avi/.flv/.m4v/.ts`，按文件名稳定排序 |
| **跨机器路径**兼容 | ✅ 已实现（本轮修） | `src/core/path_resolver.py`，旧机器绝对路径按尾部片段在仓库内重定位 |
| 镜头边界检测 | ✅ 已实现 | `detect_scenes.py` 自适应阈值 `max(base, mean+k·std)`，产出 `scene_cuts` 供选段用 |
| 封面候选提取 | ⚠️ **部分实现** | `extract_cover_candidates.py:280` 扫描上限 180s；仅当候选<2 时才自适应扩扫（`:443`），上限 600s。**首窗已有 ≥2 候选时，180s 之后的画面永不参与竞争** |
| 交互式素材录入 | ✅ 已实现 | `spec_creator.py` 本地表单（浏览器） |

> **缺口**：素材获取完全是人工前置；封面候选池存在时间窗盲区。

---

### 环节 2：语音转写（ASR）

| 项 | 状态 | 依据 |
|:--|:--|:--|
| 本地 ASR 引擎 | ✅ 已实现 | `asr_engine.py` Faster-Whisper(CTranslate2)，CPU int8 / CUDA float16 |
| **torch 可选化** | ✅ 已实现（本轮修） | `_cuda_available()`：torch 缺失降级 CPU 而非 ImportError（faster-whisper 运行时不需要 torch） |
| **prompt 回声污染** | ✅ 已修复（本轮） | 原注入 `initial_prompt` 会挤掉开头 ~30s 真实语音（8/11 素材首段是回声）。现默认不注入，回声 0/11 |
| 领域词纠错（本地本体引擎） | ✅ 已实现 | `asr_agent_verifier.py`：22 条静态同音词表 + 拼音指纹 + 滑动窗口消歧 + 商品词动态提权 |
| 叠词/冗余清理 + 超长句切分 | ✅ 已实现 | `_polish_for_subtitle`：`SUBTITLE_MAX_CHARS=22`，按字数比例分配时长（时间戳守恒） |
| LLM 复核 | ✅ 已实现 | OpenAI 兼容端点，护栏：严格 JSON / 时间戳绝对守恒 / 零价格硬约束；**无 API Key 时无缝降级本地引擎** |
| **执行者审核门禁** | ✅ 已实现（本轮） | `asr_review_gate.py` 10 校验点 A1~A10（BLOCK/WARN 分级），模块导入即挂载（机制性保证） |
| A10 提示词回声检测 | ✅ 已实现（本轮） | 11 份真实素材：4/4 真回声全检出、7 个正常首段零误报 |
| 备选引擎（SenseVoice / Paraformer） | ⚠️ **未接入** | 模型已下载（`~/.cache/modelscope`）、`funasr` 已装，但**代码零调用**。实测见 `docs/asr_benchmark_2026-10-24.md` |

> **实测结论**（90s 直播音频，参考=修复后无 prompt 配置）：
> SenseVoiceSmall **无任何时间戳**（顶层只有 key/text）→ 接不了；
> Paraformer-large 用组合模型才有**字级时间戳**（可接入但需适配层）；
> faster-whisper(base) 快 2.5×但产 **5.7% 繁体字** → 排除。
> **维持 faster-whisper(small) 是正确选择。**

---

### 环节 3：字幕与切分

| 项 | 状态 | 依据 |
|:--|:--|:--|
| ASS 字幕生成 | ✅ 已实现 | `ass_subtitles.py` |
| 安全区（避小黄车） | ✅ 已实现 | `MarginV=240`（约束要求 ≥220）；实测 QC「字幕避让小黄车盲区」PASS |
| 居中对齐 | ✅ 已实现 | `Alignment=2` |
| 关键词发光高亮 | ✅ 已实现 | `{\c&H0000E6FF&}` 亮黄覆盖 |
| 字幕零价格（决策 3） | ✅ 已实现 | `detect_price.py` + QC BLOCK 断言 |
| 字幕与口型咬合 | ✅ 已实现 | `video_assembler.py:253` 按 `source_file` 精确映射同源 segments，`rel_st/rel_end` 相对化 |
| 跨源字幕错位防护 | ✅ 已实现 | 多源仅取同源 segments（`if s_src != src: continue`），实测 5 素材 0 越界 |
| 字幕层独立编辑（OpenChatCut） | ⚠️ **未接入生产** | `chatcut_bridge.py` 完整实现且 7 单测全绿，但**仅在 tests 中被调用**，`main_pipeline`/`agent_director` 零引用 |

> **缺口**：可编辑时间线（人工精修）能力存在但没有入口——用户无法从成片反推修改。

---

### 环节 4：剪辑决策与时间轴

| 项 | 状态 | 依据 |
|:--|:--|:--|
| 漏斗配额选段 | ✅ 已实现 | `build_plan.py:421` `order=["hook","fabric","fit","styling"]`，配额内取最高分 |
| 三层过滤（黑名单/闲聊/语义门槛） | ✅ 已实现 | 黑名单 + `_is_idle_chatter` + `_top_up` 只接纳 `categories` 非空块 |
| 约束网关（硬门禁） | ✅ 已实现 | `decision_gateway.py`：封面/计划均过网关，违规抛 `DecisionViolation` 阻断 |
| 多源不合并 + 来源继承 | ✅ 已实现 | `build_blocks` 按 `source_file` 分组；3 处 block 构造继承来源；`chatcut_bridge` 回注 |
| 时长收敛 45–65s | ✅ 已实现 | 贪心 + 补齐 + 上限修剪 + 0.5s 切片安全余量；实测 5 素材 58.9~64.6s |
| **黄金 3 秒"重排"** | ⚠️ **部分实现** | `build_plan.py:446` 按 `HOOK_KEYWORDS`（37 词）打分取最高分块置首。**是"关键词打分 + 类别优先"，不是 SKILL 要求的"全片扫描挑最极端一句"** |
| 细节推镜 Detail Punch | ✅ 已实现 | `build_plan.py:546` `zoom: 1.3 if has_fabric_kw else 1.0`，锚点 `y_anchor=0.35`（符合 SKILL 1.25~1.35x / y=0.35h~0.65h） |
| **镜像翻转 hflip** | 🔴 **死代码** | `video_assembler.py:71` 支持 `effect=="hflip"`，但 `build_plan.py:548` **硬编码 `"effect": "none"`** → 5 份真实 plan 全部 `effect={'none': 4}`，从未触发 |
| **分屏对比 split** | 🔴 **死代码** | `video_assembler.py:188` 有完整 hstack 实现，但同样因 `effect="none"` 从未触发；且 `split_pair_st` 无人生成 |
| 分屏金色分割线 + 双侧标签 | ❌ **未实现** | split 路径只有 `hstack`，无 6~10px 发光分割线、无左右胶囊标签 |
| 镜像防反文字贴纸 | ❌ **未实现** | hflip 滤镜链仅 `hflip,crop,...`，**无任何遮挡 overlay**，未满足 SKILL 2.1 核心要求 |
| 决策可干预 | ✅ 已实现 | `--cover-id` / `--plan` 外部干预模式（禁缓存保证重算） |

---

### 环节 5：字幕 / 水印 / 包装叠加

| 项 | 状态 | 依据 |
|:--|:--|:--|
| ASS 字幕烧录 | ✅ 已实现 | 相对路径规避 Windows 冒号转义（`cwd=temp_dir` + `subtitles=douyin_subtitles.ass`） |
| Hook 卡（0~2.5s） | ✅ **已全局统一 720×168** | 已实现：`video_assembler.py` overlay，`enable='between(t,0,2.5)'`，尺寸 **720×168**，位置 `hook_y` 动态避让人脸顶（实测 y=310，占据 310~478 < 人物区 460 ✅）。2026-10-25 拍板：全局统一 720 宽（原 940~980 口径作废），尺寸改由 `hook_card` 约束段驱动 |
| CTA 徽章（末 2.5s） | ✅ 已实现 | `overlay=40:1440`，`enable='gte(t,{total-2.5})'` |
| Sub-Bass Boom | ✅ 已实现 | `sfx_generator.py`：45→90Hz 阻尼正弦，1.2s；`amix` 混音，volume 0.45 |
| 封面品牌区 | ✅ 已实现 | `cover_renderer.py` 品牌标 `top_y=125`（SKILL 120~290） |
| 封面商品名（金色渐变） | ✅ 已实现 | 决策 2：`#FFE9A8→#E8A91E` + 深褐描边 + shear 斜体；QC 色相断言 PASS（99.6% 像素 H∈[35,55]） |
| 封面商品贴图 | ✅ 已实现 | 280×280 正方形 + 圆角 + 边框；QC 几何断言 |
| **水印** | ❌ **未实现** | 全仓 `watermark`/`水印` **0 命中** |
| 阶段式利益点挂角徽标（6 款） | ❌ **未实现** | SKILL 流程图列明，无对应实现 |
| 副标胶囊（★ 生日专场…） | ❌ **未实现** | SKILL 1.2 要求，未见实现 |

---

### 环节 6：成片导出

| 项 | 状态 | 依据 |
|:--|:--|:--|
| 分镜切片 | ✅ 已实现 | `-ss` 前置秒级 Seek + `-t` 后置 + `-avoid_negative_ts make_zero` 物理强锁 |
| 多源拼接 | ✅ 已实现 | 按 `clip.source_file` 逐源切片后 concat |
| 音视频同步 | ⚠️ **部分实现** | 双轨 `setpts=PTS-STARTPTS` + `asetpts` 归零已做，**但 `av_sync_error_max_ms` 无独立量化断言**（constraints_map.md 已如实标 ⚠️） |
| 响度归一 | ✅ 已实现 | 两遍 loudnorm(linear) + `alimiter=0.92` 兜底；实测 -14.0 LUFS / TP -1.3 dBTP |
| 编码导出 | ✅ 已实现 | libx264 crf18 + aac 192k 48kHz + yuv420p |
| 竖屏 1080×1920 | ✅ 已实现 | 实测 5 素材全部 1080×1920 |
| 帧率 | ✅ **已全局统一 60fps** | 2026-10-25 拍板统一 60fps（原决策 4 的 30fps 作废）；SKILL.md / 约束 / 装配器 / QC / ChatCut 五处一致 |
| 无黑帧检测 | ✅ 已实现 | blackdetect，QC BLOCK 断言 |
| QC 量化门禁 | ✅ 已实现 | **20 项断言**（BLOCK 19 + WARN 1），实测 5/5 素材全绿 |
| 指标区间锁定 | ✅ 已实现（本轮修） | `fixtures/baselines.json` 9 指标 × 5 素材；注入异常值能被抓到 |

---

## 2. 需求文档与实现的失同步（3 处）

| 位置 | 文档写的 | 代码实际 | 影响 |
|:--|:--|:--|:--|
| `editing_task_plan_2026-10-21.md` 需求 1 | "缺商品图则**不插图，不补图不占位**" | 补占位图（`make_placeholder_cards.py`，`placeholder_images=True`） | **方向相反**。占位是用户后续明确追加的需求（需求优先），但文档未更新 → 后续开发者会按文档误改 |
| 同上 第四节 | "**initial_prompt 注入**商品/品牌/卖点词" | 已**禁用**（默认 `use_initial_prompt=False`） | 文档描述的是已证伪且有害的做法（挤掉 30s 真实语音） |
| 同上 第二节 / `constraints_map.md` | "QcAuditor 量化门禁（**17 项**断言）" | **20 项** | 数量过期 |

---

## 3. 阻塞端到端的问题（按优先级）

> 严格区分：**阻塞"出片"的问题 = 0**（当前 5/5 素材稳定出片）。
> 以下是阻塞"**达到 SKILL 描述的工业化水平**"的问题。

### P0 — 视觉包装层整体缺失（决定成片竞争力）

| # | 问题 | 断点位置 | 证据 |
|:--|:--|:--|:--|
| 1 | **水印完全未实现** | 无任何代码 | `grep -rn "watermark\|水印" src/` → 0 命中 |
| 2 | **镜像翻转是死代码** | `build_plan.py:548` `"effect": "none"` | 装配器支持 `hflip`，决策层从不设置；5 份 plan 全部 `effect={'none':4}` |
| 3 | **分屏对比是死代码** | 同上 + `split_pair_st` 无人生成 | `video_assembler.py:188` hstack 实现完整但进不去 |
| 4 | **镜像无防反文字贴纸** | `video_assembler.py:71-76` | 滤镜链只有 `hflip,crop,...`，无遮挡 overlay → 不满足 SKILL 2.1 核心诉求 |
| 5 | **分屏无金线/双侧标签** | `video_assembler.py:206-213` | 只有 `hstack` |

**共性根因**：`effect` 是**写死常量**而非决策变量。修 #2/#3 是"接线"（改 1 行 + 补决策规则），
但 #4/#5 需要新增渲染能力。

### P1 — 能力有但用户触达不到

| # | 问题 | 断点位置 | 证据 |
|:--|:--|:--|:--|
| 6 | **OpenChatCut 桥接未接生产** | `main_pipeline.py`/`agent_director.py` 零引用 | 仅 `scripts/run_regression.py:43` 引用其测试 |
| 7 | **无成片反推编辑入口** | — | 人工精修后无法回注流水线 |

### P2 — 质量与规范

| # | 问题 | 断点位置 | 证据 |
|:--|:--|:--|:--|
| 8 | 封面候选池 180s 盲区 | `extract_cover_candidates.py:280,443` | 首窗有 ≥2 候选则不再扩扫 |
| 9 | ~~Hook 标题卡尺寸不符~~ → **已统一 720×168** | `video_assembler.py`（720×168） | 2026-10-25 全局统一，与 SKILL.md §1.2 一致 |
| 10 | 无音画同步量化断言 | `qc_auditor.py` 缺该项 | `av_sync_error_max_ms` 键在 yaml 但零消费 |
| 11 | 60FPS vs 30FPS 规格冲突 | `constraints.yaml: fps: 30` | SKILL.md 写 60FPS |
| 12 | 文档失同步 3 处 | 见 §2 | — |

### P3 — 工程基建

| # | 问题 | 证据 |
|:--|:--|:--|
| 13 | **CI 素材层无 runner** | `gh api actions/runners` → `total_count: 0`；该层 `runs-on: [self-hosted, windows]` → PR 上对 5 条素材的端到端**零把关** |
| 14 | 素材抓取无自动化 | 人工前置 |

---

## 4. 补齐建议与最小验证路径

### 4.1 建议顺序（投入产出比排序）

**第一步：把 `effect` 从常量变成决策变量**（改 1 行，解锁 2 个死代码）

```python
# src/tools/build_plan.py:548 附近
EFFECT_RULES = {
    # 站位重复的连续分镜 → 镜像打破单调（但需同步补防反贴纸，见下）
    "mirror":  lambda i, clips, b: i > 0 and b["_quota_cat"] == "styling" and _same_pose_as_prev(clips),
    # 同商品不同色/款式 → 分屏
    "split":   lambda i, clips, b: b.get("has_pair_variant") is True,
}
```

> ⚠️ **上线顺序约束**：镜像必须与"防反文字贴纸"**同批上线**，
> 否则会出现"招牌文字反向"的瑕疵片，比不做更糟。

**第二步：补 SKILL 2.1 / 2.3 的渲染缺口**（防反贴纸 + 金线 + 双侧标签）

- 防反贴纸：复用现成 `generate_cta_badge` 风格的贴纸生成器，在 hflip 分镜上
  `overlay` 到背景文字区域（可用 `detect_ocr` 的检出框定位反向文字位置——**OCR 引擎已有，只差接线**）
- 分屏金线：hstack 后加 `drawbox`/`geq` 画 6~10px 金色竖线 + 两侧 `drawtext` 胶囊标签

**第三步：接 OpenChatCut 桥接到生产**

在 `agent_director` 的 Phase 2 之后插入可选分支：
`--timeline <exported.timeline.json>` → `chatcut_bridge.timeline_to_plan()` → 覆盖 plan → 继续总装。
代码已就绪，只差入口。

**第四步：补 QC 音画同步断言**（消费已在 yaml 的 `av_sync_error_max_ms`）
用 ffmpeg `astats`/`aphasemeter` 或逐帧音画相关检测，写入 QC 第 21 项。

**第五步：把 3 处文档失同步改回来**（需求 1 改为"缺图自动补占位"、删除 initial_prompt 描述、17→20 项）

### 4.2 最小验证路径（端到端跑通所需）

#### A. 最小可跑输入（已实测通过 ✅）

**输入**：1 个视频 + 1 个 6 字段 spec（**无需商品图**）

```json
{
  "project_id": "minimal_probe",
  "raw_video_path": "data/raw_samples/<商品目录>/<源>.mp4",
  "raw_video_paths": ["<同上>"],
  "brand":   {"title_en": "TESTBRAND", "subtitle_cn": "测试副标"},
  "product": {"title_line1": "测试材质", "title_line2": "测试品类"},
  "options": {"duration_range": [45, 65], "highlight_keywords": ["显瘦", "针织"]}
}
```

**命令**：
```bash
python -u src/main_pipeline.py --spec <spec.json>
```

**预期输出**（本次实测 2026-10-24，耗时 **5m45s**）：

| 产物 | 路径 | 预期 |
|:--|:--|:--|
| 成片 | `output/<pid>/<pid>_final.mp4` | 1080×1920 / 60fps / 58~65s / 约 53MB |
| 封面 | `output/<pid>/<pid>_cover.jpg` | 1080×1920，品牌标 top_y=125，商品名金色渐变 |
| 封面渲染元数据 | `output/<pid>/<pid>_cover_render_meta.json` | 含 brand_en/brand_cn/product_name/sticker 元素 |
| 决策清单 | `output/<pid>/agent_decision_manifest.json` | chosen_cover / chosen_clips / gateway_decisions |
| 质检报告 | `output/<pid>/qc_report.json` | **20 项断言全 PASS**（1 条 WARN：封面宽占比） |
| 字幕 | `data/work_dirs/<pid>/subtitles.ass` | MarginV=240，Alignment=2 |
| 占位图（自动） | `data/placeholders/<pid>_placeholder_{1,2}.png` | 缺商品图时自动生成并渲染进封面 |

**实测 QC 输出**：20 项中 19 PASS(BLOCK) + 1 WARN（封面宽占比 0.366 未达 0.75，属"尽力居中"语义不拦截）。

#### B. 批量回归（5 条真实素材）

```bash
python -u scripts/run_regression.py            # 三层：单测 + 端到端 + 基线锁定
python -u src/batch_runner.py --specs-dir specs  # 仅端到端
```

**预期**：`单元测试 11/11`、`batch: done=5 qc_failed=0 error=0`、`指标基线 5/5 PASS`。
⚠️ 首次跑需全量 ASR（11 个源，约 25 分钟），之后靠 `code_fingerprint()` 缓存命中。

#### C. 各修复点的针对性验证

| 验证项 | 命令 | 预期 |
|:--|:--|:--|
| 镜像真正触发 | 跑完看 `data/work_dirs/<pid>/plan.json` 的 `clips[].effect` | 出现 `"hflip"` / `"split"`，不再全是 `none` |
| 防反贴纸 | 抽帧目检 hflip 分镜的背景文字区 | 反向文字被贴纸覆盖 |
| 分屏 | 抽帧目检 split 分镜 | 左右各 540px、中缝金色竖线、两侧胶囊标签 |
| QC 音画同步 | 看 `qc_report.json` | 断言数 20 → 21，新增项 PASS |
| chatcut 接入 | `python src/main_pipeline.py --spec X --timeline edited.json` | 精修后的时间线被采纳并重新总装 |
| prompt 回声不复发 | 看 `data/work_dirs/*/asr/*.whisper.json` 首段 | 首段 `dur < 10s`（11 份素材现为 0/11 回声） |

#### D. 端到端把关的最小配置

在有直播素材的机器上注册 self-hosted runner（一次性）：
```bash
gh api -X POST repos/Xepanda/dy-video/actions/runners \
  -f name='dy-video-win' -f runner_group_id=1 \
  -f labels='self-hosted,windows' -f ephemeral=false
```
之后给 PR 打 `run-material-regression` 标签即可触发素材层。

---

## 5. 一句话给决策者

- **要出片** → 现在就能出，5/5 素材稳定，QC 20 项把关。这条链路不需要再修。
- **要 SKILL.md 描述的效果** → 差 5 项视觉包装（镜像/分屏/贴纸/水印/挂角徽标），
  其中 2 项是"改一行接线"，3 项要写新渲染代码。
- **要团队可维护** → 3 处文档失同步 + CI 无端到端把关，这两个是低成本高收益。
