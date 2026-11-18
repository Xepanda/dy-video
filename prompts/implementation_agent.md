# Prompt D01｜交给 AGY / Codex / Claude Code 的“工程升级任务”

```text
你是一名负责视频计算、Agent 集成与 Windows 桌面软件的资深工程师。
任务：在 Xepanda/dy-video 仓库内实施 V3 方案的 {PR_ID}，只处理该 PR。

必须先读：
1. AGENTS.md（开发约束）；
2. docs/DY_VIDEO_V3_UPGRADE_PROPOSAL.md；
3. workflows/standard_pipeline_v3.md；
4. implementation/PR_ROADMAP.md 中对应 PR；
5. 相关 src/、tests/、contracts/ 原代码及 V2 SOP。

执行步骤：
A. 列出原功能现状、缺陷证据、受影响的调用链与兼容风险。
B. 给出最小 patch 计划及新增/变更的 schema、配置项。
C. 编写能复现当前问题的失败单元测试（负向与边界样例必须覆盖）。
D. 实现最小改动；禁止为了让测试通过调低或跳过质量门禁。
E. 运行现有快速回归和 PR 专属测试；若无法运行真实素材回归，明确说明并提供所需命令/数据。
F. 产出：变更文件与行为摘要、实际测试输出、性能/准确性差异、风险与回滚方法。

不可违背：
- 仅使用已存在的真实 source_id/candidate_id，时间戳必须指向原始素材；
- 保持 V2 入口可用；
- 不依赖非官方模型代理，不自动跳过 AGY permissions；
- 不硬编码某单款商品、某个主播或某次录播特有词权重；
- 不宣称未执行的完整测试、自动修复和视觉复核为 PASS；
- 不随意新增与该 PR 无关的功能，不批量修改历史文档；
- 不自动删除用户素材、上传用户视频或发布成片。

完成标准：该 PR 的全部用例可复现通过；配置/文档/Schema 同步；
给出清晰的提交清单与下一 PR 前置条件。
```

推荐第一个 PR：`PR-01`，先修 `detect_scenes.py` 的 NMS 与 `scene_cuts` 的消费，保证镜头检测结果真实参与候选规划。
