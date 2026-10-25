# 免费本地 ASR 方案横向实测报告

> 实测日期：2026-10-24
> 实测脚本：`tests/asr_engine_benchmark.py`（可复现：`python -u tests/asr_engine_benchmark.py --limit-seconds 90`）
> 原始数据：`logs/asr_benchmark.json`
> 基准素材：`1011 松弛帽衫·绵羊毛假两件连帽拉链卫衣11214 0518.mp4`（直播带货，语音密集）
> 截取音频：前 90s / 16kHz / 单声道 / WAV
> 参考基线：faster-whisper(small) **无 prompt**（即本轮修复后流水线的实际配置）

---

## 一、结论速览

| 引擎 | 句块 | 字数 | RTF | 关键词命中 | 同音错字 | 繁体占比 | CER | 时间戳 | 结论 |
|:--|--:|--:|--:|--:|--:|--:|--:|:--|:--|
| **faster-whisper(small)** | 42 | 478 | 0.678 | 4 | 3 | 0.0 | 0（基准） | ✅ 段级 | **保持现状** |
| **faster-whisper(base)** | 53 | 474 | 0.517 | 3 | 3 | **5.7%** | 0.247 | ✅ 段级 | ❌ 繁体污染，不适合中文 |
| **SenseVoiceSmall**（阿里/FunASR） | 1 | 488 | **0.266** | **5** | 2 | 0.0 | **0.159** | ❌ **无** | ⚠️ 文本最好，但无时间戳 |
| **Paraformer-large**（阿里/FunASR） | 1 | 496 | **0.267** | **5** | **1** | 0.0 | **0.161** | ✅ **字级** | 🏆 **最佳候选** |

**选型建议**：
- **维持 faster-whisper(small) 作为默认**——它是当前流水线唯一能直接产出「句级 + 毫秒时间戳」的方案，改动风险最低。
- **Paraformer-large 是最佳升级候选**——文本质量（错字 1 处、CER 0.161）与 SenseVoice 相当甚至更好，速度快 2.5 倍，且**组合模型能出字级时间戳**。但需要先做一层适配（字级 ts → 句级 segment）。
- **SenseVoiceSmall 不建议直接接入**——不输出任何时间戳，无法满足字幕毫秒打点需求。
- **faster-whisper(base) 排除**——5.7% 繁体字（"那種超級厚的"），中文直播场景不可接受。

---

## 二、关键实测细节

### 2.1 SenseVoiceSmall 的硬伤：没有时间戳

```python
res = model.generate(input=wav, cache={}, language="zh", use_itn=True,
                     batch_size_s=60, merge_vad=True, merge_length_s=15)
res[0].keys()   # → ['key', 'text']     ← 只有文本！
```

输出被合并成**一整段**，顶层只有 `key` / `text`，**没有任何 timestamp 字段**。
本项目的字幕需要毫秒级打点 + 选段需要句级边界，因此 SenseVoice **无法直接接入**。

> 调研文档 `docs/knowledge_base/ai_video_engines_playbook.md` 把 SenseVoice 定位为
> "动画分镜毫秒级对齐主力"，但那是**动画分镜**场景（已知台词、按句切分），
> 与本项目的"从直播音频里自动切句"场景不同——这个区分此前被模糊了。

### 2.2 Paraformer-large 的时间戳：必须用组合模型

| 模型写法 | timestamp | sentence_info |
|:--|:--|:--|
| `AutoModel(model=..., vad_model=..., punc_model=...)` | ❌ `None` | ❌ 0 条 |
| `AutoModel(model="iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch")` | ✅ 496 个字级 ts | ❌ 0 条 |

**只有内建 VAD+PUNC 的组合模型才输出时间戳**。拆开传就丢。

组合模型输出结构：
```
text      = "厚 度 也 不 是 那 种 超 级 厚 的 但 是 ..."   ← 空格分隔的单字
timestamp = [[110,230],[230,350],...]                    ← 496 个字级 [start,end]（毫秒）
```
去掉字间空格后与 timestamp **逐字一一对应** → 可按标点切句并给出每句起止毫秒。

**两个适配坑**：
1. 尾部偶发幻觉字符（实测出现过字面 `the`，使 compact 长度 498 ≠ ts 长度 496）
   → 需按 `len(ts)` 裁剪尾部。
2. 组合模型的 `sentence_info` 为空，句级切分需自己按 `。？！` 做。

### 2.3 faster-whisper(base) 的繁体污染

```
base 引擎输出："也不是那種超級厚的" / "但是這個帽子看上去這個衣服感染很厚"
繁体占比 5.7%
```
`small` 同素材繁体占比 0%。中文直播场景对 base 模型**不可用**。

### 2.4 RTF 说明

RTF = 推理耗时 / 音频时长。< 1 表示比实时快。
本轮实测数字比 2026-10-21 那次偏高（fw_small 0.32 → 0.68），因为**两轮 benchmark
与三层回归同时在跑**，CPU 争抢。绝对值参考意义有限，**相对关系**（base 快 2.5 倍、
funasr 快 2.5 倍）才是结论依据。

---

## 三、为什么 SenseVoice / Paraformer 的"关键词命中更多"不代表更好

两者都命中 5 个商品词（vs small 的 4 个），但这是**分母效应**：两者输出 488/496 字，
而 small 只有 478 字且首段起于真实语音。字数多自然更容易撞上关键词。

真正可比的是**同音错字数**与 **CER**：
- Paraformer **1 处**错字（最少）
- SenseVoice 2 处
- faster-whisper small/base 各 3 处
- CER：Paraformer 0.161 ≈ SenseVoice 0.159 < base 0.247

而本项目的**领域词纠错**由本地校对器 `AsrContextVerifier` 负责
（`STATIC_DOMAIN_HOMOPHONES` 错别字表 + 拼音指纹 + 滑动窗口消歧 + `_polish_for_subtitle`），
所以**引擎原始错字数的差距会被后处理部分抵消**——这降低了换引擎的收益，
但 Paraformer 的字级时间戳（精度高于段级）仍是实打实的价值。

---

## 四、集成 Paraformer 的前置条件（若要换）

1. **模型体积**：`paraformer-large` 约 840MB（vs faster-whisper small 约 500MB），
   首次下载需可访问 ModelScope。
2. **适配层**：需写 `AsrEngine` 的第三种 backend，把字级 ts 按标点聚合成句级 segment，
   保持与现有 `{"start","end","duration","text"}` 结构一致，才能复用下游全部链路
   （`analyze_story_segments` / `asr_review_gate` / `build_plan` / 字幕烧录）。
3. **回归验证**：换引擎会让全部 ASR 缓存失效，5 条素材需全量重跑，
   并重新锁定 `fixtures/baselines.json` 的指标基线。
4. **审核门禁适配**：A6 单段最长时长、A5 识别密度等阈值需按新引擎的切句粒度重标定。

---

## 五、本轮修复中发现的更重要问题：initial_prompt 回声污染

写这个 benchmark 时顺带发现了一个**比换引擎更严重**的问题：

给 faster-whisper 注入 `initial_prompt` 后，模型会把 prompt 文本当成音频内容识别，
**挤占开头约 30 秒的真实语音**。

| 配置 | 段数 | 总字 | 前 90s 字数 | 首段 |
|:--|--:|--:|--:|:--|
| 有 prompt（修复前） | 151 | 1478 | **348** | `[0-30s]` "连帽衫·绵羊毛"（纯回声） |
| 无 prompt（修复后） | 162 | 1625 | **495** | `[0-1.8s]` "厚度也不是那种超级厚的"（真实） |
| 有 prompt + `condition_on_previous_text=True` | 15 | 160 | 39 | 复读循环 |

→ **注入 prompt 反而少识别 30% 的内容**。8/11 份真实素材的首段都能观察到回声
（"主播口语高频词，羽绒服…" / "蕉个朋友·环保毛绒，" / "连帽衫·绵羊毛"）。

**修复**：`AsrEngine(use_initial_prompt=False)` 默认不注入 prompt
（领域词纠错交给本地校对器，不依赖 prompt 偏置），
并在审核门禁新增 **A10 提示词回声检测**兜底
（实测 4/4 真回声全检出、7 个正常首段零误报）。

> 换言之：**当前 faster-whisper(small) + 无 prompt 的效果，已经优于当初"有 prompt"
> 的预期基线**，而 Paraformer/SenseVoice 的优势主要在速度和字级时间戳上。

---

## 六、复现方式

```bash
# 全部 4 个引擎对比（90s 音频，约 4 分钟）
python -u tests/asr_engine_benchmark.py --limit-seconds 90

# 只跑指定引擎
python -u tests/asr_engine_benchmark.py --limit-seconds 90 --engines fw_small,pf

# 更快出结果（30s）
python -u tests/asr_engine_benchmark.py --limit-seconds 30
```

依赖：`funasr`、`modelscope`（已装），模型首次运行自动从 ModelScope 下载到
`~/.cache/modelscope/`（SenseVoiceSmall 与 paraformer-large 已缓存）。
