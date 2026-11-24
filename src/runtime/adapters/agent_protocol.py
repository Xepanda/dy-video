# -*- coding: utf-8 -*-
"""
agent_protocol.py - 外部 Agent 适配器协议与响应模型（V3 PR-06 核心）
=====================================================================
负责：
  1. 定义统一的 AgentResponse 与 AgentAdapter 抽象基类；
  2. 区分不同失败类型：AUTH_REQUIRED, PERMISSION_DENIED, TIMEOUT,
     INVALID_OUTPUT, ERROR, CANCELED；
  3. 解耦 Python 确定性流水线与外部 Agent 运行器。
"""

from typing import Dict, Any, Optional, Tuple, NamedTuple


class AgentStatus:
    SUCCESS = "SUCCESS"
    AUTH_REQUIRED = "AGENT_AUTH_REQUIRED"
    PERMISSION_DENIED = "AGENT_PERMISSION_DENIED"
    TIMEOUT = "TIMEOUT"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    ERROR = "ERROR"
    CANCELED = "CANCELED"


class AgentAuthRequiredError(RuntimeError):
    """当 Agent 未安装或未登录/凭证失效时抛出。"""
    pass


class AgentPermissionDeniedError(RuntimeError):
    """当 Agent 工具调用权限被软拒绝时抛出。"""
    pass


class AgentResponse:
    """Agent 调用响应封装。"""

    def __init__(
        self,
        status: str,
        structured_output: Optional[Dict[str, Any]] = None,
        raw_output: str = "",
        error_message: str = "",
        exit_code: int = 0,
    ):
        self.status = status
        self.structured_output = structured_output or {}
        self.raw_output = raw_output
        self.error_message = error_message
        self.exit_code = exit_code

    @property
    def is_success(self) -> bool:
        return self.status == AgentStatus.SUCCESS and bool(self.structured_output)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "structured_output": self.structured_output,
            "error_message": self.error_message,
            "exit_code": self.exit_code,
        }


class AgentAdapter:
    """外部 Agent 适配器接口。"""

    def check_availability(self) -> Tuple[bool, str]:
        """检查 Agent 命令行工具与授权状态。返回 (ok, msg)。"""
        raise NotImplementedError

    def invoke(
        self,
        prompt: str,
        schema_path: str,
        work_dir: str,
        timeout_sec: int = 300,
    ) -> AgentResponse:
        """无交互调用 Agent，强制结构化 JSON 输出。"""
        raise NotImplementedError
