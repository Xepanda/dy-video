# constraints_map.md — 纪要原文 ↔ 量化指标登记表

> 每个纪要要求都有唯一 metric key 与测量工具，保证可追溯。阈值定义见 `constraints.yaml`。

## 封面底图（源于视频截取 / 用户提供商品图）

| 纪要原文 | metric key | 测量工具 | 失败级别 |
| :-- | :-- | :-- | :-- |
| 人物不要糊 | `cover.sharpness` / `cover.face_sharpness` | detect_pose/detect_face | BLOCK |
| 不要存在重影 | `cover.ghosting_energy` | detect_sharpness（帧间差分） | BLOCK |
| 有清晰外廓 | `cover.contour_completeness` | detect_pose（分割掩码） | BLOCK |
| 主播表情正常 | `cover.face_yaw_deg` / `cover.ear` / `cover.mar` | detect_face | BLOCK |
| 较完整的人体，能够看到膝盖及以下 | `cover.knees_visible` / `cover.ankles_visible` | detect_pose | BLOCK |
| 人物主体与图片边缘存在边距大概 1:6:1（尽力居中） | `cover.width_ratio`（决策1：水平，左:人:右=1:6:1，人宽 75%） | extract_cover_candidates + QC 复测 | BLOCK（[0.72,0.78]） |
| 模特身着衣服和商品图片一致 | `cover.garment_match` | detect_garment_match | BLOCK |

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

| 纪要原文 | metric key | 测量工具 | 失败级别 |
| :-- | :-- | :-- | :-- |
| 填表：有则做，无则使用默认方式 | `job_spec` 字段可选 + 默认值 + 路径校验 | spec_creator + schema 校验 | BLOCK |
| 输出符合要求的 45-65s 视频 | `video.duration` | ffprobe + QC | BLOCK |
| 竖屏成片 | `video.resolution` | ffprobe + QC | BLOCK |
| 音画/口型同步 | `video.av_sync_error_ms` | assemble 双轨 PTS 归零 + QC 抽检 | BLOCK |
| 音频质量 | `audio.loudness_lufs` / `audio.true_peak_dbtp` | loudness（ebur128） | BLOCK |
| 字幕避让小黄车 | `video.subtitle_margin_v` + 成品安全区像素检测 | QC | BLOCK |
| 无黑帧 | `video.black_frame_ratio` | visual_qc（blackdetect） | BLOCK |
| 封面纯净 | `cover.ocr_area_ratio` | detect_ocr | BLOCK |
| 字幕不出现具体价格（决策3） | `price.subtitle_matches` | detect_price + QC | BLOCK |

## 修订记录

| 日期 | 变更 |
| :-- | :-- |
| 2026-10-20 | v1.0 建立；锁定决策 1（水平 1:6:1）、决策 2（金色商品名）、决策 3（字幕零价格）、决策 4（30fps） |
