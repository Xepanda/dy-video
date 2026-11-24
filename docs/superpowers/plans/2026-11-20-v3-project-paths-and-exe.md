# Implementation Plan: dy-video PR #16 V3.1 项目化存储、EXE 点选体验与本地验收

- **Date:** 2026-11-20
- **Baseline Commit:** `5b1ff74d0eb438690d97e46806f0b1fd6b108cc7`
- **Branch:** `feat/v3-agent-directed-ecommerce-editor`

---

## 阶段一：项目目录、输入白名单、文件安全与历史成片隔离

### 目标
1. 创建统一的 `ProjectContext` 与 `ProjectPaths` 模块 (`src/core/project_context.py`)。
2. 明确项目根目录、`素材/`、`work/<run_id>/`、`output/<run_id>/` 的隔离职责。
3. 严格实现输入白名单 `input_manifest.json`，确保每个源素材生成稳定的、不含路径字符的 `source_id` 与 `candidate_id`。
4. 彻底防止历史成片（如 `{project_id}.mp4`, `*_final.mp4` 等）混入输入素材列表。
5. 消除 `AgentDirector` 的破坏性副作用：禁止向 `素材/` 写输出 MP4，禁止删除输入目录文件，禁止占位图写回 spec。
6. 调整 `JobRunner`, `desktop_spec_creator`, `generate_from_job_spec` 使用统一项目路径。

### 具体修改文件
- `src/core/project_context.py` (新增)
- `src/core/candidate_generator.py` (确保 source_id 不含路径字符)
- `src/core/frame_provider.py` (确保关键帧文件名安全)
- `src/agent/agent_director.py` (移除写回 spec、移除向素材目录复制/删除成片、采用 ProjectContext 路径)
- `src/runtime/job_runner.py` (接入 ProjectContext、管理 work/<run_id> 和 output/<run_id>)
- `scripts/generate_from_job_spec.py` (升级收集与路径解析)
- `tests/test_project_context.py` (新增回归测试)

---

## 阶段二：Agent 调度、SKU 识别、Schema、QC 和缓存整改

### 目标
1. 修复 AGY 调用时序：将 AGY 编排移至真实候选生成与精排之后（S4/S5），仅传递 `eligible=True` 且商品关系合规的候选白名单。
2. 校验 Agent 提案：严格限制候选 ID 必须来自白名单，检查同源时间重叠、排序、总时长。
3. 严格 SKU 模式与搭配/变体验证：`SkuTracker` 严格模式下，即使命中变体词或搭配词，也必须验证视觉依据，不得直接标记合规。
4. 修复缓存复用漏洞：实现 `job_fingerprint`，绑定原始视频指纹、商品图哈希、配置及代码版本；配置变更不得复用旧成片。
5. 分离商品事实与字幕高亮词：`product_profile` 不得将 `highlight_keywords` 自动升级为可信 `selling_points`；EXE 移除虚假默认材质。
6. 双层质检 Fail-Closed 机制：`editorial_qc` 异常或报告缺失时，任何策略下 `passed` 必须为 `False`，禁止签发 `DELIVERED`。
7. 修复 CI 测试：完善 `tests/test_job_runner_and_adapters.py`，完整 mock AGY 探测与输出契约。

### 具体修改文件
- `src/core/sku_tracker.py`
- `src/core/product_profile.py`
- `src/runtime/job_runner.py`
- `src/agent/agent_director.py`
- `src/runtime/adapters/agy_headless.py`
- `tests/test_job_runner_and_adapters.py`
- `tests/test_pr16_regressions.py`

---

## 阶段三：EXE 点选式交互、后台执行、进度与完成页

### 目标
1. 将 `desktop_spec_creator.py` 改造为引导式项目向导：
   - 步骤一：最近项目 / 选择项目 / 新建项目。
   - 步骤二：素材确认列表（自动排查旧成片，显示视频缩略图、时长、复选框；商品图多选/设为主图）。
   - 步骤三：商品信息点选化（商品名必填，品类下拉，变体/卖点标签点选，时长/风格/字幕/封面卡片化）。
   - 步骤四：一键全自动后台剪辑，非阻塞异步进度轮询，可取消。
   - 步骤五：完成页（播放成片、打开目录、查看 QC 报告、重新剪辑）。
2. 提供清晰友好的错误引导（未安装 AGY 引导、缺商品图引导）。

### 具体修改文件
- `src/tools/desktop_spec_creator.py`
- `tests/test_desktop_spec_creator.py`

---

## 阶段四：快速回归、Windows EXE 构建、本地 project-3 验收

### 目标
1. 运行 `scripts/run_regression.py --quick`，确保所有 32+ 测试套件全部通过。
2. 构建并验证 Windows EXE 打包工具。
3. 对 `C:\Users\ePanda\Desktop\project-3` 执行只读预检与盘点，生成输入清单。
4. 执行安全 dry-run 与端到端测试，产物写入全新 `work/<run_id>` 与 `output/<run_id>`。
5. 出具本地验收报告 `local_acceptance_report.md/json`。
6. 更新 README, SOP, Schema。
