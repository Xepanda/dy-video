# -*- coding: utf-8 -*-
"""
frame_provider.py - 候选视频帧提取与视觉样本提供器（V3.3 顺序 3 点批抽升级）
===========================================================================
负责：
  1. 支持按源视频单次打开、时间升序顺序抽取 25%/50%/75% 三点关键帧（extract_frames_batch），
     并通过 500ms 时间桶去重避免数百滑窗候选重复解码；
  2. 兼容保留单条 get_frames_for_candidate(cand) 接口，命中批抽缓存立即返回；
  3. 严禁在多源场景下静默退回第一段视频（default_video）；
  4. 文件名携带 source_id 隔离前缀，彻底杜绝同 stem 不同扩展名（live01.mp4 vs live01.mov）碰撞。
"""

import os
import re
import sys
import json
import hashlib
from typing import Dict, Any, List, Optional, Tuple, Union

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.core.path_resolver import resolve_path


def _safe_source_tag(source_path: str, source_id: str = "") -> str:
    """为源视频生成确定且不冲突的文件名前缀（区分同 stem 不同扩展名与不同目录）。"""
    base = os.path.basename(source_path or source_id or "src")
    stem, ext = os.path.splitext(base)
    ext_clean = ext.lstrip(".").lower()
    clean_stem = re.sub(r"[^\w\-]", "_", stem).strip("_")[:18] or "src"
    norm_key = os.path.normcase(os.path.abspath(source_path)) if source_path else str(source_id)
    short_h = hashlib.sha1(norm_key.encode("utf-8", errors="ignore")).hexdigest()[:8]
    if ext_clean:
        return f"{clean_stem}_{ext_clean}_{short_h}"
    return f"{clean_stem}_{short_h}"


class VideoFrameProvider:
    """基于 OpenCV 的视频候选帧提取器（支持顺序 3 点批抽 + 500ms 桶去重）。"""

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
        self.stats: Dict[str, int] = {"video_opens": 0, "frames_decoded": 0, "frames_reused": 0}

        if source_registry:
            for k, v in source_registry.items():
                if v:
                    self.source_registry[str(k)] = os.path.abspath(v)

        manifest_p = os.path.join(self.work_dir, "input_manifest.json")
        if os.path.isfile(manifest_p):
            try:
                with open(manifest_p, "r", encoding="utf-8") as mf:
                    mdata = json.load(mf)
                    for s in mdata.get("source_videos", []):
                        sid = s.get("source_id")
                        sp = s.get("path")
                        if sid and sp:
                            self.source_registry[str(sid)] = os.path.abspath(sp)
                        if s.get("filename") and sp:
                            self.source_registry[s["filename"]] = os.path.abspath(sp)
                            stem, ext = os.path.splitext(s["filename"])
                            ext_clean = ext.lstrip(".").lower()
                            if ext_clean:
                                self.source_registry[f"{stem}_{ext_clean}"] = os.path.abspath(sp)
            except Exception:
                pass

    def register_source(self, source_id: str, file_path: str) -> None:
        if file_path:
            abs_p = os.path.abspath(file_path)
            self.source_registry[str(source_id)] = abs_p
            base = os.path.basename(abs_p)
            self.source_registry[base] = abs_p
            stem, ext = os.path.splitext(base)
            ext_clean = ext.lstrip(".").lower()
            if ext_clean:
                self.source_registry[f"{stem}_{ext_clean}"] = abs_p

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
        """严格解析候选片段所属的真实视频源路径。"""
        source_file = cand.get("source_file")
        source_id = cand.get("source_id")

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

        if self.is_multi_source:
            cid = cand.get("candidate_id", "unknown")
            print(
                f"[FRAME-PROVIDER][ERROR] 多源场景下无法解析候选 {cid} 的视频源: "
                f"source_file={source_file}, source_id={source_id}。禁止在多源场景下静默退回默认视频！"
            )
            return None

        if self.default_video and os.path.isfile(self.default_video):
            default_name = os.path.basename(self.default_video)
            if source_file and os.path.isfile(str(source_file)) and os.path.abspath(str(source_file)) != self.default_video:
                return None
            sid_str = str(source_id or "").strip()
            sf_str = str(source_file or "").strip()
            if not sid_str and not sf_str:
                return self.default_video
            if sf_str and (os.path.basename(sf_str) == default_name or sf_str.startswith("src_") or sf_str in ("default", "main")):
                return self.default_video
            if sid_str and (sid_str == default_name or sid_str.startswith("src_") or sid_str in ("default", "main")):
                return self.default_video
            if sf_str == sid_str:
                return self.default_video

        return None

    def extract_frames_batch(
        self,
        candidates: List[Dict[str, Any]],
        sample_ratios: Tuple[float, ...] = (0.25, 0.50, 0.75),
        bucket_ms: int = 500,
    ) -> Dict[str, List[str]]:
        """按源视频单次打开、时间戳升序批量抽取多点关键帧（500ms 桶去重复用）。

        同时在每个 cand 上回填 frame_refs（绝对路径列表）与唯一 frame_ids（文件名列表）。
        """
        import cv2
        from src.core.path_resolver import cv2_imwrite

        if not candidates:
            return {}

        # 1. 按源分组，收集全部桶化时间戳请求
        # by_source[src_path] = {bucket_t_ms: out_frame_path}
        by_source: Dict[str, Dict[int, str]] = {}
        cand_requests: List[Tuple[Dict[str, Any], Optional[str], List[int]]] = []

        b_step = max(100, int(bucket_ms))
        for cand in candidates:
            resolved_src = self.resolve_video_for_candidate(cand)
            if not resolved_src:
                cand_requests.append((cand, None, []))
                continue
            st_ms = int(cand.get("source_start_ms", 0) or 0)
            en_ms = int(cand.get("source_end_ms", st_ms + 3000) or (st_ms + 3000))
            dur_ms = max(200, en_ms - st_ms)
            tag = _safe_source_tag(resolved_src, str(cand.get("source_id") or ""))
            req_buckets: List[int] = []
            for r in sample_ratios:
                t_ms = st_ms + int(round(dur_ms * float(r)))
                bt = int(round(t_ms / b_step) * b_step)
                bt = max(0, bt)
                if bt not in req_buckets:
                    req_buckets.append(bt)
                out_p = os.path.join(self.frames_dir, f"{tag}_t{bt:07d}.jpg")
                by_source.setdefault(resolved_src, {})[bt] = out_p
            cand_requests.append((cand, resolved_src, req_buckets))

        # 2. 对每个源视频仅打开一次 VideoCapture，按升序顺序抽帧
        extracted_ok: Dict[Tuple[str, int], str] = {}
        for src_path, bucket_map in by_source.items():
            pending: List[Tuple[int, str]] = []
            for bt, out_p in sorted(bucket_map.items()):
                if os.path.isfile(out_p) and os.path.getsize(out_p) > 0:
                    extracted_ok[(src_path, bt)] = out_p
                    self.stats["frames_reused"] += 1
                else:
                    pending.append((bt, out_p))

            if not pending:
                continue

            cap = cv2.VideoCapture(src_path)
            if not cap.isOpened():
                continue
            self.stats["video_opens"] += 1
            try:
                fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
                if fps <= 0:
                    fps = 30.0
                cur_frame_idx = -1
                for bt, out_p in pending:
                    target_frame_idx = max(0, int(round((bt / 1000.0) * fps)))
                    # 若目标帧在前方且距离近（<= 60 帧），顺序 grab 避免昂贵随机 seek
                    if 0 <= cur_frame_idx < target_frame_idx and (target_frame_idx - cur_frame_idx) <= 60:
                        while cur_frame_idx < target_frame_idx - 1:
                            if not cap.grab():
                                break
                            cur_frame_idx += 1
                        ok, frame = cap.read()
                        cur_frame_idx += 1
                    else:
                        cap.set(cv2.CAP_PROP_POS_MSEC, float(bt))
                        ok, frame = cap.read()
                        cur_frame_idx = target_frame_idx
                    if ok and frame is not None:
                        cv2_imwrite(out_p, frame)
                        if os.path.isfile(out_p) and os.path.getsize(out_p) > 0:
                            extracted_ok[(src_path, bt)] = out_p
                            self.stats["frames_decoded"] += 1
            except Exception as e:
                print(f"[FRAME-PROVIDER][WARN] 批量顺序抽帧异常 ({os.path.basename(src_path)}): {e}")
            finally:
                cap.release()

        # 3. 回填每个候选的 frame_refs 与唯一 frame_ids
        result_map: Dict[str, List[str]] = {}
        for cand, resolved_src, req_buckets in cand_requests:
            cid = str(cand.get("candidate_id", ""))
            paths: List[str] = []
            if resolved_src:
                for bt in req_buckets:
                    p = extracted_ok.get((resolved_src, bt))
                    if p:
                        paths.append(p)
            cand["frame_refs"] = paths
            if paths:
                existing_ids = list(cand.get("frame_ids") or [])
                for p in paths:
                    bn = os.path.basename(p)
                    if bn not in existing_ids:
                        existing_ids.append(bn)
                cand["frame_ids"] = existing_ids
            if cid:
                result_map[cid] = paths

        return result_map

    def get_frames_for_candidate(self, cand: Dict[str, Any]) -> List[str]:
        """获取指定候选的关键帧列表（命中批抽缓存立即返回，否则按需抽取 1 帧中点帧）。"""
        cached_refs = cand.get("frame_refs")
        if isinstance(cached_refs, list) and cached_refs:
            valid = [p for p in cached_refs if isinstance(p, str) and os.path.isfile(p) and os.path.getsize(p) > 0]
            if valid:
                return valid

        cid = cand.get("candidate_id", "cand_0")
        clean_cid = re.sub(r"[^\w\-]", "_", str(cid)).strip("_") or "cand_0"

        resolved_src = self.resolve_video_for_candidate(cand)
        if not resolved_src:
            return []

        st_ms = cand.get("source_start_ms", 0)
        en_ms = cand.get("source_end_ms", st_ms + 3000)
        mid_sec = (st_ms + en_ms) / 2000.0

        tag = _safe_source_tag(resolved_src, str(cand.get("source_id") or ""))
        out_frame_path = os.path.join(self.frames_dir, f"{tag}_{clean_cid}_mid.jpg")
        if os.path.isfile(out_frame_path) and os.path.getsize(out_frame_path) > 0:
            return [out_frame_path]

        try:
            import cv2
            cap = cv2.VideoCapture(resolved_src)
            if not cap.isOpened():
                return []
            self.stats["video_opens"] += 1
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, mid_sec * 1000.0))
            ok, frame = cap.read()
            cap.release()
            if ok and frame is not None:
                from src.core.path_resolver import cv2_imwrite
                cv2_imwrite(out_frame_path, frame)
                self.stats["frames_decoded"] += 1
                cand["frame_refs"] = [out_frame_path]
                return [out_frame_path]
        except Exception as e:
            print(f"[FRAME-PROVIDER][WARN] 抽帧异常 ({cid}): {e}")

        return []
