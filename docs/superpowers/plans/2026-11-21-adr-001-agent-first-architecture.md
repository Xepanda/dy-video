# ADR-001: V3.2 Agent-First 架构决策与变更影响图

- **日期**: 2026-11-21
- **状态**: ACCEPTED (已签署并冻结)
- **基线**: PR #16 (`b9645798e2b2bd1b9019072a80c2cc341e3b6c4a`)
- **执行人**: 主开发 Agent / 总集成负责人

---

## 1. 架构决策与控制权归属

### 1.1 核心原则
- **EXE = 无 Agent 依赖的任务配置与交接器**:
  负责项目/素材点选、输入校验、原子保存 `素材/job_spec.json`、生成根目录 `AGENT_TASK.md`，提供“复制任务指令”与“打开项目目录”；绝不隐式启动 AGY/Codex/Claude 进程，亦不在后台执行完整的媒体流水线。
- **外部 Agent = 工作流总导演**:
  用户在已有会话（AGY / Codex / Claude Code）中直接接收任务指令，作为总导演主动调用统一的 Python CLI，完成数据分析、审查候选帧与证据、生成并提交 `sequence_proposal.json`、处理 QC 反馈并执行修复。
- **Python CLI = 确定性媒体执行与质量门禁**:
  提供 `prepare`, `analyze`, `validate-plan`, `render`, `qc`, `deliver`, `status` 阶段化子命令。硬门禁（双层 QC、白名单校验、指纹匹配、时间线防重叠、SKU 身份证据）由工具端强制执行，Agent 的语言声明不能替代工具签名。
- **跨平台与通用性**:
  移除 `agent_provider=agy_headless` 的强制依赖。所有外部 Agent 只要具备项目与源码终端执行能力，共用相同的阶段契约与 Schema。
- **用户数据与历史成片安全**:
  绝不移动、重命名、覆盖或删除已有成片与原始视频；所有中间态写入 `work/<run_id>/`，最终产物写入 `output/<run_id>/`。

---

## 2. 代码调用图与变更影响分析

### 2.1 依赖追踪 (Grep Trace)
1. **`AgyHeadlessAdapter`**:
   - `src/runtime/job_runner.py`: 默认构造 `agent_adapter = AgyHeadlessAdapter()` 并调用 `check_availability()`。在 CI 无 AGY 环境下造成提前阻断。
   - `src/runtime/adapters/agy_headless.py`: 子进程调用类，包含 `Popen(PIPE)` 死锁隐患。
   - `scripts/run_local_project3_acceptance.py`: 本地脚本引用。
   - `tests/test_job_runner_and_adapters.py`, `tests/test_pr16_regressions.py`: 适配器单测。
2. **`agent_adapter` & `invoke`**:
   - `src/agent/agent_director.py`: 内部 `agent_adapter.invoke(...)`，若无适配器则隐式调用 `SequenceOptimizer` 自动出片并签署交付。
   - `src/runtime/job_runner.py`: 保存并转发 adapter。
3. **`start_pipeline` / `start_pipeline_async`**:
   - `src/tools/desktop_spec_creator.py`: GUI 第 5 步按钮直接调用，绑定 `JobRunner` 启动后台线程。
   - `tests/test_desktop_spec_creator.py`, `tests/test_pr16_round4_p0_p1.py`: 测试用例。
4. **`run_director_workflow`**:
   - `src/agent/agent_director.py`: 包含完整的端到端巨型流程（从 ASR 到双 QC）。
   - `scripts/generate_from_job_spec.py`: 旧版一键脚本。
   - `src/runtime/job_runner.py`: 委托给 `AgentDirector.run_director_workflow`。
5. **`agent_provider`**:
   - `contracts/job_spec.schema.json`: 默认 `"agy_headless"`。
   - `src/core/product_profile.py`: 读取 automation 配置。

### 2.2 模块留/改/退役划分
| 模块类别 | 涉及文件 | 处理策略 | 详细说明 |
|---|---|---|---|
| **必须保留的硬门禁与算法** | `src/core/candidate_generator.py`<br>`src/core/candidate_ranker.py`<br>`src/core/sequence_optimizer.py`<br>`src/core/sku_tracker.py`<br>`src/core/frame_provider.py`<br>`src/core/asr_engine.py`<br>`src/core/video_assembler.py`<br>`src/agent/decision_gateway.py`<br>`src/core/plan_compiler.py`<br>`src/qc/*` | **保留并暴露** | 算法层完全保留；修复 FFmpeg 死锁与 SKU 弱证据放行漏洞；提取为阶段 CLI 供外部 Agent 调用。 |
| **重构与适配** | `src/tools/desktop_spec_creator.py`<br>`src/runtime/job_runner.py`<br>`src/agent/agent_director.py`<br>`contracts/job_spec.schema.json`<br>`src/core/project_context.py` | **改造** | EXE 变为任务生成器；JobRunner 不再启动 Agent；Director 拆分阶段；Schema 升级 v3.2；ProjectPaths 彻底隔离素材与工作目录。 |
| **退役与清理** | `src/runtime/adapters/agy_headless.py`<br>`prompts/agent_bootstrap.md` (旧版 Headless)<br>旧 GUI 一键剪辑调用 | **退役/解耦** | 先解除主干代码对 AGY 的强绑定；测试通过且引用归零后，在 A05/A09 阶段安全删除。 |

---

## 3. 既有缺陷与 CI 失败机理分析

### 3.1 CI 33/35 失败机理
- **根因**: GitHub Actions Linux 容器中未预装 `agy` 命令行。在 `test_pr16_round3_p0_p1.py` 与 `test_pr16_round4_p0_p1.py` 中，测试初始化了默认 `JobRunner`，其内部 `self.agent_adapter.check_availability()` 返回 `(False, "未找到 agy...")`，使流程在 PRECHECK 阶段直接返回 `AGENT_AUTH_REQUIRED`，未能进入被 mock 的 `AgentDirector`。
- **解法**: `JobRunner` 移除默认 `AgyHeadlessAdapter`；单测直接采用 agent-free 模式或显式注入 mock，消除对系统环境变量中 `agy` 路径的隐式依赖。

### 3.2 `job_spec` 位于 `素材/` 时的路径计算缺陷
- **缺陷表现**: 当用户配置文件位于 `<proj_root>/素材/job_spec.json` 时，旧版 `scripts/generate_from_job_spec.py` 取 `os.path.dirname(spec_path)`，计算出 `proj_root` 为 `<proj_root>/素材`，导致 `work/` 与 `output/` 被写入 `<proj_root>/素材/work` 和 `<proj_root>/素材/output`，污染用户原素材目录。
- **修复**: `ProjectPaths` 增强解析：若指定路径的 `basename` 为 `素材` / `materials` / `input`，或包含 `job_spec.json`，则父级目录作为真正的 `project_root`。所有生成物严禁落入 `materials_dir`。

### 3.3 FFmpeg 管道死锁风险
- **缺陷表现**: `video_assembler.py` 中 `Popen(stdout=PIPE, stderr=PIPE)`，先 `poll()` 循环检测退出，退出后才调用 `communicate()`。若 FFmpeg 输出日志超过操作系统管道缓冲区（通常 64KB），子进程因写管道阻塞，父进程因等待子进程退出而阻塞，造成永久死锁。
- **修复**: 使用多线程实时消费 stdout/stderr，或重定向至临时文件，并接入超时与进程树终止 (`kill_process_tree`)。

---

## 4. 回滚与安全策略
1. **Git 分支保护**: 所有修改在分支 `feat/v3-agent-directed-ecommerce-editor` 上提交，保留完整 git log，不进行破坏性 force push。
2. **渐进式解耦**: 先新增 `src/cli.py` 与阶段契约，在确保所有单元测试与回归测试通过后，再逐步弃用旧入口，绝不一次性大面积物理删除。
3. **本地素材绝对保护**: 对 `project-3` 先进行只读哈希存证，任何操作不得修改用户原始文件。
