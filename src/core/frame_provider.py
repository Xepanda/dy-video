# -*- coding: utf-8 -*-
"""
frame_provider.py - 候选视频帧提取与视觉样本提供器（V3 PR-04 核心）
===================================================================
负责：
  1. 根据候选片段的时间戳区间与素材路径提取关键帧；
  2. 保存抽帧至 work_dir/candidate_frames 缓存目录；
  3. 提供 get_frames_for_candidate(cand) 标准接口供 SkuTracker 和 VisionBackend 消费。
"""

import os
import sys
from typing import Dict, Any, List, Optional, Union

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.core.path_resolver import resolve_path


class VideoFrameProvider:
    """基于 OpenCV 的视频候选帧提取器。

    核心职责与安全规范：
      1. 优先使用 candidate.source_file（真实文件路径）；
      2. 若仅提供 source_id，仅可通过经过校验的 source_registry 解析为真实路径；
      3. 严禁在多源场景下静默退回第一段视频（default_video）；
      4. 自动尝试从 work_dir/input_manifest.json 加载 Source Registry。
    """

    def __init__(
        self,
        work_dir: str,
        default_video_path: Optional[str] = None,
        source_registry: Optional[Dict[str, str]] = None,
    ):
        self.work_dir = os.path.abspath(work_dir)
        self.default_video = os.path.abspath(default_video_path) if default_video_path else None
        self.frames_dir = os.path.join(self.work_dir, "candidate_frames")
        os.makedirs(self.frames_dir, exist_ok=True)
        self.source_registry: Dict[str, str] = {}
        if source_registry:
            for k, v in source_registry.items():
                if v:
                    self.source_registry[str(k)] = os.path.abspath(v)

        # 尝试自动从 work_dir/input_manifest.json 加载未显式指定的 source_registry
        manifest_p = os.path.join(self.work_dir, "input_manifest.json")
        if os.path.isfile(manifest_p):
            try:
                import json
                with open(manifest_p, "r", encoding="utf-8") as mf:
                    mdata = json.load(mf)
                    for s in mdata.get("source_videos", []):
                        sid = s.get("source_id")
                        sp = s.get("path")
                        if sid and sp:
                            self.source_registry[str(sid)] = os.path.abspath(sp)
                        if s.get("filename") and sp:
                            self.source_registry[s["filename"]] = os.path.abspath(sp)
            except Exception:
                pass

    def register_source(self, source_id: str, file_path: str) -> None:
        if file_path:
            self.source_registry[str(source_id)] = os.path.abspath(file_path)

    def register_sources(self, sources: Union[Dict[str, str], List[Dict[str, Any]]]) -> None:
        if isinstance(sources, dict):
            for k, v in sources.items():
                self.register_source(k, v)
        elif isinstance(sources, list):
            for item in sources:
                if isinstance(item, dict):
                    sid = item.get("source_id")
                    sp = item.get("path") or item.get("source_file")
                    if sid and sp:
                        self.register_source(sid, sp)

    @property
    def is_multi_source(self) -> bool:
        unique_paths = set(self.source_registry.values())
        if self.default_video:
            unique_paths.add(self.default_video)
        return len(unique_paths) > 1

    def resolve_video_for_candidate(self, cand: Dict[str, Any]) -> Optional[str]:
        """严格解析候选片段所属的真实视频源路径。

        解析原则：
          1. 优先使用 candidate.source_file；
          2. 其次使用 candidate.source_id 并通过 source_registry 解析；
          3. 禁止在多源场景下或源标识不匹配时静默退回 default_video；
          4. 仅在明确为单源场景且 candidate 无有效源标识时，才允许使用 default_video。
        """
        source_file = cand.get("source_file")
        source_id = cand.get("source_id")

        # 1. 优先使用 candidate.source_file
        if source_file:
            resolved_sf = resolve_path(str(source_file), self.work_dir)
            if resolved_sf and os.path.isfile(resolved_sf):
                return resolved_sf
            if os.path.isfile(str(source_file)):
                return os.path.abspath(str(source_file))
            if str(source_file) in self.source_registry:
                reg_p = self.source_registry[str(source_file)]
                if os.path.isfile(reg_p):
                    return reg_p
            sf_base = os.path.basename(str(source_file))
            if sf_base in self.source_registry:
                reg_p = self.source_registry[sf_base]
                if os.path.isfile(reg_p):
                    return reg_p

        # 2. 通过 Source Registry 解析 source_id
        if source_id:
            if str(source_id) in self.source_registry:
                reg_p = self.source_registry[str(source_id)]
                if os.path.isfile(reg_p):
                    return reg_p
            sid_base = os.path.basename(str(source_id))
            if sid_base in self.source_registry:
                reg_p = self.source_registry[sid_base]
                if os.path.isfile(reg_p):
                    return reg_p
            resolved_sid = resolve_path(str(source_id), self.work_dir)
            if resolved_sid and os.path.isfile(resolved_sid):
                return resolved_sid

        # 3. 兜底与防退回阻断
        if self.is_multi_source:
            cid = cand.get("candidate_id", "unknown")
            print(
                f"[FRAME-PROVIDER][ERROR] 多源场景下无法解析候选 {cid} 的视频源: "
                f"source_file={source_file}, source_id={source_id}。禁止在多源场景下静默退回默认视频！"
            )
            return None

        if source_file or source_id:
            # 单源场景但指定了不匹配的源，也拒绝错误关联
            if self.default_video and os.path.isfile(self.default_video):
                default_name = os.path.basename(self.default_video)
                if (source_file and os.path.basename(str(source_file)) == default_name) or \
                   (source_id and str(source_id) in ("src_01", "src_00", default_name)):
                    return self.default_video
            return None

        # 仅当 candidate 未提供任何源信息且为单源场景时，方可使用 default_video
        if self.default_video and os.path.isfile(self.default_video):
            return self.default_video

        return None

    def get_frames_for_candidate(self, cand: Dict[str, Any]) -> List[str]:
        """为指定候选片段抽取 1~2 帧核心关键帧供视觉对比。"""
        import re
        cid = cand.get("candidate_id", "cand_0")
        clean_cid = re.sub(r"[^\w\-]", "_", str(cid)).strip("_") or "cand_0"

        resolved_src = self.resolve_video_for_candidate(cand)
        if not resolved_src:
            return []

        st_ms = cand.get("source_start_ms", 0)
        en_ms = cand.get("source_end_ms", st_ms + 3000)
        mid_sec = (st_ms + en_ms) / 2000.0

        out_frame_path = os.path.join(self.frames_dir, f"{clean_cid}_mid.jpg")
        if os.path.isfile(out_frame_path) and os.path.getsize(out_frame_path) > 0:
            return [out_frame_path]

        try:
            import cv2
            cap = cv2.VideoCapture(resolved_src)
            if not cap.isOpened():
                return []
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, mid_sec * 1000.0))
            ok, frame = cap.read()
            cap.release()
            if ok and frame is not None:
                from src.core.path_resolver import cv2_imwrite
                cv2_imwrite(out_frame_path, frame)
                return [out_frame_path]
        except Exception as e:
            print(f"[FRAME-PROVIDER][WARN] 抽帧异常 ({cid}): {e}")

        return []
