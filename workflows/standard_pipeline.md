# Live E-Commerce Video Production Workflow (直播带货短视频标准化作业流程)

本文件定义了将多小时直播素材剪辑为高完播、高转化 45~65 秒短视频的完整标准操作流程（SOP）。

---

## 阶段一：素材准备与环境就绪 (Setup)
1. 将直播原素材（`.mp4`）存放在目标项目的 `video/` 目录下（如 `project1/video/`）。
2. 确保已安装 FFmpeg 并在 PATH 中可用（运行 `ffmpeg -version` 确认）。
3. 激活 Python 环境并安装依赖：
   ```bash
   pip install -r requirements.txt
   ```

---

## 阶段二：语音转录与台词时间戳定位 (ASR)
1. 运行 Faster-Whisper 转录脚本，生成全量带毫秒时间戳的台词数据：
   ```bash
   python -c "from src.pipeline import LiveVideoPipeline; p = LiveVideoPipeline('project1'); p.transcribe_audio()"
   ```
2. 查看生成的 `temp/whisper_segments.json`，搜索核心卖点词汇：
   - 品牌/大促金句（“生日专场只排这一个”、“别的棉服都被比下去”）
   - 材质与结构（“3M新雪丽”、“夹棉”、“假两件连帽”、“竖插口袋”）
   - 穿搭推荐（“搭卫裤”、“运动鞋”、“松弛感”）
   - 多配色（“藏青”、“米白”）
   - 福利到手价（“300多块钱”）

---

## 阶段三：分镜脚本规划与素材切片 (Clipping)
1. 依据【黄金 3 秒法则】，**打破原本时间顺序**：
   - 将整场最极端、最煽动的重音金句切片作为前 0~3 秒开场钩子。
2. 挑选 8~12 个核心讲解镜头，总时长严格计算在 **45 ~ 65 秒之间**。
3. 对各分镜施加镜头语言：
   - 做工细节讲解处：施加 `1.3x` 推镜特写（Detail Zoom）；
   - 人物站位单调处：施加 `hflip` 水平镜像，并**用贴纸覆盖反向背景字**；
   - 双色对比展示处：施加左右 `540:540` 等比分屏（左右模特身材等大且均为真实试穿）。

---

## 阶段四：视觉与音频特效注入 (Effects & Hook)
1. 生成 45Hz~90Hz 重低音 Sub-Bass Boom 冲击音效：
   ```bash
   python -c "from src.pipeline import LiveVideoPipeline; p = LiveVideoPipeline('project1'); p.generate_sub_bass_boom()"
   ```
2. 自动生成 980px 超大黄金 3 秒视觉冲击标题卡（定位于 `y=150~430`，帽子上方安全避障，不遮挡面部）：
   ```bash
   python -c "from src.pipeline import LiveVideoPipeline; p = LiveVideoPipeline('project1'); p.render_hook_card()"
   ```
3. 配合分镜时间轴生成阶段性卖点徽章贴纸（存放在 `temp/stickers/`）。

---

## 阶段五：同步发光字幕与整片渲染 (Subtitles & Master Export)
1. 编写或生成精确贴合发音停顿的 `.ass` 字幕，核心卖点词亮黄色高亮，设置 `MarginV=240` 避开底部小黄车。
2. 字幕零价格硬约束（决策 3）：`python src/tools/detect_price.py --ass temp/subtitles.ass`，检出价格即回炉重剪。
3. 调用 FFmpeg 一键合流成片，输出规格：
   - 分辨率：`1080x1920` (9:16)
   - 帧率：`30 FPS`（决策 4 定稿）
   - 音频：`48 kHz` 高保真立体声，响度归一 −14±1.5 LUFS / True Peak ≤ −0.5 dBTP
   - 输出至：`project1/output/douyin_jacket_final.mp4`

---

## 阶段六：爆款封面矩阵制作 (High-CTR Covers)
1. 从原视频提取模特神态自然、姿态舒展的**纯净无字正帧**。
2. 一键生成 Option 1 爆款大促型主封面（金红大标 + 底部 300+ 价格腰封 + 原生实心星号排版）：
   ```bash
   python -c "from src.pipeline import LiveVideoPipeline; p = LiveVideoPipeline('project1'); p.render_default_cover('project1/temp/raw_white_worn.jpg')"
   ```
3. 产出文件：
   - `output/cover_default.jpg`（主选）
   - `output/cover_option_1_promotion.jpg`（大促促销型）
   - `output/cover_option_2_split_colors.jpg`（双色真实对比型）
   - `output/cover_option_3_lifestyle.jpg`（松弛感种草型）
