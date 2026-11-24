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
import time
import shutil
import subprocess
from typing import Dict, Any, Optional, Tuple, List

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.runtime.adapters.agent_protocol import AgentAdapter, AgentResponse, AgentStatus


def validate_sequence_proposals_schema(data: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """本地校验 AGY 结构化输出是否严格满足 sequence_proposals.schema.json 契约。"""
    errors = []
    if not isinstance(data, dict):
        return False, ["响应必须为字典/JSON对象"]

    # 优先尝试使用 jsonschema 库执行权威校验
    schema_path = os.path.join(PROJECT_ROOT, "contracts", "schemas", "sequence_proposals.schema.json")
    if os.path.isfile(schema_path):
        try:
            import jsonschema
            with open(schema_path, "r", encoding="utf-8") as sf:
                schema_def = json.load(sf)
            validator = jsonschema.Draft202012Validator(schema_def)
            for err in validator.iter_errors(data):
                errors.append(f"{err.json_path}: {err.message}")
            if errors:
                return False, errors
            return True, []
        except ImportError:
            pass
        except Exception as e:
            # jsonschema 校验异常时回退到内建严格校验
            pass

    # 内建强校验（严格对标 sequence_proposals.schema.json）
    ver = data.get("schema_version")
    if ver != "3.0":
        errors.append(f"schema_version 必须为 '3.0'，当前为: '{ver}'")

    status = data.get("status")
    if status not in ("PROPOSED", "INSUFFICIENT_EVIDENCE"):
        errors.append(f"status 必须为 'PROPOSED' 或 'INSUFFICIENT_EVIDENCE'，当前为: '{status}'")

    gap = data.get("evidence_gap")
    if not isinstance(gap, list):
        errors.append("'evidence_gap' 必须为列表")
    elif not all(isinstance(g, str) for g in gap):
        errors.append("'evidence_gap' 中的元素必须全部为字符串")

    proposals = data.get("proposals")
    if not isinstance(proposals, list):
        errors.append("'proposals' 必须为列表")
    else:
        if status == "PROPOSED" and len(proposals) == 0:
            errors.append("status 为 PROPOSED 时，'proposals' 必须为非空列表")

        for idx, prop in enumerate(proposals):
            if not isinstance(prop, dict):
                errors.append(f"proposals[{idx}] 必须为对象")
                continue

            # 必填字段检查
            required_keys = [
                "proposal_id", "style", "ordered_candidate_ids",
                "expected_duration_ms", "selling_point_coverage",
                "narrative_rationale", "risk_notes", "confidence"
            ]
            for rk in required_keys:
                if rk not in prop:
                    errors.append(f"proposals[{idx}] 缺少必填字段 '{rk}'")

            # 字段类型与值域检查
            if not isinstance(prop.get("proposal_id"), str) or not prop.get("proposal_id"):
                errors.append(f"proposals[{idx}].proposal_id 必须为非空字符串")

            style = prop.get("style")
            if style not in ("balanced", "fast_paced", "showcase", "fast", "detailed"):
                errors.append(f"proposals[{idx}].style 必须为 'balanced', 'fast_paced', 'showcase', 'fast' 或 'detailed'，当前为: '{style}'")

            cids = prop.get("ordered_candidate_ids")
            if not isinstance(cids, list) or len(cids) == 0:
                errors.append(f"proposals[{idx}].ordered_candidate_ids 必须为非空列表")
            elif not all(isinstance(c, str) and c for c in cids):
                errors.append(f"proposals[{idx}].ordered_candidate_ids 元素必须为非空字符串")

            exp_dur = prop.get("expected_duration_ms")
            if not isinstance(exp_dur, int) or exp_dur < 0:
                errors.append(f"proposals[{idx}].expected_duration_ms 必须为非负整数")

            for list_field in ("selling_point_coverage", "narrative_rationale", "risk_notes"):
                lv = prop.get(list_field)
                if not isinstance(lv, list):
                    errors.append(f"proposals[{idx}].{list_field} 必须为列表")
                elif not all(isinstance(x, str) for x in lv):
                    errors.append(f"proposals[{idx}].{list_field} 元素必须为字符串")

            conf = prop.get("confidence")
            if not isinstance(conf, (int, float)) or conf < 0 or conf > 1:
                errors.append(f"proposals[{idx}].confidence 必须为 0~1 之间的浮点数")

    return len(errors) == 0, errors


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
        cancellation_token: Optional[Any] = None,
    ) -> AgentResponse:
        """执行无交互结构化决策生成，支持取消令牌实时中断。"""
        if cancellation_token:
            is_c = getattr(cancellation_token, "is_canceled", False)
            if callable(is_c):
                is_c = is_c()
            if is_c:
                return AgentResponse(
                    status=AgentStatus.ERROR,
                    error_message="AGY 调用已被用户取消",
                    exit_code=-5,
                )

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

        proc = None
        raw_out, raw_err = "", ""
        returncode = 0
        try:
            if cancellation_token:
                proc = subprocess.Popen(
                    cmd,
                    cwd=work_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    shell=(os.name == "nt"),
                    encoding="utf-8",
                    errors="replace",
                )
                if hasattr(cancellation_token, "register_process"):
                    cancellation_token.register_process(proc)

                start_t = time.time()
                while True:
                    is_c = getattr(cancellation_token, "is_canceled", False)
                    if callable(is_c):
                        is_c = is_c()
                    if is_c:
                        try:
                            from src.runtime.job_runner import terminate_process_tree
                            terminate_process_tree(proc)
                        except Exception:
                            try:
                                proc.kill()
                            except Exception:
                                pass
                        return AgentResponse(
                            status=AgentStatus.ERROR,
                            error_message="AGY 调用已被用户取消",
                            exit_code=-5,
                        )

                    ret = proc.poll()
                    if ret is not None:
                        raw_out, raw_err = proc.communicate()
                        returncode = ret
                        break

                    if time.time() - start_t > timeout_sec + 15:
                        try:
                            from src.runtime.job_runner import terminate_process_tree
                            terminate_process_tree(proc)
                        except Exception:
                            try:
                                proc.kill()
                            except Exception:
                                pass
                        return AgentResponse(
                            status=AgentStatus.TIMEOUT,
                            error_message=f"AGY 调用超时 ({timeout_sec}s)",
                            exit_code=-3,
                        )
                    time.sleep(0.05)
            else:
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
                raw_out = proc.stdout or ""
                raw_err = proc.stderr or ""
                returncode = proc.returncode
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
        finally:
            if proc and cancellation_token and hasattr(cancellation_token, "unregister_process"):
                cancellation_token.unregister_process(proc)

        try:
            with open(os.path.join(work_dir, "agy_debug.log"), "w", encoding="utf-8") as f:
                f.write(f"=== RETCODE ===\n{returncode}\n\n")
                f.write(f"=== STDOUT ===\n{raw_out}\n\n")
                f.write(f"=== STDERR ===\n{raw_err}\n\n")
        except Exception:
            pass

        # 检查是否命中未授权/登录态过期
        lower_err = (raw_out + " " + raw_err).lower()
        if "not logged in" in lower_err or "login required" in lower_err or "auth" in lower_err and returncode != 0:
            return AgentResponse(
                status=AgentStatus.AUTH_REQUIRED,
                raw_output=raw_out,
                error_message="AGY 未登录或凭证过期，请先在终端运行 agy login 进行官方授权",
                exit_code=returncode,
            )
        if "permission denied" in lower_err or "access denied" in lower_err:
            return AgentResponse(
                status=AgentStatus.PERMISSION_DENIED,
                raw_output=raw_out,
                error_message="AGY 工具执行权限被软拒绝",
                exit_code=returncode,
            )

        if returncode != 0:
            return AgentResponse(
                status=AgentStatus.ERROR,
                raw_output=raw_out,
                error_message=f"AGY 退出码非零 ({returncode}): {raw_err.strip()}",
                exit_code=returncode,
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
                for k in ("schema_version", "status", "proposals", "evidence_gap"):
                    if k in parsed and k not in structured:
                        structured[k] = parsed[k]

            # 契约 Schema 与业务有效性强校验
            is_valid, schema_errs = validate_sequence_proposals_schema(structured)
            if not is_valid:
                return AgentResponse(
                    status=AgentStatus.INVALID_OUTPUT,
                    raw_output=raw_out,
                    structured_output=structured,
                    error_message=f"AGY 结构化输出契约校验失败: {'; '.join(schema_errs)}",
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
