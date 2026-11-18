# dy-video V3｜工程实施任务单（建议拆分 8 个 PR）

本文件是**待实施的工程计划**。开发 Agent 对每个 PR 只改相关模块并补充回归，所有改动须经过用户审查后合并。先不要把设计文件复制覆盖正在运行的 V2。

## PR-01（P0）｜镜头边界正确接入

**入口：** `src/tools/detect_scenes.py`、`src/tools/build_plan.py`、`src/agent/agent_director.py`。  
**现状证据：** `detect_scene_cuts` NMS 当前比较最大值包含自身，不能用 `s > max(...)` 替换邻近的最强点；`build_plan` 接收 `scene_cuts` 参数却未消费。  
**任务：** 修 NMS 聚类，每个 `min_gap_sec` 区域选择最高 cut score；多源边界按 source_id 保存；将 scene cuts 加入候选起止边界集合，不能混淆不同源时间轴；保留 V2 旧行为 flag。  
**新增测试：** 双峰相邻 cut 保留更强者；长视频 600 秒后逻辑可扩展；相同数值/空帧；多源两个时间轴不会跨源切段。  
**验收：** 提供最小模拟输入输出、候选边界前后对照及 `scripts/run_regression.py --quick` 真正运行输出。

## PR-02（P0）｜商品档案自动生成 + EXE 新配置

**入口：** `src/tools/desktop_spec_creator.py`、`scripts/generate_from_job_spec.py`、`contracts/job_spec.schema.json`。  
**新增：** `src/core/product_profile.py`、`schemas/product_profile_v3.schema.json`、spec migrator。  
**任务：** 支持 SKU、品类、属性、允许变体、卖点、参考图、自动化质量策略；自动生成 `product_profile.json`，模型推断不覆盖用户事实；保持旧 spec 解析与默认值；V3 严格模式商品图缺失可阻断。  
**测试：** 旧五类 spec 不变；路径重定位；空/重复 SKU；无图 strict 与 limited；商品图 hash 更新触发缓存失效。

## PR-03（P0）｜完整语句与证据候选层

**入口：** `src/core/asr_engine.py`、`src/tools/build_dialogue_script.py`、`src/tools/build_plan.py`。  
**新增：** `utterance_aligner.py`、`evidence_store.py`、`candidate_generator.py`。  
**任务：** 提供 source-relative `start_ms/end_ms` 统一时间基；将 dialogue_script 用作 S4 真实输入；可选字词对齐（WhisperX 应先在中文直播评测）；输出候选的语义/镜头/动作边界与证据 ID；对齐失败不得声称词级精度。  
**测试：** 不截断中文意群；声源切换；字幕映射一致；对齐失败可安全回退；母集来源 hash 正确。

## PR-04（P0）｜SKU 关系识别与视觉证据

**新增：** `sku_tracker.py`、`vision_backend.py`、`sku_intervals.json`。  
**任务：** 结合用户商品图、文本指代、相邻画面和换款事件，输出 SAME_SKU/SAME_SKU_VARIANT/ACCESSORY_OR_STYLING/OTHER_SKU/UNCERTAIN；对单帧难例进行多帧复核；视觉能力不存在时标 UNVERIFIABLE；策略 strict 阻止误选。  
**测试：** 上衣搭裤子、换另一件毛衣、同色不同款、同款不同色、模特身穿他款手持本品、临时离场、商品图不存在。

## PR-05（P1）｜多模态重排与全局编排

**新增：** `candidate_ranker.py`、`sequence_optimizer.py`、`candidate_scores.schema.json`、`sequence_proposals.schema.json`。  
**任务：** 硬过滤与软评分分离；基于合法 candidate IDs 生成多草案；用 Beam Search 首版替换单纯配额贪心；保留 greedy baseline；新序列经过旧 `DecisionGateway` / SKU Gate。  
**测试：** 同源重叠禁入、卖点覆盖、跨源合法、时间约束、重复惩罚、相邻关系影响排序、无候选优雅失败。量化比对 P@10/连贯评分。

## PR-06（P0）｜AGY Headless + 状态机 + EXE 一键启动

**新增：** `runtime/job_runner.py`、`runtime/adapters/agy_headless.py`、`runtime/adapters/agent_protocol.py`。  
**任务：** EXE 启动独立 Job Runner；运行 `agy -p` 结构化输出；解析 `status` 与 `structured_output`；JSON Schema、候选 ID、文件校验；checkpoint、重试、取消、超时、日志、首次登录探测。保持无需 AGY 的 V2 兼容路径。  
**特别测试：** AGY 不存在、未登录、软拒绝权限但退出码 0、JSON 错误、超时、CANCELED、工作目录包含中文和空格、WSL 路径映射、打包后资源路径。  
**安全要求：** 不默认 `--dangerously-skip-permissions`，不借用非官方代理，不出本机上传原始视频。

## PR-07（P1）｜切点、动作与声音衔接

**新增：** `cut_optimizer.py`、`continuity_qc.py`、低清 preview 工具。  
**任务：** 句意/字词/相对气口/动作阶段/音频跳变联合确定切点；默认硬切+微音频修整；有条件 J/L Cut；时间轴变化同步字幕/违规处理/特效；局部修复后复验。  
**测试：** 半个词、句末停顿、衣服拉展开到一半、跨源噪声陡变、可辨口型时禁止语音跨镜、音轨采样率变化、字幕不越界、画面无黑帧。

## PR-08（P0）｜内容 QC、SOP/Skill 与黄金数据集

**新增：** `qc/editorial_qc.py`、golden set manifest、分片对照回归，正式 `.agents/skills/live-ecommerce-video-editor/SKILL.md` 与 `workflows/standard_pipeline_v3.md`。  
**任务：** 定义商品准确、卖点/画面匹配、句意完整、动作完整、跨片音频与转场视觉自然度的量化/人工盲评方案；脚本报告实际执行覆盖率；整体 end-to-end 可中断恢复；建立能力开关与 A/B；对齐所有提示词版本。  
**测试：** 至少 30 个建议起点的多类型任务金标；真实媒体回归、负向毒化测试、自动化成功/安全阻断情况；记录性能和模型额度趋势。

## 统一 PR 完成清单

- [ ] 现有行为、失败用例和改动边界明晰。
- [ ] 新增 JSON Schema、消费方与兼容迁移测试。
- [ ] 所有 Agent 写回数据均通过结构、候选与时间戳强校验。
- [ ] 新功能关掉时 V2 原始调用链不退化。
- [ ] 快速回归实际执行且记录命令和输出；不能把未跑的媒体回归写成 PASS。
- [ ] 真实素材测试用例、质量指标对照和反向校验均有证据。
- [ ] 安全权限、隐私、账号登录、缓存失效与打包资源经验证。
- [ ] 文档说明新的状态码、失败回退、用户界面与回滚方式。

## 推荐 AGY 首条工程实现指令

读取 `AGENTS.md`、本文件、V3 升级方案和 V2 SOP。只实施 **PR-01**：先复现并修复 `detect_scenes.py` 的 NMS 错误，使镜头边界按源参与候选生成。先增加失败测试，确认最小 patch，再运行快速回归。禁止引入新大模型、改写 EXE 或调整无关音视频门禁。交付修改文件清单、真实测试结果、已知风险和下一 PR 的依赖。
