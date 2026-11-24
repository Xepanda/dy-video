# -*- coding: utf-8 -*-
"""
project_context.py - 统一项目上下文与安全路径管理 (V3 PR-16 核心架构)
===================================================================
负责：
  1. 统一管理任务项目根目录、素材目录（素材/）、工作目录（work/<run_id>/）与交付目录（output/<run_id>/）；
  2. 生成并审计任务输入清单 input_manifest.json；
  3. 彻底隔离历史成片，确保历史成品绝不混入新任务的输入池；
  4. 绝不修改、覆盖或删除素材目录及原始文件；
  5. 生成安全、稳定、不含路径字符的 source_id 与 candidate_id 命名空间。
"""

import os
import re
import sys
import json
import time
import uuid
import hashlib
import ntpath
import posixpath
from pathlib import Path, PureWindowsPath, PurePosixPath
from typing import Dict, Any, List, Optional, Tuple

# 视频扩展名定义
VIDEO_EXTENSIONS = (".mp4", ".mov", ".mkv", ".avi", ".flv", ".m4v", ".ts")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")


def sanitize_id(raw_name: str) -> str:
    """清理字符串为合法的文件名与标识符（仅保留字母数字下划线与连字符）。"""
    s = re.sub(r"[^\w\-]", "_", str(raw_name))
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "src"


def normalize_project_path(path_str: str, target_platform: Optional[str] = None) -> str:
    """跨平台统一解析项目根路径。
    在 Linux/WSL 环境下，Windows 驱动器盘符（如 D:/... 或 D:\\...）将被规范映射到 WSL 挂载路径（/mnt/d/...）；
    在 Windows 环境下，保持 Windows 驱动器路径；若传入 /mnt/d/... 自动映射为 D:\\...。
    可通过 target_platform 显式指定目标平台语义（'win32' 或 'linux'），实现与测试宿主解耦。
    """
    if not path_str:
        return ""
    s = str(path_str).strip()
    plat = target_platform if target_platform is not None else sys.platform
    if plat != "win32":
        # POSIX / Linux: 将 Windows 盘符路径映射为 WSL 规范路径
        m = re.match(r"^([a-zA-Z]):(?:[\\/](.*))?$", s)
        if m:
            drive = m.group(1).lower()
            rest = (m.group(2) or "").replace("\\", "/").strip("/")
            return f"/mnt/{drive}/{rest}" if rest else f"/mnt/{drive}"
        return posixpath.normpath(s)
    else:
        # Windows: 将 WSL 路径映射为 Windows 盘符路径
        m = re.match(r"^/mnt/([a-zA-Z])(?:/(.*))?$", s)
        if m:
            drive = m.group(1).upper()
            rest = (m.group(2) or "").replace("/", "\\").strip("\\")
            return f"{drive}:\\{rest}" if rest else f"{drive}:\\"
        m_win = re.match(r"^([a-zA-Z]):[\\/]?$", s)
        if m_win:
            return f"{m_win.group(1).upper()}:\\"
        return ntpath.normpath(s)


class ProjectPaths:
    """项目路径结构与运行时定位。"""

    def __init__(self, project_root: str, run_id: Optional[str] = None, materials_dir: Optional[str] = None):
        norm_root = normalize_project_path(project_root)
        if os.path.isfile(norm_root):
            norm_root = os.path.dirname(norm_root)

        # 保护：优先使用显式项目根目录；只有存在有效 run_context.json 时才反向识别运行目录并截断
        # 绝不盲目依据路径末尾的 'work/<目录名>' 猜测截断，防止类似 D:\projects\work\my-video 被误判
        is_sub_run_dir = False
        if os.path.isfile(os.path.join(norm_root, "run_context.json")):
            try:
                with open(os.path.join(norm_root, "run_context.json"), "r", encoding="utf-8") as f:
                    ctx_info = json.load(f)
                if isinstance(ctx_info, dict) and "run_id" in ctx_info:
                    is_sub_run_dir = True
            except Exception:
                pass

        # 针对尚未物理创建的 mock/测试路径：仅当父目录名为 work/output 且当前目录名符合 run_id 典型特征时才识别
        p_parts = Path(norm_root).parts
        if not is_sub_run_dir and len(p_parts) >= 3 and p_parts[-2] in ("work", "output"):
            last_part = p_parts[-1]
            has_project_files = (
                os.path.isfile(os.path.join(norm_root, "job_spec.json")) or
                os.path.isdir(os.path.join(norm_root, "素材")) or
                os.path.isdir(os.path.join(norm_root, "materials")) or
                os.path.isdir(os.path.join(norm_root, "input"))
            )
            if not has_project_files and (
                last_part.startswith("run_") or
                last_part.startswith("run-") or
                re.match(r"^\d{8}", last_part)
            ):
                is_sub_run_dir = True

        if is_sub_run_dir:
            is_abs = Path(norm_root).is_absolute()
            if sys.platform != "win32":
                clean_parts = [part.strip("/\\") for part in p_parts[:-2] if part.strip("/\\")]
                norm_root = ("/" + "/".join(clean_parts)) if is_abs else ("/".join(clean_parts) or ".")
            else:
                norm_root = str(Path(*p_parts[:-2])) if p_parts[:-2] else "."
        elif len(p_parts) >= 2 and p_parts[-1] in ("work", "output"):
            has_project_files = (
                os.path.isfile(os.path.join(norm_root, "job_spec.json")) or
                os.path.isdir(os.path.join(norm_root, "素材")) or
                os.path.isdir(os.path.join(norm_root, "materials"))
            )
            if not has_project_files:
                is_abs = Path(norm_root).is_absolute()
                if sys.platform != "win32":
                    clean_parts = [part.strip("/\\") for part in p_parts[:-1] if part.strip("/\\")]
                    norm_root = ("/" + "/".join(clean_parts)) if is_abs else ("/".join(clean_parts) or ".")
                else:
                    norm_root = str(Path(*p_parts[:-1])) if p_parts[:-1] else "."

        if os.path.basename(norm_root) in ("素材", "materials", "input"):
            materials_dir = materials_dir or norm_root
            self.project_root = os.path.dirname(norm_root)
        else:
            self.project_root = norm_root

        self.project_id = os.path.basename(self.project_root)

        if run_id:
            raw_run_id = str(run_id).strip()
            if not raw_run_id or ".." in raw_run_id or "/" in raw_run_id or "\\" in raw_run_id:
                raise ValueError(f"非法 run_id: '{run_id}'，禁止包含路径分隔符或 '..'")
            if not re.fullmatch(r"[\w\-]+", raw_run_id):
                raise ValueError(f"非法 run_id: '{run_id}'，仅允许包含字母、数字、下划线和连字符")
            self.run_id = raw_run_id
        else:
            self.run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]

        # 确定素材目录
        if materials_dir:
            self.materials_dir = normalize_project_path(materials_dir)
        else:
            # 优先寻找标准已知子目录
            cand_dirs = ["素材", "input", "materials"]
            found = None
            for cd in cand_dirs:
                p = os.path.join(self.project_root, cd)
                if os.path.isdir(p):
                    found = p
                    break
            self.materials_dir = found or self.project_root

        # 统一的中间生成目录与输出交付目录
        self.work_base = os.path.join(self.project_root, "work")
        self.output_base = os.path.join(self.project_root, "output")

        self.work_dir = os.path.join(self.work_base, self.run_id)
        self.output_dir = os.path.join(self.output_base, self.run_id)
        self.latest_pointer = os.path.join(self.output_base, "latest.json")

        # 真实路径做目录包含校验，禁止逃逸出 work_base 与 output_base，且禁止进入素材目录
        real_work = os.path.realpath(self.work_dir)
        real_work_base = os.path.realpath(self.work_base)
        real_output = os.path.realpath(self.output_dir)
        real_output_base = os.path.realpath(self.output_base)
        real_materials = os.path.realpath(self.materials_dir)
        real_project_root = os.path.realpath(self.project_root)

        try:
            if os.path.commonpath([real_work, real_work_base]) != real_work_base:
                raise ValueError(f"非法 work_dir: {self.work_dir}，路径越界逃逸出 work 根目录")
        except Exception as e:
            raise ValueError(f"非法 work_dir: {self.work_dir} ({e})")

        try:
            if os.path.commonpath([real_output, real_output_base]) != real_output_base:
                raise ValueError(f"非法 output_dir: {self.output_dir}，路径越界逃逸出 output 根目录")
        except Exception as e:
            raise ValueError(f"非法 output_dir: {self.output_dir} ({e})")

        if real_work == real_materials:
            raise ValueError(f"运行时目录冲突: work_dir ({self.work_dir}) 不能与素材目录重叠")
        if real_output == real_materials:
            raise ValueError(f"交付目录冲突: output_dir ({self.output_dir}) 不能与素材目录重叠")
        if real_materials != real_project_root:
            if os.path.commonpath([real_work, real_materials]) == real_materials:
                raise ValueError(f"运行时目录冲突: work_dir ({self.work_dir}) 不能位于素材目录 ({self.materials_dir}) 内")
            if os.path.commonpath([real_output, real_materials]) == real_materials:
                raise ValueError(f"交付目录冲突: output_dir ({self.output_dir}) 不能位于素材目录 ({self.materials_dir}) 内")

        # 配置文件路径优先位于素材目录下
        spec_in_mat = os.path.join(self.materials_dir, "job_spec.json")
        spec_in_root = os.path.join(self.project_root, "job_spec.json")
        if os.path.isfile(spec_in_mat):
            self.spec_path = spec_in_mat
        elif os.path.isfile(spec_in_root):
            self.spec_path = spec_in_root
        else:
            self.spec_path = spec_in_mat

    def ensure_runtime_dirs(self) -> None:
        """安全创建当前运行所需的运行时目录（只在 work 与 output 创建，不写素材目录）。"""
        os.makedirs(self.work_dir, exist_ok=True)
        os.makedirs(self.output_dir, exist_ok=True)

    def is_safe_to_clean(self, path: str) -> bool:
        """检查指定路径是否严格属于当前 run 的 work 目录子路径，防止误删任何外部或素材文件。"""
        try:
            real_p = os.path.normcase(os.path.realpath(os.path.abspath(path)))
            real_work = os.path.normcase(os.path.realpath(os.path.abspath(self.work_dir)))
            if real_p == real_work:
                return False
            return os.path.normcase(os.path.commonpath([real_p, real_work])) == real_work
        except Exception:
            return False


def is_historical_output(file_path: str, project_id: str = "", project_root: Optional[str] = None) -> Tuple[bool, str]:
    """判定文件是否为既有成品、中间文件或导出产物，防止被重新当成原始素材。"""
    if not file_path:
        return False, ""
    norm_p = normalize_project_path(os.path.abspath(file_path))
    parts = Path(norm_p).parts

    # 1. 严格检查是否位于当前项目的 output 或 work 系统产物目录下
    # 若提供了 project_root，以真实物理包含关系为准，绝不误杀祖先叫 work 的素材目录
    if project_root:
        norm_root = normalize_project_path(os.path.abspath(project_root))
        try:
            real_p = os.path.normcase(os.path.realpath(norm_p))
            real_work = os.path.normcase(os.path.realpath(os.path.join(norm_root, "work")))
            real_out = os.path.normcase(os.path.realpath(os.path.join(norm_root, "output")))
            if real_p == real_work or os.path.commonpath([real_p, real_work]) == real_work:
                return True, "文件位于项目 work 系统产物目录"
            if real_p == real_out or os.path.commonpath([real_p, real_out]) == real_out:
                return True, "文件位于项目 output 系统产物目录"
        except Exception:
            pass

    # 路径各级结构判定：
    # 绝不能只要祖先目录包含 'work' 就把文件判死（如 /home/dev/work/project-3/素材/live.mp4）
    # 只有当 'work' 或 'output' 作为系统生成目录（位于 project_id 之后，且其后不含 '素材'/'materials'/'input'）时才生效
    for i, part in enumerate(parts[:-1]):
        if part in ("output", "work", "work_dir", "work_dirs"):
            # 排除 CI runner 路径（如 /home/runner/work/...）
            if i > 0 and parts[i-1] in ("runner", "actions-runner"):
                continue
            sub_parts = parts[i+1:]
            # 检查此 'work' / 'output' 后面是否有素材目录标记
            if any(sp in ("素材", "materials", "input") for sp in sub_parts):
                continue
            # 若给定了 project_id 且 project_id 在此 part 之后，说明也是宿主外层工作区目录
            if project_id and project_id in sub_parts:
                continue

            # 判定此 output/work 是否为真正的运行产物目录：
            # 1. 紧随其后的子级为典型 run_id 结构（run_*, run-*, 8位日期等）或直接为产物文件
            # 2. 或者该 output/work 位于已知 project_id 之后
            child = sub_parts[0] if sub_parts else ""
            is_run_like = (
                len(sub_parts) == 1 or
                child.startswith("run_") or
                child.startswith("run-") or
                re.match(r"^\d{8}", child) or
                child in ("latest", "temp", "cache") or
                (project_id and project_id in parts[:i])
            )
            if not is_run_like:
                continue
            return True, "文件位于 output/work 系统产物目录"

    base_name = os.path.basename(norm_p)
    stem, ext = os.path.splitext(base_name)
    stem_lower = stem.lower()

    # 2. 匹配 project_id 同名成片或标准输出名
    if project_id:
        pid_lower = project_id.lower()
        if stem_lower == pid_lower:
            return True, f"文件名匹配项目成片命名 '{pid_lower}'"
        if stem_lower in (f"{pid_lower}_final", f"{pid_lower}_cover", f"{pid_lower}_out"):
            return True, "文件名匹配历史成片派生命名"
    if stem_lower in ("final", "output", "render", "delivered", "out"):
        return True, "文件名匹配常见成品关键字"
    if stem_lower.endswith("_final") or stem_lower.startswith("final_"):
        return True, "文件名包含 final 标记"

    # 3. 伴随有 qc_report 或 generation_manifest 的视频
    parent_dir = os.path.dirname(norm_p)
    if os.path.isfile(os.path.join(parent_dir, f"{stem}_qc_report.json")):
        return True, "同目录下存在对应的质检报告，判定为历史成片"
    if os.path.isfile(os.path.join(parent_dir, f"{stem}_generation_manifest.json")):
        return True, "同目录下存在对应的生成清单，判定为历史成片"
    if os.path.isfile(os.path.join(parent_dir, "delivery_manifest.json")) and stem_lower in ("final", pid_lower if project_id else "final"):
        return True, "同目录下存在交付清单，判定为成片"

    # 4. 结合历史交付 Manifest 深度比对（无论是否位于标准子目录）
    try:
        # 扫描同级或上级 output/work 目录下的历史交付记录
        candidate_manifest_paths = []
        dm_curr = os.path.join(parent_dir, "delivery_manifest.json")
        if os.path.isfile(dm_curr):
            candidate_manifest_paths.append(dm_curr)
        out_sub = os.path.join(parent_dir, "output")
        if os.path.isdir(out_sub):
            for root, _, files in os.walk(out_sub):
                if "delivery_manifest.json" in files:
                    candidate_manifest_paths.append(os.path.join(root, "delivery_manifest.json"))
                if "generation_manifest.json" in files:
                    candidate_manifest_paths.append(os.path.join(root, "generation_manifest.json"))

        for mp in candidate_manifest_paths[:5]:  # 限制最多检查5个清单，兼顾性能
            with open(mp, "r", encoding="utf-8") as f:
                m_data = json.load(f)
            # 检查输出路径或视频文件名匹配
            deliv = m_data.get("final_deliverables", {}) or {}
            target_v = m_data.get("video_path") or deliv.get("video") or deliv.get("final_video")
            if target_v:
                if os.path.abspath(target_v) == norm_p or os.path.basename(target_v) == base_name:
                    return True, f"命中历史交付清单 ({os.path.basename(mp)}) 记录的成片"
    except Exception:
        pass

    return False, ""


def compute_full_file_sha256(file_path: str, block_size: int = 65536) -> str:
    """计算文件的完整标准 SHA-256 哈希值 (64位十六进制小写)。
    遍历整个文件所有字节，用于原始素材不可篡改防护与最终交付物严格审计。
    """
    if not os.path.isfile(file_path):
        return ""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(block_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def compute_file_fast_fingerprint(file_path: str, block_size: int = 65536, max_blocks: int = 16) -> str:
    """快速计算文件首尾块的采样指纹（16位十六进制）。
    仅读取首尾各约 1MB 区域，用于快速缓存键生成与初步文件辨识。
    """
    if not os.path.isfile(file_path):
        return ""
    h = hashlib.sha256()
    size = os.path.getsize(file_path)
    h.update(str(size).encode("utf-8"))
    with open(file_path, "rb") as f:
        # 读头部块
        for _ in range(max_blocks):
            chunk = f.read(block_size)
            if not chunk:
                break
            h.update(chunk)
        # 若大文件，跳至尾部读
        if size > block_size * max_blocks * 2:
            f.seek(max(0, size - block_size * max_blocks))
            while True:
                chunk = f.read(block_size)
                if not chunk:
                    break
                h.update(chunk)
    return h.hexdigest()[:16]


def compute_file_sha256(file_path: str, block_size: int = 65536, max_blocks: int = 16) -> str:
    """计算文件指纹哈希（兼顾性能与可比对性，默认返回快速采样指纹）。"""
    return compute_file_fast_fingerprint(file_path, block_size=block_size, max_blocks=max_blocks)


class InputManifest:
    """输入素材清单，提供明确的白名单与多源唯一标识映射。"""

    def __init__(self, project_paths: ProjectPaths):
        self.paths = project_paths
        self.sources: List[Dict[str, Any]] = []
        self.excluded: List[Dict[str, Any]] = []
        self.images: List[Dict[str, Any]] = []

    def scan_and_resolve(self, explicit_video_paths: Optional[List[str]] = None) -> Dict[str, Any]:
        """扫描与解析输入素材，生成结构化清单。
        如果提供了 explicit_video_paths，以此确认为准；
        否则在素材目录下扫描候选，并严格排除历史成品。
        """
        self.sources = []
        self.excluded = []
        self.images = []

        mat_dir = self.paths.materials_dir
        pid = self.paths.project_id

        # 收集图片素材
        if os.path.isdir(mat_dir):
            for fname in sorted(os.listdir(mat_dir)):
                fpath = os.path.join(mat_dir, fname)
                if not os.path.isfile(fpath):
                    continue
                if fname.lower().endswith(IMAGE_EXTENSIONS):
                    is_hist, reason = is_historical_output(fpath, pid, self.paths.project_root)
                    if is_hist:
                        self.excluded.append({
                            "path": fpath,
                            "type": "image",
                            "reason": reason,
                        })
                    else:
                        self.images.append({
                            "path": fpath,
                            "filename": fname,
                            "size": os.path.getsize(fpath),
                            "sha256": compute_full_file_sha256(fpath),
                            "fast_fingerprint": compute_file_fast_fingerprint(fpath),
                        })

        if explicit_video_paths is not None:
            # 严格依据用户确认的显式列表（若显式传入空列表 []，表示用户取消勾选全部视频，严禁回退扫描）
            src_idx = 1
            for raw_p in explicit_video_paths:
                if not raw_p or not str(raw_p).strip():
                    continue
                # 跨平台路径映射支持 (Windows盘符与WSL路径互通)
                norm_raw_p = normalize_project_path(str(raw_p).strip())
                if os.path.isabs(norm_raw_p):
                    abs_p = norm_raw_p
                else:
                    abs_p = os.path.normpath(os.path.join(mat_dir, norm_raw_p))

                if not os.path.isfile(abs_p):
                    # 尝试从 project_root 解析
                    alt_p = os.path.normpath(os.path.join(self.paths.project_root, norm_raw_p))
                    if os.path.isfile(alt_p):
                        abs_p = alt_p
                    else:
                        self.excluded.append({
                            "path": raw_p,
                            "type": "video",
                            "reason": "文件不存在",
                        })
                        continue

                is_hist, reason = is_historical_output(abs_p, pid, self.paths.project_root)
                if is_hist:
                    self.excluded.append({
                        "path": abs_p,
                        "type": "video",
                        "reason": f"显式指定的视频被确认为历史成片，拒绝作为原始素材入选: {reason}",
                    })
                    continue

                stem = os.path.splitext(os.path.basename(abs_p))[0]
                ext = os.path.splitext(os.path.basename(abs_p))[1].lstrip('.').lower()
                source_id = f"src_{src_idx:02d}_{sanitize_id(stem)}_{ext}" if ext else f"src_{src_idx:02d}_{sanitize_id(stem)}"
                src_idx += 1
                self.sources.append({
                    "source_id": source_id,
                    "path": abs_p,
                    "relative_path": os.path.relpath(abs_p, self.paths.project_root),
                    "filename": os.path.basename(abs_p),
                    "size": os.path.getsize(abs_p),
                    "mtime": os.path.getmtime(abs_p),
                    "sha256": compute_full_file_sha256(abs_p),
                    "fast_fingerprint": compute_file_fast_fingerprint(abs_p),
                    "is_confirmed": True,
                })
        else:
            # 未提供显式列表时，扫描素材目录，并排除历史成品
            if os.path.isdir(mat_dir):
                all_files = sorted(os.listdir(mat_dir))
                src_idx = 1
                for fname in all_files:
                    fpath = os.path.join(mat_dir, fname)
                    if not os.path.isfile(fpath):
                        continue
                    if not fname.lower().endswith(VIDEO_EXTENSIONS):
                        continue

                    is_hist, reason = is_historical_output(fpath, pid, self.paths.project_root)
                    if is_hist:
                        self.excluded.append({
                            "path": fpath,
                            "type": "video",
                            "reason": reason,
                        })
                        continue

                    stem = os.path.splitext(fname)[0]
                    ext = os.path.splitext(fname)[1].lstrip('.').lower()
                    source_id = f"src_{src_idx:02d}_{sanitize_id(stem)}_{ext}" if ext else f"src_{src_idx:02d}_{sanitize_id(stem)}"
                    src_idx += 1
                    self.sources.append({
                        "source_id": source_id,
                        "path": os.path.abspath(fpath),
                        "relative_path": os.path.relpath(fpath, self.paths.project_root),
                        "filename": fname,
                        "size": os.path.getsize(fpath),
                        "mtime": os.path.getmtime(fpath),
                        "sha256": compute_full_file_sha256(fpath),
                        "fast_fingerprint": compute_file_fast_fingerprint(fpath),
                        "is_confirmed": False,  # 扫描发现，待确认
                    })

        manifest = {
            "schema_version": "3.1",
            "project_id": pid,
            "project_root": self.paths.project_root,
            "materials_dir": self.paths.materials_dir,
            "run_id": self.paths.run_id,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source_videos": self.sources,
            "product_images": self.images,
            "excluded_files": self.excluded,
            "quarantined_files": self.excluded,
        }
        return manifest

    def save_manifest(self, manifest_data: Optional[Dict[str, Any]] = None) -> str:
        """保存清单到当前运行的 work_dir 目录，绝不污染素材目录。"""
        self.paths.ensure_runtime_dirs()
        out_p = os.path.join(self.paths.work_dir, "input_manifest.json")
        data = manifest_data or {
            "schema_version": "3.1",
            "project_id": self.paths.project_id,
            "run_id": self.paths.run_id,
            "source_videos": self.sources,
            "product_images": self.images,
            "excluded_files": self.excluded,
        }
        with open(out_p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return out_p


def compute_job_fingerprint(spec: Dict[str, Any], manifest: Optional[Dict[str, Any]] = None) -> str:
    """计算当前剪辑任务的确定性输入指纹。

    涵盖：原始视频哈希列表、商品图哈希列表、核心商品属性、剪辑配置及约束版本。
    任一项变动均使指纹改变，防止配置变更后复用旧成片。
    """
    h = hashlib.sha256()

    # 1. 原始视频指纹
    sources = (manifest.get("source_videos") if manifest else None) or []
    if sources:
        for s in sorted(sources, key=lambda x: str(x.get("path", ""))):
            h.update(str(s.get("sha256", "")).encode("utf-8"))
            h.update(str(s.get("size", "")).encode("utf-8"))
    else:
        vids = spec.get("raw_video_paths") or ([spec.get("raw_video_path")] if spec.get("raw_video_path") else [])
        for v in sorted(str(x) for x in vids if x):
            if os.path.isfile(v):
                h.update(compute_full_file_sha256(v).encode("utf-8"))
            else:
                h.update(v.encode("utf-8"))

    # 2. 商品参考图指纹
    imgs = (manifest.get("product_images") if manifest else None) or []
    if imgs:
        for im in sorted(imgs, key=lambda x: str(x.get("path", ""))):
            h.update(str(im.get("sha256", "")).encode("utf-8"))
    else:
        p_imgs = (spec.get("product", {}) or {}).get("item_images", [])
        for im in sorted(str(x) for x in p_imgs if x):
            if os.path.isfile(im):
                h.update(compute_full_file_sha256(im).encode("utf-8"))
            else:
                h.update(im.encode("utf-8"))

    # 3. 品牌要素指纹（修改品牌必换指纹，兼容 dict/str/None）
    brand = spec.get("brand") or {}
    if isinstance(brand, str):
        brand_core = {"title_en": brand.strip(), "subtitle_cn": ""}
    elif isinstance(brand, dict):
        brand_core = {
            "title_en": str(brand.get("title_en", "")).strip(),
            "subtitle_cn": str(brand.get("subtitle_cn", "")).strip(),
        }
    else:
        brand_core = {"title_en": "", "subtitle_cn": ""}
    h.update(json.dumps(brand_core, sort_keys=True, ensure_ascii=False).encode("utf-8"))

    # 4. 商品核心要素与规则配置
    prod = spec.get("product") or {}
    if not isinstance(prod, dict):
        prod = {}
    opts = spec.get("options") or {}
    if not isinstance(opts, dict):
        opts = {}
    config_core = {
        "sku_id": str(prod.get("sku_id", "")),
        "name": str(prod.get("name") or prod.get("title_line1", "")),
        "category": str(prod.get("category", "")),
        "title_line1": str(prod.get("title_line1", "")),
        "title_line2": str(prod.get("title_line2", "")),
        "attributes": dict(sorted((str(k), str(v)) for k, v in (prod.get("attributes") or {}).items())),
        "allowed_variants": sorted(str(x) for x in (prod.get("allowed_variants") or [])),
        "selling_points": sorted(str(x) for x in (prod.get("selling_points") or [])),
        "reference_images": sorted(str(x) for x in (prod.get("reference_images") or [])),
        "item_images": sorted(str(x) for x in (prod.get("item_images") or [])),
        "duration_range": str(opts.get("duration_range", "45-65")),
        "style_preset": str(opts.get("style_preset", "balanced")),
        "split_strategy": str(opts.get("split_strategy", "force_off")),
        "highlight_keywords": sorted(str(x) for x in (opts.get("highlight_keywords") or [])),
        "cover_strategy": str(opts.get("cover_strategy", "smart_frontal")),
        "features": opts.get("features", {}),
        "automation": opts.get("automation", {}),
        "tool_version": "3.2",
    }
    h.update(json.dumps(config_core, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return h.hexdigest()[:24]

