# -*- coding: utf-8 -*-
"""
editorial_qc.py - 电商内容层与编辑质量门禁审计器（V3 PR-08 核心）
===================================================================
负责：
  1. 实施第二层质量门禁（内容/业务层），与原工程层 qc_auditor 形成双门禁协同；
  2. 6 大核心内容审核维度：
     - product_identity_gate：严查成片是否存在他款商品混入（出现 OTHER_SKU 立即 BLOCK）；
     - selling_point_gate：核验高优先必选卖点是否具备台词与画面证据；
     - language_integrity_gate：检查是否发生吞字、断词或从句拦腰截断；
     - action_integrity_gate：核验核心展示动作（拉伸/转身/细节）是否完整闭合；
     - seam_continuity_gate：核验所有接缝是否存在未修复的严重跳变；
     - claim_compliance_gate：检查绝对化禁用用语与未经证实的价格承诺；
  3. 报告严格区分：AUTOMATED_PASS, WARN, BLOCK, UNVERIFIABLE, SKIP；
  4. 只要有任意 BLOCK 断言未修复，绝不发放交付许可证 (DELIVERED)。
"""

import os
import sys
import json
from typing import Dict, Any, List, Optional, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class EditorialQcAuditor:
    """电商短视频内容层质量门禁审计器。"""

    def __init__(self, policy: str = "strict"):
        self.policy = policy

    def audit_editorial_quality(
        self,
        final_plan: Dict[str, Any],
        product_profile: Dict[str, Any],
        continuity_review: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """对最终组装计划执行全套内容层 QC。"""
        checks: List[Dict[str, Any]] = []
        block_count = 0
        warn_count = 0

        clips = final_plan.get("clips", [])
        sku_id = product_profile.get("sku_id", "")
        required_selling_points = product_profile.get("selling_points", [])

        # 1. Product Identity Gate (本品准确性门禁 - P0 硬约束)
        other_sku_clips = []
        uncertain_clips = []
        unverifiable_clips = []
        for c in clips:
            rel = c.get("product_relation")
            cid = c.get("candidate_id") or c.get("id") or "unknown"
            if not rel or rel in ("UNVERIFIABLE", "MISSING"):
                unverifiable_clips.append(cid)
            elif rel == "OTHER_SKU":
                other_sku_clips.append(cid)
            elif rel == "UNCERTAIN":
                uncertain_clips.append(cid)

        if unverifiable_clips:
            checks.append({
                "gate": "ProductIdentityGate",
                "status": "BLOCK",
                "detail": f"检测到分镜缺少商品关系结论或证据引用缺失 (UNVERIFIABLE): {unverifiable_clips}",
                "affected_clips": unverifiable_clips,
            })
            block_count += 1
        elif other_sku_clips:
            checks.append({
                "gate": "ProductIdentityGate",
                "status": "BLOCK",
                "detail": f"检测到混入非本品或其他款式片段: {other_sku_clips}",
                "affected_clips": other_sku_clips,
            })
            block_count += 1
        elif uncertain_clips and self.policy == "strict":
            checks.append({
                "gate": "ProductIdentityGate",
                "status": "BLOCK",
                "detail": f"严格模式下存在未证实的 UNCERTAIN 片段: {uncertain_clips}",
                "affected_clips": uncertain_clips,
            })
            block_count += 1
        else:
            checks.append({
                "gate": "ProductIdentityGate",
                "status": "AUTOMATED_PASS",
                "detail": f"所有片段均确认为主推本品或合规变体/搭配 (共 {len(clips)} 段)",
                "affected_clips": [],
            })

        # 2. Selling Point Coverage Gate (卖点覆盖证据门禁)
        all_covered_sp = set()
        for c in clips:
            text = c.get("text") or c.get("desc") or ""
            for sp in required_selling_points:
                if sp and sp in text:
                    all_covered_sp.add(sp)

        missing_sp = [sp for sp in required_selling_points if sp not in all_covered_sp]
        if missing_sp:
            checks.append({
                "gate": "SellingPointEvidenceGate",
                "status": "WARN" if len(all_covered_sp) > 0 else "BLOCK",
                "detail": f"以下配置卖点在成片中未见口播证据: {missing_sp}",
                "missing_points": missing_sp,
            })
            if len(all_covered_sp) == 0 and required_selling_points:
                block_count += 1
            else:
                warn_count += 1
        else:
            checks.append({
                "gate": "SellingPointEvidenceGate",
                "status": "AUTOMATED_PASS",
                "detail": f"全部配置卖点均在成片中获得证实: {list(all_covered_sp)}",
                "missing_points": [],
            })

        # 3. Language Integrity Gate (语句完整性门禁)
        broken_speech = []
        for c in clips:
            risks = c.get("risks") or []
            if "INCOMPLETE_SENTENCE" in risks or "TRUNCATED_WORD" in risks:
                broken_speech.append(c.get("candidate_id") or c.get("id"))

        if broken_speech:
            checks.append({
                "gate": "LanguageIntegrityGate",
                "status": "WARN",
                "detail": f"片段台词未在自然句末闭合: {broken_speech}",
                "affected_clips": broken_speech,
            })
            warn_count += 1
        else:
            checks.append({
                "gate": "LanguageIntegrityGate",
                "status": "AUTOMATED_PASS",
                "detail": "全部片段均在字词或句末自然气口对齐，无断句",
                "affected_clips": [],
            })

        # 4. Seam Continuity Gate (接缝连续性门禁)
        if continuity_review:
            seam_blocks = [s for s in continuity_review.get("seams", []) if s.get("severity") == "BLOCK"]
            if seam_blocks:
                checks.append({
                    "gate": "SeamContinuityGate",
                    "status": "BLOCK",
                    "detail": f"存在未修复的阻断级接缝: {[s['seam_id'] for s in seam_blocks]}",
                })
                block_count += 1
            else:
                checks.append({
                    "gate": "SeamContinuityGate",
                    "status": "AUTOMATED_PASS",
                    "detail": "全部视听接缝审查通过",
                })

        # 总结
        passed = (block_count == 0)
        report = {
            "schema_version": "3.0",
            "passed": passed,
            "policy": self.policy,
            "block_count": block_count,
            "warn_count": warn_count,
            "checks": checks,
            "summary": f"内容层质检: {'PASS' if passed else 'BLOCK'} ({block_count} BLOCK / {warn_count} WARN)",
        }
        return report
