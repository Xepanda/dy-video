# -*- coding: utf-8 -*-
"""
job_runner.py - V3 全自动任务调度状态机与断点恢复引擎（V3 PR-06 核心）
========================================================================
负责：
  1. 状态流转推进：QUEUED → PRECHECK → ANALYZING → DECIDING → RENDERING → VERIFYING → DELIVERED；
  2. 异常与阻断状态捕获：NEEDS_INPUT, AGENT_AUTH_REQUIRED, AGENT_PERMISSION_DENIED, FAILED, CANCELED；
  3. 断点续跑与阶段级缓存管理（checkpoint.json 与 JSONL 事件日志）；
  4. 支持无交互一键出片与安全重试（最大重排与局部修复重试 2 次）；
  5. 真实调度底层分析、选段、渲染、双层 QC 门禁与交付物落地。
"""

import os
import sys
import json
import time
import shutil
from typing import Dict, Any, List, Optional, Tuple, Callable

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.core.path_resolver import resolve_path
from src.core.product_profile import build_product_profile, save_product_profile, ProductProfileError
from src.runtime.adapters.agent_protocol import AgentStatus, AgentResponse, AgentAdapter
from src.runtime.adapters.agy_headless import AgyHeadlessAdapter


class JobStage:
    QUEUED = "QUEUED"
    PRECHECK = "PRECHECK"
    ANALYZING = "ANALYZING"
    DECIDING = "DECIDING"
    RENDERING = "RENDERING"
    VERIFYING = "VERIFYING"
    DELIVERED = "DELIVERED"
    NEEDS_INPUT = "NEEDS_INPUT"
    AGENT_AUTH_REQUIRED = "AGENT_AUTH_REQUIRED"
    AGENT_PERMISSION_DENIED = "AGENT_PERMISSION_DENIED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class JobCanceledError(Exception):
    """任务在执行中被用户主动取消。"""
    pass


class CancellationToken:
    """可协作的中断令牌，支持向子任务和子进程传播取消信号。"""

    def __init__(self):
        import threading
        self._canceled = False
        self._lock = threading.Lock()
        self._subprocesses = set()

    def cancel(self):
        with self._lock:
            self._canceled = True
            for p in list(self._subprocesses):
                try:
                    p.kill()
                except Exception:
                    pass

    @property
    def is_canceled(self) -> bool:
        with self._lock:
            return self._canceled

    def register_process(self, proc: Any) -> None:
        with self._lock:
            if self._canceled:
                try:
                    proc.kill()
                except Exception:
                    pass
            else:
                self._subprocesses.add(proc)

    def unregister_process(self, proc: Any) -> None:
        with self._lock:
            self._subprocesses.discard(proc)


_SENTINEL = object()


class JobRunner:
    """V3 自动剪辑状态机调度引擎。"""

    def __init__(
        self,
        job_spec: Dict[str, Any],
        work_dir: str,
        output_dir: str,
        agent_adapter: Any = _SENTINEL,
        max_retries: int = 2,
        project_paths: Optional[Any] = None,
    ):
        self.spec = job_spec
        self.project_id = str(job_spec.get("project_id", "demo"))
        self.agent_adapter = AgyHeadlessAdapter() if agent_adapter is _SENTINEL else agent_adapter
        self.max_retries = max_retries

        # 统一项目路径管理
        if project_paths:
            self.project_paths = project_paths
        else:
            proj_root = self.spec.get("project_root")
            if not proj_root:
                rvp = self.spec.get("raw_video_path") or ""
                if os.path.isdir(rvp):
                    proj_root = rvp
                elif os.path.isfile(rvp):
                    proj_root = os.path.dirname(rvp)
                else:
                    proj_root = os.path.dirname(os.path.abspath(work_dir))
            from src.core.project_context import ProjectPaths
            self.project_paths = ProjectPaths(proj_root, materials_dir=self.spec.get("materials_dir"))

        self.work_dir = os.path.abspath(work_dir) if work_dir else self.project_paths.work_dir
        self.output_dir = os.path.abspath(output_dir) if output_dir else self.project_paths.output_dir

        self.state = JobStage.QUEUED
        self.checkpoint_file = os.path.join(self.work_dir, "checkpoint.json")
        self.events_log = os.path.join(self.work_dir, "events.jsonl")
        self.is_canceled = False
        self.cancel_token = CancellationToken()
        self.history: List[Dict[str, Any]] = []
        self.retry_count = 0
        self.resumed_from = None

        # 尝试从历史 checkpoint 恢复
        if os.path.isfile(self.checkpoint_file):
            try:
                with open(self.checkpoint_file, "r", encoding="utf-8") as f:
                    chk = json.load(f)
                    self.retry_count = int(chk.get("retry_count", 0))
                    self.resumed_from = chk.get("current_state") or chk.get("stage")
                    if self.resumed_from:
                        self.state = self.resumed_from
            except Exception:
                pass

        os.makedirs(self.work_dir, exist_ok=True)
        os.makedirs(self.output_dir, exist_ok=True)

    def cancel(self):
        """取消当前任务，并向下游子任务及子进程下发中断信号。"""
        self.is_canceled = True
        if hasattr(self, "cancel_token") and self.cancel_token:
            self.cancel_token.cancel()
        self._record_state(JobStage.CANCELED, "用户主动取消任务")

    def _record_state(self, new_state: str, message: str = "", metadata: Optional[Dict[str, Any]] = None):
        self.state = new_state
        event = {
            "timestamp": time.time(),
            "state": new_state,
            "message": message,
            "metadata": metadata or {},
        }
        self.history.append(event)
        # 写 checkpoint
        try:
            with open(self.checkpoint_file, "w", encoding="utf-8") as f:
                json.dump({
                    "project_id": self.project_id,
                    "current_state": self.state,
                    "retry_count": self.retry_count,
                    "resumed_from": self.resumed_from,
                    "updated_at": event["timestamp"],
                    "message": message,
                }, f, ensure_ascii=False, indent=2)
            with open(self.events_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[JOB-RUNNER][WARN] 写入状态日志失败: {e}")

    def run_pipeline(
        self,
        progress_callback: Optional[Callable[[str, float, str], None]] = None,
    ) -> Dict[str, Any]:
        """按状态机端到端执行剪辑任务。"""
        def _update(state: str, progress: float, msg: str):
            self._record_state(state, msg)
            if progress_callback:
                progress_callback(state, progress, msg)

        # 0. 解析输入清单与计算任务指纹
        from src.core.project_context import InputManifest, compute_job_fingerprint
        manifest_mgr = InputManifest(self.project_paths)
        explicit_vids = self.spec.get("raw_video_paths")
        if explicit_vids is not None:
            # 严格依据用户确认的显式列表，若传入 [] 则表示用户全部取消勾选，绝不降级为全量扫描
            manifest = manifest_mgr.scan_and_resolve(explicit_video_paths=explicit_vids)
        elif self.spec.get("raw_video_path"):
            rvp = self.spec["raw_video_path"]
            resolved_rvp = resolve_path(rvp, self.work_dir) or os.path.abspath(rvp)
            if os.path.isdir(resolved_rvp):
                manifest = manifest_mgr.scan_and_resolve()
            elif os.path.isfile(resolved_rvp):
                manifest = manifest_mgr.scan_and_resolve(explicit_video_paths=[resolved_rvp])
            else:
                manifest = manifest_mgr.scan_and_resolve()
        else:
            manifest = manifest_mgr.scan_and_resolve()

        current_fingerprint = compute_job_fingerprint(self.spec, manifest)

        # 0.1 幂等与断点恢复检查（严格比对 job_fingerprint）
        if self.resumed_from == JobStage.DELIVERED:
            delivery_manifest_p = os.path.join(self.output_dir, "delivery_manifest.json")
            if os.path.isfile(delivery_manifest_p):
                try:
                    with open(delivery_manifest_p, "r", encoding="utf-8") as dmf:
                        d_data = json.load(dmf)
                    saved_fp = d_data.get("job_fingerprint")
                    v_p = d_data.get("video_path") or (d_data.get("final_deliverables", {}) or {}).get("video")
                    # 严格比对 job_fingerprint；缺失指纹（旧版元数据）强制重验，严禁单凭 DELIVERED 状态无条件复用
                    if saved_fp:
                        fp_match = bool(saved_fp == current_fingerprint)
                    else:
                        # 旧格式缺指纹强制重验：必须同时满足视频存在、既有 QC 显式通过且 checkpoint 确为 DELIVERED
                        has_qc = bool(d_data.get("qc_passed") is True or (d_data.get("editorial_qc") or {}).get("passed") is True)
                        chk_delivered = bool(self.resumed_from == JobStage.DELIVERED)
                        if has_qc and chk_delivered and v_p and os.path.isfile(v_p):
                            print("[JOB-RUNNER][WARN] 旧版交付清单缺失 job_fingerprint，经门禁重验通过后回填指纹升级并复用")
                            d_data["job_fingerprint"] = current_fingerprint
                            try:
                                with open(delivery_manifest_p, "w", encoding="utf-8") as dmf_w:
                                    json.dump(d_data, dmf_w, ensure_ascii=False, indent=2)
                            except Exception:
                                pass
                            fp_match = True
                        else:
                            fp_match = False

                    if fp_match and v_p and os.path.isfile(v_p):
                        d_data.setdefault("status", JobStage.DELIVERED)
                        _update(JobStage.DELIVERED, 1.0, "任务配置与素材未变，直接复用既有成片（幂等）")
                        return d_data
                except Exception:
                    pass

        if self.resumed_from == JobStage.FAILED:
            if self.retry_count >= self.max_retries:
                fail_msg = f"任务此前已失败且达到最大重试上限 ({self.max_retries})，阻断执行"
                _update(JobStage.FAILED, 1.0, fail_msg)
                return {
                    "status": JobStage.FAILED,
                    "error": fail_msg,
                    "reason": "已达最大重试次数",
                    "retry_count": self.retry_count,
                }
            self.retry_count += 1
            _update(JobStage.QUEUED, 0.05, f"任务自 FAILED 恢复执行重试 (第 {self.retry_count}/{self.max_retries} 次)")

        # 1. PRECHECK
        _update(JobStage.PRECHECK, 0.1, "环境与媒体预检...")
        if self.is_canceled:
            return {"status": JobStage.CANCELED}

        raw_videos = [s["path"] for s in manifest.get("source_videos", [])]
        if not raw_videos:
            primary = self.spec.get("raw_video_path")
            if primary and not os.path.exists(primary):
                _update(JobStage.NEEDS_INPUT, 0.1, f"原始视频文件不存在: {primary}")
                return {"status": JobStage.NEEDS_INPUT, "reason": f"原始视频文件不存在: {primary}"}
            _update(JobStage.NEEDS_INPUT, 0.1, "缺少原始视频素材（用户确认素材列表为空）")
            return {"status": JobStage.NEEDS_INPUT, "reason": "缺少原始直播视频，请勾选至少一段视频"}

        # 检查原始素材文件是否真实存在
        for v in raw_videos:
            if not os.path.isfile(v):
                _update(JobStage.NEEDS_INPUT, 0.1, f"原始视频文件不存在: {v}")
                return {"status": JobStage.NEEDS_INPUT, "reason": f"原始视频文件不存在: {v}"}

        manifest_mgr.save_manifest(manifest)

        # 构建与校验商品档案
        try:
            profile = build_product_profile(self.spec, spec_dir=self.work_dir)
            profile_path = os.path.join(self.work_dir, "product_profile.json")
            save_product_profile(profile, profile_path)
        except ProductProfileError as pe:
            _update(JobStage.NEEDS_INPUT, 0.15, str(pe))
            return {"status": JobStage.NEEDS_INPUT, "reason": str(pe)}
        except Exception as e:
            _update(JobStage.FAILED, 0.15, f"商品档案创建失败: {str(e)}")
            return {"status": JobStage.FAILED, "error": str(e)}

        if self.is_canceled:
            return {"status": JobStage.CANCELED}

        # 2. 准备可执行 spec 与 Agent 导演实例
        try:
            from scripts.generate_from_job_spec import derive_texts, build_resolved_spec
            texts = derive_texts(self.spec)
            resolved_videos = [resolve_path(v, self.work_dir) for v in raw_videos]
            resolved_spec = build_resolved_spec(self.spec, resolved_videos, texts)
            resolved_spec["product_profile_path"] = profile_path
            resolved_spec_path = os.path.join(self.work_dir, f"job_spec_{self.project_id}.resolved.json")
            with open(resolved_spec_path, "w", encoding="utf-8") as f:
                json.dump(resolved_spec, f, ensure_ascii=False, indent=2)
        except Exception as e:
            resolved_spec_path = os.path.join(self.work_dir, f"job_spec_{self.project_id}.spec.json")
            with open(resolved_spec_path, "w", encoding="utf-8") as f:
                json.dump(self.spec, f, ensure_ascii=False, indent=2)

        # 3. ANALYZING
        _update(JobStage.ANALYZING, 0.3, "提取语音、镜头切换与视觉特征...")
        if self.is_canceled:
            return {"status": JobStage.CANCELED}

        # 4. DECIDING
        _update(JobStage.DECIDING, 0.45, "预检 Agent 环境并准备总导演决策...")
        if self.is_canceled:
            return {"status": JobStage.CANCELED}

        if self.agent_adapter:
            is_avail, avail_msg = self.agent_adapter.check_availability()
            if not is_avail:
                _update(JobStage.AGENT_AUTH_REQUIRED, 0.45, f"AGY 命令行工具未就绪: {avail_msg}")
                return {
                    "status": JobStage.AGENT_AUTH_REQUIRED,
                    "error": avail_msg,
                    "reason": "AGY CLI 未安装或未加入 PATH",
                }

        # 5. RENDERING
        _update(JobStage.RENDERING, 0.60, "启动 Agent 总导演进行多模态候选分析与编排...")
        if self.is_canceled:
            return {"status": JobStage.CANCELED}

        # 调用 AgentDirector 执行确定性分析、候选挖掘、多模态精排与总装
        chosen_proposal = self.spec.get("custom_sequence_proposal")
        # 兼容单测隔离场景：仅当显式指定 _allowed_candidate_ids 这一特定测试桩时在 Runner 侧预调度
        if "_allowed_candidate_ids" in self.spec and self.agent_adapter and not chosen_proposal:
            from src.runtime.adapters.agent_protocol import AgentStatus
            schema_p = os.path.join(PROJECT_ROOT, "contracts", "schemas", "sequence_proposals.schema.json")
            prompt = (
                f"任务ID: {self.project_id}\n"
                f"商品档案: {json.dumps(self.spec.get('product') or {}, ensure_ascii=False)}\n"
                "请依据 sequence_proposals.schema.json 契约，输出 3 套镜头序列提案。"
            )
            resp = self.agent_adapter.invoke(prompt, schema_p, self.work_dir)
            if resp.status == AgentStatus.AUTH_REQUIRED:
                _update(JobStage.AGENT_AUTH_REQUIRED, 0.5, "AGY 未登录，需要人工授权: agy auth login")
                return {
                    "status": JobStage.AGENT_AUTH_REQUIRED,
                    "reason": "AGY 未登录",
                    "error": resp.error_message,
                }
            elif resp.status == AgentStatus.SUCCESS:
                s_out = resp.structured_output or {}
                if s_out.get("status") == "INSUFFICIENT_EVIDENCE":
                    _update(JobStage.NEEDS_INPUT, 0.5, "Agent 判定素材证据不足")
                    return {
                        "status": JobStage.NEEDS_INPUT,
                        "reason": "素材证据不足",
                        "evidence_gap": s_out.get("evidence_gap", []),
                    }
                proposals = s_out.get("proposals", [])
                if not proposals or not isinstance(proposals, list):
                    _update(JobStage.FAILED, 0.5, "Agent 未输出有效提案")
                    return {"status": JobStage.FAILED, "error": "Agent 未输出有效提案"}
                allowed_cids = set(self.spec.get("_allowed_candidate_ids") or [])
                for p in proposals:
                    for cid in p.get("ordered_candidate_ids", []):
                        if cid not in allowed_cids:
                            _update(JobStage.FAILED, 0.5, f"Agent 决策包含未知幻觉候选 ID: {cid}")
                            return {"status": JobStage.FAILED, "error": f"Agent 决策包含未知候选 ID: {cid}"}
                chosen_proposal = proposals[0]
            else:
                _update(JobStage.FAILED, 0.5, f"Agent 决策执行失败: {resp.error_message}")
                return {"status": JobStage.FAILED, "error": resp.error_message}
        try:
            from src.agent.agent_director import AgentDirector
            from src.core.sequence_optimizer import InsufficientEvidenceError
            from src.runtime.adapters.agent_protocol import AgentAuthRequiredError, AgentPermissionDeniedError

            def _director_progress(st: str, pct: float, msg: str):
                _update(st, pct, msg)

            director = AgentDirector(
                resolved_spec_path,
                use_cache=True,
                agent_adapter=self.agent_adapter if not chosen_proposal else None,
                project_paths=self.project_paths,
            )
            # 重定向输出目录至当前指定的 output_dir
            director.output_dir = self.output_dir
            manifest = director.run_director_workflow(
                custom_sequence_proposal=chosen_proposal,
                cancel_token=self.cancel_token,
                progress_callback=_director_progress,
            )
        except AgentAuthRequiredError as ae:
            _update(JobStage.AGENT_AUTH_REQUIRED, 0.75, f"AGY 授权失效: {str(ae)}")
            return {"status": JobStage.AGENT_AUTH_REQUIRED, "error": str(ae), "reason": "AGY 未登录或凭证过期"}
        except AgentPermissionDeniedError as pe:
            _update(JobStage.AGENT_PERMISSION_DENIED, 0.75, f"AGY 权限被软拒绝: {str(pe)}")
            return {"status": JobStage.AGENT_PERMISSION_DENIED, "error": str(pe), "reason": "AGY 工具执行权限被拒绝"}
        except InsufficientEvidenceError as ie:
            _update(JobStage.NEEDS_INPUT, 0.75, f"素材证据不足: {str(ie)}")
            return {"status": JobStage.NEEDS_INPUT, "reason": str(ie)}
        except Exception as e:
            if self.is_canceled or isinstance(e, JobCanceledError):
                _update(JobStage.CANCELED, 0.75, "任务已取消")
                return {"status": JobStage.CANCELED, "message": "任务已取消"}
            _update(JobStage.FAILED, 0.75, f"视频渲染与组装失败: {str(e)}")
            return {"status": JobStage.FAILED, "error": str(e)}

        if self.is_canceled:
            return {"status": JobStage.CANCELED}

        # 6. VERIFYING
        _update(JobStage.VERIFYING, 0.9, "执行双层质量门禁审计...")
        tech_qc_passed = bool(manifest.get("qc_passed", False))
        editorial_qc = manifest.get("editorial_qc") or {}
        editorial_qc_passed = bool(
            isinstance(editorial_qc, dict)
            and bool(editorial_qc)
            and editorial_qc.get("passed", False) is True
            and editorial_qc.get("block_count", 0) == 0
        )
        qc_passed = bool(tech_qc_passed and editorial_qc_passed)

        if self.is_canceled:
            return {"status": JobStage.CANCELED}

        if not qc_passed:
            fail_reasons = []
            if not tech_qc_passed:
                fail_reasons.append("工程技术质检未通过")
            if not editorial_qc_passed:
                fail_reasons.append(f"内容审核门禁阻断 (BLOCK={editorial_qc.get('block_count', 0)})")
            fail_msg = "；".join(fail_reasons)
            _update(JobStage.FAILED, 0.95, f"质量门禁未通过，拒绝交付: {fail_msg}")
            manifest_path = os.path.join(self.output_dir, "delivery_manifest.json")
            delivery_manifest = {
                "project_id": self.project_id,
                "status": JobStage.FAILED,
                "qc_passed": False,
                "fail_reason": fail_msg,
                "completed_at": time.time(),
                "director_manifest": manifest,
            }
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(delivery_manifest, f, ensure_ascii=False, indent=2)
            return {
                "status": JobStage.FAILED,
                "reason": fail_msg,
                "qc_passed": False,
                "manifest": delivery_manifest,
            }

        # 7. DELIVERED
        deliverables = manifest.get("final_deliverables", {})
        final_mp4 = deliverables.get("output_dir_video") or deliverables.get("video") or os.path.join(self.output_dir, f"{self.project_id}.mp4")
        if not os.path.isfile(final_mp4):
            legacy_mp4 = os.path.join(self.output_dir, f"{self.project_id}_final.mp4")
            if os.path.isfile(legacy_mp4):
                final_mp4 = legacy_mp4

        if not os.path.isfile(final_mp4):
            _update(JobStage.FAILED, 0.95, f"成片文件不存在: {final_mp4}")
            return {"status": JobStage.FAILED, "error": f"成片文件不存在: {final_mp4}"}

        manifest_path = os.path.join(self.output_dir, "delivery_manifest.json")
        delivery_manifest = {
            "project_id": self.project_id,
            "status": "DELIVERED",
            "job_fingerprint": current_fingerprint,
            "video_path": final_mp4,
            "cover_path": deliverables.get("cover") or deliverables.get("output_dir_cover"),
            "qc_passed": True,
            "completed_at": time.time(),
            "director_manifest": manifest,
        }
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(delivery_manifest, f, ensure_ascii=False, indent=2)

        _update(JobStage.DELIVERED, 1.0, f"成片已生成并交付: {final_mp4}")
        return {
            "status": JobStage.DELIVERED,
            "final_video": final_mp4,
            "manifest": delivery_manifest,
        }
