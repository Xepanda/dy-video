#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
spec_creator.py - 抖音带货直播切片任务表单生成小工具
=====================================================
双击或运行此脚本，将自动在浏览器中打开现代化可视化表单。
用户点选素材视频、商品图片、填写品牌与商品名后，
一键生成标准 `job_spec.json` 任务契约文件。
"""

import os
import sys
import json
import webbrowser
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, unquote

PORT = 8989
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUTPUT_SPEC_PATH = os.path.join(PROJECT_ROOT, "job_spec.json")

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>抖音带货直播剪辑任务生成器</title>
    <style>
        :root {
            --primary: #f59e0b;
            --primary-hover: #d97706;
            --bg: #0f172a;
            --card: #1e293b;
            --border: #334155;
            --text: #f8fafc;
            --muted: #94a3b8;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif;
            background: var(--bg);
            color: var(--text);
            min-height: 100vh;
            padding: 30px 20px;
            display: flex;
            justify-content: center;
        }
        .container {
            max-width: 780px;
            width: 100%;
        }
        .header {
            text-align: center;
            margin-bottom: 28px;
        }
        .header h1 {
            font-size: 26px;
            font-weight: 700;
            color: #fbbf24;
            letter-spacing: 1px;
            margin-bottom: 8px;
        }
        .header p {
            font-size: 14px;
            color: var(--muted);
        }
        .card {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 24px;
            margin-bottom: 20px;
            box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.3);
        }
        .card-title {
            font-size: 16px;
            font-weight: 600;
            color: #f1f5f9;
            margin-bottom: 16px;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .form-group {
            margin-bottom: 16px;
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
        input[type="text"], input[type="number"], select {
            width: 100%;
            padding: 10px 14px;
            border-radius: 8px;
            border: 1px solid var(--border);
            background: #0b1120;
            color: #f8fafc;
            font-size: 14px;
            outline: none;
            transition: border-color 0.2s;
        }
        input:focus {
            border-color: var(--primary);
        }
        .grid-2 {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
        }
        .btn-submit {
            width: 100%;
            padding: 14px;
            border: none;
            border-radius: 10px;
            background: linear-gradient(135deg, #f59e0b, #d97706);
            color: #ffffff;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            box-shadow: 0 4px 14px rgba(245, 158, 11, 0.4);
            transition: transform 0.1s, opacity 0.2s;
        }
        .btn-submit:hover {
            opacity: 0.95;
            transform: translateY(-1px);
        }
        .btn-submit:active {
            transform: translateY(0);
        }
        .success-box {
            display: none;
            background: rgba(16, 185, 129, 0.15);
            border: 1px solid #10b981;
            color: #34d399;
            padding: 16px;
            border-radius: 10px;
            margin-top: 20px;
            text-align: center;
            font-size: 14px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🎬 抖音带货直播剪辑任务生成器</h1>
            <p>填写并导出规范化任务文件 (job_spec.json) 提交给剪辑引擎</p>
        </div>

        <form id="specForm" onsubmit="submitForm(event)">
            <!-- 1. 素材与任务ID -->
            <div class="card">
                <div class="card-title">📁 1. 任务标识与素材路径</div>
                <div class="form-group">
                    <label>任务代号 (Project ID)</label>
                    <input type="text" id="project_id" name="project_id" value="sample_silk_knitwear_01" required>
                    <div class="hint">用于输出目录与文件名命名前缀</div>
                </div>
                <div class="form-group">
                    <label>原始直播视频绝对路径 (Raw Video File)</label>
                    <input type="text" id="raw_video_path" name="raw_video_path" value="{{RAW_VIDEO_DEFAULT}}" required>
                    <div class="hint">支持复制完整路径粘贴（支持 .mp4, .mov）；可填相对仓库根的路径</div>
                </div>
            </div>

            <!-- 2. 品牌与封面主标 -->
            <div class="card">
                <div class="card-title">🏷️ 2. 品牌与封面顶部标题 (MIXCITY 风格)</div>
                <div class="grid-2">
                    <div class="form-group">
                        <label>品牌主英文 (Brand EN)</label>
                        <input type="text" id="brand_en" name="brand_en" value="MIXCITY" required>
                    </div>
                    <div class="form-group">
                        <label>品牌中文副标 (Brand CN)</label>
                        <input type="text" id="brand_cn" name="brand_cn" value="深圳高端女装" required>
                    </div>
                </div>
            </div>

            <!-- 3. 商品信息与贴图 -->
            <div class="card">
                <div class="card-title">👗 3. 商品标题与封面贴图 (0~2张)</div>
                <div class="grid-2">
                    <div class="form-group">
                        <label>商品名第 1 行 (材质/卖点属性)</label>
                        <input type="text" id="product_title_l1" name="product_title_l1" value="桑蚕丝羊绒羊毛" required>
                    </div>
                    <div class="form-group">
                        <label>商品名第 2 行 (核心品类名)</label>
                        <input type="text" id="product_title_l2" name="product_title_l2" value="渐变针织衫" required>
                    </div>
                </div>
                <div class="form-group">
                    <label>封面浮动商品贴图 1 (正方形平铺白底图/挂件)</label>
                    <input type="text" id="product_img_1" name="product_img_1" value="{{PRODUCT_IMG_DEFAULT}}">
                    <div class="hint">选填。留空时流水线会**自动补一张占位示例图**（标明为占位，后续替换为真实商品图）</div>
                </div>
                <div class="form-group">
                    <label>封面浮动商品贴图 2 (第二款/另一颜色)</label>
                    <input type="text" id="product_img_2" name="product_img_2" value="">
                    <div class="hint">选填，若填两张贴图将自动垂直居中并排</div>
                </div>
            </div>

            <!-- 4. 剪辑控制参数 -->
            <div class="card">
                <div class="card-title">⚙️ 4. 剪辑控制与营销选项</div>
                <div class="grid-2">
                    <div class="form-group">
                        <label>成片目标时长区间 (秒)</label>
                        <select id="duration_range" name="duration_range">
                            <option value="45-65" selected>45 ~ 65 秒 (标准区间，区间内浮动)</option>
                            <option value="45-55">45 ~ 55 秒 (完播率优先)</option>
                            <option value="50-60">50 ~ 60 秒 (紧凑)</option>
                            <option value="55-65">55 ~ 65 秒 (丰富卖点)</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>双色对比分屏策略</label>
                        <select id="split_strategy" name="split_strategy">
                            <option value="force_off" selected>强制关闭分屏（当前版本默认）</option>
                            <option value="auto" disabled>自动探测（规划中：v2 分屏算子开发后开放）</option>
                        </select>
                        <div class="hint">分屏算子（540:540 双色对比）开发中，暂不生效；选 auto 也不会改变输出</div>
                    </div>
                </div>
                <div class="form-group">
                    <label>字幕亮黄色高亮营销词 (逗号分隔)</label>
                    <input type="text" id="keywords" name="keywords" value="桑蚕丝,羊绒,羊毛,显瘦,针织衫,闭眼入,降温">
                </div>
            </div>

            <button type="submit" class="btn-submit">🚀 生成任务文件 (job_spec.json)</button>
            <div class="success-box" id="successBox"></div>
        </form>
    </div>

    <script>
        function submitForm(e) {
            e.preventDefault();
            const payload = {
                project_id: document.getElementById('project_id').value.trim(),
                raw_video_path: document.getElementById('raw_video_path').value.trim(),
                brand: {
                    title_en: document.getElementById('brand_en').value.trim(),
                    subtitle_cn: document.getElementById('brand_cn').value.trim()
                },
                product: {
                    title_line1: document.getElementById('product_title_l1').value.trim(),
                    title_line2: document.getElementById('product_title_l2').value.trim(),
                    item_images: []
                },
                options: {
                    duration_range: document.getElementById('duration_range').value,
                    split_strategy: document.getElementById('split_strategy').value,
                    highlight_keywords: document.getElementById('keywords').value.split(/[,，]/).map(s => s.trim()).filter(Boolean)
                }
            };
            const img1 = document.getElementById('product_img_1').value.trim();
            const img2 = document.getElementById('product_img_2').value.trim();
            if (img1) payload.product.item_images.push(img1);
            if (img2) payload.product.item_images.push(img2);

            // 跨素材选段（2026-10-21）：同目录多视频视为同一商品的直播素材，
            // 流水线会自动扫描 raw_video_path 所在目录的全部视频并跨源挑选最优片段。
            // 此处显式置空数组，由流水线侧按目录扫描补全（避免 GUI 猜测）。
            payload.raw_video_paths = [];

            fetch('/save', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            })
            .then(res => res.json())
            .then(data => {
                const box = document.getElementById('successBox');
                box.style.display = 'block';
                box.innerHTML = '✅ 任务文件已成功生成并写入：<br><strong>' + data.path + '</strong><br><br>可直接交付流水线或 Agent 执行剪辑！';
            })
            .catch(err => {
                alert('保存失败: ' + err);
            });
        }
    </script>
</body>
</html>
"""

class RequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        # 注入本机实际路径（可移植性，2026-10-24）
        html = (HTML_TEMPLATE
                .replace("{{RAW_VIDEO_DEFAULT}}", _default_raw_video())
                .replace("{{PRODUCT_IMG_DEFAULT}}", _default_product_image()))
        self.wfile.write(html.encode("utf-8"))

    def do_POST(self):
        if self.path == "/save":
            content_len = int(self.headers.get("Content-Length", 0))
            post_body = self.rfile.read(content_len)
            data = json.loads(post_body.decode("utf-8"))
            
            with open(OUTPUT_SPEC_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"[OK] Job spec saved successfully to: {OUTPUT_SPEC_PATH}")
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            resp = json.dumps({"status": "success", "path": OUTPUT_SPEC_PATH})
            self.wfile.write(resp.encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # 简化请求日志输出
        pass

def start_server():
    server = HTTPServer(("127.0.0.1", PORT), RequestHandler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"\n=======================================================")
    print(f"🎬 填表小工具已在本地启动: {url}")
    print(f"👉 浏览器将自动打开，若未打开请手动访问上述链接")
    print(f"   按 Ctrl+C 可停止小工具")
    print(f"=======================================================\n")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
        server.server_close()


def _default_raw_video() -> str:
    """挑一个本机存在的直播素材作为表单默认值（无则留空）。"""
    base = os.path.join(PROJECT_ROOT, "data", "raw_samples")
    if not os.path.isdir(base):
        return ""
    for dirpath, _, files in os.walk(base):
        for fn in sorted(files):
            if fn.lower().endswith(".mp4"):
                return os.path.join(dirpath, fn).replace("\\", "/")
    return ""


def _default_product_image() -> str:
    """挑一个本机存在的商品图作为表单默认值（无则留空）。"""
    for rel in ("data/raw_samples/sample_product_card.png",):
        p = os.path.join(PROJECT_ROOT, rel)
        if os.path.exists(p):
            return p.replace("\\", "/")
    return ""

if __name__ == "__main__":
    start_server()
