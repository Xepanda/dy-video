# 抖音带货直播视频剪辑 · 技术评审报告

> **评审对象**：`Xepanda/dy-video` PR #2（feature/upgrade-deterministic-pipeline-and-asr-verifier，5 commits，+2986/−236，Open 状态）及其所在仓库；关联仓库 `Xepanda/ai-video-studio`
> **评审输入**：《会议纪要.doc》（含 MIXCITY 实例封面图）、本地封样工程 `silk_knitwear_7270`（0826 绵羊·半岛棉镂空钩花气质显瘦针织衫 7270，4min25s 直播录像）
> **评审目标**：为"抖音直播剪辑功能拆分独立新仓库"提供需求符合性结论、工具链缺口清单与架构设计建议
> **评审日期**：2026-10-20

---

## 0. 执行摘要

1. **PR #2 的方向是正确的**：它将仓库从"单体黑盒脚本" pivoting 为 **Agent-in-the-Loop（确定性原子工具 + Agent 总导演）** 架构，确立了"工具做确定性测量、Agent 做候选裁决"的分工，与"尽可能量化、避免 agent 随机操作"的目标完全一致。封样视频端到端跑通：52.38s 成片、QC 8/8 PASS、18 处 ASR 语境纠错、本地 CPU 42.1s 出片。
2. **但当前实现是"demo 拟合"而非"通用管线"**：`plan_compiler.py` / `analyze_story_segments.py` 的分镜时间戳（109.91~120.19s 等）、黑名单时间窗（55~108s、121s+）、QC 错词表均为 7270 这一条视频硬编码。**换任意一条新素材，自主模式立即失效**。这是拆分新仓库前必须解决的头号问题。
3. **对照《会议纪要》24 条可量化细则：完全符合 8 条（33%）、部分符合 9 条（38%）、缺失 7 条（29%）**。封面版式（品牌上/商品名下/贴图右侧 280×280 圆角淡灰边）已 1:1 复刻实例图；但**衣着与商品图一致性、人脸表情/偏航角、无重影、品牌名换行、响度归一化、黑帧检测、封面 OCR 纯净度**七项完全缺失。
4. **工具链主要缺口**：人脸检测（现有 `frame_picker.py` 是死代码，未接入主链路）、衣着一致性检测、场景/镜头检测、OCR 纯净度、响度归一化、泛化分镜编译器。其中 4 项可直接从 `ai-video-studio` 移植复用（visual_qc、EBU R128 归一化、SHA256 缓存、OpenChatCut 桥接）。
5. **新仓库建议采用"契约先行 + 检测/执行分离 + 决策网关"架构**：所有纪要要求转写为 `constraints.yaml` 中的唯一 metric key，检测工具输出实测值，QC 门禁双源复测，Agent 只能在"合格候选"内做选择题且每次选择落盘可审。

---

## 0.5 需求决策确认（2026-10-20）

客户已对 4 个开放问题给出决策，后续设计与本报告所有结论以此为准：

| # | 决策内容 | 对设计的修正 |
| :-- | :-- | :-- |
| 1 | **1:6:1 为水平方向**：左:人:右 = 1:6:1，人物居中、宽占画布 3/4（810px，左右边距各 135px） | 封面构图主约束从"身高 74% 归一化"改为"人体轮廓宽 75% 居中"；身高降级为上限约束（≤88% 画布）以保全"完整人体露膝"；两者冲突时以多目标候选评分求解（详见改进计划 B5） |
| 2 | **商品名称标准色 = 金色** | 商品名由白字深褐描边改为金色渐变（#FFE9A8→#E8A91E）+ 深褐描边；QC 增加色相断言（H∈[35°,55°]） |
| 3 | **封面不要求价格，视频字幕也不出现具体价格** | 价格防控从"clip 级正则黑名单"升级为"中文数字价格检测器"三级防护（clip 排除 + 字幕过滤 + QC 零价格断言）；SKILL.md 中"300+ 价格腰封"封面方案废弃 |
| 4 | ~~**暂时 30fps**~~ → **2026-10-25 拍板统一 60fps** | 帧率口径最终统一为 60（原 30fps 决策作废） |

> 落地行动计划见同目录《improvement_plan_2026-10-20.md》（含 constraints.yaml v1.0 草案与三期实施任务）。

---

## 1. 需求基线：会议纪要的量化转写

将《会议纪要》转写为可测量、可断言的指标（测量工具列即新仓库工具规划，详见第 4、5 节）：

### 1.1 封面底图（源于视频截取 / 用户提供商品图）

| ID | 纪要原文要求 | 量化指标（建议阈值） | 测量工具 |
| :-- | :-- | :-- | :-- |
| C1 | 人物不要糊 | 全帧 Laplacian 方差 ≥ 200；人脸区域单独 ≥ 150 | detect_sharpness |
| C2 | 不要存在重影 | 帧间差分能量比 ≤ 0.15（或光流残差低于阈值） | detect_motion |
| C3 | 有清晰外廓 | 人像掩码完整度 ≥ 0.95；人像边缘 Sobel 梯度均值 ≥ 20 | detect_pose（分割掩码） |
| C4 | 主播表情正常 | EAR ≥ 0.18 且 MAR ≤ 0.35； blendshapes 无夸张表情 | detect_face |
| C5 | 较完整人体，能看到膝盖及以下 | 双膝 y ≤ 0.92 且 visibility ≥ 0.4；双脚踝 y ≤ 0.96 | detect_pose |
| C6 | 人物主体与图片边缘边距约 1:6:1，尽力居中（**已确认：水平方向，左:人:右 = 1:6:1，人物宽占 75% 居中**） | 人体轮廓宽 = 0.75×1080 = 810px ±3%；左右边距各 135px；身高 ≤ 88% 画幅（保完整人体露膝） | framing_normalizer + QC 复测 |
| C7 | 模特身着衣服和商品图片一致 | CLIP 图-图相似度 ≥ 0.75；或主色直方图相关性 ≥ 0.6 | detect_garment_match |

### 1.2 商品名称（用户提供，固定视频下方）

| ID | 纪要原文要求 | 量化指标（建议阈值） | 测量工具 |
| :-- | :-- | :-- | :-- |
| C8 | 固定视频下方，居中保持固定比例 | 文本块中心 x ∈ [50%±1%]；首行基线 y = 1530±10px；底部留白 ≥ 200px | render + QC 像素断言 |
| C9 | 名称过长换行，保持比例，换行间距一定 | 单行宽 > 92% 画布宽触发换行；字号按行数反比例缩放（64pt 起，下限 48pt）；行距 = 1.35×字号 | render |
| C10 | 字体种类、大小、颜色、艺术字保持一定（**已确认：商品名标准色 = 金色**） | 金色渐变 #FFE9A8→#E8A91E + 深褐描边 #3D1F0E + shear −0.12；QC 色相断言 H∈[35°,55°]、S≥0.5 | render 模板 + QC |

### 1.3 封面商品贴图（用户提供 0~2 个）

| ID | 纪要原文要求 | 量化指标（建议阈值） | 测量工具 |
| :-- | :-- | :-- | :-- |
| C11 | 图片位置、大小固定，正方形比例 | 280×280px；x = 1080−280−a | render + QC |
| C12 | 贴图靠左/靠右距离固定为 a | a = 32px（默认可配）；支持左/右侧配置 | render |
| C13 | 两张图上下距离为 a，商品图上下居中 | 间隙 = a = 32px；组合块中心 y = 960±2px | render + QC |
| C14 | 边缘优化，圆角，淡灰色边缘 | 圆角半径 20px；描边 #E2E8F0 宽 2px | render + QC 像素采样 |

### 1.4 品牌名称（用户表格提供，固定视频上方）

| ID | 纪要原文要求 | 量化指标（建议阈值） | 测量工具 |
| :-- | :-- | :-- | :-- |
| C15 | 名称固定视频上方，居中保持固定比例 | 文本块中心 x = 50%±1%；y ∈ [120, 300] | render + QC |
| C16 | 名称过长换行，保持比例，间距一定 | 同 C9 换行规则 | render |

### 1.5 输入方式与整体契约

| ID | 纪要原文要求 | 量化指标 | 测量工具 |
| :-- | :-- | :-- | :-- |
| C17 | 填表：有则做，无则使用默认方式 | 表单字段全可选 + 默认值兜底；输入路径存在性校验 | spec_creator |
| V1 | 输出 45~65s 视频 | ffprobe duration ∈ [45, 65] | qc_audit |
| V2 | 竖屏成片 | 1080×1920 | qc_audit |
| V3 | 音画/口型同步（剪辑基本盘） | 切片音画 PTS 双归零；字幕映射误差 ≤ 40ms | assemble + qc |
| V4 | 音频质量（知识库标准） | −14±1.5 LUFS，True Peak ≤ −0.5 dBTP | loudness |
| V5 | 字幕避让小黄车 | MarginV ≥ 220；成片底部 220px 像素级无字幕 | qc_audit |
| V6 | 无黑帧 | 黑帧占比 = 0 | visual_qc |
| V7 | 封面纯净（SOP"纯净无字正帧"） | 背景文字 OCR 面积占比 ≤ 2% | detect_ocr |
| V8 | 视频字幕不出现具体价格（**已确认**） | 最终 .ass 中价格表达 token 数 = 0 | detect_price + qc_audit |

---

## 2. 现状实现盘点

### 2.1 PR #2 交付内容（本地 feature 分支实测）

| 层 | 模块 | 职责 | 状态 |
| :-- | :-- | :-- | :-- |
| 工具 | `extract_cover_candidates.py` | MediaPipe Pose 正面筛选 + 74% 身高归一化 + 居中 + 镜像补边 + 微锐化 | 可用（缺人脸/表情/偏航角） |
| 工具 | `analyze_story_segments.py` | Faster-Whisper + ASR 语境纠错 + 电商漏斗打标 | 可用（分镜大块硬编码） |
| 工具 | `render_cover_tool.py` / `cover_renderer.py` | MIXCITY 参数化封面（品牌 3D 金字/商品名斜体/280×280 贴图卡） | 可用 |
| 工具 | `assemble_video_tool.py` / `video_assembler.py` | FFmpeg 物理音画强锁 + Hook 卡 + CTA + Boom + ASS 烧录 | 可用（无变焦/镜像/分屏算子） |
| 工具 | `spec_creator.py` | 免安装浏览器填表 → job_spec.json | 可用（字段不可缺省、含死配置） |
| 核心 | `asr_engine.py` / `asr_agent_verifier.py` | 转录缓存 + 同音词/语境纠错（确定性引擎） | 可用（LLM 路径为桩） |
| 核心 | `frame_picker.py` | EAR/MAR 表情 + 露膝 + 遮挡评分 | **死代码，未接入主链路** |
| 核心 | `plan_compiler.py` | 纯产品介绍漏斗编排 | **分镜时间戳硬编码于 demo 视频** |
| 决策 | `agent_director.py` / `main_pipeline.py` | 总导演：候选审阅 → plan.json → 组装 → QC 签署 | 可用（"审美裁决"实为取最高分候选） |
| 质检 | `qc_auditor.py` | 8 项断言（存在性/时长/分辨率/MarginV/错词/封面体积） | 部分（docstring 声称的响度、露膝、表情检查未实现） |

### 2.2 PR 描述与实现的差异（需修正文档或补实现）

| PR 声称 | 代码实际 |
| :-- | :-- |
| 面部偏航角 ≤ 12° 严格过滤 | 未实现，仅双肩深度差 \|ls.z−rs.z\| ≤ 0.055（docstring 写 0.05，代码 0.055，自身不一致） |
| 髋部深度差 ≤ 0.05 | 未实现 |
| 支持 LLM Agent（Gemini/OpenAI）语义纠错 | `_run_llm_verification` 恒返回 None，永远走本地规则引擎 |
| Sub-Bass Boom (4590Hz) | 实际为 90→45Hz 阻尼衰减，PR 文本笔误 |
| 封面 QC 含露膝/人脸表情合规 | 未实现，仅文件体积 ≥ 100KB |

### 2.3 与 ai-video-studio 的关系

- `ai-video-studio` 是通用 AI 视频生产平台（TTS 克隆/Remotion/HyperFrames/Google Flow），其中 `scripts/live_ecommerce_pipeline.py` 是**上一代单体直播剪辑脚本**（已被 PR #2 的新架构取代）；`skills/live-ecommerce-video-editor` 与 dy-video 仓库为同一份 Skill（已做 8 skill 双仓库同步）。
- ai-video-studio 已有可移植资产：`visual_qc.py`（黑帧/比例/安全区）、EBU R128 响度归一化（20ms cosine micro-fade 防爆音）、`asset_cache_manager.py`（SHA256 参数哈希缓存）、OpenChatCut 双向桥接（timeline↔chatcut）、`validator.py` 契约校验 + `AUDIT_CHECKLIST.md` 五重门禁方法论、`live_ecommerce_batch_pipeline.py` 批量骨架。
- **拆分建议**：新仓库独占"直播带货剪辑"领域（检测/编排/封面/组装/QC 全链）；ai-video-studio 回归通用视频生产，通过 Skill 文档引用而非代码依赖。

---

## 3. 符合性逐条对比与量化差距

### 3.1 封面底图（C1~C7）

| ID | 纪要要求 | 现状 | 评级 | 量化差距 |
| :-- | :-- | :-- | :-- | :-- |
| C1 | 人物不糊 | 全帧 Laplacian ≥ 200 才入候选 | ⚠️ 部分 | 人脸区域未单独测量（人脸占全帧 <5%，全局阈值会放过糊脸）；`frame_picker` 用 180、`extract_cover_candidates` 用 200，两模块阈值不一致 |
| C2 | 无重影 | 无任何运动/重影检测 | ❌ 缺失 | 需新增帧间差分/光流检测，0 覆盖 |
| C3 | 清晰外廓 | 仅有 unsharp 增强，无外廓量化 | ❌ 缺失 | 无人像掩码完整度/边缘梯度指标 |
| C4 | 表情正常 | `frame_picker.py` 有 EAR/MAR 但**未接入**；主链路 `extract_cover_candidates.py` 完全不做人脸检测 | ⚠️ 部分 | 表情检查 100% 依赖死代码；PR 声称的偏航角 ≤12° 未实现（0% 覆盖） |
| C5 | 露膝及以下 | 检查双脚踝 y≤0.96 + vis≥0.4（隐含露膝） | ⚠️ 部分 | 膝盖 landmarks 25/26 未直接断言；frame_picker 有膝盖检查但未接入；候选仅扫描**前 180s**（265s 素材的后 85s 无候选），步长 1.5s |
| C6 | 1:6:1 边距居中（**决策 1：水平方向，人宽占 75% 居中**） | 归一化：头顶 y=16%H、人体高 74%H、水平 x=50% | ⚠️ 部分 | 实现的是**垂直向** 16:74:10 ≈ 1:4.6:0.6，与决策的水平 1:6:1（左右各 135px、人宽 810px）不是同一约束；**水平边距完全未控制**（仅锁中心线，不锁人体宽占比）；需重写构图算法并处理与 C5 完整人体的几何张力；QC 无几何复测 |
| C7 | 衣着与商品图一致 | 无任何实现 | ❌ 缺失 | 0 覆盖；需 CLIP/直方图检测（多模态核心项） |

### 3.2 商品名称（C8~C10）

| ID | 纪要要求 | 现状 | 评级 | 量化差距 |
| :-- | :-- | :-- | :-- | :-- |
| C8 | 固定下方居中 | y=1530 两行居中，底部留白 ~200px | ✅ 符合 | 无 QC 像素级复测（建议补） |
| C9 | 过长换行保持比例 | 仅 2 行（表单拆行）；>8 字时缩字号 64→48pt | ⚠️ 部分 | **无自动换行**（超长名称直接缩字甚至溢出）；换行仅支持 2 行；行距 = 字号+24px 固定（≈1.37×，基本符合） |
| C10 | 字体保持一定（**决策 2：商品名金色**） | 微软雅黑 Bold、白字 #FFFFFF、深褐描边 #422115、shear −0.12 | ⚠️ 部分 | 与实例图金色渐变字存在色差；按决策 2 需改为金色渐变 #FFE9A8→#E8A91E，并增加 QC 色相断言 |

### 3.3 商品贴图（C11~C14）

| ID | 纪要要求 | 现状 | 评级 | 量化差距 |
| :-- | :-- | :-- | :-- |
| C11 | 位置大小固定正方形 | 280×280，x=768 | ✅ 符合 | — |
| C12 | 靠左/靠右距离固定 a | 仅右侧（a=32px 硬编码） | ⚠️ 部分 | **无靠左配置**；a 不可配 |
| C13 | 两图间距 a、上下居中 | 双卡间隙 32px、组合中心 y=960 | ✅ 符合 | — |
| C14 | 圆角、淡灰边缘 | radius=20、#E2E8F0 2px 描边 + 柔影 | ✅ 符合 | — |

### 3.4 品牌名称（C15~C16）

| ID | 纪要要求 | 现状 | 评级 | 量化差距 |
| :-- | :-- | :-- | :-- | :-- |
| C15 | 固定上方居中 | y=125（EN 74pt 3D 金字 + CN 52pt 白字） | ✅ 符合 | — |
| C16 | 过长换行 | 无任何换行逻辑 | ❌ 缺失 | 长品牌名直接溢出画布 |

### 3.5 输入方式（C17）

| ID | 纪要要求 | 现状 | 评级 | 量化差距 |
| :-- | :-- | :-- | :-- | :-- |
| C17 | 有则做，无则默认 | 表单品牌/商品名为 required；`split_strategy`（分屏策略）与 `duration_range`（时长区间）为**死配置**——无任何代码读取 | ⚠️ 部分 | ① 无"封面背景来源（视频截取/用户供图）"选项；② 无细节参数（a 值、比例、字体）输入；③ 无路径存在性校验；④ 2 个死配置误导用户；⑤ 时长区间未生效（`plan_compiler` 硬编码 45~55，QC 硬编码 45~65，三处口径不一） |

### 3.6 视频成片（V1~V7）

| ID | 要求 | 现状 | 评级 | 量化差距 |
| :-- | :-- | :-- | :-- | :-- |
| V1 | 45~65s | 52.38s，QC 断言 [45,65] | ✅ 符合 | QC 与 PlanCompiler（45~55）口径不一致 |
| V2 | 1080×1920 | QC 断言通过 | ✅ 符合 | — |
| V3 | 口型同步 | 切片音画双轨 PTS 归零 + `-avoid_negative_ts make_zero`；字幕按 clip 相对时间映射 | ⚠️ 部分 | 字幕映射用 ±0.2s 容差窗口截断，**无端到端唇形同步量化断言**（建议抽帧 + 音频能量峰值比对，误差 ≤40ms） |
| V4 | 响度 −14±1.5 LUFS | QC docstring 声称，**代码未实现** | ❌ 缺失 | 无 ebur128 测量、无归一化、无 True Peak 检查 |
| V5 | 字幕避让小黄车 | MarginV=240 参数合规（断言 ≥220） | ⚠️ 部分 | 仅查 ASS 参数，**成品无像素级安全区验证** |
| V6 | 无黑帧 | 未检测 | ❌ 缺失 | 0 覆盖 |
| V7 | 封面纯净无字 | 未检测 | ❌ 缺失 | 0 覆盖（背景店招/衣架文字直接透出） |

### 3.7 跨领域结构性差距（不限单条纪要）

| # | 问题 | 影响 | 严重度 |
| :-- | :-- | :-- | :-- |
| G1 | **分镜时间戳硬编码**（`plan_compiler` 忽略 `segments` 参数返回固定 3 块；`analyze_story_segments` 的 `continuous_blocks_summary` 写死） | 换素材即失效，自主模式不可用 | 🔴 致命 |
| G2 | **黑名单时间窗硬编码**（55~108s 卖裤子窗、121s+ 换衣窗为 7270 定制）+ 30+ 条 demo 专属正则 | 换素材误杀/漏杀 | 🔴 严重 |
| G3 | QC 错词表硬编码 8 个 demo 错词 | 新素材错词漏检 | 🟠 中 |
| G4 | 帧率口径不一：SOP 文档 60FPS，`video_assembler` 实际 30FPS | 交付规格争议（**决策 4：30fps 定稿**，SOP 需修订） | ✅ 已决策 |
| G5 | 字体路径 Windows 专属（Linux 回落 DejaVu 无中文字形 → 方框） | 跨平台部署即烂字 | 🟠 中 |
| G6 | SKILL.md 封面矩阵 Option 1 含"到手只要 300+"价格腰封，与 `plan_compiler` 零价格规则**自相矛盾** | 规范冲突（**决策 3：封面与字幕均无价格**，SKILL 需废弃价格腰封方案） | ✅ 已决策 |
| G7 | 无断点恢复、无批量、封面候选仅扫前 180s | 长直播（>3min）覆盖不足 | 🟡 低 |
| G8 | `frame_picker.py` 死代码 + `pipeline.py` 兼容壳 + `.bat` 先删后恢复（456fed7→5eac5ad） | 仓库整洁度 | 🟡 低 |

### 3.8 符合性汇总

**24 条可量化细则：✅ 完全符合 8 条（33%）｜⚠️ 部分符合 9 条（38%）｜❌ 缺失 7 条（29%）。**
封面"版式骨架"已复刻实例图；缺口集中在**检测类指标（C2/C3/C4/C7/V6/V7）与泛化能力（G1/G2）**。

---

## 4. 工具链分析与新增工具建议

### 4.1 现有工具链评价

| 工具 | 确定性 | 量化输出 | 泛化性 | 结论 |
| :-- | :-- | :-- | :-- | :-- |
| extract_cover_candidates | ✅ 高 | ✅ 正面分/清晰度/构图 meta | ⚠️ 时间窗 180s | 保留，补人脸检测 |
| analyze_story_segments | ✅ 高 | ✅ 五池打标 | ❌ 大块硬编码 | 保留打标，拆出通用 build_plan |
| asr_agent_verifier | ✅ 高（规则） | ✅ 18 处纠错审计 | ⚠️ 词表静态 | 保留，词表外置 + LLM 路径落地 |
| cover_renderer | ✅ 高 | ❌ 无几何自检 | ✅ 参数化 | 保留，输出渲染几何 meta 供 QC |
| video_assembler | ✅ 高 | ✅ timeline 回测 | ✅ plan 驱动 | 保留，算子化（zoom/hflip/split） |
| qc_auditor | ✅ 高 | ✅ 8 项 | ⚠️ 错词表硬编码 | 保留，接入 constraints.yaml |
| spec_creator | ✅ 高 | ❌ 无校验 | ⚠️ 死配置 | 保留，字段可选化 + 去死配置 |
| frame_picker | — | — | — | **并入 detect_face，删死代码** |

### 4.2 建议新增工具（按优先级）

**P0（拆分新仓库前必须）**

| 工具 | 职责 | 方案选型 | 对应纪要 |
| :-- | :-- | :-- | :-- |
| `tools/detect_face.py` | 人脸 landmark：偏航角（≤12°）、EAR/MAR 表情、人脸区域清晰度、遮挡 | MediaPipe FaceLandmarker（已有模型文件 data/face_landmarker.task） | C1/C4 |
| `tools/build_plan.py` | **泛化分镜编译**：场景边界 → 语义打标池 → 漏斗配额（Hook/面料/版型/穿搭）→ 时长约束 [45,65] 贪心求解 → plan.json | PySceneDetect 边界 + 现有打标池 + 约束求解；**严禁任何硬编码时间戳** | V1/G1 |
| `tools/detect_garment_match.py` | 封面候选帧人像衣着区域 vs 商品图一致性 | 首选 open_clip/cn-clip 图-图相似度（≥0.75）；降级：HSV 主色直方图相关性（≥0.6）+ 纹理 LBP | C7 |
| `tools/loudness.py` | ebur128 响度测量 + EBU R128 归一化（−14 LUFS / TP ≤ −0.5 dBTP / 20ms micro-fade） | 移植 ai-video-studio `audio_mastering` 组件 | V4 |
| `tools/visual_qc.py` | 黑帧/静帧/比例/移动端安全区检测 | 移植 ai-video-studio `visual_qc.py` | V5/V6 |

**P1（首个迭代）**

| 工具 | 职责 | 方案选型 | 对应纪要 |
| :-- | :-- | :-- | :-- |
| `tools/detect_ocr.py` | 封面背景文字面积占比（纯净度 ≤2%） | PaddleOCR（优先，中文强）或 EasyOCR | V7 |
| `tools/detect_scenes.py` | 镜头切换点检测（供 build_plan 使用，内置则免单建） | PySceneDetect ContentDetector | G1 |
| `templates/` 封面模板引擎 | 版式参数与代码分离：MIXCITY 模板 1:1 复刻纪要实例图；品牌/商品名统一自动换行规则 | JSON/YAML 模板 + 现有 PIL 渲染器重构 | C8~C16 |
| assemble 算子化 | clip 级 `zoom`（1.25~1.35×）、`hflip`+文字遮罩、`split`（540:540 双色对比）落到 plan 参数并真实生效 | 现有 `build_clip_filter` 扩展 | SKILL 2.1~2.3 |
| `qc` 增强 | 封面几何复测（边距比/贴图坐标/圆角色彩像素采样）、成品字幕安全区像素检测、时长口径统一为 constraints | 现有 qc_auditor 重构 | C6/C13/V1/V5 |

**P2（增强）**

| 工具 | 职责 | 说明 |
| :-- | :-- | :-- |
| decision gateway | Agent 的每次选择（封面/分镜）先过约束校验，不合格即拒绝并重选 | "避免 agent 随机操作"的核心机制 |
| batch runner + resume | 多素材批量 + 阶段产物断点续跑 | 移植 ai-video-studio 批量骨架 + SHA256 缓存 |
| OpenChatCut 桥接 | timeline ↔ chatcut 双向转换，支持人工精修回注 | 移植 ai-video-studio bridge |
| LLM 审核落地 | asr_agent_verifier 的 LLM 路径（Gemini/OpenAI 兼容）真实实现，作为规则引擎的上层复核 | 修复 PR 声称能力 |

### 4.3 "更好的方案"评估

1. **衣着一致性**：CLIP 类模型是唯一能同时覆盖"款式/颜色/材质"语义的方案，但需离线标定阈值（建议用 6/7/8/9 号商品实拍各 20 帧建立分数分布，取 P5 分位点）；纯颜色直方图会混淆同色系不同款，仅作降级。
2. **约束检查双源化**：检测工具在"候选阶段"测一次（选人用），QC 在"成品阶段"独立复测一次（验收用），防止"检测通过但渲染/组装过程劣化"。这是对"执行与检查环节通过工具量化确认"最直接的落地。
3. **分镜编译用约束求解而非 LLM 自由发挥**：漏斗配额 + 时长窗口 + 黑名单是确定性整数/区间约束，贪心+回溯可解；LLM 只做候选池的语义补充（如Hook 金句排序），不做时长与边界的最终裁决。
4. **封面模板配置化**：把会议纪要实例图拆成模板参数（坐标/字号/色值/圆角/a 值），换品牌只换 JSON，且每项参数都有对应 QC 断言，实现"复刻即合规"。
5. **复用 > 重写**：visual_qc、响度归一化、缓存、桥接四件套直接从 ai-video-studio 移植，预计节省 1~1.5 周。

---

## 5. 新仓库架构与工作流设计

### 5.1 设计原则（由评审结论直接推导）

1. **契约先行**：`job_spec` / `constraints` / `plan` / `qc_report` 四份 Schema 是仓库的"宪法"，工具只认契约不认口头需求。
2. **检测与执行分离**：`detect_*` 只读媒体、输出指标；`render/assemble` 才写文件。任何"检查"不得由"执行者"自说自话。
3. **Agent 只做选择题**：每个决策点交付带量化指标的候选列表，Agent 在合格集内优选；选择经 decision gateway 校验后落盘 `agent_decision_manifest.json`。
4. **确定性可复现**：缓存键 = sha256(输入文件 + 参数 + constraints 版本)，同输入必同输出。
5. **可恢复可审计**：每阶段产物落盘，支持 resume；全链路决策可回溯。
6. **一票否决 + 分级告警**：QC 分 BLOCK（不符纪要硬指标）与 WARN（建议优化）两级。

### 5.2 模块划分（七层）

```
┌──────────────────────────────────────────────────────────────┐
│ L6 templates/   封面模板资产（MIXCITY.json 等，参数化版式）      │
├──────────────────────────────────────────────────────────────┤
│ L5 runtime/     manifests/ decisions/ cache/（阶段产物+审计）    │
├──────────────────────────────────────────────────────────────┤
│ L4 qc/          qc_audit.py（双源复测 + BLOCK/WARN 门禁）       │
├──────────────────────────────────────────────────────────────┤
│ L3 agent/       director.py 总导演                              │
│                 cover_selector.py 封面优选（选择题）             │
│                 plan_compiler.py  分镜编排（约束求解）           │
│                 decision_gateway.py 选择校验（不合格拒绝）        │
├──────────────────────────────────────────────────────────────┤
│ L2 tools-exec/  render_cover.py / assemble_video.py /          │
│                 loudness.py（写文件，全部幂等 + 缓存）            │
├──────────────────────────────────────────────────────────────┤
│ L1 tools-detect/ probe_media / asr_transcribe / asr_verify /   │
│                 detect_face / detect_pose / detect_garment /   │
│                 detect_scenes / detect_ocr / detect_sharpness  │
│                 （只读，输出 metrics manifest）                  │
├──────────────────────────────────────────────────────────────┤
│ L0 contracts/   job_spec.schema.json / constraints.yaml /      │
│                 plan.schema.json / qc_report.schema.json       │
└──────────────────────────────────────────────────────────────┘
```

### 5.3 约束规则量化方式（constraints.yaml）

每条纪要要求 → 唯一 metric key → 测量工具 → 阈值/容差 → 失败级别。示例：

```yaml
version: 1
updated: 2026-10-20        # 含 4 项需求决策
cover:
  sharpness_min: 200              # C1 全帧 Laplacian 方差
  face_sharpness_min: 150         # C1 人脸区域
  ghosting_energy_max: 0.15       # C2 帧间差分能量比
  contour_completeness_min: 0.95  # C3 人像掩码完整度
  face_yaw_max_deg: 12            # C4 偏航角
  ear_min: 0.18                   # C4 睁眼
  mar_max: 0.35                   # C4 闭嘴
  face_area_ratio: [0.02, 0.25]   # C4 防过远/过近
  knees_visible: true             # C5
  # —— 决策 1：水平 1:6:1，人宽占 75% 居中 ——
  horizontal_margin_ratio: [1, 6, 1]   # C6 左:人:右
  person_width_ratio: 0.75             # C6 810/1080
  person_width_tolerance: 0.03
  person_height_max_ratio: 0.88        # C6 与 C5 冲突时的身高上限
  garment_match_min: 0.75         # C7 CLIP 分数
  ocr_area_ratio_max: 0.02        # V7 背景文字
text:
  wrap_width_ratio: 0.92          # C9/C16 触发换行
  line_spacing_ratio: 1.35
  font_size_base: 64
  font_size_min: 48
  # —— 决策 2：商品名金色 ——
  product_name_gradient: ["#FFE9A8", "#E8A91E"]
  product_name_stroke: "#3D1F0E"
  product_name_hue_range: [35, 55]     # QC 色相断言
  product_name_sat_min: 0.5
sticker:
  size: 280                       # C11
  margin_a: 32                    # C12/C13
  gap_a: 32                       # C13
  side: right                     # C12 left | right 可配
  radius: 20                      # C14
  border_color: "#E2E8F0"         # C14
video:
  duration_range: [45, 65]        # V1
  resolution: [1080, 1920]        # V2
  fps: 60                         # 全局统一 60fps（2026-10-25 决策，覆盖原决策 4 的 30fps）
  subtitle_margin_v_min: 220      # V5
  av_sync_error_max_ms: 40        # V3
  black_frame_max_ratio: 0.0      # V6
audio:
  loudness_lufs: -14              # V4
  loudness_tolerance: 1.5
  true_peak_max_dbtp: -0.5
price:                            # 决策 3：封面与字幕零价格
  clip_exclude: true
  subtitle_assert_zero: true      # V8
  detector: chinese_numeral_v1
```

要点：
- **检测工具 manifest 必须携带每个指标的实测值**，QC 直接消费 + 成品独立复测（双源）；
- 阈值调整只改 yaml，不改代码；约束版本进入缓存键；
- 每个 metric key 在 `docs/constraints_map.md` 中登记对应纪要原文，保证可追溯。

### 5.4 工具接口设计要点

```
统一 CLI：  python -m tools.<name> --input X --out-dir Y \
           --constraints contracts/constraints.yaml --format json
退出码：    0=成功  1=软失败(可降级, warnings)  2=硬失败(契约违反)
统一 manifest（每个工具必出）：
{
  "tool": "detect_face", "version": "1.0.0",
  "input_hash": "sha256:...", "params": {...},
  "metrics": { "face_yaw_deg": 8.2, "ear": 0.23, "mar": 0.18, ... },
  "artifacts": [...], "warnings": [...], "elapsed_ms": 1234
}
```

1. **JSON Schema 校验一切进出文件**（spec/plan/manifest/qc_report）；
2. **缓存键 = sha256(输入 + 参数 + constraints 版本)**，移植 ai-video-studio `asset_cache_manager`；
3. **Agent 工具协议采用 MCP 风格 schema**（name/description/inputSchema/outputSchema），使任意 Agent 运行时可即插即用；
4. **检测工具禁止产生副作用**（不写媒体文件，只写 manifest + 缩略图）；
5. **决策类接口（gateway）只做校验与记录**，不重新测量（消费已有 metrics）。

### 5.5 Agent 决策协议（以封面优选为例）

```
1. extract_cover_candidates → 候选帧 + C1~C6 实测 metrics
2. detect_garment_match    → 每候选 C7 分数
3. detect_ocr              → 每候选 V7 分数
4. gateway 过滤：任一 BLOCK 级指标不合格 → 直接淘汰（agent 无权选择）
5. Agent 在合格候选中做审美优选（多模态查看缩略图 + 指标表）
6. 选择写入 decision manifest：{ chosen_id, reason, metrics_snapshot }
7. QC 终审复测 → 通过则交付，否则携带失败项回到步骤 4/5
```

分镜编排同理：`build_plan` 先按约束求解出 2~3 个候选 plan（不同 Hook 开头/时长档位），Agent 选择叙事逻辑最佳者，gateway 校验时长/黑名单/配额后落盘。

### 5.6 端到端工作流（8 步）

```
0. spec_creator/API → job_spec.json ── Schema 校验 + 路径存在性校验（C17）
1. probe_media      → 时长/分辨率/帧率/音画流
2. asr_transcribe + asr_verify → story_candidates.json（词表外置，LLM 可选复核）
3. 检测集群（只读）→ cover_candidates（C1~C7）+ shot_boundaries + ocr 纯净度
4. Agent 封面优选 + gateway 校验 → cover_decision.json
5. build_plan 约束求解 → 候选 plan.json → Agent 编排 + gateway 校验
6. render_cover（模板引擎）→ 封面 + 渲染几何 meta → QC 复测 C8~C16
7. assemble_video（算子化）→ 成片 → loudness 归一化（V4）
8. qc_audit 全量门禁（V1~V7 + C 类复测）→ qc_report.json
   ├─ 全绿 → agent_decision_manifest.json 签署交付
   └─ 有 BLOCK → 携带失败项回退至对应步骤（支持人工 override 并记录理由）
```

### 5.7 建议目录结构

```
dy-live-editor/                  # 新仓库（建议名）
├── contracts/                   # L0 四份 Schema + constraints.yaml + constraints_map.md
├── tools/
│   ├── detect_*.py              # L1 只读检测（9 个）
│   └── exec_*.py                # L2 执行（render_cover/assemble_video/loudness）
├── agent/                       # L3 director/cover_selector/plan_compiler/decision_gateway
├── qc/qc_audit.py               # L4
├── templates/mixcity.json       # L6 封面模板（复刻纪要实例图）
├── runtime/                     # L5 manifests/decisions/cache（gitignore）
├── tests/                       # 单元 + 契约 + fixture 回归（3 条新素材锁指标区间）
├── fixtures/                    # 小体积测试视频（<30s 切片）
└── docs/                        # SOP + 工具手册 + 评审报告
```

---

## 6. 实施路线图

| 阶段 | 周期 | 内容 | 验收标准 |
| :-- | :-- | :-- | :-- |
| P0 打地基 | 第 1 周 | contracts 四件套（含 4 项决策）；detect_face 合并 frame_picker；build_plan 泛化（消除全部硬编码）；loudness + visual_qc 移植；QC 重构接入 constraints | **用 6/7/8/9 号 4 条新素材零人工干预跑通**，QC 全绿；分镜不再出现写死时间戳 |
| P1 补检测 | 第 2~3 周 | detect_garment_match（CLIP 标定）；detect_ocr；模板引擎（品牌/商品名自动换行 + 金色商品名）；assemble 算子化；QC 封面几何复测 | C2/C3/C7/C16/V6/V7/V8 七项缺失清零；4 条素材 C 类指标 ≥95% 通过 |
| P2 强管控 | 第 4 周起 | decision gateway；batch + resume；OpenChatCut 桥接；LLM 审核落地；口径统一为 constraints | 批量 10 条/晚无人值守；每次 agent 决策 100% 落盘可审 |

**里程碑风险**：衣着一致性 CLIP 阈值需实物标定（建议 P1 第一天启动数据收集）；水平 1:6:1（人宽 75%）与完整人体在 9:16 画幅下存在几何张力，需"宽高双约束 + 多目标候选评分"求解（见改进计划 B5）。

> 详细任务分解（文件级）与验收标准见同目录《improvement_plan_2026-10-20.md》，本路线图为其摘要。

---

## 7. 风险与开放问题

1. ~~**1:6:1 语义**~~：**已决策（2026-10-20）——水平方向，左:人:右 = 1:6:1，人物宽占 75% 居中**。遗留工程问题：与"完整人体露膝"在 9:16 画幅下的几何张力，解法见改进计划 B5（宽高双约束 + 多目标候选评分）。
2. ~~**商品名字色**~~：**已决策——金色**（渐变 #FFE9A8→#E8A91E），实现与 QC 断言见改进计划 B9。
3. ~~**价格规则冲突**~~：**已决策——封面与视频字幕均无价格**。SKILL.md 价格腰封方案废弃，字幕级零价格断言见改进计划 B3。
4. ~~**帧率口径**~~：**已决策——全局统一 60fps**（2026-10-25 最终拍板，SKILL / 约束 / 装配器 / QC / ChatCut 五处一致）。
5. **多人/频繁换装素材**：封面策略（始终取主推款时段）与黑名单（换装窗）需要场景检测自动识别，依赖 build_plan 泛化质量。
6. **原仓库处置**：建议 dy-video 合入 PR #2 后冻结归档（保留 demo 复现能力），直播剪辑能力全部迁入新仓库；ai-video-studio 仅保留 Skill 文档引用。

---

## 附录 A：量化指标总清单（24 条）

C1 清晰度（人脸区域）｜C2 重影能量比｜C3 外廓完整度｜C4 偏航角/EAR/MAR｜C5 膝踝可见性｜C6 边距比 1:6:1±10%｜C7 衣着一致分｜C8 商品名坐标｜C9 换行规则｜C10 字体四要素｜C11 贴图 280²｜C12 边距 a=32 左右可配｜C13 间隙 a=32 居中｜C14 圆角 20 + #E2E8F0｜C15 品牌名坐标｜C16 品牌换行｜C17 表单可选+校验｜V1 45~65s｜V2 1080×1920｜V3 唇形同步 ≤40ms｜V4 −14±1.5 LUFS / TP≤−0.5｜V5 MarginV≥220 + 像素验证｜V6 黑帧 0｜V7 OCR 文字占比 ≤2%

## 附录 B：关键代码位置索引

| 问题 | 位置 |
| :-- | :-- |
| 分镜硬编码 | `src/core/plan_compiler.py:86-117`、`src/tools/analyze_story_segments.py:152-174` |
| 黑名单时间窗 | `src/core/plan_compiler.py:59-64`、`src/tools/analyze_story_segments.py:66-69` |
| 人脸检测死代码 | `src/core/frame_picker.py`（全文件，未被 import） |
| 偏航角未实现 | `src/tools/extract_cover_candidates.py:164-168`（仅肩深） |
| 帧率 30 与 SOP 60 冲突 | `src/core/video_assembler.py:25` vs `workflows/standard_pipeline.md:59` |
| QC 声称未实现项 | `src/qc/qc_auditor.py:9-11`（docstring）vs `52-135`（实现） |
| 死配置 | `src/tools/spec_creator.py:227-233`（split_strategy/duration_range 无消费方） |
| LLM 桩 | `src/core/asr_agent_verifier.py:119-126` |

---

*本报告基于 PR #2 feature 分支本地实测（commit 5eac5ad）与《会议纪要.doc》需求逐条比对生成，所有结论均可按附录 B 索引复核。*
