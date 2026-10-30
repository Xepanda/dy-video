# -*- coding: utf-8 -*-
"""
desktop_spec_creator.py - 抖音带货直播切片任务桌面配置器 (基于 Edge WebView2 零网络端口)
================================================================================
纯本地原生 GUI 窗口，零本地 HTTP 端口监听，无防火墙拦截，支持系统原生文件选择对话框。
可直接作为模块导入，也可打包为独立 Windows .exe 双击运行。
"""

import os
import sys
import json
import shutil
import subprocess
import threading
from typing import Dict, Any, List, Optional


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
    # 1. 显式指定
    for env_key in ("PROJECT_PYTHON", "LIVECLIP_PYTHON"):
        env_py = os.environ.get(env_key)
        if env_py and os.path.exists(env_py):
            return env_py

    # 2. 项目内置虚拟环境
    for candidate in [
        os.path.join(PROJECT_ROOT, ".venv", "Scripts", "python.exe"),
        os.path.join(PROJECT_ROOT, "venv", "Scripts", "python.exe"),
        os.path.join(PROJECT_ROOT, ".venv", "bin", "python"),
        os.path.join(PROJECT_ROOT, "venv", "bin", "python"),
    ]:
        if os.path.exists(candidate):
            return candidate

    # 3. WorkBuddy 托管环境（不写死用户名；liveclip 优先，其余按名字排序）
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

    # 4. 未打包状态下使用当前正在运行的 Python
    if not getattr(sys, 'frozen', False) and sys.executable:
        return sys.executable

    # 5. 系统 PATH 中的 python
    sys_py = shutil.which("python")
    if sys_py:
        return sys_py

    return "python"


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
        if not p_l1 and not p_l2:
            errors.append("商品名称至少填写一行")

        return errors

    def build_spec_dict(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        project_id = str(payload.get("project_id") or "demo").strip()
        raw_video_path = str(payload.get("raw_video_path") or "").strip()
        raw_video_paths = payload.get("raw_video_paths") or []
        if not isinstance(raw_video_paths, list):
            raw_video_paths = []

        brand_en = str(payload.get("brand_en") or "").strip()
        brand_cn = str(payload.get("brand_cn") or "").strip()

        p_l1 = str(payload.get("product_title_l1") or "").strip()
        p_l2 = str(payload.get("product_title_l2") or "").strip()

        item_images = []
        for key in ["product_img_1", "product_img_2"]:
            img = str(payload.get(key) or "").strip()
            if img and img not in item_images:
                item_images.append(img)
        # 支持传入已有的 item_images 数组
        if "item_images" in payload and isinstance(payload["item_images"], list):
            for img in payload["item_images"]:
                s = str(img).strip()
                if s and s not in item_images:
                    item_images.append(s)

        duration_range = str(payload.get("duration_range") or "45-65").strip()
        split_strategy = str(payload.get("split_strategy") or "force_off").strip()

        raw_keywords = payload.get("keywords") or payload.get("highlight_keywords") or []
        if isinstance(raw_keywords, str):
            # 支持逗号、顿号、分号或换行分隔
            parts = raw_keywords.replace("，", ",").replace("、", ",").replace(";", ",").replace("\n", ",").split(",")
            keywords = [p.strip() for p in parts if p.strip()]
        elif isinstance(raw_keywords, list):
            keywords = [str(k).strip() for k in raw_keywords if str(k).strip()]
        else:
            keywords = []

        spec = {
            "project_id": project_id,
            "raw_video_path": raw_video_path,
            "raw_video_paths": raw_video_paths,
            "brand": {
                "title_en": brand_en,
                "subtitle_cn": brand_cn
            },
            "product": {
                "title_line1": p_l1,
                "title_line2": p_l2,
                "item_images": item_images
            },
            "options": {
                "duration_range": duration_range,
                "split_strategy": split_strategy,
                "highlight_keywords": keywords
            }
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

    def __init__(self, spec_path: str = DEFAULT_SPEC_PATH):
        self.spec_path = os.path.abspath(spec_path)
        self.manager = SpecManager()
        self.window = None
        self._pipeline_process = None
        self._pipeline_logs = []
        self._pipeline_status = "idle"  # idle | running | success | error

    def set_window(self, window):
        self.window = window

    def load_initial_data(self) -> Dict[str, Any]:
        """加载初始数据（优先读取已有 job_spec.json）。"""
        loaded = self.manager.load_spec(self.spec_path)
        if loaded:
            brand = loaded.get("brand", {})
            product = loaded.get("product", {})
            options = loaded.get("options", {})
            imgs = product.get("item_images", [])
            return {
                "project_id": loaded.get("project_id", "sample_silk_knitwear_01"),
                "raw_video_path": loaded.get("raw_video_path", "data/raw_samples/silk_knitwear_sample"),
                "brand_en": brand.get("title_en", "MIXCITY"),
                "brand_cn": brand.get("subtitle_cn", "深圳高端女装"),
                "product_title_l1": product.get("title_line1", "桑蚕丝羊绒羊毛"),
                "product_title_l2": product.get("title_line2", "渐变针织衫"),
                "product_img_1": imgs[0] if len(imgs) > 0 else "data/raw_samples/sample_product_card.png",
                "product_img_2": imgs[1] if len(imgs) > 1 else "",
                "duration_range": options.get("duration_range", "45-65"),
                "split_strategy": options.get("split_strategy", "force_off"),
                "keywords": "，".join(options.get("highlight_keywords", ["桑蚕丝", "羊绒", "羊毛", "显瘦", "针织衫", "闭眼入", "降温"])),
                "has_existing": True,
                "spec_path": self.spec_path,
            }
        return {
            "project_id": "sample_silk_knitwear_01",
            "raw_video_path": "data/raw_samples/silk_knitwear_sample",
            "brand_en": "MIXCITY",
            "brand_cn": "深圳高端女装",
            "product_title_l1": "桑蚕丝羊绒羊毛",
            "product_title_l2": "渐变针织衫",
            "product_img_1": "data/raw_samples/sample_product_card.png",
            "product_img_2": "",
            "duration_range": "45-65",
            "split_strategy": "force_off",
            "keywords": "桑蚕丝，羊绒，羊毛，显瘦，针织衫，闭眼入，降温",
            "has_existing": False,
            "spec_path": self.spec_path,
        }

    def select_video_file(self) -> Optional[str]:
        """弹出原生文件选择对话框（单视频文件）。"""
        if not self.window:
            return None
        import webview
        file_types = ("视频文件 (*.mp4;*.mov;*.mkv;*.ts;*.flv)", "所有文件 (*.*)")
        res = self.window.create_file_dialog(
            dialog_type=webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=file_types
        )
        if res and len(res) > 0:
            return os.path.normpath(res[0])
        return None

    def select_video_folder(self) -> Optional[str]:
        """弹出原生文件夹选择对话框（视频素材目录）。"""
        if not self.window:
            return None
        import webview
        res = self.window.create_file_dialog(
            dialog_type=webview.FOLDER_DIALOG
        )
        if res and len(res) > 0:
            return os.path.normpath(res[0])
        return None

    def select_image_file(self) -> Optional[str]:
        """弹出原生图片选择对话框。"""
        if not self.window:
            return None
        import webview
        file_types = ("图片文件 (*.png;*.jpg;*.jpeg;*.webp)", "所有文件 (*.*)")
        res = self.window.create_file_dialog(
            dialog_type=webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=file_types
        )
        if res and len(res) > 0:
            return os.path.normpath(res[0])
        return None

    def save_spec(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """校验并保存 job_spec.json。"""
        errors = self.manager.validate(payload)
        if errors:
            return {"success": False, "message": "；".join(errors)}

        try:
            spec = self.manager.build_spec_dict(payload)
            self.manager.save_spec(spec, self.spec_path)
            return {
                "success": True,
                "message": f"任务配置文件保存成功！路径：{self.spec_path}",
                "spec": spec
            }
        except Exception as e:
            return {"success": False, "message": f"保存失败: {str(e)}"}

    def start_pipeline(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """保存并启动剪辑流水线。"""
        save_res = self.save_spec(payload)
        if not save_res["success"]:
            return save_res

        if self._pipeline_status == "running":
            return {"success": False, "message": "剪辑任务正在运行中，请等待完成"}

        self._pipeline_logs = []
        self._pipeline_status = "running"

        def _run_worker():
            try:
                python_exe = find_python_interpreter()
                script_path = os.path.join(PROJECT_ROOT, "scripts", "generate_from_job_spec.py")
                cmd = [python_exe, script_path, "--spec", self.spec_path]
                self._pipeline_logs.append(f"启动 Python: {python_exe}\n")
                self._pipeline_logs.append(f"执行命令: {' '.join(cmd)}\n")

                proc = subprocess.Popen(
                    cmd,
                    cwd=PROJECT_ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1
                )
                self._pipeline_process = proc

                for line in proc.stdout:
                    self._pipeline_logs.append(line)
                    if self.window:
                        try:
                            clean_line = json.dumps(line)
                            self.window.evaluate_js(f"window.appendLog({clean_line});")
                        except Exception:
                            pass

                proc.wait()
                if proc.returncode == 0:
                    self._pipeline_status = "success"
                    if self.window:
                        self.window.evaluate_js("window.onPipelineComplete(true, '剪辑与质检全部通过！');")
                else:
                    self._pipeline_status = "error"
                    if self.window:
                        self.window.evaluate_js(f"window.onPipelineComplete(false, '剪辑流程退出，返回码: {proc.returncode}');")
            except Exception as e:
                self._pipeline_status = "error"
                err_msg = f"启动流水线异常: {str(e)}"
                self._pipeline_logs.append(err_msg + "\n")
                if self.window:
                    self.window.evaluate_js(f"window.onPipelineComplete(false, {json.dumps(err_msg)});")

        thread = threading.Thread(target=_run_worker, daemon=True)
        thread.start()

        return {"success": True, "message": "剪辑流水线已启动！"}

    def get_pipeline_status(self) -> Dict[str, Any]:
        return {
            "status": self._pipeline_status,
            "logs": "".join(self._pipeline_logs[-50:])
        }


def get_html_content() -> str:
    """生成内嵌的现代化暗黑风格桌面表单 UI（无任何外部网络依赖）。"""
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>抖音带货直播剪辑任务配置器</title>
    <style>
        :root {
            --primary: #f59e0b;
            --primary-hover: #d97706;
            --success: #10b981;
            --success-hover: #059669;
            --bg: #0b1120;
            --card: #1e293b;
            --border: #334155;
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
            padding: 24px;
            user-select: none;
            overflow-y: auto;
        }
        .container {
            max-width: 860px;
            margin: 0 auto;
        }
        .header {
            text-align: center;
            margin-bottom: 24px;
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
            gap: 8px;
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
        }
        .input-row {
            display: flex;
            gap: 8px;
        }
        input[type="text"], select {
            width: 100%;
            padding: 9px 12px;
            border-radius: 8px;
            border: 1px solid var(--border);
            background: var(--input-bg);
            color: var(--text);
            font-size: 13px;
            outline: none;
            transition: border-color 0.2s;
        }
        input:focus, select:focus {
            border-color: var(--primary);
        }
        .btn-browse {
            padding: 9px 14px;
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
        .action-bar {
            display: grid;
            grid-template-columns: 1fr 1.3fr;
            gap: 14px;
            margin-top: 20px;
        }
        .btn {
            padding: 13px 20px;
            border: none;
            border-radius: 10px;
            font-size: 15px;
            font-weight: 600;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            transition: transform 0.1s, opacity 0.15s;
        }
        .btn:active {
            transform: scale(0.98);
        }
        .btn-save {
            background: #334155;
            color: #f8fafc;
            border: 1px solid #475569;
        }
        .btn-save:hover {
            background: #475569;
        }
        .btn-run {
            background: linear-gradient(135deg, #f59e0b, #d97706);
            color: #ffffff;
            box-shadow: 0 4px 15px rgba(245, 158, 11, 0.35);
        }
        .btn-run:hover {
            opacity: 0.95;
        }
        .btn:disabled {
            opacity: 0.5;
            cursor: not-allowed;
            transform: none !important;
        }
        .alert-box {
            display: none;
            margin-top: 14px;
            padding: 12px 16px;
            border-radius: 8px;
            font-size: 13px;
            line-height: 1.5;
        }
        .alert-success {
            background: rgba(16, 185, 129, 0.15);
            border: 1px solid #10b981;
            color: #34d399;
        }
        .alert-error {
            background: rgba(239, 68, 68, 0.15);
            border: 1px solid #ef4444;
            color: #f87171;
        }
        .log-section {
            display: none;
            margin-top: 20px;
            background: #050811;
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 14px;
        }
        .log-header {
            font-size: 13px;
            color: #38bdf8;
            font-weight: 600;
            margin-bottom: 10px;
            display: flex;
            justify-content: space-between;
        }
        .log-box {
            font-family: Consolas, "Courier New", monospace;
            font-size: 12px;
            color: #a5f3fc;
            background: #020617;
            padding: 10px;
            border-radius: 6px;
            height: 200px;
            overflow-y: auto;
            white-space: pre-wrap;
            line-height: 1.4;
            border: 1px solid #1e293b;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🎬 抖音带货直播剪辑 · 桌面任务配置器</h1>
            <p>零网络端口 · 原生窗口 · 规范化契约 (job_spec.json) · 一键端到端生成</p>
        </div>

        <form id="specForm" onsubmit="return false;">
            <!-- 1. 任务与素材 -->
            <div class="card">
                <div class="card-title">📁 1. 任务标识与素材路径</div>
                <div class="form-group">
                    <label>任务代号 (Project ID)</label>
                    <input type="text" id="project_id" value="sample_silk_knitwear_01" required>
                    <div class="hint">用于输出目录与成片命名前缀（如 output/sample_silk_knitwear_01/）</div>
                </div>
                <div class="form-group">
                    <label>原始直播视频路径 (文件或素材目录)</label>
                    <div class="input-row">
                        <input type="text" id="raw_video_path" value="data/raw_samples/silk_knitwear_sample" required>
                        <button type="button" class="btn-browse" onclick="browseVideoFile()">选择视频</button>
                        <button type="button" class="btn-browse" onclick="browseVideoFolder()">选择目录</button>
                    </div>
                    <div class="hint">支持单个 .mp4/.mov 视频文件，或者包含多段切片的素材文件夹（自动按序扫描）</div>
                </div>
            </div>

            <!-- 2. 品牌与主标 -->
            <div class="card">
                <div class="card-title">🏷️ 2. 品牌与封面顶部标题 (MIXCITY 风格)</div>
                <div class="grid-2">
                    <div class="form-group">
                        <label>品牌主英文 (Brand EN)</label>
                        <input type="text" id="brand_en" value="MIXCITY" required>
                    </div>
                    <div class="form-group">
                        <label>品牌中文副标 (Brand CN)</label>
                        <input type="text" id="brand_cn" value="深圳高端女装" required>
                    </div>
                </div>
            </div>

            <!-- 3. 商品信息与贴图 -->
            <div class="card">
                <div class="card-title">👗 3. 商品标题与封面贴图 (0~2张)</div>
                <div class="grid-2">
                    <div class="form-group">
                        <label>商品名第 1 行 (材质/卖点属性)</label>
                        <input type="text" id="product_title_l1" value="桑蚕丝羊绒羊毛" required>
                    </div>
                    <div class="form-group">
                        <label>商品名第 2 行 (核心品类名)</label>
                        <input type="text" id="product_title_l2" value="渐变针织衫" required>
                    </div>
                </div>
                <div class="form-group">
                    <label>封面浮动商品贴图 1 (正方形平铺白底图/挂件)</label>
                    <div class="input-row">
                        <input type="text" id="product_img_1" value="data/raw_samples/sample_product_card.png">
                        <button type="button" class="btn-browse" onclick="browseImage(1)">浏览...</button>
                    </div>
                    <div class="hint">选填。留空时流水线会自动补一张占位示例图</div>
                </div>
                <div class="form-group">
                    <label>封面浮动商品贴图 2 (第二款/另一颜色)</label>
                    <div class="input-row">
                        <input type="text" id="product_img_2" value="">
                        <button type="button" class="btn-browse" onclick="browseImage(2)">浏览...</button>
                    </div>
                </div>
            </div>

            <!-- 4. 剪辑选项 -->
            <div class="card">
                <div class="card-title">⚙️ 4. 剪辑控制与营销选项</div>
                <div class="grid-2">
                    <div class="form-group">
                        <label>成片目标时长区间 (秒)</label>
                        <select id="duration_range">
                            <option value="45-65" selected>45 ~ 65 秒 (标准区间，区间内浮动)</option>
                            <option value="45-55">45 ~ 55 秒 (完播率优先)</option>
                            <option value="50-60">50 ~ 60 秒 (紧凑节奏)</option>
                            <option value="55-65">55 ~ 65 秒 (丰富卖点)</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>双色对比分屏策略</label>
                        <select id="split_strategy">
                            <option value="force_off" selected>强制关闭分屏 (force_off · 当前推荐)</option>
                            <option value="auto" disabled>自动探测 (规划中)</option>
                        </select>
                    </div>
                </div>
                <div class="form-group">
                    <label>字幕亮黄色高亮营销词 (以逗号分隔)</label>
                    <input type="text" id="keywords" value="桑蚕丝，羊绒，羊毛，显瘦，针织衫，闭眼入，降温">
                </div>
            </div>

            <div class="action-bar">
                <button type="button" class="btn btn-save" id="btnSave" onclick="saveOnly()">
                    💾 仅保存任务配置 (job_spec.json)
                </button>
                <button type="button" class="btn btn-run" id="btnRun" onclick="saveAndRun()">
                    🚀 保存并一键运行剪辑流水线
                </button>
            </div>

            <div class="alert-box alert-success" id="alertSuccess"></div>
            <div class="alert-box alert-error" id="alertError"></div>

            <div class="log-section" id="logSection">
                <div class="log-header">
                    <span>⚡ 实时剪辑与质检日志</span>
                    <span id="logStatus">运行中...</span>
                </div>
                <div class="log-box" id="logBox"></div>
            </div>
        </form>
    </div>

    <script>
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

        function collectPayload() {
            return {
                project_id: document.getElementById('project_id').value.trim(),
                raw_video_path: document.getElementById('raw_video_path').value.trim(),
                brand_en: document.getElementById('brand_en').value.trim(),
                brand_cn: document.getElementById('brand_cn').value.trim(),
                product_title_l1: document.getElementById('product_title_l1').value.trim(),
                product_title_l2: document.getElementById('product_title_l2').value.trim(),
                product_img_1: document.getElementById('product_img_1').value.trim(),
                product_img_2: document.getElementById('product_img_2').value.trim(),
                duration_range: document.getElementById('duration_range').value,
                split_strategy: document.getElementById('split_strategy').value,
                keywords: document.getElementById('keywords').value.trim()
            };
        }

        async function browseVideoFile() {
            if (window.pywebview && window.pywebview.api) {
                const path = await window.pywebview.api.select_video_file();
                if (path) document.getElementById('raw_video_path').value = path;
            }
        }

        async function browseVideoFolder() {
            if (window.pywebview && window.pywebview.api) {
                const path = await window.pywebview.api.select_video_folder();
                if (path) document.getElementById('raw_video_path').value = path;
            }
        }

        async function browseImage(index) {
            if (window.pywebview && window.pywebview.api) {
                const path = await window.pywebview.api.select_image_file();
                if (path) {
                    document.getElementById('product_img_' + index).value = path;
                }
            }
        }

        async function saveOnly() {
            const payload = collectPayload();
            if (window.pywebview && window.pywebview.api) {
                const res = await window.pywebview.api.save_spec(payload);
                showAlert(res.success, res.message);
            }
        }

        async function saveAndRun() {
            const payload = collectPayload();
            if (window.pywebview && window.pywebview.api) {
                document.getElementById('btnRun').disabled = true;
                document.getElementById('btnSave').disabled = true;
                document.getElementById('logSection').style.display = 'block';
                document.getElementById('logBox').innerText = '正在启动流水线...\\n';
                document.getElementById('logStatus').innerText = '运行中...';

                const res = await window.pywebview.api.start_pipeline(payload);
                showAlert(res.success, res.message);
                if (!res.success) {
                    document.getElementById('btnRun').disabled = false;
                    document.getElementById('btnSave').disabled = false;
                }
            }
        }

        window.appendLog = function(line) {
            const box = document.getElementById('logBox');
            box.innerText += line;
            box.scrollTop = box.scrollHeight;
        };

        window.onPipelineComplete = function(success, msg) {
            document.getElementById('btnRun').disabled = false;
            document.getElementById('btnSave').disabled = false;
            document.getElementById('logStatus').innerText = success ? '✓ 已完成' : '✗ 异常中止';
            showAlert(success, msg);
        };

        window.addEventListener('pywebviewready', async function() {
            if (window.pywebview && window.pywebview.api) {
                const data = await window.pywebview.api.load_initial_data();
                if (data) {
                    if (data.project_id) document.getElementById('project_id').value = data.project_id;
                    if (data.raw_video_path) document.getElementById('raw_video_path').value = data.raw_video_path;
                    if (data.brand_en) document.getElementById('brand_en').value = data.brand_en;
                    if (data.brand_cn) document.getElementById('brand_cn').value = data.brand_cn;
                    if (data.product_title_l1) document.getElementById('product_title_l1').value = data.product_title_l1;
                    if (data.product_title_l2) document.getElementById('product_title_l2').value = data.product_title_l2;
                    if (data.product_img_1) document.getElementById('product_img_1').value = data.product_img_1;
                    if (data.product_img_2) document.getElementById('product_img_2').value = data.product_img_2;
                    if (data.duration_range) document.getElementById('duration_range').value = data.duration_range;
                    if (data.split_strategy) document.getElementById('split_strategy').value = data.split_strategy;
                    if (data.keywords) document.getElementById('keywords').value = data.keywords;
                }
            }
        });
    </script>
</body>
</html>
"""


def main():
    import webview

    api = DesktopSpecApi()
    window = webview.create_window(
        title="🎬 抖音带货直播剪辑 · 任务配置与启动器",
        html=get_html_content(),
        js_api=api,
        width=920,
        height=940,
        min_size=(800, 720),
        resizable=True
    )
    api.set_window(window)
    webview.start(debug=False)


if __name__ == "__main__":
    main()
