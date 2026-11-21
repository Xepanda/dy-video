# -*- coding: utf-8 -*-
"""
evidence_store.py - 证据中心与多模态数据仓库（V3 PR-03 核心）
============================================================
负责：
  1. 统一源素材的媒体指纹、时间轴与元数据注册（源相对毫秒整数 start_ms / end_ms）；
  2. 集中存储与索引 utterances（语句）、shots（镜头）、frames（参考帧）、candidates（候选段）；
  3. 保证大模型只接收不可篡改的证据摘要与合法引用 ID，防止外部 Agent 凭空发明时间戳与候选；
  4. 支持落盘为 evidence_store.json 与增量加载。
"""

import os
import sys
import json
import hashlib
from typing import Dict, Any, List, Optional, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def compute_file_hash(path: str) -> str:
    """计算文件 SHA256。"""
    if not os.path.isfile(path):
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class EvidenceStore:
    """多模态证据中心，所有选剪决策的单一真值数据源。"""

    def __init__(self, project_id: str = "demo"):
        self.project_id = project_id
        self.sources: Dict[str, Dict[str, Any]] = {}       # source_id -> metadata
        self.utterances: List[Dict[str, Any]] = []         # list of utterance dicts
        self.shots: List[Dict[str, Any]] = []              # list of shot dicts
        self.frames: Dict[str, Dict[str, Any]] = {}        # frame_id -> frame metadata
        self.candidates: Dict[str, Dict[str, Any]] = {}    # candidate_id -> candidate dict

    def register_source(
        self,
        file_path: str,
        source_id: Optional[str] = None,
        duration_sec: Optional[float] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        fps: Optional[float] = None,
    ) -> str:
        """注册原始视频素材，返回规范 source_id。"""
        norm_path = os.path.abspath(file_path)
        sha256 = compute_file_hash(norm_path)
        sid = source_id or f"src_{len(self.sources)}"
        self.sources[sid] = {
            "source_id": sid,
            "path": norm_path,
            "rel_path": os.path.basename(norm_path),
            "sha256": sha256,
            "duration_ms": int(round((duration_sec or 0.0) * 1000)),
            "width": width or 0,
            "height": height or 0,
            "fps": fps or 30.0,
        }
        return sid

    def add_utterance(
        self,
        utterance_id: str,
        source_id: str,
        start_ms: int,
        end_ms: int,
        text: str,
        words: Optional[List[Dict[str, Any]]] = None,
        alignment_precision: str = "SEGMENT_LEVEL",
        categories: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """记录一条语句证据。"""
        utt = {
            "utterance_id": utterance_id,
            "source_id": source_id,
            "start_ms": int(start_ms),
            "end_ms": int(end_ms),
            "duration_ms": int(end_ms - start_ms),
            "text": str(text or "").strip(),
            "words": words or [],
            "alignment_precision": alignment_precision,
            "categories": categories or [],
        }
        self.utterances.append(utt)
        return utt

    def add_shot(
        self,
        shot_id: str,
        source_id: str,
        start_ms: int,
        end_ms: int,
        stable_t_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        """记录一个镜头区间。"""
        shot = {
            "shot_id": shot_id,
            "source_id": source_id,
            "start_ms": int(start_ms),
            "end_ms": int(end_ms),
            "duration_ms": int(end_ms - start_ms),
            "stable_t_ms": int(stable_t_ms if stable_t_ms is not None else (start_ms + end_ms) // 2),
        }
        self.shots.append(shot)
        return shot

    def add_frame(
        self,
        frame_id: str,
        source_id: str,
        timestamp_ms: int,
        file_path: Optional[str] = None,
        frame_hash: Optional[str] = None,
    ) -> Dict[str, Any]:
        """记录一个关键参考帧。"""
        fr = {
            "frame_id": frame_id,
            "source_id": source_id,
            "timestamp_ms": int(timestamp_ms),
            "file_path": file_path or "",
            "hash": frame_hash or (compute_file_hash(file_path) if file_path else ""),
        }
        self.frames[frame_id] = fr
        return fr

    def add_candidate(self, candidate: Dict[str, Any]) -> None:
        """注册一个候选片段。"""
        cid = candidate["candidate_id"]
        self.candidates[cid] = candidate

    def get_candidate(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        return self.candidates.get(candidate_id)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": "3.0",
            "project_id": self.project_id,
            "sources": self.sources,
            "utterances": self.utterances,
            "shots": self.shots,
            "frames": self.frames,
            "candidates": self.candidates,
        }

    def save(self, output_path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str) -> "EvidenceStore":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        store = cls(project_id=data.get("project_id", "demo"))
        store.sources = data.get("sources", {})
        store.utterances = data.get("utterances", [])
        store.shots = data.get("shots", [])
        store.frames = data.get("frames", {})
        store.candidates = data.get("candidates", {})
        return store
