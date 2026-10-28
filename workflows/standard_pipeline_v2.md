# Live E-Commerce Video Production Workflow v2（标准化作业流程 · 2026-10-27 重构版）

> 反馈驱动重构：封面全幅底图 / ASR 驱动完整对话剪辑 / 特效集成 / 违规词三通道处理 /
> 全阶段审核机制 / 工具缺口反思。
> 本文件是**流程宪法**：每个阶段的输入、输出、审核机制、失败回退均为确定化定义。
> 代码与阈值的唯一来源分别是 `src/` 与 `contracts/constraints.yaml`（v1.4）。

---

## 0. 阶段总览（素材输入 → 成片输出）

```text
S0 契约与素材准入 ──► S1 候选池提取 ──► S1.5 一致性预检 ──► S2 Agent 决策编排
      │                      │                    │                    │
      ▼                      ▼                    ▼                    ▼
 job_spec.json         镜头/封面/ASR        关键词覆盖×契约         plan.json
 素材清单校验           三池候选            对话脚本生成             （句子边界+
                                                                    放大镜锚点+
                                                                    违规处理参数）
      │                                                              │
      │                     ┌────────────────────────────────────────┘
      ▼                     ▼
S3 封面渲染 ◄── 决策底图    S4 音画强锁组装（转场+特效+违规三通道+字幕屏蔽）
      │                     │
      ▼                     ▼
S5 QC 门禁 ◄── 25+ 项断言（含违规词零残留/人脸安全/转场/放大镜复测）
      │
      ▼
S6 交付签署（manifest + QC 报告 + 说明书）
```

**每个阶段的独立审核机制**（反馈第 5 条要求的落地）：

| 阶段 | 审核机制 | 不通过时的行为 |
| :--- | :--- | :--- |
| S0 | ffprobe 可解码校验 + 商品图存在校验 + 素材清单非空 | 中止并报告缺失项 |
| S1 | ASR 审核门禁（密度/CJK 比/重复串/时长）+ 封面人脸量化门禁（偏航/EAR/MAR/清晰度/完整人体） | 候选淘汰，池空则报错 |
| S1.5 | **素材-契约一致性预检**（关键词覆盖率/商品名词覆盖率/衣着一致性/违规词扫描） | WARN 登记并显式告警；`consistency_check.enforce_level=BLOCK` 时可中止 |
| S2 | 决策约束网关（每次选择先过 BLOCK 校验）+ 句子边界吸附 + 放大镜锚点避让 | 拒绝重选/回退默认值 |
| S3 | 封面渲染几何 meta + QC 像素级复测（品牌区/名称区/贴图/金色色相/宽占比/背景文字） | BLOCK 拦截 |
| S4 | 组装过程断言（切片时长回测/字幕事件守恒/叠加时序完整） | 单特效失败降级跳过并记录 |
| S5 | QC 门禁 30+ 项 BLOCK/WARN 断言 | BLOCK 失败即拦截交付 |
| S6 | Agent 签署（manifest 全量决策落册） | — |

---

## 1. S0 契约与素材准入

- **输入**：`job_spec.json`（品牌/商品/素材/时长/关键词/分屏策略）。
- **执行**：`python scripts/generate_from_job_spec.py --spec job_spec.json`
  1. 素材收集：`raw_video_paths` 显式列表优先；否则 `raw_video_path` 目录扫描（按名排序）；
  2. ffprobe 逐段校验（分辨率/帧率/时长/音轨）；
  3. 商品图存在校验（缺失走占位兜底并显式提示）；
  4. 文案派生：Hook 卡/CTA 严格取自商品名（零硬编码，`split_strategy=force_off` 时不注入分屏配对）；
  5. 落盘可执行 spec：`specs/job_spec_<project_id>.resolved.json`。
- **输出**：可执行 spec + 素材清单。
- **审核**：素材不可解码/为空 → 中止（S0 是唯一允许"拒绝开工"的阶段）。

## 2. S1 候选池提取（确定性工具链）

| 工具 | 职责 | 审核点 |
| :--- | :--- | :--- |
| `detect_scenes.py` | 镜头边界检测 | 失败不阻断（降级无 cuts） |
| `extract_cover_candidates.py` | 封面候选：人脸门禁 + **全幅底图构图**（v1.4：`max(构图scale,填充scale)`，顶部黑边根因消除）+ 水平 1:6:1 | sharpness/face_yaw/EAR/MAR/完整人体/膝盖 |
| `analyze_story_segments.py` | ASR 转录 + 语境纠错 + 漏斗四类打标 + 黑名单剔除 | ASR 审核门禁（`asr_review` 段） |
| `detect_price.py` | 中文数字价格检测（选段/字幕/QC 三级） | 字幕零价格 BLOCK |

- **输出**：`cover_candidates.json` / `story_candidates.json` / `scenes_*.json` / `whisper_segments_verified.json`。

## 3. S1.5 素材-契约一致性预检（2026-10-27 新增，防"挂羊头卖狗肉"）

- **工具**：`check_spec_material_consistency.py`
- **检查**：
  - C1 规范关键词覆盖率（ASR 台词命中 `highlight_keywords` 比例）；
  - C2 商品名词覆盖率（商品名核心词命中比例）；
  - C3 衣着一致性（封面帧 vs 商品图，直方图降级）；
  - C4 违规词扫描（登记，进入三通道处理）。
- **输出**：`output/<pid>/consistency_report.json`，WARN 写入决策 manifest。
- **配套**：`build_dialogue_script.py` 生成**完整对话脚本**（`dialogue_script.json`）：
  相邻静默 ≤0.6s 的 ASR 段合并为 utterance，标注句末完整标记/漏斗类目/违规命中——
  这是 S2 剪辑的唯一剧本依据。

## 4. S2 Agent 决策编排（总导演中枢）

1. **封面优选**：候选按多目标分降序，先过约束网关（B12）；
2. **分镜编译**：`build_plan.py` 泛化编译（漏斗配额 + 时长 [45,65] + 黑名单），
   **句子边界吸附**（v1.4：硬裁剪点吸附 ≤2s 窗口内最近的句末标点，宁短勿长）；
3. **Hook 卡避让**：人脸顶 → 卡底 ≥30px，结果过网关；
4. **放大镜锚点分配**（v1.4）：仅面料类且未推镜分镜，锚点=肩胯中点（胸腹区，天然避让面部），
   每条成片 ≤2 个，Pose 不可用退兜底位；
5. **违规词三通道处理准备**（v1.4）：`apply_violation_handling.py` 计算
   逐分镜消音时段 + OCR 打码区域 + 字幕命中清单 → `violation_handling.json`；
6. **计划过网关**后落盘 `plan.json`（分镜/特效/放大镜/违规处理参数一体）。

## 5. S3 封面渲染（全幅底图 + 自然叠加）

- **工具**：`render_cover_tool.py` → `cover_renderer.py`
- **v1.4 修复**（反馈第 1 条）：
  - 底图**全幅覆盖**：构图缩放与填充缩放取大者，垂直位置钳制到充分覆盖区间
    `[new_h−OUT_H, 0]`——从几何上根除「顶部黑边 + 整体下移」；
  - 文字可读性用**渐变遮罩**（scrim）而非硬黑边：顶部深褐渐隐（品牌区）、
    底部渐深（商品名区），画面中部保持纯净；
  - 贴图（商品卡）圆角 + 投影 + 淡灰描边，右侧垂直堆叠，自然叠加于图片上方。
- **输出**：`<pid>_cover.jpg` + `<pid>_cover_render_meta.json`（几何 meta 供 QC 复测）。

### 5.1 商品图封面适配器（v1.6 新增，2026-10-28）

商品图直接作为封面主体时的自然过渡方案（`product_image_adapter.py`）：
四模式 × 全参数可配 × 六项量化验证。详见 `src/graphics/product_image_adapter.py`
模块文档与 `src/tools/adapt_product_cover.py --help`。

### 5.2 右侧商品贴图形态体系（2026-10-28 四轮反馈收敛，终版）

**终版共识：商品图的白底是商品图自身的背景，不可抠除。** 默认形态为 card；
cutout 为可选形态，仅用于商品图无自身背景的场景。四级回退：

| 形态 | 配置 | 行为 |
| :--- | :--- | :--- |
| **card（默认）** | `sticker_style: card` | 商品图**原样保留**（含自身背景）+ 圆角 20px + 外边缘 10px 柔和淡化（`sticker_feather_px`）+ 矩形投影 + **无描边** |
| cutout | `sticker_style: cutout` | 去白底贴纸形态（见下），商品主体直接融入直播画面 |
| 自然过渡卡 | `card` + `use_adapter_edges: true` | 即默认形态的边缘处理链（缩放+锐化 → dehalo → 圆角+EDT 羽化 → 向背景渐变融合） |
| 硬边直贴 | `use_adapter_edges: false` | 最早的硬圆角掩码直贴（不推荐） |

**cutout 形态细节**（可选能力，`remove_white_background` + 贴纸感处理）：

- **去白底**：`ProductImageAdapter.remove_white_background` —— flood fill
  连通域语义，仅移除与图像边界连通的近白像素（亮度>205 且低饱和度）与
  最外 3px 深色外框环；**主体内部的浅色/白色区域绝对保留**（如毛衣上的
  浅色条纹），两件毛衣主体保留率实测 95.6%；
- **圆角**：圆角掩码自身 2px 高斯羽化（半径默认沿用 `sticker.radius`）；
- **贴纸感**（反馈"商品贴图要贴上去"）：剪影浅色描边
  （`cutout.outline`，MaxFilter 膨胀 alpha 得外扩环）+ 剪影投影
  （`cutout.shadow`，alpha 下移+高斯模糊，**跟随商品轮廓**而非矩形卡）——
  去底商品明确读作贴在封面表面的贴纸，消除"漂浮感"；
- **淡化强度**：`cutout.feather_sigma` 默认 1.8（约 4px 过渡带）——
  2026-10-28 反馈「淡化太夸张，只是把边缘淡化一下」，从 4.0 收敛。

**形态演进教训（后续需求判断基准）**：
「去除白边」类反馈存在两种语义——① 商品图自身的背景（**不可抠**，本次终版确认）；
② 贴图卡片边缘的生硬分界/浅色描边（用圆角+柔和淡化+关描边解决）。
遇到类似反馈先与用户确认属哪种，再动处理链。

**历史死键修复**：`radius/border_color/border_width/vertical_center` 四键曾被错放在
`split_effect` 段（git 历史证明为既有错位；split_effect 只消费 divider/label 键，
四键是死键，贴图实际读 cover_renderer.py 的 DEFAULTS）→ 已迁回 `sticker` 段。
**教训：改配置前先用 `yaml.safe_load` 验证键的归属段与消费方。**

**QC 兼容**：贴图几何 meta（box/side/size/layout）不变，`_audit_cover_geometry`
元数据级检查不受影响；meta 追加 style/matte 字段（bg_removed_ratio /
subject_retention / feather_sigma）供审计。

## 6. S4 音画强锁组装（转场 + 特效 + 违规三通道）

- **工具**：`assemble_video_tool.py` → `video_assembler.py`
- **v1.4 新增能力**：

| 能力 | 实现 | 约束来源 |
| :--- | :--- | :--- |
| 切片间转场 | 视频 `xfade=fade` 链 + 音频 `acrossfade` 链（0.25s），时间轴/字幕/贴纸时序同步折算 | `transition_effect` |
| 放大镜圆圈 | 圆形 alpha 掩码 + `alphamerge` + 金属环 PNG，镜内 1.8x 内容、淡入淡出时序 | `magnifier_effect` |
| 镜像 hflip | 穿搭/版型类条件决策 + OCR 反向文字遮挡，遮挡覆盖率不足自动放弃 | `mirror_effect` |
| 推镜 zoom | 分镜内 smoothstep 缓动 1.3x，锚点防主体跑位 | `zoom_effect` |
| 语音消音（违规通道 2） | `volume` 表达式区间消音，80ms 淡入出防爆音 | `violation_words.handling` |
| 视频打码（违规通道 3） | OCR 定位画面可见违规词 → crop+pixelate+overlay 回填 | 同上 |
| 字幕屏蔽（违规通道 1） | ASS 生成前命中词替换 `***` | 同上 |
| 头尾淡变 | 全片 fade in/out 0.3s | `fade_effect` |
| 挂角贴纸/平台条/Hook 卡/CTA | 时序淡入淡出，避让底部小黄车 | `video_sticker` 等 |

- **输出**：`<pid>_final.mp4` + `video_overlay_meta.json`（叠加元素权威清单）
  + `violation_handling_report.json`（三通道执行记录）。

## 7. S5 QC 门禁（30+ 项量化断言）

在既有断言（时长/分辨率/60fps/−14 LUFS/黑帧/音画同步/MarginV/零价格/封面几何/色相/
纯净度/贴纸时序/镜像遮挡）之外，v1.4 新增：

| 断言 | 级别 | 依据 |
| :--- | :--- | :--- |
| 字幕违规词零残留 | BLOCK | `detect_violation.audit_ass_file` 全量扫描成片字幕 |
| 叠加层人脸安全 | BLOCK | `check_overlay_face_safety`：叠加元素活跃时段抽帧 + 人脸 bbox 几何交叠（间距 <30px 记 WARN，压脸 BLOCK） |
| 切片转场复测 | WARN | overlay meta 的 transition 记录 |
| 放大镜特效复测 | WARN | overlay meta 的 magnifier_events 参数合规 |

### 7.1 成片 ASR 复检门禁（v1.5 新增，2026-10-27 闭环"物理咬合原声"承诺）

**背景**：原工作流对**素材** ASR 有三重校验（`asr_review_gate` 的 A1~A10 /
`asr_agent_verifier` 语境纠错 / `consistency_check` 关键词覆盖），但对**拼接后
完整视频**没有任何 ASR 复检——所有字词断言都是 ASS 字幕**文件级**文本检查，
无法验证字幕与成片原声一致、消音真生效、转场不吞音、价格/违规词不在音频残留。
`verify_final_video_asr.py` + QC `_audit_final_video_asr` 补齐这一环：

| 编号 | 检查 | 判定标准 | 异常处理 |
| :--- | :--- | :--- | :--- |
| V1 | 字幕-原声一致性 | 成片 ASR 全文 vs 烧录字幕全文 difflib 相似度；<0.50 BLOCK，<0.70 WARN | BLOCK：疑似烧录错位/切片串位/转场吞音 → 回查 timeline 与字幕映射 |
| V2 | 语音违规词零残留 | ViolationDetector 扫成片 ASR；检出即 BLOCK | BLOCK：消音时段算错或漏检 → 回查 violation_handling.json 重算 |
| V3 | 语音价格零残留（决策 3 音频通道） | detect_price 扫成片 ASR；检出即 BLOCK | BLOCK：价格话术进入成片音轨 → 回查选段黑名单 |
| V4 | 识别密度健全性 | 成片 ASR 句数/时长 ≥ 0.06 句/秒 | WARN：组装吞音/音轨损坏 → 回查切片抽取 |
| V5 | 消音执行核查 | 消音窗口**核心区**（去两端 80ms 淡变边）实测 max_volume ≤ −30dB | BLOCK：词仍可闻 → 回查消音表达式与时段换算 |

- **转录缓存**：`work_dir/final_asr_segments.json`（按时长校验命中），复跑不重转录；
- **降级策略**：转录环境故障（模型加载失败等）→ WARN 不拦截交付，但显式记录；
- **反向测试已验证**：毒化转录（违规词+价格+错位字幕）与毒化消音窗口均被正确 BLOCK。

- **输出**：`qc_report.json`（BLOCK 全过才准予交付）。

## 8. S6 交付签署

- **输出**：`output/<pid>/` 下成片、封面、封面 meta、QC 报告、`agent_decision_manifest.json`
  （含一致性预检与对话脚本统计）、`generation_manifest.json`；
- **文档**：`docs/live_content_structure_<pid>.md`（`build_content_structure_doc.py` 生成）。

---

## 9. 工具缺口反思与补充清单（反馈第 6 条）

### 9.1 本轮已补充的工具

| 工具 | 解决的缺口 |
| :--- | :--- |
| `detect_violation.py` + `violation_words.yaml` | 原体系只有价格检测，无抖音极限词库（反馈第 4 条） |
| `apply_violation_handling.py` | 原体系无"命中后如何处理"的执行参数生成器 |
| `build_dialogue_script.py` | 原体系无完整对话脚本这一剪辑剧本（反馈第 2 条） |
| `check_spec_material_consistency.py` | 原体系无素材-契约一致性预检（2026-10-26 马甲/针织衫错配教训） |
| `check_overlay_face_safety.py` | 原体系无叠加层压脸量化审核（反馈第 5 条） |
| `generate_magnifier_mask/ring` | 原体系无放大镜素材（反馈第 3 条） |
| xfade/acrossfade 转场链 | 原体系为硬 concat，切片间语音过渡生硬（反馈第 2 条） |

### 9.2 仍缺失、建议后续建设的工具

| 缺口 | 建议方案 | 优先级 |
| :--- | :--- | :--- |
| **违规词库在线同步** | 飞书文档 API 直连拉取（当前为本地镜像 YAML，需人工同步） | 高 |
| **ASR 时间戳精细化** | Whisper word-level timestamps（当前段级线性插值，打码/消音边界为近似） | 高 |
| **口型驱动字幕对齐** | 字幕事件与音素级对齐（当前段级映射已毫秒咬合，但词级更佳） | 中 |
| **智能转场选择** | 按画面运动矢量选转场类型（fade/slide/wipe），当前全局 fade | 中 |
| **多模态封面优选** | CLIP 打分替代启发式规则（神态/构图/点击率预估） | 中 |
| **成片抽帧审核台** | 一键导出关键帧序列供 Agent/人工终审（当前靠 QC 自动化） | 中 |
| **爆款因子回归** | 历史成片数据（完播/转化）回流，反哺漏斗配额与 Hook 选择 | 低 |
| **爆款标题 A/B** | 多封面矩阵的点击率追踪与自动优选 | 低 |

---

## 10. 运行方式

```bash
# 端到端（规范驱动，推荐）
python scripts/generate_from_job_spec.py --spec job_spec.json

# 分阶段干预
python src/main_pipeline.py --spec specs/job_spec_<pid>.resolved.json --cover-id cand_01
python scripts/build_content_structure_doc.py --project-id <pid>

# 回归
python scripts/run_regression.py
```

---

## 11. 实践经验与注意事项（2026-10-28 沉淀，改动前必读）

### 11.1 FFmpeg 参数陷阱

1. **`-t` 位置决定归属**：输入侧 `-ss/-t` 必须在 `-i` **之前**；输出侧 `-t` 放在**所有输入之后**。
   写错的后果实测：复合滤镜路径中 `-t dur` 被下一个 `-i`（掩码 PNG）吞掉作为输入选项，
   源视频无时长限制 → 20s 分镜抽出 164.6s → 成片 206s。
2. **`-avoid_negative_ts make_zero`** 与输入侧 `-ss` 配合，保证切片时间轴归零。
3. **filter_complex 输入序号**：多输入（源视频 + 循环 PNG）时，掩码/贴图的输入序号按
   `-i` 出现顺序硬编号（0=源视频，1=掩码，2=环），改输入顺序必须同步改滤镜引用。

### 11.2 审核工具设计纪律

4. **审核工具不得硬编码动态值**： Hook 卡位置是 Agent 动态避让的结果，
   人脸安全检查必须从 `video_overlay_meta.json` 读取实际盒子
   （2026-10-27 实测：检查工具写死 y=280，实际 195 → 正确成片被误判压脸）。
5. **避让人脸必须多帧采样**：主播在黄金 3 秒内会前倾/晃动（实测单帧与 1.875s 后人脸顶差 20px），
   Hook 卡避让改前 2.5s 七帧采样取最高人脸顶 + 10px 缓冲。
6. **消音核查只测窗口核心区**：消音表达式两端 80ms 淡变区播放的是**合规的相邻语音**
   （非违规词），整窗测量经 loudnorm 放大后误报（−22.6dB→−16dB）；
   核查区去掉两端 0.15s。
7. **融合质量检查须区分融合区与主体内部**：alpha=1.00 的不透明内部内容边缘
   （商品自身边框对比，实测跳变 50）不是融合缺陷；只测 alpha∈(0.05,0.95) 的半透明区。
8. **门禁有效性必须反向验证**：毒化输入测试（毒化转录注违规词/价格、毒化消音窗口对准响亮语音）
   证明断言能拦住真问题，不是摆设。

### 11.3 图像处理细节

9. **EDT 距离变换需外加 False 边框**：距离变换只看数组内部的 False 像素，
   矩形顶边行距圆角区很远 → 边缘行 alpha≈1，羽化完全失效；掩码外加 1px False 边框修复。
10. **mode/样式参数必须驱动形态默认**：样式开关与视觉参数解耦会「空转」
    （实测三模式产出 md5 完全相同）；用 MODE_DEFAULTS 分层（DEFAULTS < constraints < MODE_DEFAULTS < 用户 config）。
11. **PIL `width=0` 描边行为不确定**：条件跳过（`border_width > 0` 才画）。
12. **重放大场景补锐化**：源图远小于目标框（>3x）时 LANCZOS 偏软，
    补轻度 UnsharpMask（radius=2, percent=55）。

### 11.4 配置与工程纪律

13. **改配置前验证键的归属段与消费方**：用 `yaml.safe_load` 确认键在哪个段、
    代码从哪个段读。历史教训：贴图四键（radius/border_width 等）错放在 `split_effect` 段
    达数月，改配置始终无效（死键）。
14. **CRLF 文件的多行编辑**：本仓库部分文件（constraints.yaml 等）为 CRLF 行尾，
    Edit 工具多行匹配会失败；用 Python `open(newline="")` 读写处理。
15. **heredoc 补丁脚本不要手写 `\uXXXX` 转义**：易出 typo 且报错信息难定位；
    中文代码块一律用 Edit 工具直接写入。
16. **阶段产物缓存清理**：失败运行的坏成片会写入阶段缓存（final_video/qc），
    重跑前必须删除对应缓存文件，否则坏结果被复用。
17. **文案零硬编码**：Hook 卡 / CTA / 贴纸文案从契约商品信息派生，并有测试锁定
    驱动与 build_plan 的派生一致性；换商品不换代码。
18. **提交前必须跑 CI 同款入口**：`python -u scripts/run_regression.py --quick`
    （14 个测试文件）。`pytest tests/` 覆盖不全——`test_path_resolver.py`（spec 路径审计）
    与 `test_requirements_audit.py`（需求/死键审计）不在 pytest 常规收集路径内，
    曾导致「本地全绿、CI 挂三项」。
19. **入库 spec 不得含机器专属绝对路径**：`job_spec*.json` 的 `raw_video_path` /
    `raw_video_paths` / `product.item_images` 必须是相对路径（素材放
    `data/raw_samples/<任务名>/`，mp4 已被 gitignore）；运行时生成的
    `specs/*.resolved.json` 含绝对路径，已 gitignore 不入库。
20. **新增约束键必须接线或登记**：`contracts/requirements.yaml` 是机器可读基线，
    死键审计（AST 扫描 `.get()/[]` 消费路径）要求每个 yaml 键都有消费者或
    `allow_dead` 登记（附原因）。注意审计只看 AST 消费链——经 `_deep_merge`
    间接消费的键不可见（见 R-X11 商品图适配器条目），需登记并靠单测覆盖。

### 11.5 需求判断基准（贴图类反馈）

18. 「去除白边」类反馈有两种语义，先确认再动处理链：
    ① **商品图自身的背景**——不可抠（本次终版确认，白底是商品图的一部分）；
    ② **贴图卡片边缘的生硬分界 / 浅色描边**——用圆角 + 外边缘柔和淡化 + 关描边解决。
19. 「淡化太夸张 / 只要边缘柔和一些」——调 `sticker.sticker_feather_px`
    （card 形态）或 `cutout.feather_sigma`（cutout 形态），小步迭代（每次幅度 ≤2x）。
20. 「贴图要贴上去」——贴纸感三要素：剪影描边 / 剪影投影 / 明确边界；
    但若同时要求「保留背景、只要边缘柔和」，则退回 card 形态（投影 + 圆角即可，
    不要描边和去底）。
