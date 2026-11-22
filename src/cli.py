# -*- coding: utf-8 -*-
"""
src.cli - dy-video V3.2 统一阶段 CLI 契约入口
=============================================
支持外部 Agent (AGY / Codex / Claude Code) 独立调度的标准化工具：
  python -m src.cli prepare --spec <path_to_job_spec.json>
  python -m src.cli analyze --run <work_dir_or_run_context>
  python -m src.cli validate-plan --run <work_dir> --proposal <proposal.json>
  python -m src.cli render --run <work_dir>
  python -m src.cli qc --run <work_dir>
  python -m src.cli deliver --run <work_dir>
  python -m src.cli status --run <work_dir>

契约规范：
  - 标准输出 (stdout) 唯一定向输出可直接被 Agent 解析的纯结构化 JSON；
  - 过程文本、进度与详细日志重定向至对应的 run/stage 日志与 stderr；
  - 发生任何错误、校验失败或硬门禁阻断时，返回非零退出码 (1 / 2)。
"""

import os
import sys
import json
import argparse
from typing import Dict, Any

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from src.runtime.stage_services import (
    stage_prepare,
    stage_analyze,
    stage_validate_plan,
    stage_render,
    stage_qc,
    stage_deliver,
    stage_status,
)


def _output_json(payload: Dict[str, Any], exit_code: int = 0) -> None:
    try:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f'{{"error": "JSON 序列化异常: {str(e)}"}}')
    sys.exit(exit_code)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.cli",
        description="dy-video V3.2 Agent-First 统一媒体处理与质量门禁 CLI",
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="子命令列表")

    # 1. prepare
    p_prepare = subparsers.add_parser("prepare", help="初始化任务运行环境、素材白名单与快照")
    p_prepare.add_argument("--spec", required=True, help="job_spec.json 任务契约路径")
    p_prepare.add_argument("--run-id", default=None, help="显式指定运行编号（可选）")

    # 2. analyze
    p_analyze = subparsers.add_parser("analyze", help="执行 ASR、分镜抽帧、候选片段提取与冻结")
    p_analyze.add_argument("--run", required=True, help="work/<run_id> 目录或 run_context.json 路径")

    # 3. validate-plan
    p_validate = subparsers.add_parser("validate-plan", help="审计外部 Agent 提交的分镜提案")
    p_validate.add_argument("--run", required=True, help="work/<run_id> 目录路径")
    p_validate.add_argument("--proposal", required=True, help="Agent 生成的 sequence_proposal.json 路径")

    # 4. render
    p_render = subparsers.add_parser("render", help="基于通过审计的计划渲染成片与封面")
    p_render.add_argument("--run", required=True, help="work/<run_id> 目录路径")

    # 5. qc
    p_qc = subparsers.add_parser("qc", help="执行工程技术 QC 与内容 Editorial QC 双层质检门禁")
    p_qc.add_argument("--run", required=True, help="work/<run_id> 目录路径")

    # 6. deliver
    p_deliver = subparsers.add_parser("deliver", help="核验双质检通过与文件指纹，签发正式交付清单")
    p_deliver.add_argument("--run", required=True, help="work/<run_id> 目录路径")

    # 7. status
    p_status = subparsers.add_parser("status", help="查询任务阶段进度与状态信息")
    p_status.add_argument("--run", required=True, help="work/<run_id> 目录路径")

    args = parser.parse_args()

    if not args.subcommand:
        parser.print_help(sys.stderr)
        return 2

    # 将运行过程中的所有库日志/打印重定向至 stderr，确保 stdout 仅输出面向 Agent 的纯 JSON
    real_stdout = sys.stdout
    sys.stdout = sys.stderr

    try:
        res = None
        exit_code = 0

        if args.subcommand == "prepare":
            res = stage_prepare(spec_path=args.spec, run_id=args.run_id)
            exit_code = 0 if res.get("status") == "PREPARED" else 1

        elif args.subcommand == "analyze":
            res = stage_analyze(run_arg=args.run)
            exit_code = 0 if res.get("status") == "ANALYZED" else 1

        elif args.subcommand == "validate-plan":
            res = stage_validate_plan(run_arg=args.run, proposal_path=args.proposal)
            exit_code = 0 if res.get("status") == "PLAN_VALIDATED" else 1

        elif args.subcommand == "render":
            res = stage_render(run_arg=args.run)
            exit_code = 0 if res.get("status") == "RENDERED" else 1

        elif args.subcommand == "qc":
            res = stage_qc(run_arg=args.run)
            exit_code = 0 if res.get("dual_qc_passed") else 1

        elif args.subcommand == "deliver":
            res = stage_deliver(run_arg=args.run)
            exit_code = 0 if res.get("status") == "DELIVERED" else 1

        elif args.subcommand == "status":
            res = stage_status(run_arg=args.run)
            exit_code = 0

        sys.stdout = real_stdout
        _output_json(res, exit_code=exit_code)

    except Exception as e:
        sys.stdout = real_stdout
        err_res = {
            "schema_version": "3.2",
            "status": "ERROR",
            "subcommand": args.subcommand,
            "error_type": type(e).__name__,
            "error_message": str(e),
        }
        _output_json(err_res, exit_code=1)

    return 0


if __name__ == "__main__":
    sys.exit(main())
