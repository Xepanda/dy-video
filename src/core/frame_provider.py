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
from typing import Dict, Any, List, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.core.path_resolver import resolve_path


class VideoFrameProvider:
    """基于 OpenCV 的视频候选帧提取器。"""

    def __init__(self, work_dir: str, default_video_path: Optional[str] = None):
        self.work_dir = os.path.abspath(work_dir)
        self.default_video = default_video_path
        self.frames_dir = os.path.join(self.work_dir, "candidate_frames")
        os.makedirs(self.frames_dir, exist_ok=True)

    def get_frames_for_candidate(self, cand: Dict[str, Any]) -> List[str]:
        """为指定候选片段抽取 1~2 帧核心关键帧供视觉对比。"""
        import re
        cid = cand.get("candidate_id", "cand_0")
        clean_cid = re.sub(r"[^\w\-]", "_", str(cid)).strip("_") or "cand_0"
        src = cand.get("source_id") or cand.get("source_file") or self.default_video
        if not src:
            return []

        resolved_src = resolve_path(src, self.work_dir)
        if not resolved_src or not os.path.isfile(resolved_src):
            if self.default_video and os.path.isfile(self.default_video):
                resolved_src = self.default_video
            else:
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
