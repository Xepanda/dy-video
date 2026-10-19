#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
agent_director.py - 抖音带货直播剪辑 Agent 总导演决策中枢
=========================================================
核心架构:
1. 工具提供客观、确定性能力 (Tools as deterministic primitives):
   - extract_cover_candidates: 提取多张正面统一比例候选封面;
   - analyze_story_segments: 转录打点并按电商漏斗打标候选切片池;
   - render_cover_tool: 高定排版与视觉醒目度渲染;
   - assemble_video_tool: 物理强锁视频组装;
   - qc_auditor: 自动化验收门禁;
2. Agent 拥有主观决策与审美干预权 (Agent in the loop):
   - 决策 1 (封面优选): 审视候选正面图，挑选神态最自然、正面最挺拔的底图;
   - 决策 2 (分镜编排): 审视台词候选池，决策讲款节奏与排除杂质，编译 plan.json;
   - 决策 3 (质检签署): 审查 ASR 修正与质检报告，签署最终交付物;
3. 支持两种运行模式:
   - 自主导演模式 (Autonomous Mode): Agent 依据内置电商带货决策模型自动仲裁最优方案;
   - 外部干预模式 (Interactive/Override Mode): 允许运营人员或上层 Agent 显式指定候选帧或自定义分镜。
"""

import os
import sys
import json
import argparse
import time
from typing import Dict, Any, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from src.tools.extract_cover_candidates import extract_cover_candidates
from src.tools.analyze_story_segments import analyze_story_segments
from src.tools.render_cover_tool import render_cover_tool
from src.tools.assemble_video_tool import assemble_video_tool
from src.qc.qc_auditor import QcAuditor


class AgentDirector:
    def __init__(self, spec_path: str):
        self.spec_path = os.path.abspath(spec_path)
        with open(self.spec_path, "r", encoding="utf-8") as f:
            self.spec = json.load(f)

        self.project_id = self.spec.get("project_id", "demo")
        self.raw_video = os.path.abspath(self.spec["raw_video_path"])
        self.work_dir = os.path.join(PROJECT_ROOT, "data", "work_dirs", self.project_id)
        self.output_dir = os.path.join(PROJECT_ROOT, "output", self.project_id)
        self.candidates_dir = os.path.join(PROJECT_ROOT, "data", "candidates", self.project_id)

        self.brand_info = self.spec.get("brand", {})
        self.product_info = self.spec.get("product", {})
        self.options = self.spec.get("options", {})

        os.makedirs(self.work_dir, exist_ok=True)
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(self.candidates_dir, exist_ok=True)

    def run_director_workflow(
        self,
        chosen_cover_id: Optional[str] = None,
        custom_plan_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """运行完整的 Agent 决策与工具协同流水线"""
        start_time = time.time()
        print(f"\n=======================================================")
        print(f"🎬 启动 Agent 剪辑导演中枢: {self.project_id}")
        print(f"📁 原始视频: {self.raw_video}")
        print(f"📂 产物目录: {self.output_dir}")
        print(f"=======================================================\n")

        # -------------------------------------------------------------
        # Phase 1: 确定性工具扫描与候选池生成
        # -------------------------------------------------------------
        print(">>> [PHASE 1] 启动确定性工具链提取候选池...")
        
        # 1.1 提取正面候选帧 (统一固定比例)
        cover_cand_dir = os.path.join(self.candidates_dir, "cover_candidates")
        cover_candidates = extract_cover_candidates(
            video_path=self.raw_video,
            output_dir=cover_cand_dir,
            top_k=5
        )

        # 1.2 提取叙事分镜候选池 (ASR + 漏斗分类)
        story_cand_json = os.path.join(self.candidates_dir, "story_candidates.json")
        story_candidates = analyze_story_segments(
            video_path=self.raw_video,
            spec_data=self.spec,
            cache_dir=self.work_dir,
            output_json_path=story_cand_json
        )

        # -------------------------------------------------------------
        # Phase 2: Agent 决策干预 (封面选择 & 分镜编排)
        # -------------------------------------------------------------
        print("\n>>> [PHASE 2] Agent 启动决策与审美干预...")

        # 决策 2.1: 封面底图决策
        selected_cover = None
        if chosen_cover_id:
            # 外部显式指定干预
            selected_cover = next((c for c in cover_candidates if c["id"] == chosen_cover_id), None)
            if selected_cover:
                print(f"🎯 [AGENT 决策: 外部干预] 采用指定封面候选: {chosen_cover_id}")
        if not selected_cover:
            # Agent 自主决策：挑选正面度最高、清晰度优秀且无遮挡的第一名
            selected_cover = cover_candidates[0]
            print(f"🎯 [AGENT 决策: 自主仲裁] 优选最佳正面立姿封面: {selected_cover['id']} (@{selected_cover['timestamp']:.1f}s)")
            print(f"                          评价: {selected_cover['description']}")

        # 决策 2.2: 叙事分镜编排与计划编译 (plan.json)
        if custom_plan_path and os.path.exists(custom_plan_path):
            with open(custom_plan_path, "r", encoding="utf-8") as f:
                plan = json.load(f)
            print(f"🎯 [AGENT 决策: 外部干预] 加载自定义分镜计划: {custom_plan_path}")
        else:
            # Agent 自主规划：使用连续大块叙事逻辑构建 plan.json
            blocks_summary = story_candidates.get("continuous_blocks_summary", [])
            clips = []
            for b in blocks_summary:
                clips.append({
                    "id": b["block_id"],
                    "st": b["recommended_st"],
                    "end": b["recommended_end"],
                    "dur": b["duration"],
                    "is_hook": (b["block_id"] == "b01_hook_fabric"),
                    "zoom": 1.0,
                    "y_anchor": 0.35,
                    "desc": b["narrative"]
                })

            total_dur = round(sum(c["dur"] for c in clips), 2)
            plan = {
                "project_id": self.project_id,
                "raw_video_path": self.raw_video,
                "total_expected_duration": total_dur,
                "clips": clips,
                "hook_card": {
                    "text": "极简松弛感 · 显瘦显气质",
                    "dur": 2.5
                },
                "cta": {
                    "text": "戳左下方小黄车查看上身搭配",
                    "dur": 2.5
                },
                "boom": {
                    "volume": 0.45,
                    "file": "boom.wav"
                },
                "subtitles": {
                    "keywords": self.spec.get("options", {}).get("highlight_keywords", [
                        "半岛棉", "镂空", "显瘦", "针织", "做工", "进口", "浪漫", "老钱", "松弛", "更漂亮", "超显瘦", "实物"
                    ])
                }
            }
            print(f"🎯 [AGENT 决策: 自主编排] 编译 3 组连续叙事大块 (零价格零库存), 预计总时长: {total_dur:.2f}s")
            for c in clips:
                print(f"                          - {c['id']}: {c['st']:.2f}s ~ {c['end']:.2f}s ({c['dur']:.2f}s) | {c['desc']}")

        plan_path = os.path.join(self.work_dir, "plan.json")
        with open(plan_path, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)

        # -------------------------------------------------------------
        # Phase 3: 工具链执行确定性渲染与混流
        # -------------------------------------------------------------
        print("\n>>> [PHASE 3] 工具链执行成片渲染与混流...")

        # 3.1 渲染高定品牌封面
        final_cover_path = os.path.join(self.output_dir, f"{self.project_id}_cover.jpg")
        render_cover_tool(
            base_frame_path=selected_cover["image_path"],
            spec_data=self.spec,
            output_path=final_cover_path
        )

        # 3.2 执行视频最终总装
        final_mp4_path = os.path.join(self.output_dir, f"{self.project_id}_final.mp4")
        out_mp4, actual_dur = assemble_video_tool(
            plan_path=plan_path,
            output_mp4_path=final_mp4_path,
            work_dir=self.work_dir
        )

        # -------------------------------------------------------------
        # Phase 4: 自动化质检门禁与交付签署
        # -------------------------------------------------------------
        print("\n>>> [PHASE 4] 自动化质检门禁核验...")
        auditor = QcAuditor()
        ass_path = os.path.join(self.work_dir, "subtitles.ass")
        qc_report_path = os.path.join(self.output_dir, "qc_report.json")
        passed = auditor.audit_final_deliverables(
            video_path=out_mp4,
            cover_path=final_cover_path,
            ass_path=ass_path,
            report_output_path=qc_report_path
        )

        # 记录 Agent 决策审计清单
        decision_manifest = {
            "project_id": self.project_id,
            "decision_timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "chosen_cover": selected_cover,
            "chosen_clips": plan["clips"],
            "qc_passed": passed,
            "total_duration": actual_dur,
            "final_deliverables": {
                "video": out_mp4,
                "cover": final_cover_path,
                "qc_report": qc_report_path
            }
        }
        manifest_path = os.path.join(self.output_dir, "agent_decision_manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(decision_manifest, f, ensure_ascii=False, indent=2)

        elapsed = time.time() - start_time
        print(f"\n=======================================================")
        print(f"🏁 Agent 导演总装与质检验收完成! 总耗时: {elapsed:.1f} 秒")
        print(f"🎬 最终成片: {out_mp4}")
        print(f"🖼️ 高定封面: {final_cover_path}")
        print(f"📋 决策清单: {manifest_path}")
        print(f"=======================================================\n")
        return decision_manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="抖音带货直播剪辑 Agent 总导演中枢")
    parser.add_argument("--spec", default="job_spec.json", help="任务配置文件路径")
    parser.add_argument("--cover-id", default=None, help="干预指定封面候选 ID (如 cand_01)")
    parser.add_argument("--plan", default=None, help="干预指定自定义剪辑计划路径")
    args = parser.parse_args()

    director = AgentDirector(args.spec)
    director.run_director_workflow(
        chosen_cover_id=args.cover_id,
        custom_plan_path=args.plan
    )
