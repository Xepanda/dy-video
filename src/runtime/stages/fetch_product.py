# -*- coding: utf-8 -*-
"""
fetch_product.py - 商品链接抓取与 job_spec 自动组装服务 (V3.4)
"""

import os
import json
from typing import Dict, Any, Optional

from src.core.project_context import ProjectPaths, InputManifest
from src.tools.fetch_product_link import (
    fetch_product_from_url,
    merge_product_into_spec,
)


def stage_fetch_product(
    url: str,
    project_arg: Optional[str] = None,
    spec_out: Optional[str] = None,
    max_images: int = 4,
    timeout_ms: int = 30000,
) -> Dict[str, Any]:
    """从抖音商品链接抓取商品结构化信息与参考图，并可选写入 job_spec.json。

    安全规则：
      - 下载的商品参考图严格落盘到 `<project_root>/product_ref/`，绝不修改 raw/素材 目录；
      - 仅当指定 `--spec-out` 时才写入或覆盖该目标路径，不破坏素材目录内历史文件。
    """
    download_dir: Optional[str] = None
    paths: Optional[ProjectPaths] = None

    if project_arg:
        paths = ProjectPaths(project_root=project_arg)
        download_dir = os.path.join(paths.project_root, "product_ref")
    elif spec_out:
        download_dir = os.path.join(os.path.dirname(os.path.abspath(spec_out)), "product_ref")

    product_data = fetch_product_from_url(
        url_or_text=url,
        download_dir=download_dir,
        max_images=max_images,
        timeout_ms=timeout_ms,
    )

    spec_path_written: Optional[str] = None
    source_videos = []

    if paths is not None:
        manifest = InputManifest(paths).scan_and_resolve()
        source_videos = [s["path"] for s in manifest.get("source_videos", [])]

    if spec_out:
        spec_abs = os.path.abspath(spec_out)
        base_spec: Dict[str, Any] = {}
        if os.path.isfile(spec_abs):
            try:
                with open(spec_abs, "r", encoding="utf-8") as f:
                    base_spec = json.load(f)
            except Exception:
                base_spec = {}

        if not base_spec:
            pid = paths.project_id if paths else (product_data.get("sku_id") or "product_job")
            base_spec = {
                "project_id": pid,
                "raw_video_path": source_videos[0] if source_videos else "",
                "raw_video_paths": source_videos,
                "brand": {"title_en": "", "subtitle_cn": ""},
                "product": {},
                "options": {
                    "duration_range": "45-65",
                    "split_strategy": "force_off",
                    "style_preset": "balanced",
                    "cover_strategy": "smart_frontal",
                    "highlight_keywords": [],
                    "features": {"render_profile": "branded"},
                },
            }
        else:
            if source_videos and not base_spec.get("raw_video_paths"):
                base_spec["raw_video_paths"] = source_videos
                base_spec["raw_video_path"] = source_videos[0]

        merged = merge_product_into_spec(base_spec, product_data, overwrite=True)
        os.makedirs(os.path.dirname(spec_abs), exist_ok=True)
        with open(spec_abs, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        spec_path_written = spec_abs

    res: Dict[str, Any] = {
        "schema_version": "3.4",
        "status": "FETCHED",
        "product_url": product_data.get("product_url"),
        "product_id": product_data.get("product_id"),
        "sku_id": product_data.get("sku_id"),
        "source_mode": product_data.get("source_mode"),
        "brand": product_data.get("brand"),
        "product": {
            "product_url": product_data.get("product_url"),
            "sku_id": product_data.get("sku_id"),
            "name": product_data.get("name"),
            "raw_title": product_data.get("raw_title"),
            "title_line1": product_data.get("title_line1"),
            "title_line2": product_data.get("title_line2"),
            "category": product_data.get("category"),
            "attributes": product_data.get("attributes"),
            "allowed_variants": product_data.get("allowed_variants"),
            "selling_points": product_data.get("selling_points"),
            "item_images": product_data.get("item_images", []),
            "reference_images": product_data.get("reference_images", []),
        },
        "highlight_keywords": product_data.get("highlight_keywords", []),
        "downloaded_images": product_data.get("downloaded_images", []),
        "image_urls_found": len(product_data.get("image_urls") or []),
    }
    if paths is not None:
        res["project_root"] = paths.project_root
        res["materials_dir"] = paths.materials_dir
        res["source_videos_detected"] = len(source_videos)
    if spec_path_written:
        res["spec_path"] = spec_path_written
        res["next_step"] = f"python -m src.cli prepare --spec {spec_path_written}"
    return res
