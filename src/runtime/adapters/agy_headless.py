# -*- coding: utf-8 -*-
"""
agy_headless.py - Antigravity (AGY) 官方无交互 Headless 适配器（V3 PR-06 核心）
=============================================================================
负责：
  1. 调用官方 `agy -p` 命令行，使用 `--output-format json` 与 `--json-schema` 接收决策；
  2. 探测 AGY 本地安装、未登录、授权过期及权限软拒绝状态；
  3. 异常捕获：超时、进程退出码、非合法 JSON 与 Schema 违规；
  4. 绝不默认使用 `--dangerously-skip-permissions`，严守安全基线；
  5. 兼容 Windows 包含中文与空格的工作目录及 WSL 路径。
"""

import os
import sys
import re
import json
import shutil
import subprocess
from typing import Dict, Any, Optional, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.runtime.adapters.agent_protocol import AgentAdapter, AgentResponse, AgentStatus


class AgyHeadlessAdapter(AgentAdapter):
    """Google Antigravity CLI 官方无交互进程适配器。"""

    def __init__(self, agy_executable: Optional[str] = None):
        self.agy_bin = agy_executable or shutil.which("agy") or "agy"

    def check_availability(self) -> Tuple[bool, str]:
        """探测 AGY CLI 命令是否存在及可执行。"""
        resolved = shutil.which(self.agy_bin)
        if not resolved and not os.path.isfile(self.agy_bin):
            return False, f"AGY 命令行工具未找到: '{self.agy_bin}'。请确认已安装 Antigravity CLI 并加入 PATH。"

        try:
            res = subprocess.run(
                [self.agy_bin, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                shell=(os.name == "nt"),
            )
            if res.returncode == 0:
                ver = (res.stdout or "").strip()
                return True, f"AGY 可用: {ver}"
            else:
                return False, f"AGY 探测返回码非零: {res.stderr}"
        except subprocess.TimeoutExpired:
            return False, "AGY --version 探测超时"
        except Exception as e:
            return False, f"AGY 探测异常: {str(e)}"

    def invoke(
        self,
        prompt: str,
        schema_path: str,
        work_dir: str,
        timeout_sec: int = 300,
    ) -> AgentResponse:
        """执行无交互结构化决策生成。"""
        is_avail, msg = self.check_availability()
        if not is_avail:
            return AgentResponse(
                status=AgentStatus.AUTH_REQUIRED,
                error_message=f"AGY 不可用: {msg}",
                exit_code=-1,
            )

        abs_schema = os.path.abspath(schema_path)
        if not os.path.isfile(abs_schema):
            return AgentResponse(
                status=AgentStatus.ERROR,
                error_message=f"指定的 JSON Schema 不存在: {abs_schema}",
                exit_code=-2,
            )

        cmd = [
            self.agy_bin,
            "-p", prompt,
            "--output-format", "json",
            "--json-schema", abs_schema,
            "--print-timeout", f"{timeout_sec}s",
        ]

        try:
            proc = subprocess.run(
                cmd,
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=timeout_sec + 15,
                shell=(os.name == "nt"),
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            return AgentResponse(
                status=AgentStatus.TIMEOUT,
                error_message=f"AGY 调用超时 ({timeout_sec}s)",
                exit_code=-3,
            )
        except Exception as e:
            return AgentResponse(
                status=AgentStatus.ERROR,
                error_message=f"启动 AGY 进程失败: {str(e)}",
                exit_code=-4,
            )

        raw_out = proc.stdout or ""
        raw_err = proc.stderr or ""

        # 检查是否命中未授权/登录态过期
        lower_err = (raw_out + " " + raw_err).lower()
        if "not logged in" in lower_err or "login required" in lower_err or "auth" in lower_err and proc.returncode != 0:
            return AgentResponse(
                status=AgentStatus.AUTH_REQUIRED,
                raw_output=raw_out,
                error_message="AGY 未登录或凭证过期，请先在终端运行 agy login 进行官方授权",
                exit_code=proc.returncode,
            )
        if "permission denied" in lower_err or "access denied" in lower_err:
            return AgentResponse(
                status=AgentStatus.PERMISSION_DENIED,
                raw_output=raw_out,
                error_message="AGY 工具执行权限被软拒绝",
                exit_code=proc.returncode,
            )

        if proc.returncode != 0:
            return AgentResponse(
                status=AgentStatus.ERROR,
                raw_output=raw_out,
                error_message=f"AGY 退出码非零 ({proc.returncode}): {raw_err.strip()}",
                exit_code=proc.returncode,
            )

        # 解析 JSON 输出（支持直接 JSON、Markdown 代码块与混合日志提取）
        parsed = None
        clean_text = raw_out.strip()
        try:
            parsed = json.loads(clean_text)
        except Exception:
            # 尝试提取 markdown code fence
            m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", clean_text, re.IGNORECASE)
            if m:
                try:
                    parsed = json.loads(m.group(1).strip())
                except Exception:
                    pass
            if parsed is None:
                # 尝试定位最外层 { ... }
                fb = clean_text.find("{")
                lb = clean_text.rfind("}")
                if fb != -1 and lb > fb:
                    try:
                        parsed = json.loads(clean_text[fb:lb+1])
                    except Exception:
                        pass

        if parsed is not None:
            structured = parsed.get("structured_output") if (isinstance(parsed, dict) and "structured_output" in parsed) else parsed
            if not isinstance(structured, dict):
                structured = {"data": structured}
            if isinstance(parsed, dict):
                for k in ("schema_version", "status"):
                    if k in parsed and k not in structured:
                        structured[k] = parsed[k]

            # 基础业务有效性校验
            if "status" not in structured and "proposal_id" not in structured and "proposals" not in structured:
                return AgentResponse(
                    status=AgentStatus.INVALID_OUTPUT,
                    raw_output=raw_out,
                    structured_output=structured,
                    error_message="AGY 结构化输出缺少必要契约字段 (status / proposals)",
                    exit_code=proc.returncode,
                )

            return AgentResponse(
                status=AgentStatus.SUCCESS,
                structured_output=structured,
                raw_output=raw_out,
                exit_code=0,
            )
        else:
            return AgentResponse(
                status=AgentStatus.INVALID_OUTPUT,
                raw_output=raw_out,
                error_message="AGY 未输出合法 JSON 结构",
                exit_code=proc.returncode,
            )
