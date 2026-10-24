# constraints_map.md — 纪要原文 ↔ 量化指标登记表

> 每个纪要要求都有唯一 metric key 与测量工具，保证可追溯。阈值定义见 `constraints.yaml`。

## 📌 状态图例（2026-10-24 审计后新增）

本表原先把"计划实现"当成"已实现"，导致文档与代码脱节。现明确标注真实状态：

| 标记 | 含义 |
| :-- | :-- |
| ✅ | **已落地**：QC 有同名断言，阈值来自 constraints.yaml，改 yaml 即生效 |
| ⚠️ | **部分落地**：有检测能力但未进 QC 断言，或阈值未接线（如实标注，不假装） |
| ❌ | **未落地**：仅登记需求，尚无实现 |

> 审计方法：逐一比对 QC 实际断言名（`output/*/qc_report.json`）与本表；
> 并用"注入极端值看代码行为是否变化"验证阈值确实被消费。

## 封面底图（源于视频截取 / 用户提供商品图）

| 纪要原文 | metric key | 测量工具 | 状态 | 失败级别 |
| :-- | :-- | :-- | :-- | :-- |
| 人物不要糊 | `cover.sharpness` / `cover.face_sharpness` | detect_face + Laplacian | ✅ | BLOCK |
| 不要存在重影 | `cover.ghosting_energy_max` | — | ❌ **未实现**（键已保留在 yaml，但无任何代码消费） | — |
| 有清晰外廓 | `cover.contour_completeness_min` | — | ❌ **未实现**（同上） | — |
| 主播表情正常 | `cover.face_yaw_max_deg` / `ear_min` / `mar_max` | detect_face | ✅ 候选阶段硬门禁 | BLOCK（候选阶段拦截） |
| 较完整的人体，能够看到膝盖及以下 | `cover.knees_visible` / `knee_y_max` | detect_pose + QC 复测 | ✅ | BLOCK |
| 人物主体与图片边缘存在边距大概 1:6:1（尽力居中） | `cover.person_width_ratio` | extract_cover_candidates + QC 复测 | ✅（阈值已接线到代码） | WARN（示例版式偏差记 WARN） |
| 模特身着衣服和商品图片一致 | `cover.garment_match` | detect_garment_match + QC 复测 | ✅（2026-10-24 接入：约束真正生效，候选阶段计算 + QC 独立复测） | CLIP→BLOCK / 直方图→WARN / 占位图→跳过 |
| 人体关键点置信度 | `cover.landmark_visibility_min` | — | ❌ **未实现**（MediaPipe 自带置信度，未额外设阈） | — |
| 头顶/脚底/身高上限构图约束 | `cover.feet_target_ratio` / `head_floor_ratio` / `head_top_min_ratio` / `person_height_max_ratio` | extract_cover_candidates | ✅（2026-10-24 接线，原为硬编码死值） | — |
| 水平边距比例 | `cover.horizontal_margin_ratio` | — | ⚠️ **未接线**（1:6:1 语义已由 person_width_ratio 承载） | — |

## 商品名称（固定视频下方）

| 纪要原文 | metric key | 测量工具 | 失败级别 |
| :-- | :-- | :-- | :-- |
| 名称固定视频下方，居中保持固定比例 | `text.product_name_bbox`（模板 meta + QC 复测） | render_cover + QC | BLOCK |
| 名称过长换行，保持比例，换行间距保持一定 | `text.wrap_width_ratio` / `text.line_spacing_ratio` | render_cover | BLOCK |
| 字体种类、大小、颜色、艺术字保持一定 | `text.product_name_gradient`（决策2：金色）/ `shear` | render_cover + QC 色相断言 | BLOCK |

## 封面商品贴图（0~2 个）

| 纪要原文 | metric key | 测量工具 | 失败级别 |
| :-- | :-- | :-- | :-- |
| 图片位置、大小固定，正方形比例 | `sticker.size` / `sticker.boxes` | render_cover + QC 像素复测 | BLOCK |
| 贴图靠左/靠右距离固定为 a | `sticker.margin_a` / `sticker.side` | render_cover | BLOCK |
| 两张图上下距离为 a，商品图上下居中 | `sticker.gap_a` / `sticker.vertical_center` | render_cover + QC | BLOCK |
| 边缘优化，圆角，淡灰色边缘 | `sticker.radius` / `sticker.border_color` | render_cover + QC 像素采样 | BLOCK |

## 品牌名称（固定视频上方）

| 纪要原文 | metric key | 测量工具 | 失败级别 |
| :-- | :-- | :-- | :-- |
| 名称固定视频上方，居中保持固定比例 | `text.brand_bbox` | render_cover + QC | BLOCK |
| 名称过长换行，保持比例，间距一定 | `text.wrap_width_ratio` / `text.line_spacing_ratio` | render_cover | BLOCK |
| 字体保持一定 | `text.brand_font_size_*` / 色值 | render_cover | BLOCK |

## 输入与整体契约

| 纪要原文 | metric key | 测量工具 | 状态 | 失败级别 |
| :-- | :-- | :-- | :-- | :-- |
| 填表：有则做，无则使用默认方式 | `job_spec` 字段可选 + 默认值 + 路径校验 | spec_creator + schema 校验 | ✅ | BLOCK |
| 输出符合要求的 45-65s 视频 | `video.duration_range` | ffprobe + QC | ✅ | BLOCK |
| 竖屏成片 | `video.resolution` | ffprobe + QC | ✅ | BLOCK |
| 音画/口型同步 | `video.av_sync_error_max_ms` | assemble 双轨 PTS 归零 | ⚠️ **未做QC 抽检**（PTS 归零已保证物理同步，但无独立量化断言；键也未被消费） | — |
| 音频质量 | `audio.loudness_lufs` / `audio.true_peak_dbtp` | loudness（ebur128） | ✅ | BLOCK |
| 字幕避让小黄车 | `video.subtitle_margin_v_min` | QC | ✅ | BLOCK |
| 无黑帧 | `video.black_frame_max_ratio` | blackdetect | ✅ | BLOCK |
| 封面纯净 | `ocr.background_text_area_max` | detect_ocr + QC | ✅ | BLOCK |
| 字幕不出现具体价格（决策3） | `price.subtitle_matches` | detect_price + QC | ✅ | BLOCK |
| 帧率 | `video.fps` | ffprobe + QC | ✅ | BLOCK |

## 字幕与 ASR（2026-10-24 新增）

| 项 | metric key | 工具 | 状态 | 级别 |
| :-- | :-- | :-- | :-- | :-- |
| ASR 语境纯净度（禁用错词） | `subtitle_banned_tokens` | detect_price/黑名单 + QC | ✅ | BLOCK |
| ASR 执行者审核门禁（10 校验点） | `asr_review.*` | asr_review_gate | ✅ | BLOCK/WARN 分级 |
| 提示词回声污染 | `asr_review.A10` | asr_review_gate | ✅ | WARN |
| 音视频时长容差 | `video.duration_tolerance_sec` | — | ❌ 未实现（键未消费） | — |

## 修订记录

| 日期 | 变更 |
| :-- | :-- |
| 2026-10-20 | v1.0 建立；锁定决策 1（水平 1:6:1）、决策 2（金色商品名）、决策 3（字幕零价格）、决策 4（30fps） |
| 2026-10-24 | v1.1 审计后修订：新增状态图例；如实标注 6 个"声称 BLOCK 但未落地"的项（重影/外廓/landmark 置信度/av_sync 抽检/horizontal_margin_ratio/duration_tolerance_sec）；补齐已落地项的真实 metric key；新增 ASR 审核与字幕章节 |
