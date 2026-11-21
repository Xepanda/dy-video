# -*- coding: utf-8 -*-
"""
desktop_spec_creator.py - 抖音带货直播切片任务桌面配置器与智能向导 (基于 Edge WebView2 零网络端口)
================================================================================
纯本地原生 GUI 窗口，零本地 HTTP 端口监听，无防火墙拦截，支持系统原生文件选择对话框。
V3.1 项目向导交互模式：
  1. 选择已有项目或新建项目（统一项目目录：素材/、work/<run_id>/、output/<run_id>/）
  2. 自动识别原始视频与商品图片，展示时长、尺寸与勾选框，自动识别并安全隔离历史成片
  3. 结构化商品属性点选：品类下拉、颜色变体标签、真实卖点标签、字幕营销高亮词标签
  4. 剪辑风格与时长卡片化点选（时长、节奏、字幕、分屏、高级开关折叠）
  5. 后台异步执行流水线，实时展示阶段状态机与进度条
  6. 交付中心：一键播放成片、打开输出目录、查看 QC 质检报告与清单
"""

import os
import sys
import json
import time
import base64
import shutil
import subprocess
import threading
from typing import Dict, Any, List, Optional, Tuple


def get_project_root() -> str:
    """获取项目根目录绝对路径（兼顾 PyInstaller 打包与常规源码运行）。"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


PROJECT_ROOT = get_project_root()
DEFAULT_SPEC_PATH = os.path.join(PROJECT_ROOT, "job_spec.json")


def find_python_interpreter() -> str:
    """自动探测用于执行剪辑流水线的 Python 解释器。

    探测顺序（2026-10-30 去除硬编码个人路径，改按通用规则探测）：
      1. 环境变量 PROJECT_PYTHON / LIVECLIP_PYTHON（显式指定，优先级最高）
      2. 项目内置虚拟环境 .venv / venv
      3. WorkBuddy 托管环境 ~/.workbuddy/binaries/python/envs/<任意环境>/
      4. 未打包状态下用当前解释器 sys.executable
      5. 系统 PATH 中的 python
    """
    for env_key in ("PROJECT_PYTHON", "LIVECLIP_PYTHON"):
        env_py = os.environ.get(env_key)
        if env_py and os.path.exists(env_py):
            return env_py

    for candidate in [
        os.path.join(PROJECT_ROOT, ".venv", "Scripts", "python.exe"),
        os.path.join(PROJECT_ROOT, "venv", "Scripts", "python.exe"),
        os.path.join(PROJECT_ROOT, ".venv", "bin", "python"),
        os.path.join(PROJECT_ROOT, "venv", "bin", "python"),
    ]:
        if os.path.exists(candidate):
            return candidate

    envs_root = os.path.join(os.path.expanduser("~"), ".workbuddy",
                             "binaries", "python", "envs")
    if os.path.isdir(envs_root):
        names = sorted(os.listdir(envs_root),
                       key=lambda n: (n != "liveclip", n))
        for name in names:
            for rel in (("Scripts", "python.exe"), ("bin", "python")):
                cand = os.path.join(envs_root, name, *rel)
                if os.path.exists(cand):
                    return cand

    if not getattr(sys, 'frozen', False) and sys.executable:
        return sys.executable

    sys_py = shutil.which("python")
    if sys_py:
        return sys_py

    return "python"


def probe_video_duration(path: str) -> float:
    """轻量探测视频文件时长（秒）。若探测失败则返回 0.0。"""
    if not path or not os.path.isfile(path):
        return 0.0
    try:
        cmd = [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
            path
        ]
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=5)
        return round(float(out.strip()), 1)
    except Exception:
        return 0.0


def generate_image_thumbnail_b64(image_path: str, max_dim: int = 120) -> str:
    """生成图片的 Base64 Data URL 缩略图。"""
    if not image_path or not os.path.isfile(image_path):
        return ""
    try:
        import cv2
        from src.core.path_resolver import cv2_imread
        img = cv2_imread(image_path)
        if img is None:
            return ""
        h, w = img.shape[:2]
        scale = min(max_dim / max(w, 1), max_dim / max(h, 1))
        if scale < 1.0:
            new_w, new_h = max(1, int(w * scale)), max(1, int(h * scale))
            img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 65])
        if ok:
            return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii")
    except Exception:
        pass
    return ""


def generate_video_thumbnail_b64(video_path: str, max_w: int = 120, max_h: int = 68) -> str:
    """轻量抽取视频 1.0s 处关键帧生成 Base64 Data URL 缩略图。"""
    if not video_path or not os.path.isfile(video_path):
        return ""
    try:
        import cv2
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return ""
        cap.set(cv2.CAP_PROP_POS_MSEC, 1000.0)
        ok, frame = cap.read()
        if not ok or frame is None:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = cap.read()
        cap.release()
        if ok and frame is not None:
            h, w = frame.shape[:2]
            scale = min(max_w / max(w, 1), max_h / max(h, 1))
            new_w, new_h = max(1, int(w * scale)), max(1, int(h * scale))
            small = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), 55])
            if ok:
                return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii")
    except Exception:
        pass
    return ""


class SpecManager:
    """负责 job_spec.json 规范构建、校验与读写。"""

    def validate(self, payload: Dict[str, Any]) -> List[str]:
        errors = []
        project_id = str(payload.get("project_id") or "").strip()
        if not project_id:
            errors.append("任务代号 (project_id) 不能为空")

        raw_video_path = str(payload.get("raw_video_path") or "").strip()
        raw_video_paths = payload.get("raw_video_paths") or []
        if not raw_video_path and not raw_video_paths:
            errors.append("原始直播视频路径 (raw_video_path) 不能为空")

        brand_en = str(payload.get("brand_en") or "").strip()
        if not brand_en:
            errors.append("品牌主英文 (brand_en) 不能为空")

        p_l1 = str(payload.get("product_title_l1") or "").strip()
        p_l2 = str(payload.get("product_title_l2") or "").strip()
        p_name = str(payload.get("product_name") or payload.get("name") or "").strip()
        if not p_l1 and not p_l2 and not p_name:
            errors.append("商品名称至少填写一行")

        try:
            from src.tools.feature_flags import validate as _fv
            errors.extend(_fv(payload))
        except Exception as e:
            print(f"[SPEC][WARN] 开关校验不可用: {e}")

        return errors

    def build_spec_dict(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        project_id = str(payload.get("project_id") or "demo").strip()
        raw_video_path = str(payload.get("raw_video_path") or "").strip()
        explicit_vpaths = payload.get("raw_video_paths")
        if explicit_vpaths is not None:
            raw_video_paths = [str(p).strip() for p in explicit_vpaths if str(p).strip()]
        else:
            raw_video_paths = [raw_video_path] if raw_video_path else []

        brand_en = str(payload.get("brand_en") or "").strip()
        brand_cn = str(payload.get("brand_cn") or "").strip()

        p_l1 = str(payload.get("product_title_l1") or "").strip()
        p_l2 = str(payload.get("product_title_l2") or "").strip()
        p_name = str(payload.get("product_name") or payload.get("name") or "").strip()
        if not p_l1 and p_name:
            p_l1 = p_name

        item_images = []
        for key in ["product_img_1", "product_img_2"]:
            img = str(payload.get(key) or "").strip()
            if img and img not in item_images:
                item_images.append(img)
        if "item_images" in payload and isinstance(payload["item_images"], list):
            for img in payload["item_images"]:
                s = str(img).strip()
                if s and s not in item_images:
                    item_images.append(s)

        duration_range = str(payload.get("duration_range") or "45-65").strip()
        split_strategy = str(payload.get("split_strategy") or "force_off").strip()

        raw_keywords = payload.get("keywords") or payload.get("highlight_keywords") or []
        if isinstance(raw_keywords, str):
            parts = raw_keywords.replace("，", ",").replace("、", ",").replace(";", ",").replace("\n", ",").split(",")
            keywords = [p.strip() for p in parts if p.strip()]
        elif isinstance(raw_keywords, list):
            keywords = [str(k).strip() for k in raw_keywords if str(k).strip()]
        else:
            keywords = []

        sku_id = str(payload.get("sku_id") or payload.get("sku") or "").strip()
        category = str(payload.get("category") or "").strip()
        attributes = dict(payload.get("attributes") or {})
        allowed_variants = list(payload.get("allowed_variants") or [])
        selling_points = list(payload.get("selling_points") or [])

        # V3 严格解耦：卖点为事实声明，highlight_keywords 为字幕高亮，不自动混合
        automation = dict(payload.get("automation") or {})

        prod_dict = {
            "title_line1": p_l1,
            "title_line2": p_l2,
            "item_images": item_images
        }
        if sku_id:
            prod_dict["sku_id"] = sku_id
        if category:
            prod_dict["category"] = category
        if p_name:
            prod_dict["name"] = p_name
        if attributes:
            prod_dict["attributes"] = attributes
        if allowed_variants:
            prod_dict["allowed_variants"] = allowed_variants
        if selling_points:
            prod_dict["selling_points"] = selling_points

        feat_dict = dict(payload.get("features") or {})
        sub_strat = str(payload.get("subtitle_strategy") or "").strip()
        if sub_strat in ("plain", "branded"):
            feat_dict["render_profile"] = sub_strat
        if payload.get("render_profile"):
            feat_dict["render_profile"] = str(payload["render_profile"]).strip()

        opt_dict = {
            "duration_range": duration_range,
            "split_strategy": split_strategy,
            "highlight_keywords": keywords,
            "features": feat_dict,
        }
        if "style_preset" in payload:
            opt_dict["style_preset"] = payload["style_preset"]
        if "cover_strategy" in payload:
            opt_dict["cover_strategy"] = str(payload["cover_strategy"]).strip()
        if automation:
            opt_dict["automation"] = automation

        spec = {
            "project_id": project_id,
            "raw_video_path": raw_video_path,
            "raw_video_paths": raw_video_paths,
            "brand": {
                "title_en": brand_en,
                "subtitle_cn": brand_cn
            },
            "product": prod_dict,
            "options": opt_dict
        }
        return spec

    def save_spec(self, spec: Dict[str, Any], path: str = DEFAULT_SPEC_PATH) -> bool:
        abs_path = os.path.abspath(path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            json.dump(spec, f, ensure_ascii=False, indent=2)
        return True

    def load_spec(self, path: str = DEFAULT_SPEC_PATH) -> Optional[Dict[str, Any]]:
        abs_path = os.path.abspath(path)
        if not os.path.exists(abs_path):
            return None
        with open(abs_path, "r", encoding="utf-8") as f:
            return json.load(f)


class DesktopSpecApi:
    """提供给前端 JS 调用的桌面原生桥接 API（零网络端口）。"""

    def __init__(self, spec_path: Optional[str] = None):
        self._custom_spec_path = os.path.abspath(spec_path) if spec_path else None
        self._manager = SpecManager()
        self._window = None
        self._lock = threading.Lock()
        self._job_thread: Optional[threading.Thread] = None
        self._current_runner = None
        self._job_state: Dict[str, Any] = {
            "status": "IDLE",
            "state": "IDLE",
            "progress": 0.0,
            "message": "就绪",
            "logs": [],
            "result": None,
            "error": None,
            "output_dir": None,
            "final_video": None,
            "qc_report": None,
        }

    def set_window(self, window):
        self._window = window

    def resolve_material_dir(self, raw_video_path: str) -> Optional[str]:
        """解析用户提供的素材目录绝对路径。"""
        if not raw_video_path:
            return None
        abs_p = os.path.abspath(raw_video_path)
        if os.path.isdir(abs_p):
            return abs_p
        elif os.path.isfile(abs_p):
            return os.path.dirname(abs_p)
        elif os.path.isdir(os.path.dirname(abs_p)):
            return os.path.dirname(abs_p)
        return None

    def load_initial_data(self) -> Dict[str, Any]:
        """加载初始数据供界面展示（仅作为表单默认填项，不填入未经证实的虚假材质）。"""
        default_data = {
            "project_id": "demo_project",
            "raw_video_path": "",
            "raw_video_paths": [],
            "brand_en": "",
            "brand_cn": "",
            "product_title_l1": "",
            "product_title_l2": "",
            "product_img_1": "",
            "product_img_2": "",
            "category": "女装/毛针织",
            "allowed_variants": [],
            "selling_points": [],
            "duration_range": "45-65",
            "split_strategy": "force_off",
            "style_preset": "balanced",
            "keywords": "",
            "features": {},
            "has_existing": False,
            "spec_path": "",
        }
        if os.path.exists(DEFAULT_SPEC_PATH):
            try:
                loaded = self._manager.load_spec(DEFAULT_SPEC_PATH)
                if loaded:
                    brand = loaded.get("brand", {})
                    product = loaded.get("product", {})
                    options = loaded.get("options", {})
                    imgs = product.get("item_images", [])
                    default_data.update({
                        "project_id": loaded.get("project_id", default_data["project_id"]),
                        "raw_video_path": loaded.get("raw_video_path", default_data["raw_video_path"]),
                        "raw_video_paths": loaded.get("raw_video_paths", []),
                        "brand_en": brand.get("title_en", default_data["brand_en"]),
                        "brand_cn": brand.get("subtitle_cn", default_data["brand_cn"]),
                        "product_title_l1": product.get("title_line1", ""),
                        "product_title_l2": product.get("title_line2", ""),
                        "category": product.get("category", default_data["category"]),
                        "allowed_variants": product.get("allowed_variants", []),
                        "selling_points": product.get("selling_points", []),
                        "product_img_1": imgs[0] if len(imgs) > 0 else "",
                        "product_img_2": imgs[1] if len(imgs) > 1 else "",
                        "duration_range": options.get("duration_range", default_data["duration_range"]),
                        "split_strategy": options.get("split_strategy", default_data["split_strategy"]),
                        "keywords": "，".join([k for k in options.get("highlight_keywords", []) if k not in ("桑蚕丝", "羊绒", "羊毛")]),
                        "features": options.get("features") or {},
                        "has_existing": True,
                    })
            except Exception:
                pass
        return default_data

    def select_video_file(self) -> Optional[str]:
        """弹出原生文件选择对话框（单视频文件）。"""
        if not self._window:
            return None
        import webview
        file_types = ("视频文件 (*.mp4;*.mov;*.mkv;*.ts;*.flv)", "所有文件 (*.*)")
        res = self._window.create_file_dialog(
            dialog_type=webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=file_types
        )
        if res and len(res) > 0:
            return os.path.normpath(res[0])
        return None

    def select_video_folder(self) -> Optional[str]:
        """弹出原生文件夹选择对话框（视频素材目录）。"""
        if not self._window:
            return None
        import webview
        res = self._window.create_file_dialog(
            dialog_type=webview.FOLDER_DIALOG
        )
        if res and len(res) > 0:
            return os.path.normpath(res[0])
        return None

    def select_project_folder(self) -> Optional[str]:
        """弹出原生文件夹选择对话框（选择项目根目录）。"""
        if not self._window:
            return None
        import webview
        res = self._window.create_file_dialog(
            dialog_type=webview.FOLDER_DIALOG
        )
        if res and len(res) > 0:
            return os.path.normpath(res[0])
        return None

    def select_image_file(self) -> Optional[str]:
        """弹出原生图片选择对话框。"""
        if not self._window:
            return None
        import webview
        file_types = ("图片文件 (*.png;*.jpg;*.jpeg;*.webp)", "所有文件 (*.*)")
        res = self._window.create_file_dialog(
            dialog_type=webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=file_types
        )
        if res and len(res) > 0:
            return os.path.normpath(res[0])
        return None

    def create_new_project(self, project_dir: str, project_id: Optional[str] = None) -> Dict[str, Any]:
        """新建项目工作目录，初始化 素材/、work/ 与 output/ 结构。"""
        if not project_dir:
            return {"success": False, "message": "项目目录路径不能为空"}
        try:
            norm_input = os.path.normpath(os.path.abspath(project_dir))
            # 兼容两种调用模式：
            # 1) (parent_folder, project_name) -> 自动安全拼接子目录
            # 2) (full_project_path, optional_project_id) -> 直接使用完整目录
            if project_id and os.path.isdir(norm_input) and os.path.basename(norm_input) != project_id:
                norm_dir = os.path.join(norm_input, project_id)
                pid = project_id
            else:
                norm_dir = norm_input
                pid = project_id or os.path.basename(norm_dir)

            mat_dir = os.path.join(norm_dir, "素材")
            os.makedirs(mat_dir, exist_ok=True)
            from src.core.project_context import ProjectPaths
            paths = ProjectPaths(project_root=norm_dir)
            paths.ensure_runtime_dirs()

            spec_p = paths.spec_path
            if not os.path.isfile(spec_p):
                init_spec = {
                    "project_id": pid,
                    "raw_video_path": "",
                    "raw_video_paths": [],
                    "brand": {
                        "title_en": "",
                        "subtitle_cn": ""
                    },
                    "product": {
                        "title_line1": "",
                        "title_line2": "",
                        "item_images": []
                    },
                    "options": {
                        "duration_range": "45-65",
                        "split_strategy": "force_off",
                        "style_preset": "balanced",
                        "cover_strategy": "smart_frontal",
                        "highlight_keywords": [],
                        "features": {
                            "render_profile": "branded"
                        }
                    }
                }
                with open(spec_p, "w", encoding="utf-8") as f:
                    json.dump(init_spec, f, ensure_ascii=False, indent=2)

            scan_res = self.scan_project(norm_dir)
            scan_res["message"] = f"成功新建并初始化项目：{pid}"
            return scan_res
        except Exception as e:
            return {"success": False, "message": f"新建项目失败: {str(e)}"}

    def scan_project(self, project_dir: str) -> Dict[str, Any]:
        """扫描项目目录，使用 ProjectPaths 和 InputManifest 分析素材与隔离历史成片。"""
        if not project_dir or not os.path.isdir(project_dir):
            return {"success": False, "message": f"目录不存在: {project_dir}"}
        try:
            from src.core.project_context import ProjectPaths, InputManifest
            paths = ProjectPaths(project_root=project_dir)
            manifest_mgr = InputManifest(paths)
            manifest = manifest_mgr.scan_and_resolve()

            sources = []
            for s in manifest.get("source_videos", []):
                p = s["path"]
                sz_mb = round(os.path.getsize(p) / (1024 * 1024), 2) if os.path.isfile(p) else 0.0
                dur = probe_video_duration(p)
                dur_fmt = f"{int(dur // 60):02d}:{int(dur % 60):02d}" if dur > 0 else "--:--"
                thumb_b64 = generate_video_thumbnail_b64(p, max_w=120, max_h=68)
                sources.append({
                    "source_id": s["source_id"],
                    "path": p,
                    "filename": s["filename"],
                    "size_mb": sz_mb,
                    "duration_sec": dur,
                    "duration_fmt": dur_fmt,
                    "thumbnail": thumb_b64,
                    "selected": True,
                })

            quarantined = []
            for ex in (manifest.get("quarantined_files") or manifest.get("excluded_files") or []):
                p = ex["path"]
                quarantined.append({
                    "path": p,
                    "filename": os.path.basename(p),
                    "type": ex.get("type", "unknown"),
                    "reason": ex.get("reason", "历史成片或系统产物已隔离"),
                })

            images = []
            raw_imgs = manifest.get("product_images") or manifest.get("images") or []
            for img in raw_imgs:
                p = img["path"]
                thumb_b64 = generate_image_thumbnail_b64(p, max_dim=120)
                images.append({
                    "path": p,
                    "filename": img["filename"],
                    "size_kb": round(img.get("size", 0) / 1024, 1),
                    "thumbnail": thumb_b64,
                })

            existing_spec = None
            if os.path.isfile(paths.spec_path):
                try:
                    with open(paths.spec_path, "r", encoding="utf-8") as f:
                        existing_spec = json.load(f)
                except Exception:
                    pass

            return {
                "success": True,
                "project_root": paths.project_root,
                "materials_dir": paths.materials_dir,
                "project_id": paths.project_id,
                "source_videos": sources,
                "quarantined_files": quarantined,
                "images": images,
                "existing_spec": existing_spec,
            }
        except Exception as e:
            return {"success": False, "message": f"扫描项目失败: {str(e)}"}

    def save_spec(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """校验并保存配置至用户素材目录，绝不修改仓库，并打开系统文件资源管理器。"""
        errors = self._manager.validate(payload)
        if errors:
            return {"success": False, "message": "；".join(errors)}

        project_root = payload.get("project_root")
        raw_video = str(payload.get("raw_video_path") or "").strip()
        raw_video_paths = payload.get("raw_video_paths") or []

        if project_root and os.path.isdir(project_root):
            from src.core.project_context import ProjectPaths
            paths = ProjectPaths(project_root=project_root)
            material_dir = paths.materials_dir
            target_path = paths.spec_path
        elif self._custom_spec_path:
            target_path = self._custom_spec_path
            material_dir = os.path.dirname(target_path)
        else:
            material_dir = self.resolve_material_dir(raw_video)
            if not material_dir and raw_video_paths and isinstance(raw_video_paths, list):
                material_dir = self.resolve_material_dir(raw_video_paths[0])
            if material_dir:
                from src.core.project_context import ProjectPaths
                paths = ProjectPaths(project_root=material_dir)
                material_dir = paths.materials_dir
                target_path = os.path.join(material_dir, "job_spec.json")
            else:
                return {"success": False, "message": f"未找到有效的素材路径，请先选择视频文件或素材文件夹（当前填写：{raw_video}）"}

        try:
            spec = self._manager.build_spec_dict(payload)
            self._manager.save_spec(spec, target_path)

            project_id = str(payload.get("project_id") or "").strip()
            if project_id and project_id != "job_spec":
                p_spec = os.path.join(material_dir, f"{project_id}_job_spec.json")
                if os.path.abspath(p_spec) != os.path.abspath(target_path):
                    shutil.copy2(target_path, p_spec)

            if not self._custom_spec_path and not os.environ.get("PYTEST_CURRENT_TEST"):
                try:
                    if os.name == "nt":
                        norm_p = os.path.normpath(target_path)
                        subprocess.Popen(f'explorer.exe /select,"{norm_p}"')
                    else:
                        subprocess.Popen(["xdg-open", material_dir])
                except Exception as e:
                    print(f"[SPEC][WARN] 打开资源管理器失败: {e}")

            return {
                "success": True,
                "message": f"任务配置已生成至素材目录：\n{target_path}\n已自动为您打开文件资源管理器！",
                "spec": spec,
                "target_path": target_path,
                "material_dir": material_dir
            }
        except Exception as e:
            return {"success": False, "message": f"保存配置失败: {str(e)}"}

    def start_pipeline(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """校验、保存配置并调度 JobRunner 端到端执行一键全自动智能剪辑（同步执行，兼顾测试）。"""
        raw_vids = payload.get("raw_video_paths")
        if raw_vids is not None and len(raw_vids) == 0:
            return {
                "success": False,
                "status": "NEEDS_INPUT",
                "message": "未勾选任何原始视频素材，请在素材清单中勾选至少一段原始直播视频！",
            }

        save_res = self.save_spec(payload)
        if not save_res.get("success"):
            return save_res

        spec = save_res["spec"]
        material_dir = save_res["material_dir"]
        from src.core.project_context import ProjectPaths
        paths = ProjectPaths(project_root=material_dir)
        paths.ensure_runtime_dirs()

        try:
            from src.runtime.job_runner import JobRunner
            runner = JobRunner(spec, paths.work_dir, paths.output_dir, project_paths=paths)
            result = runner.run_pipeline()
            is_delivered = (result.get("status") == "DELIVERED")
            return {
                "success": is_delivered,
                "status": result.get("status"),
                "message": f"剪辑任务执行状态: {result.get('status')}",
                "result": result,
                "material_dir": material_dir,
                "work_dir": paths.work_dir,
                "output_dir": paths.output_dir,
                "final_video": result.get("final_video") or result.get("video_path"),
            }
        except Exception as e:
            return {
                "success": False,
                "status": "FAILED",
                "message": f"启动剪辑任务异常: {str(e)}"
            }

    def start_pipeline_async(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """后台异步启动剪辑流水线，支持实时状态与阶段轮询。"""
        raw_vids = payload.get("raw_video_paths")
        if raw_vids is not None and len(raw_vids) == 0:
            return {
                "success": False,
                "status": "NEEDS_INPUT",
                "message": "未勾选任何原始视频素材，请在素材清单中勾选至少一段原始直播视频！",
            }

        with self._lock:
            if self._job_thread and self._job_thread.is_alive():
                return {"success": False, "message": "任务正在执行中，请等待完成或取消后再试"}

            self._job_state = {
                "status": "RUNNING",
                "state": "QUEUED",
                "progress": 0.05,
                "message": "任务初始化中...",
                "logs": [f"[{time.strftime('%H:%M:%S')}] [INIT] 正在初始化任务与工作目录..."],
                "result": None,
                "error": None,
                "output_dir": None,
                "final_video": None,
                "qc_report": None,
            }

            save_res = self.save_spec(payload)
            if not save_res.get("success"):
                self._job_state["status"] = "FAILED"
                self._job_state["error"] = save_res.get("message")
                return save_res

            spec = save_res["spec"]
            material_dir = save_res["material_dir"]
            from src.core.project_context import ProjectPaths
            paths = ProjectPaths(project_root=material_dir)
            paths.ensure_runtime_dirs()

            from src.runtime.job_runner import JobRunner
            runner = JobRunner(spec, paths.work_dir, paths.output_dir, project_paths=paths)
            self._current_runner = runner

            def _progress_cb(state: str, progress: float, msg: str):
                with self._lock:
                    self._job_state["state"] = state
                    self._job_state["progress"] = progress
                    self._job_state["message"] = msg
                    ts = time.strftime("%H:%M:%S")
                    self._job_state["logs"].append(f"[{ts}] [{state}] {msg}")
                    if len(self._job_state["logs"]) > 250:
                        self._job_state["logs"].pop(0)

            def _thread_target():
                try:
                    res = runner.run_pipeline(progress_callback=_progress_cb)
                    with self._lock:
                        self._job_state["result"] = res
                        st = res.get("status")
                        self._job_state["state"] = st
                        self._job_state["status"] = "DELIVERED" if st == "DELIVERED" else "FAILED"
                        self._job_state["progress"] = 1.0
                        self._job_state["output_dir"] = paths.output_dir

                        final_vid = res.get("final_video") or res.get("video_path")
                        if not final_vid:
                            dmf = os.path.join(paths.output_dir, "delivery_manifest.json")
                            if os.path.isfile(dmf):
                                try:
                                    with open(dmf, "r", encoding="utf-8") as f:
                                        d_info = json.load(f)
                                        final_vid = d_info.get("video_path") or (d_info.get("final_deliverables", {}) or {}).get("video")
                                except Exception:
                                    pass
                        self._job_state["final_video"] = final_vid

                        qc_p = os.path.join(paths.output_dir, "qc_report.json")
                        self._job_state["qc_report"] = qc_p if os.path.isfile(qc_p) else None
                except Exception as e:
                    with self._lock:
                        self._job_state["status"] = "FAILED"
                        self._job_state["error"] = str(e)
                        self._job_state["logs"].append(f"[ERROR] 执行异常: {str(e)}")

            t = threading.Thread(target=_thread_target, daemon=True)
            self._job_thread = t
            t.start()
            return {
                "success": True,
                "message": "剪辑任务已在后台启动",
                "output_dir": paths.output_dir,
                "work_dir": paths.work_dir
            }

    def get_job_status(self) -> Dict[str, Any]:
        """获取当前后台任务的实时进度与日志状态。"""
        with self._lock:
            return dict(self._job_state)

    def cancel_pipeline(self) -> Dict[str, Any]:
        """取消当前运行中的任务。"""
        with self._lock:
            if self._current_runner:
                try:
                    self._current_runner.cancel()
                    self._job_state["status"] = "CANCELED"
                    self._job_state["message"] = "任务已被用户取消"
                    return {"success": True, "message": "已发送取消信号"}
                except Exception as e:
                    return {"success": False, "message": f"取消失败: {e}"}
            return {"success": False, "message": "当前没有运行中的任务"}

    def open_path(self, target_path: str) -> Dict[str, Any]:
        """打开文件或目录。"""
        if not target_path or not os.path.exists(target_path):
            return {"success": False, "message": f"路径不存在: {target_path}"}
        try:
            norm_p = os.path.normpath(os.path.abspath(target_path))
            if os.name == "nt":
                os.startfile(norm_p)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", norm_p])
            else:
                subprocess.Popen(["xdg-open", norm_p])
            return {"success": True}
        except Exception as e:
            return {"success": False, "message": f"打开路径失败: {str(e)}"}

    def open_video(self, video_path: str) -> Dict[str, Any]:
        """使用系统默认播放器打开成片视频。"""
        return self.open_path(video_path)

    def open_output_dir(self, output_dir: str) -> Dict[str, Any]:
        """打开输出目录。"""
        return self.open_path(output_dir)

    def open_qc_report(self, qc_report_path: str) -> Dict[str, Any]:
        """打开 QC 质检报告。"""
        return self.open_path(qc_report_path)


def get_html_content() -> str:
    """生成内嵌的现代化暗黑风格向导式桌面 UI（无任何外部网络依赖）。"""
    try:
        from src.tools.feature_flags import load_definitions
        _feat_json = json.dumps(load_definitions(), ensure_ascii=False)
    except Exception as _e:
        print(f"[SPEC][WARN] 开关定义不可用，将隐藏开关面板: {_e}")
        _feat_json = '{"groups": [], "combos": []}'

    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>抖音带货直播剪辑 · 智能桌面向导</title>
    <style>
        :root {
            --primary: #f59e0b;
            --primary-hover: #d97706;
            --success: #10b981;
            --success-hover: #059669;
            --accent: #3b82f6;
            --danger: #ef4444;
            --bg: #0b1120;
            --card: #1e293b;
            --card-sub: #162032;
            --border: #334155;
            --border-active: #f59e0b;
            --text: #f8fafc;
            --muted: #94a3b8;
            --input-bg: #0f172a;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif;
            background: var(--bg);
            color: var(--text);
            min-height: 100vh;
            padding: 20px 24px 40px;
            user-select: none;
            overflow-y: auto;
        }
        .container {
            max-width: 920px;
            margin: 0 auto;
        }
        .header {
            text-align: center;
            margin-bottom: 20px;
        }
        .header h1 {
            font-size: 24px;
            font-weight: 700;
            color: #fbbf24;
            margin-bottom: 6px;
            letter-spacing: 0.5px;
        }
        .header p {
            font-size: 13px;
            color: var(--muted);
        }

        /* 顶部向导步进条 */
        .step-nav {
            display: flex;
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 6px;
            margin-bottom: 22px;
            gap: 6px;
        }
        .step-tab {
            flex: 1;
            text-align: center;
            padding: 10px 4px;
            border-radius: 8px;
            font-size: 13px;
            font-weight: 600;
            color: var(--muted);
            cursor: pointer;
            transition: all 0.2s;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
        }
        .step-tab:hover {
            color: var(--text);
            background: rgba(255, 255, 255, 0.05);
        }
        .step-tab.active {
            background: linear-gradient(135deg, #f59e0b, #d97706);
            color: #ffffff;
            box-shadow: 0 2px 10px rgba(245, 158, 11, 0.35);
        }

        /* 步骤内容面板 */
        .step-panel {
            display: none;
        }
        .step-panel.active {
            display: block;
            animation: fadeIn 0.25s ease-out;
        }
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(4px); }
            to { opacity: 1; transform: translateY(0); }
        }

        .card {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 14px;
            padding: 20px 22px;
            margin-bottom: 18px;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.25);
        }
        .card-title {
            font-size: 15px;
            font-weight: 600;
            color: #f1f5f9;
            margin-bottom: 14px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        .form-group {
            margin-bottom: 14px;
        }
        .form-group:last-child {
            margin-bottom: 0;
        }
        label {
            display: block;
            font-size: 13px;
            font-weight: 500;
            color: #cbd5e1;
            margin-bottom: 6px;
        }
        .hint {
            font-size: 12px;
            color: var(--muted);
            margin-top: 4px;
            line-height: 1.4;
        }
        .input-row {
            display: flex;
            gap: 8px;
        }
        input[type="text"], select, textarea {
            width: 100%;
            padding: 10px 12px;
            border-radius: 8px;
            border: 1px solid var(--border);
            background: var(--input-bg);
            color: var(--text);
            font-size: 13px;
            outline: none;
            transition: border-color 0.2s;
        }
        input:focus, select:focus, textarea:focus {
            border-color: var(--primary);
        }
        .btn-browse {
            padding: 10px 16px;
            background: #334155;
            color: #f8fafc;
            border: 1px solid #475569;
            border-radius: 8px;
            font-size: 13px;
            cursor: pointer;
            white-space: nowrap;
            transition: background 0.15s;
        }
        .btn-browse:hover {
            background: #475569;
        }
        .grid-2 {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
        }

        /* 标签选择器 */
        .tag-cloud {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-top: 8px;
        }
        .tag-item {
            padding: 6px 12px;
            background: var(--input-bg);
            border: 1px solid var(--border);
            border-radius: 20px;
            font-size: 12px;
            color: #cbd5e1;
            cursor: pointer;
            transition: all 0.15s;
            user-select: none;
        }
        .tag-item:hover {
            border-color: #64748b;
            color: #fff;
        }
        .tag-item.active {
            background: rgba(245, 158, 11, 0.2);
            border-color: var(--primary);
            color: #fbbf24;
            font-weight: 600;
        }

        /* 策略卡片网格 */
        .card-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
            gap: 12px;
            margin-top: 8px;
        }
        .choice-card {
            background: var(--card-sub);
            border: 1.5px solid var(--border);
            border-radius: 10px;
            padding: 14px 16px;
            cursor: pointer;
            transition: all 0.2s;
            position: relative;
        }
        .choice-card:hover {
            border-color: #64748b;
            transform: translateY(-1px);
        }
        .choice-card.active {
            border-color: var(--primary);
            background: rgba(245, 158, 11, 0.08);
            box-shadow: 0 0 12px rgba(245, 158, 11, 0.2);
        }
        .choice-title {
            font-size: 14px;
            font-weight: 600;
            color: #f1f5f9;
            margin-bottom: 4px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        .choice-card.active .choice-title {
            color: #fbbf24;
        }
        .choice-desc {
            font-size: 12px;
            color: var(--muted);
            line-height: 1.4;
        }

        /* 隔离区告警框 */
        .quarantine-box {
            background: rgba(234, 179, 8, 0.1);
            border: 1px solid rgba(234, 179, 8, 0.35);
            border-radius: 10px;
            padding: 12px 16px;
            margin-bottom: 14px;
        }
        .quarantine-title {
            color: #facc15;
            font-size: 13px;
            font-weight: 600;
            margin-bottom: 6px;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .quarantine-list {
            font-size: 12px;
            color: #e2e8f0;
            padding-left: 20px;
            line-height: 1.6;
        }

        /* 素材列表 */
        .material-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
            margin-top: 8px;
        }
        .material-table th, .material-table td {
            padding: 10px 12px;
            text-align: left;
            border-bottom: 1px solid #283548;
        }
        .material-table th {
            color: var(--muted);
            font-weight: 500;
        }
        .badge {
            display: inline-block;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
        }
        .badge-success { background: rgba(16, 185, 129, 0.2); color: #34d399; }
        .badge-info { background: rgba(59, 130, 246, 0.2); color: #60a5fa; }
        .badge-warn { background: rgba(245, 158, 11, 0.2); color: #fbbf24; }

        /* 导航与动作按钮 */
        .step-actions {
            display: flex;
            justify-content: space-between;
            margin-top: 24px;
            gap: 12px;
        }
        .btn-prev {
            padding: 12px 24px;
            background: #334155;
            color: #f8fafc;
            border: 1px solid #475569;
            border-radius: 10px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: background 0.15s;
        }
        .btn-prev:hover { background: #475569; }
        .btn-next {
            padding: 12px 28px;
            background: linear-gradient(135deg, #3b82f6, #2563eb);
            color: #ffffff;
            border: none;
            border-radius: 10px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.15s;
        }
        .btn-next:hover { opacity: 0.95; transform: translateY(-1px); }

        .btn-run-primary {
            padding: 14px 28px;
            border: none;
            border-radius: 10px;
            font-size: 16px;
            font-weight: 700;
            cursor: pointer;
            background: linear-gradient(135deg, #10b981, #059669);
            color: #ffffff;
            box-shadow: 0 4px 15px rgba(16, 185, 129, 0.35);
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            transition: transform 0.1s, opacity 0.15s, box-shadow 0.2s;
        }
        .btn-run-primary:hover {
            opacity: 0.95;
            box-shadow: 0 6px 20px rgba(16, 185, 129, 0.5);
            transform: translateY(-1px);
        }
        .btn-run-primary:disabled {
            opacity: 0.5;
            cursor: not-allowed;
            transform: none !important;
        }

        /* 进度条与状态机 */
        .stage-flow {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin: 16px 0;
        }
        .stage-node {
            flex: 1;
            min-width: 85px;
            padding: 8px 6px;
            background: var(--input-bg);
            border: 1px solid var(--border);
            border-radius: 8px;
            text-align: center;
            font-size: 11px;
            color: var(--muted);
            transition: all 0.2s;
        }
        .stage-node.active {
            border-color: var(--primary);
            color: #fbbf24;
            background: rgba(245, 158, 11, 0.12);
            font-weight: 600;
            animation: pulse 1.5s infinite;
        }
        .stage-node.done {
            border-color: var(--success);
            color: #34d399;
            background: rgba(16, 185, 129, 0.12);
        }
        @keyframes pulse {
            0% { box-shadow: 0 0 0 0 rgba(245, 158, 11, 0.4); }
            70% { box-shadow: 0 0 0 6px rgba(245, 158, 11, 0); }
            100% { box-shadow: 0 0 0 0 rgba(245, 158, 11, 0); }
        }

        .progress-bar-container {
            width: 100%;
            height: 10px;
            background: #1e293b;
            border-radius: 5px;
            overflow: hidden;
            margin: 14px 0 8px;
            border: 1px solid #334155;
        }
        .progress-bar-fill {
            height: 100%;
            width: 0%;
            background: linear-gradient(90deg, #3b82f6, #10b981);
            transition: width 0.3s ease;
        }

        /* 控制台日志流 */
        .log-terminal {
            background: #090d16;
            border: 1px solid #1e293b;
            border-radius: 8px;
            padding: 12px;
            height: 180px;
            overflow-y: auto;
            font-family: Consolas, Monaco, monospace;
            font-size: 12px;
            color: #94a3b8;
            line-height: 1.5;
            white-space: pre-wrap;
        }

        /* 交付成片面板 */
        .deliverable-card {
            background: linear-gradient(135deg, rgba(16, 185, 129, 0.12), rgba(5, 150, 105, 0.05));
            border: 1.5px solid #10b981;
            border-radius: 12px;
            padding: 20px;
            margin-top: 18px;
            display: none;
        }
        .deliverable-actions {
            display: flex;
            gap: 10px;
            margin-top: 14px;
            flex-wrap: wrap;
        }
        .btn-action {
            padding: 10px 16px;
            border-radius: 8px;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            border: none;
            display: flex;
            align-items: center;
            gap: 6px;
            transition: all 0.15s;
        }
        .btn-action-primary {
            background: #10b981;
            color: #fff;
        }
        .btn-action-primary:hover { background: #059669; }
        .btn-action-secondary {
            background: #334155;
            color: #f8fafc;
            border: 1px solid #475569;
        }
        .btn-action-secondary:hover { background: #475569; }

        .alert-box {
            display: none;
            margin-top: 14px;
            padding: 12px 16px;
            border-radius: 8px;
            font-size: 13px;
            line-height: 1.5;
        }
        .alert-success { background: rgba(16, 185, 129, 0.15); border: 1px solid #10b981; color: #34d399; }
        .alert-error { background: rgba(239, 68, 68, 0.15); border: 1px solid #ef4444; color: #f87171; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🎬 抖音带货直播剪辑 · 智能桌面向导</h1>
            <p>项目化工作区 · 原生素材隔离 · 点击式策略卡片 · 实时端到端交付</p>
        </div>

        <!-- 顶部向导步进条 -->
        <div class="step-nav">
            <div class="step-tab active" onclick="goToStep(1)">① 项目工作区</div>
            <div class="step-tab" onclick="goToStep(2)">② 素材与隔离</div>
            <div class="step-tab" onclick="goToStep(3)">③ 商品与标签</div>
            <div class="step-tab" onclick="goToStep(4)">④ 剪辑与风格</div>
            <div class="step-tab" onclick="goToStep(5)">⑤ 智能出片</div>
        </div>

        <form id="wizardForm" onsubmit="return false;">
            <!-- ================= STEP 1: 项目工作区 ================= -->
            <div class="step-panel active" id="step1">
                <div class="card">
                    <div class="card-title">📁 1. 选择项目工作目录</div>
                    <div class="form-group">
                        <label>项目根目录 (包含 素材/ 与规格配置)</label>
                        <div class="input-row">
                            <input type="text" id="project_root" placeholder="请选择或输入项目文件夹路径 (如 Desktop/project-3)">
                            <button type="button" class="btn-browse" onclick="browseProjectFolder()">选择已有项目</button>
                            <button type="button" class="btn-browse" style="background:#059669;border-color:#10b981;" onclick="createNewProjectDialog()">➕ 新建项目</button>
                        </div>
                        <div class="hint">系统将自动识别素材、历史成片，并将中间文件写入 work/，成片交付至 output/。</div>
                    </div>
                    <div class="form-group">
                        <label>任务代号 (Project ID)</label>
                        <input type="text" id="project_id" placeholder="由目录名自动生成，如 project-3" required>
                        <div class="hint">用于本次出片标识与任务指纹绑定。</div>
                    </div>
                </div>

                <div class="step-actions">
                    <div></div>
                    <button type="button" class="btn-next" onclick="nextFromStep1()">下一步：素材核对 ➔</button>
                </div>
            </div>

            <!-- ================= STEP 2: 素材核对与隔离 ================= -->
            <div class="step-panel" id="step2">
                <div class="card">
                    <div class="card-title">
                        <span>🎬 2. 原始素材清单核对 (已排除历史成片)</span>
                        <button type="button" class="btn-browse" style="padding:4px 10px;font-size:12px;" onclick="rescanMaterials()">重新扫描</button>
                    </div>

                    <!-- 隔离区提示 -->
                    <div class="quarantine-box" id="quarantineBox" style="display:none;">
                        <div class="quarantine-title">🛡️ 已自动隔离的历史成片/系统产物 (<span id="quarantineCount">0</span> 个)</div>
                        <div class="hint" style="color:#fef08a;margin-bottom:6px;">以下文件为既往成品或导出报告，已自动隔离防护，绝不混入原始剪辑素材池：</div>
                        <ul class="quarantine-list" id="quarantineList"></ul>
                    </div>

                    <!-- 原始视频清单 -->
                    <label>勾选本次要剪辑的原始直播视频：</label>
                    <table class="material-table" id="videoTable">
                        <thead>
                            <tr>
                                <th style="width:40px;"><input type="checkbox" id="checkAllVideos" checked onchange="toggleAllVideos(this.checked)"></th>
                                <th style="width:85px;">预览</th>
                                <th>视频文件名</th>
                                <th style="width:90px;">预估时长</th>
                                <th style="width:80px;">文件大小</th>
                            </tr>
                        </thead>
                        <tbody id="videoTableBody">
                            <tr><td colspan="5" style="color:var(--muted);text-align:center;">暂无素材，请先返回第一步选择项目</td></tr>
                        </tbody>
                    </table>

                    <div style="margin-top:16px;">
                        <label>商品展示图/贴图 (自动扫描素材目录下的图片)：</label>
                        <div id="imageListContainer" style="display:flex;gap:12px;flex-wrap:wrap;margin-top:8px;"></div>
                    </div>
                </div>

                <div class="step-actions">
                    <button type="button" class="btn-prev" onclick="goToStep(1)">⬅ 返回上一步</button>
                    <button type="button" class="btn-next" onclick="goToStep(3)">下一步：商品信息 ➔</button>
                </div>
            </div>

            <!-- ================= STEP 3: 商品信息点选 ================= -->
            <div class="step-panel" id="step3">
                <div class="card">
                    <div class="card-title">👗 3. 商品基础信息与标签点选</div>
                    
                    <div class="grid-2">
                        <div class="form-group">
                            <label>商品核心名称 (必填)</label>
                            <input type="text" id="product_name" placeholder="例如：圆领羊绒打底衫 / 宽松显瘦针织开衫" required oninput="syncProductTitles()">
                            <div class="hint">用于商品身份判定与字幕排片核心识别。</div>
                        </div>
                        <div class="form-group">
                            <label>核心品类 (选择或输入)</label>
                            <select id="category" onchange="onCategoryChange(this.value)">
                                <option value="女装/毛针织">女装 / 毛针织衫</option>
                                <option value="女装/大衣外套">女装 / 大衣外套</option>
                                <option value="女装/连衣裙">女装 / 连衣裙</option>
                                <option value="女装/衬衫T恤">女装 / 衬衫T恤</option>
                                <option value="女装/裤装裙装">女装 / 裤装裙装</option>
                                <option value="箱包配饰">箱包 / 配饰</option>
                                <option value="美妆护肤">美妆 / 护肤</option>
                                <option value="通用电商">通用电商其他</option>
                            </select>
                        </div>
                    </div>

                    <div class="grid-2">
                        <div class="form-group">
                            <label>品牌英文主标 (Brand EN，留空为无品牌包装)</label>
                            <input type="text" id="brand_en" value="" placeholder="选填，例如: MIXCITY (留空为无品牌包装)">
                        </div>
                        <div class="form-group">
                            <label>品牌中文副标 (Brand CN)</label>
                            <input type="text" id="brand_cn" value="" placeholder="选填，例如: 高端女装">
                        </div>
                    </div>

                    <!-- 隐藏兼容字段 -->
                    <input type="hidden" id="product_title_l1" value="">
                    <input type="hidden" id="product_title_l2" value="">
                    <input type="hidden" id="product_img_1" value="">
                    <input type="hidden" id="product_img_2" value="">

                    <!-- 变体/颜色点击选择 -->
                    <div class="form-group" style="margin-top:10px;">
                        <label>主推颜色与款式变体 (点击选择，支持多选)：</label>
                        <div class="tag-cloud" id="variantTags">
                            <span class="tag-item" onclick="toggleTag(this, 'variant')">米白色</span>
                            <span class="tag-item" onclick="toggleTag(this, 'variant')">黑色</span>
                            <span class="tag-item" onclick="toggleTag(this, 'variant')">燕麦色</span>
                            <span class="tag-item" onclick="toggleTag(this, 'variant')">驼色</span>
                            <span class="tag-item" onclick="toggleTag(this, 'variant')">花灰色</span>
                            <span class="tag-item" onclick="toggleTag(this, 'variant')">藏青色</span>
                            <span class="tag-item" onclick="toggleTag(this, 'variant')">酒红色</span>
                            <span class="tag-item" onclick="toggleTag(this, 'variant')">焦糖色</span>
                        </div>
                        <div class="hint">选中的颜色将被严格用于变体身份追踪校验。</div>
                    </div>

                    <!-- 真实商品卖点点击选择 -->
                    <div class="form-group" style="margin-top:14px;">
                        <label>可信商品卖点 (点击选择或下方补充输入)：</label>
                        <div class="tag-cloud" id="sellingPointTags">
                            <span class="tag-item" onclick="toggleTag(this, 'sp')">显瘦版型</span>
                            <span class="tag-item" onclick="toggleTag(this, 'sp')">细腻亲肤</span>
                            <span class="tag-item" onclick="toggleTag(this, 'sp')">垂顺抗皱</span>
                            <span class="tag-item" onclick="toggleTag(this, 'sp')">保暖舒适</span>
                            <span class="tag-item" onclick="toggleTag(this, 'sp')">立体剪裁</span>
                            <span class="tag-item" onclick="toggleTag(this, 'sp')">透气不闷</span>
                            <span class="tag-item" onclick="toggleTag(this, 'sp')">不易起球</span>
                        </div>
                        <input type="text" id="selling_points_input" placeholder="可输入其他自定义卖点，以逗号分隔" style="margin-top:8px;" onchange="syncSellingPoints()">
                        <div class="hint">严禁填入虚假未核实材质；仅作为事实描述提取。</div>
                    </div>

                    <!-- 字幕高亮词 -->
                    <div class="form-group" style="margin-top:14px;">
                        <label>字幕亮黄高亮营销词 (点击选择，与商品事实分离)：</label>
                        <div class="tag-cloud" id="keywordTags">
                            <span class="tag-item active" onclick="toggleTag(this, 'kw')">闭眼入</span>
                            <span class="tag-item active" onclick="toggleTag(this, 'kw')">爆款推荐</span>
                            <span class="tag-item" onclick="toggleTag(this, 'kw')">现货秒发</span>
                            <span class="tag-item" onclick="toggleTag(this, 'kw')">实物实拍</span>
                            <span class="tag-item" onclick="toggleTag(this, 'kw')">降温必备</span>
                            <span class="tag-item" onclick="toggleTag(this, 'kw')">专柜同款</span>
                        </div>
                        <input type="text" id="keywords" value="闭眼入，爆款推荐" style="margin-top:8px;">
                    </div>
                </div>

                <div class="step-actions">
                    <button type="button" class="btn-prev" onclick="goToStep(2)">⬅ 返回上一步</button>
                    <button type="button" class="btn-next" onclick="goToStep(4)">下一步：剪辑策略 ➔</button>
                </div>
            </div>

            <!-- ================= STEP 4: 剪辑与风格策略卡片 ================= -->
            <div class="step-panel" id="step4">
                <div class="card">
                    <div class="card-title">⚙️ 4. 剪辑偏好与风格策略卡片</div>

                    <label>成片目标时长区间：</label>
                    <div class="card-grid" id="durationCards">
                        <div class="choice-card" onclick="selectCard(this, 'duration_range', '45-55')">
                            <div class="choice-title">⏱️ 45 ~ 55 秒</div>
                            <div class="choice-desc">完播率优先 · 节奏紧凑 · 适合大促投流引流</div>
                        </div>
                        <div class="choice-card active" onclick="selectCard(this, 'duration_range', '45-65')">
                            <div class="choice-title">⭐ 45 ~ 65 秒 (标准推荐)</div>
                            <div class="choice-desc">平衡型黄金时长 · 兼顾卖点展示与自然转化</div>
                        </div>
                        <div class="choice-card" onclick="selectCard(this, 'duration_range', '55-65')">
                            <div class="choice-title">🔍 55 ~ 65 秒</div>
                            <div class="choice-desc">丰富细节优先 · 充分展开面料做工与上身效果</div>
                        </div>
                    </div>
                    <input type="hidden" id="duration_range" value="45-65">

                    <label style="margin-top:16px;">剪辑叙事与节奏风格：</label>
                    <div class="card-grid" id="styleCards">
                        <div class="choice-card active" onclick="selectCard(this, 'style_preset', 'balanced')">
                            <div class="choice-title">🎯 标准商业带货 (推荐)</div>
                            <div class="choice-desc">痛点引入 ➔ 细节特写 ➔ 上身实穿 ➔ 催单促单</div>
                        </div>
                        <div class="choice-card" onclick="selectCard(this, 'style_preset', 'fast')">
                            <div class="choice-title">⚡ 紧凑高能带货</div>
                            <div class="choice-desc">前3秒吸睛 ➔ 高频金句 ➔ 密集卖点轰炸</div>
                        </div>
                        <div class="choice-card" onclick="selectCard(this, 'style_preset', 'detailed')">
                            <div class="choice-title">✨ 高质感品鉴风格</div>
                            <div class="choice-desc">纯净大画面 ➔ 慢镜头细节品味 ➔ 高端调性</div>
                        </div>
                    </div>
                    <input type="hidden" id="style_preset" value="balanced">

                    <div class="grid-2" style="margin-top:16px;">
                        <div>
                            <label>字幕与包装策略：</label>
                            <div class="card-grid" id="subtitleCards" style="grid-template-columns:1fr 1fr;">
                                <div class="choice-card active" onclick="selectCard(this, 'subtitle_strategy', 'branded')">
                                    <div class="choice-title">🏷️ 品牌包装版 (推荐)</div>
                                    <div class="choice-desc">智能高亮字幕 + 顶部品牌条 + 卖点贴纸</div>
                                </div>
                                <div class="choice-card" onclick="selectCard(this, 'subtitle_strategy', 'plain')">
                                    <div class="choice-title">🎬 纯画面无字版</div>
                                    <div class="choice-desc">全片无字幕与文字图层，视听纯净原生</div>
                                </div>
                            </div>
                            <input type="hidden" id="subtitle_strategy" value="branded">
                        </div>
                        <div>
                            <label>封面优选策略：</label>
                            <div class="card-grid" id="coverCards" style="grid-template-columns:1fr 1fr;">
                                <div class="choice-card active" onclick="selectCard(this, 'cover_strategy', 'smart_frontal')">
                                    <div class="choice-title">👤 AI 正脸优选 (推荐)</div>
                                    <div class="choice-desc">人脸质量 + 1:6:1 构图全局最优正面关键帧</div>
                                </div>
                                <div class="choice-card" onclick="selectCard(this, 'cover_strategy', 'product_image')">
                                    <div class="choice-title">🖼️ 商品主图优先</div>
                                    <div class="choice-desc">优先以素材目录中的商品展示图渲染封面</div>
                                </div>
                            </div>
                            <input type="hidden" id="cover_strategy" value="smart_frontal">
                        </div>
                    </div>

                    <div class="grid-2" style="margin-top:16px;">
                        <div>
                            <label>双色对比分屏策略：</label>
                            <div class="card-grid" style="grid-template-columns:1fr 1fr;">
                                <div class="choice-card active" onclick="selectCard(this, 'split_strategy', 'force_off')">
                                    <div class="choice-title">单画面全屏</div>
                                    <div class="choice-desc">强制单机位全屏（当前稳定推荐）</div>
                                </div>
                                <div class="choice-card" onclick="selectCard(this, 'split_strategy', 'auto')">
                                    <div class="choice-title">双色分屏</div>
                                    <div class="choice-desc">多颜色对照自动左右分屏</div>
                                </div>
                            </div>
                            <input type="hidden" id="split_strategy" value="force_off">
                        </div>
                    </div>

                    <!-- 高级参数折叠面板 -->
                    <details style="margin-top:18px;border:1px solid var(--border);border-radius:8px;padding:10px 14px;background:var(--card-sub);">
                        <summary style="font-size:13px;font-weight:600;color:var(--muted);cursor:pointer;">
                            🛠️ 高级参数与统一能力开关 (默认已按最佳调优，通常无需改动)
                        </summary>
                        <div id="featurePanel" style="margin-top:12px;"></div>
                        <div id="featureNote" class="hint" style="margin-top:8px;color:#f59e0b;"></div>
                    </details>
                </div>

                <div class="step-actions">
                    <button type="button" class="btn-prev" onclick="goToStep(3)">⬅ 返回上一步</button>
                    <button type="button" class="btn-next" onclick="goToStep(5)">下一步：启动剪辑 ➔</button>
                </div>
            </div>

            <!-- ================= STEP 5: 执行与交付监控 ================= -->
            <div class="step-panel" id="step5">
                <div class="card">
                    <div class="card-title">🚀 5. 智能剪辑执行与实时监控</div>
                    
                    <div style="display:flex;gap:12px;margin-bottom:14px;">
                        <button type="button" class="btn-run-primary" id="btnRunMain" style="flex:2;" onclick="runPipelineOneClick()">
                            🚀 开始一键全自动智能剪辑
                        </button>
                        <button type="button" class="btn-prev" id="btnSaveOnly" style="flex:1;" onclick="saveOnly()">
                            💾 仅保存任务配置
                        </button>
                    </div>

                    <!-- 状态阶段流 -->
                    <label>流水线执行状态：<span id="currentStatusText" style="color:#fbbf24;font-weight:600;">就绪</span></label>
                    <div class="stage-flow" id="stageFlow">
                        <div class="stage-node" id="sn_PRECHECK">1. 媒体预检</div>
                        <div class="stage-node" id="sn_ASR">2. 智能分段</div>
                        <div class="stage-node" id="sn_VISION">3. 视觉追踪</div>
                        <div class="stage-node" id="sn_CANDIDATES">4. 优质金句</div>
                        <div class="stage-node" id="sn_DIRECTOR">5. Agent排片</div>
                        <div class="stage-node" id="sn_RENDER">6. 音画合成</div>
                        <div class="stage-node" id="sn_QC">7. 质量门禁</div>
                        <div class="stage-node" id="sn_DELIVERED">8. 交付归档</div>
                    </div>

                    <div class="progress-bar-container">
                        <div class="progress-bar-fill" id="progressBarFill"></div>
                    </div>
                    <div class="hint" id="progressHint" style="display:flex;justify-content:space-between;">
                        <span id="progressMessage">等待启动...</span>
                        <span id="progressPercent">0%</span>
                    </div>

                    <!-- 控制台终端输出 -->
                    <label style="margin-top:14px;">实时执行日志：</label>
                    <div class="log-terminal" id="logTerminal">欢迎使用抖音带货直播剪辑系统。请确认配置后点击“开始一键智能剪辑”。</div>

                    <!-- 交付展示卡片 -->
                    <div class="deliverable-card" id="deliverableCard">
                        <div style="font-size:16px;font-weight:700;color:#34d399;display:flex;align-items:center;gap:8px;">
                            <span>🎉 智能剪辑完成并通过双层质检！</span>
                            <span class="badge badge-success">QC PASSED</span>
                        </div>
                        <div class="hint" style="color:#cbd5e1;margin-top:6px;" id="deliverablePathText">成片位置：--</div>
                        <div class="deliverable-actions">
                            <button type="button" class="btn-action btn-action-primary" onclick="actionPlayVideo()">▶️ 立即播放成片</button>
                            <button type="button" class="btn-action btn-action-secondary" onclick="actionOpenOutputDir()">📁 打开输出目录</button>
                            <button type="button" class="btn-action btn-action-secondary" onclick="actionOpenQcReport()">📊 查看 QC 质检报告</button>
                        </div>
                    </div>
                </div>

                <div class="step-actions">
                    <button type="button" class="btn-prev" onclick="goToStep(4)">⬅ 返回上一步调整参数</button>
                    <div></div>
                </div>

                <div class="alert-box alert-success" id="alertSuccess"></div>
                <div class="alert-box alert-error" id="alertError"></div>
            </div>
        </form>
    </div>

    <script>
        let currentStep = 1;
        let projectScannedData = null;
        let jobPollInterval = null;
        let lastDeliverableVideo = null;
        let lastOutputDir = null;
        let lastQcReport = null;

        const STAGES = ["PRECHECK", "ASR", "VISION", "CANDIDATES", "DIRECTOR", "RENDER", "QC", "DELIVERED"];

        function goToStep(step) {
            currentStep = step;
            document.querySelectorAll('.step-tab').forEach((tab, idx) => {
                tab.classList.toggle('active', (idx + 1) === step);
            });
            document.querySelectorAll('.step-panel').forEach((panel, idx) => {
                panel.classList.toggle('active', (idx + 1) === step);
            });
            window.scrollTo({ top: 0, behavior: 'smooth' });
        }

        function showAlert(isSuccess, msg) {
            const sBox = document.getElementById('alertSuccess');
            const eBox = document.getElementById('alertError');
            if (isSuccess) {
                sBox.innerText = msg;
                sBox.style.display = 'block';
                eBox.style.display = 'none';
            } else {
                eBox.innerText = msg;
                eBox.style.display = 'block';
                sBox.style.display = 'none';
            }
        }

        function selectCard(cardEl, hiddenId, value) {
            const parent = cardEl.parentElement;
            parent.querySelectorAll('.choice-card').forEach(c => c.classList.remove('active'));
            cardEl.classList.add('active');
            document.getElementById(hiddenId).value = value;
        }

        function toggleTag(tagEl, type) {
            tagEl.classList.toggle('active');
            if (type === 'variant') {
                // 更新选中的变体列表
            } else if (type === 'sp') {
                syncSellingPoints();
            } else if (type === 'kw') {
                syncKeywords();
            }
        }

        function syncProductTitles() {
            const val = document.getElementById('product_name').value.trim();
            document.getElementById('product_title_l1').value = val;
            if (!document.getElementById('product_title_l2').value) {
                document.getElementById('product_title_l2').value = val ? "直播精选" : "";
            }
        }

        function syncKeywords() {
            const activeKw = [];
            document.querySelectorAll('#keywordTags .tag-item.active').forEach(t => activeKw.push(t.innerText.trim()));
            document.getElementById('keywords').value = activeKw.join('，');
        }

        function syncSellingPoints() {
            const activeSp = [];
            document.querySelectorAll('#sellingPointTags .tag-item.active').forEach(t => activeSp.push(t.innerText.trim()));
            const custom = document.getElementById('selling_points_input').value.trim();
            if (custom) {
                custom.replace(/，/g, ',').split(',').forEach(s => {
                    const str = s.trim();
                    if (str && !activeSp.includes(str)) activeSp.push(str);
                });
            }
            return activeSp;
        }

        function getSelectedVariants() {
            const vars = [];
            document.querySelectorAll('#variantTags .tag-item.active').forEach(t => vars.push(t.innerText.trim()));
            return vars;
        }

        function toggleAllVideos(checked) {
            document.querySelectorAll('.video-checkbox').forEach(cb => cb.checked = checked);
        }

        function collectSelectedVideos() {
            const list = [];
            document.querySelectorAll('.video-checkbox:checked').forEach(cb => {
                list.push(cb.getAttribute('data-path'));
            });
            return list;
        }

        async function browseProjectFolder() {
            if (window.pywebview && window.pywebview.api) {
                const folder = await window.pywebview.api.select_project_folder();
                if (folder) {
                    document.getElementById('project_root').value = folder;
                    await handleProjectScanned(folder);
                }
            }
        }

        async function createNewProjectDialog() {
            if (!window.pywebview || !window.pywebview.api) return;
            const parentFolder = await window.pywebview.api.select_project_folder();
            if (!parentFolder) return;
            const defaultName = "project_" + (new Date().toISOString().slice(0, 10).replace(/-/g, ''));
            const projName = prompt("请输入新建项目名称（如 project-4）：", defaultName);
            if (!projName || !projName.trim()) return;
            const cleanName = projName.trim();
            const res = await window.pywebview.api.create_new_project(parentFolder, cleanName);
            if (res.success) {
                document.getElementById('project_root').value = res.project_root;
                document.getElementById('project_id').value = res.project_id;
                await handleProjectScanned(res.project_root);
                showAlert(true, res.message || "项目创建成功！");
                goToStep(2);
            } else {
                showAlert(false, res.message || "创建项目失败");
            }
        }

        async function nextFromStep1() {
            const pRoot = document.getElementById('project_root').value.trim();
            if (!pRoot) {
                showAlert(false, "请先选择或输入项目工作目录！");
                return;
            }
            if (!projectScannedData || projectScannedData.project_root !== pRoot) {
                await handleProjectScanned(pRoot);
            }
            goToStep(2);
        }

        async function rescanMaterials() {
            const pRoot = document.getElementById('project_root').value.trim();
            if (pRoot) {
                await handleProjectScanned(pRoot);
            }
        }

        async function handleProjectScanned(folderPath) {
            if (!window.pywebview || !window.pywebview.api) return;
            const res = await window.pywebview.api.scan_project(folderPath);
            if (!res.success) {
                showAlert(false, res.message);
                return;
            }
            projectScannedData = res;
            if (res.project_id) {
                document.getElementById('project_id').value = res.project_id;
            }

            // 渲染隔离区提示
            const qBox = document.getElementById('quarantineBox');
            const qList = document.getElementById('quarantineList');
            const qCount = document.getElementById('quarantineCount');
            if (res.quarantined_files && res.quarantined_files.length > 0) {
                qBox.style.display = 'block';
                qCount.innerText = res.quarantined_files.length;
                qList.innerHTML = res.quarantined_files.map(f => `<li><b>${f.filename}</b> <span style="color:#94a3b8;">(${f.reason})</span></li>`).join('');
            } else {
                qBox.style.display = 'none';
            }

            // 渲染视频列表（含缩略图、时长与勾选框）
            const vTable = document.getElementById('videoTableBody');
            if (res.source_videos && res.source_videos.length > 0) {
                vTable.innerHTML = res.source_videos.map(v => `
                    <tr>
                        <td><input type="checkbox" class="video-checkbox" data-path="${v.path}" checked></td>
                        <td>${v.thumbnail ? `<img src="${v.thumbnail}" style="width:72px;height:42px;object-fit:cover;border-radius:4px;display:block;">` : '<span style="font-size:11px;color:var(--muted);">无预览</span>'}</td>
                        <td><b>${v.filename}</b></td>
                        <td><span class="badge badge-info">${v.duration_fmt}</span></td>
                        <td>${v.size_mb} MB</td>
                    </tr>
                `).join('');
            } else {
                vTable.innerHTML = '<tr><td colspan="5" style="color:#ef4444;text-align:center;">素材目录下未扫描到可用原始视频（已自动过滤历史成片）</td></tr>';
            }

            // 渲染图片（含缩略图、尺寸与设为主图操作）
            const imgBox = document.getElementById('imageListContainer');
            if (res.images && res.images.length > 0) {
                imgBox.innerHTML = res.images.map((img, i) => `
                    <div style="border:1px solid #334155;border-radius:8px;padding:8px 12px;background:var(--input-bg);display:flex;align-items:center;gap:12px;">
                        ${img.thumbnail ? `<img src="${img.thumbnail}" style="width:54px;height:54px;object-fit:cover;border-radius:6px;border:1px solid #475569;">` : '🖼️'}
                        <div style="flex:1;">
                            <div style="font-weight:600;font-size:13px;">${img.filename}</div>
                            <div style="font-size:11px;color:var(--muted);">${img.size_kb} KB</div>
                        </div>
                        <button type="button" class="btn-browse" style="padding:4px 10px;font-size:12px;" onclick="setProductImageByIndex(${i}, 1)">设为主图</button>
                        <button type="button" class="btn-browse" style="padding:4px 10px;font-size:12px;" onclick="setProductImageByIndex(${i}, 2)">设为副图</button>
                    </div>
                `).join('');
                if (!document.getElementById('product_img_1').value && res.images.length > 0) {
                    document.getElementById('product_img_1').value = res.images[0].path;
                }
            } else {
                imgBox.innerHTML = '<div style="color:var(--muted);font-size:12px;">素材目录下无商品图片，将使用默认封面占位</div>';
            }

            // 回填已有的 job_spec.json
            if (res.existing_spec) {
                applyExistingSpec(res.existing_spec);
            }
        }

        function setProductImageByIndex(index, slot) {
            if (!projectScannedData || !projectScannedData.images || !projectScannedData.images[index]) return;
            const img = projectScannedData.images[index];
            document.getElementById('product_img_' + slot).value = img.path;
            showAlert(true, `已将 ${img.filename} 设为商品贴图 ${slot}`);
        }

        function setAsProductImage(path, index) {
            document.getElementById('product_img_' + index).value = path;
            const parts = String(path).split(/[\\\\/]/);
            const fname = parts[parts.length - 1] || path;
            showAlert(true, `已将 ${fname} 设为商品贴图 ${index}`);
        }

        function applyExistingSpec(spec) {
            if (!spec) return;
            const prod = spec.product || {};
            const brand = spec.brand || {};
            const opt = spec.options || {};
            if (spec.project_id) document.getElementById('project_id').value = spec.project_id;
            if (brand.title_en) document.getElementById('brand_en').value = brand.title_en;
            if (brand.subtitle_cn) document.getElementById('brand_cn').value = brand.subtitle_cn;
            if (prod.name || prod.title_line1) {
                document.getElementById('product_name').value = prod.name || prod.title_line1;
                syncProductTitles();
            }
            if (prod.category) document.getElementById('category').value = prod.category;
            if (opt.duration_range) {
                const el = document.querySelector(`#durationCards .choice-card[onclick*="'${opt.duration_range}'"]`);
                if (el) selectCard(el, 'duration_range', opt.duration_range);
            }
            if (opt.style_preset) {
                const el = document.querySelector(`#styleCards .choice-card[onclick*="'${opt.style_preset}'"]`);
                if (el) selectCard(el, 'style_preset', opt.style_preset);
            }
            const renderProf = (opt.features || {}).render_profile || (opt.subtitle_strategy);
            if (renderProf) {
                const el = document.querySelector(`#subtitleCards .choice-card[onclick*="'${renderProf}'"]`);
                if (el) selectCard(el, 'subtitle_strategy', renderProf);
            }
            if (opt.cover_strategy) {
                const el = document.querySelector(`#coverCards .choice-card[onclick*="'${opt.cover_strategy}'"]`);
                if (el) selectCard(el, 'cover_strategy', opt.cover_strategy);
            }
            if (opt.highlight_keywords && opt.highlight_keywords.length) {
                document.getElementById('keywords').value = opt.highlight_keywords.join('，');
            }
            if (opt.features) restoreFeatures(opt.features);
        }

        function collectPayload() {
            const vList = collectSelectedVideos();
            const pRoot = document.getElementById('project_root').value.trim();
            const sps = syncSellingPoints();
            const vars = getSelectedVariants();

            return {
                project_id: document.getElementById('project_id').value.trim(),
                project_root: pRoot,
                raw_video_path: vList.length > 0 ? vList[0] : "",
                raw_video_paths: vList,
                brand_en: document.getElementById('brand_en').value.trim(),
                brand_cn: document.getElementById('brand_cn').value.trim(),
                product_name: document.getElementById('product_name').value.trim(),
                product_title_l1: document.getElementById('product_title_l1').value.trim() || document.getElementById('product_name').value.trim(),
                product_title_l2: document.getElementById('product_title_l2').value.trim(),
                product_img_1: document.getElementById('product_img_1').value.trim(),
                product_img_2: document.getElementById('product_img_2').value.trim(),
                category: document.getElementById('category').value,
                allowed_variants: vars,
                selling_points: sps,
                duration_range: document.getElementById('duration_range').value,
                split_strategy: document.getElementById('split_strategy').value,
                style_preset: document.getElementById('style_preset').value,
                subtitle_strategy: document.getElementById('subtitle_strategy') ? document.getElementById('subtitle_strategy').value : 'branded',
                cover_strategy: document.getElementById('cover_strategy') ? document.getElementById('cover_strategy').value : 'smart_frontal',
                keywords: document.getElementById('keywords').value.trim(),
                features: readFeatures()
            };
        }

        // 统一能力开关（从 contracts/features.yaml 渲染）
        const FEATURES_DEFS = __FEATURES_JSON__;
        function esc(v) {
            return String(v == null ? '' : v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        }
        function renderFeatures() {
            const root = document.getElementById('featurePanel');
            if (!root) return;
            if (!FEATURES_DEFS || !FEATURES_DEFS.groups || !FEATURES_DEFS.groups.length) {
                root.innerHTML = '<div style="color:#f59e0b;font-size:12px;">未加载到开关定义（contracts/features.yaml 缺失）——本次按推荐默认值执行。</div>';
                return;
            }
            let html = '';
            FEATURES_DEFS.groups.forEach(g => {
                html += '<div style="margin:12px 0 6px;font-weight:700;color:#e8a91e;font-size:12px;">' + esc(g.label) + '</div>';
                (g.items || []).forEach(it => {
                    const dis = (it.state === 'disabled');
                    html += '<div style="border:1px solid #2a2f3a;border-radius:8px;padding:8px 10px;margin-bottom:6px;' + (dis ? 'opacity:.55;' : '') + '">';
                    html += '<div style="font-weight:600;font-size:12px;">' + esc(it.label) + (dis ? ' <span style="color:#f59e0b;font-size:11px;">[未开放]</span>' : '') + '</div>';
                    (it.options || []).forEach(o => {
                        const checked = (String(o.value) === String(it.default)) ? 'checked' : '';
                        html += '<label style="display:inline-block;margin:4px 12px 4px 0;font-size:12px;">'
                            + '<input type="radio" name="feat_' + esc(it.key) + '" value="' + esc(o.value) + '" ' + checked + (dis ? ' disabled' : '') + '> '
                            + esc(o.label) + '</label>';
                    });
                    html += '</div>';
                });
            });
            root.innerHTML = html;
        }

        function readFeatures() {
            const out = {};
            if (!FEATURES_DEFS || !FEATURES_DEFS.groups) return out;
            FEATURES_DEFS.groups.forEach(g => {
                (g.items || []).forEach(it => {
                    if (it.state === 'disabled') return;
                    const el = document.querySelector('input[name="feat_' + it.key + '"]:checked');
                    if (el) out[it.key] = el.value;
                });
            });
            return out;
        }

        function restoreFeatures(saved) {
            Object.keys(saved || {}).forEach(k => {
                const val = String(saved[k]);
                const el = document.querySelector('input[name="feat_' + k + '"][value="' + val + '"]');
                if (el) el.checked = true;
            });
        }

        async function saveOnly() {
            const payload = collectPayload();
            if (!payload.project_id) {
                showAlert(false, "任务代号不能为空！");
                return;
            }
            if (window.pywebview && window.pywebview.api) {
                const btn = document.getElementById('btnSaveOnly');
                btn.disabled = true;
                btn.innerText = "⏳ 正在保存配置...";
                try {
                    const res = await window.pywebview.api.save_spec(payload);
                    showAlert(res.success, res.message);
                } catch (e) {
                    showAlert(false, "保存异常: " + e);
                } finally {
                    btn.disabled = false;
                    btn.innerText = "💾 仅保存任务配置";
                }
            }
        }

        async function runPipelineOneClick() {
            const payload = collectPayload();
            if (!payload.raw_video_paths || payload.raw_video_paths.length === 0) {
                showAlert(false, "请先在素材清单中勾选至少一段原始直播视频！");
                goToStep(2);
                return;
            }
            if (!payload.product_name) {
                showAlert(false, "请填写商品核心名称！");
                goToStep(3);
                return;
            }

            if (!window.pywebview || !window.pywebview.api) return;

            const btn = document.getElementById('btnRunMain');
            const btnSave = document.getElementById('btnSaveOnly');
            btn.disabled = true;
            btnSave.disabled = true;
            btn.innerText = "⏳ 智能剪辑执行中...";

            // 重置 UI 状态
            document.getElementById('deliverableCard').style.display = 'none';
            document.querySelectorAll('.stage-node').forEach(sn => {
                sn.classList.remove('active', 'done');
            });

            try {
                const startRes = await window.pywebview.api.start_pipeline_async(payload);
                if (!startRes.success) {
                    showAlert(false, startRes.message);
                    btn.disabled = false;
                    btnSave.disabled = false;
                    btn.innerText = "🚀 开始一键全自动智能剪辑";
                    return;
                }

                // 启动轮询
                if (jobPollInterval) clearInterval(jobPollInterval);
                jobPollInterval = setInterval(pollJobProgress, 500);
            } catch (e) {
                showAlert(false, "启动任务异常: " + e);
                btn.disabled = false;
                btnSave.disabled = false;
                btn.innerText = "🚀 开始一键全自动智能剪辑";
            }
        }

        async function pollJobProgress() {
            if (!window.pywebview || !window.pywebview.api) return;
            try {
                const status = await window.pywebview.api.get_job_status();
                if (!status) return;

                // 更新阶段与进度条
                const pct = Math.round((status.progress || 0) * 100);
                document.getElementById('progressBarFill').style.width = pct + '%';
                document.getElementById('progressPercent').innerText = pct + '%';
                document.getElementById('progressMessage').innerText = status.message || "";
                document.getElementById('currentStatusText').innerText = status.state || status.status;

                // 阶段高亮更新
                const curState = status.state;
                let curIdx = STAGES.indexOf(curState);
                if (curIdx === -1 && curState === "DELIVERED") curIdx = STAGES.length - 1;

                STAGES.forEach((st, idx) => {
                    const node = document.getElementById('sn_' + st);
                    if (!node) return;
                    if (idx < curIdx) {
                        node.classList.add('done');
                        node.classList.remove('active');
                    } else if (idx === curIdx) {
                        node.classList.add('active');
                        node.classList.remove('done');
                    } else {
                        node.classList.remove('active', 'done');
                    }
                });

                // 更新日志
                if (status.logs && status.logs.length) {
                    const term = document.getElementById('logTerminal');
                    term.innerText = status.logs.join('\\n');
                    term.scrollTop = term.scrollHeight;
                }

                // 检查是否已完成或终止
                if (status.status === "DELIVERED" || status.status === "SUCCESS") {
                    clearInterval(jobPollInterval);
                    jobPollInterval = null;
                    onJobCompleted(status);
                } else if (status.status === "FAILED" || status.status === "CANCELED") {
                    clearInterval(jobPollInterval);
                    jobPollInterval = null;
                    onJobFailed(status);
                }
            } catch (e) {
                console.error("轮询异常:", e);
            }
        }

        function onJobCompleted(status) {
            const btn = document.getElementById('btnRunMain');
            const btnSave = document.getElementById('btnSaveOnly');
            btn.disabled = false;
            btnSave.disabled = false;
            btn.innerText = "🚀 再次运行剪辑";

            document.querySelectorAll('.stage-node').forEach(sn => {
                sn.classList.add('done');
                sn.classList.remove('active');
            });

            lastDeliverableVideo = status.final_video;
            lastOutputDir = status.output_dir;
            lastQcReport = status.qc_report;

            const card = document.getElementById('deliverableCard');
            card.style.display = 'block';
            document.getElementById('deliverablePathText').innerText = "成片路径：" + (lastDeliverableVideo || "已写入交付目录");
            showAlert(true, "剪辑流水线执行完毕并成功交付！");
        }

        function onJobFailed(status) {
            const btn = document.getElementById('btnRunMain');
            const btnSave = document.getElementById('btnSaveOnly');
            btn.disabled = false;
            btnSave.disabled = false;
            btn.innerText = "🚀 重新尝试剪辑";
            showAlert(false, "剪辑任务未能成功交付: " + (status.error || status.message || "未知原因"));
        }

        async function actionPlayVideo() {
            if (window.pywebview && window.pywebview.api && lastDeliverableVideo) {
                await window.pywebview.api.open_video(lastDeliverableVideo);
            }
        }

        async function actionOpenOutputDir() {
            if (window.pywebview && window.pywebview.api && lastOutputDir) {
                await window.pywebview.api.open_output_dir(lastOutputDir);
            }
        }

        async function actionOpenQcReport() {
            if (window.pywebview && window.pywebview.api && lastQcReport) {
                await window.pywebview.api.open_qc_report(lastQcReport);
            }
        }

        document.addEventListener('DOMContentLoaded', () => {
            renderFeatures();
        });

        window.addEventListener('pywebviewready', async () => {
            if (window.pywebview && window.pywebview.api) {
                const initData = await window.pywebview.api.load_initial_data();
                if (initData) {
                    if (initData.project_id) document.getElementById('project_id').value = initData.project_id;
                    if (initData.brand_en) document.getElementById('brand_en').value = initData.brand_en;
                    if (initData.brand_cn) document.getElementById('brand_cn').value = initData.brand_cn;
                    if (initData.product_name) document.getElementById('product_name').value = initData.product_name;
                    if (initData.features) restoreFeatures(initData.features);
                }
            }
        });
    </script>
</body>
</html>
"""
    return html.replace("__FEATURES_JSON__", _feat_json)


def main():
    try:
        import pyi_splash
        pyi_splash.close()
    except ImportError:
        pass

    try:
        import webview

        api = DesktopSpecApi()
        window = webview.create_window(
            title="🎬 抖音带货直播剪辑 · 智能桌面向导",
            html=get_html_content(),
            js_api=api,
            width=960,
            height=860,
            min_size=(820, 720),
            resizable=True
        )
        api.set_window(window)
        webview.start(debug=False)
    except Exception as e:
        import traceback
        err_msg = traceback.format_exc()
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, f"启动任务配置向导遇到异常：\n\n{err_msg}", "启动失败", 0x10)
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
