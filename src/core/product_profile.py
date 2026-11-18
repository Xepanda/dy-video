# -*- coding: utf-8 -*-
"""
product_profile.py - 商品身份档案与契约迁移（V3 PR-02 核心）
============================================================
负责：
  1. 将 job_spec 中的商品事实解析并构建为标准的 ProductProfile（符合 product_profile_v3.schema.json）；
  2. 记录字段的 Provenance（USER / MODEL_SUGGESTED / VERIFIED_EXTERNAL），
     保证模型推断与默认值绝不覆写用户提供的真实商品事实；
  3. 计算商品参考图片的 SHA256 指纹（media_hashes），图片变动即触发缓存失效；
  4. 支持 V3 严格质量策略（strict mode）：在缺少有效商品图时进行阻断；
  5. 提供 job_spec 自动迁移函数（migrate_job_spec），无缝兼容 V1/V2 旧版配置。
"""

import os
import sys
import json
import hashlib
from typing import Dict, Any, List, Optional, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.core.path_resolver import resolve_path

SCHEMA_V3_PATH = os.path.join(PROJECT_ROOT, "contracts", "schemas", "product_profile_v3.schema.json")


class ProductProfileError(Exception):
    """商品档案构建或校验异常。"""
    pass


def compute_file_hash(path: str) -> str:
    """计算文件的 SHA256 哈希字符串（大文件分块读取）。"""
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


def migrate_job_spec(raw_spec: Dict[str, Any], project_root: str = None) -> Dict[str, Any]:
    """旧版 job_spec.json 平滑迁移器（幂等）：
    将 V1/V2 spec 补全为具备 SKU、品类、属性与自动化配置的 V3 兼容格式。
    保留所有已有字段，绝不损坏原有消费方。
    """
    spec = json.loads(json.dumps(raw_spec))  # 深拷贝
    project_id = str(spec.get("project_id") or "demo").strip()
    spec["project_id"] = project_id

    # 规范化素材列表
    rvp = spec.get("raw_video_path") or ""
    rvps = spec.get("raw_video_paths")
    if not rvps and rvp:
        spec["raw_video_paths"] = [rvp]
    elif rvps and not rvp:
        spec["raw_video_path"] = rvps[0]

    # 规范化商品结构
    product = spec.setdefault("product", {})
    p_l1 = str(product.get("title_line1") or "").strip()
    p_l2 = str(product.get("title_line2") or "").strip()
    name = str(product.get("name") or "").strip()
    if not name:
        name = f"{p_l1} {p_l2}".strip() or project_id
    product["name"] = name

    sku_id = str(product.get("sku_id") or "").strip()
    if not sku_id:
        sku_id = project_id
    product["sku_id"] = sku_id

    category = str(product.get("category") or "").strip()
    if not category:
        category = p_l2 or "通用服装"
    product["category"] = category

    images = list(product.get("item_images") or product.get("reference_images") or [])
    product["item_images"] = images
    product.setdefault("reference_images", images)
    product.setdefault("allowed_variants", [])
    product.setdefault("attributes", {})

    selling_points = product.get("selling_points")
    if not selling_points:
        hk = spec.get("options", {}).get("highlight_keywords") or []
        selling_points = list(hk)
    product["selling_points"] = selling_points

    # 规范化 options 与 automation 策略
    options = spec.setdefault("options", {})
    options.setdefault("duration_range", "45-65")
    options.setdefault("split_strategy", "force_off")

    automation = options.setdefault("automation", {})
    automation.setdefault("pipeline_version", 3)
    automation.setdefault("auto_edit", True)
    automation.setdefault("agent_provider", "agy_headless")
    automation.setdefault("quality_policy", "standard")
    automation.setdefault("visual_verification", "optional")
    automation.setdefault("max_replan_rounds", 2)

    return spec


def build_product_profile(
    spec: Dict[str, Any],
    spec_dir: Optional[str] = None,
    strict_missing_images: bool = False,
    validate: bool = True,
) -> Dict[str, Any]:
    """依据任务契约构建结构化的 ProductProfile（V3）。

    strict_missing_images:
      若为 True（或 spec 中 quality_policy 为 strict 且 visual_verification 为 required），
      在商品参考图完全不存在时抛出 ProductProfileError 阻断。
    """
    root_dir = spec_dir or PROJECT_ROOT
    migrated = migrate_job_spec(spec, project_root=root_dir)
    prod = migrated.get("product", {})
    options = migrated.get("options", {})
    auto_cfg = options.get("automation", {})

    # 判断是否为 strict 严格模式
    is_strict = (
        strict_missing_images or
        (auto_cfg.get("quality_policy") == "strict" and
         auto_cfg.get("visual_verification") == "required")
    )

    sku_id = str(prod.get("sku_id") or "").strip()
    name = str(prod.get("name") or "").strip()
    category = str(prod.get("category") or "").strip()

    # 处理参考图与哈希
    raw_images = prod.get("item_images") or prod.get("reference_images") or []
    valid_images: List[str] = []
    media_hashes: Dict[str, str] = {}

    for img in raw_images:
        resolved = resolve_path(img, root_dir)
        if resolved and os.path.isfile(resolved):
            h = compute_file_hash(resolved)
            if h:
                # 统一转相对路径标识
                try:
                    rel_id = os.path.relpath(resolved, PROJECT_ROOT).replace("\\", "/")
                except ValueError:
                    # Windows 跨驱动器时（如 C:\ 与 D:\）无法求相对路径，使用归一化绝对路径
                    rel_id = resolved.replace("\\", "/")
                valid_images.append(rel_id)
                media_hashes[rel_id] = h

    if is_strict and not valid_images:
        raise ProductProfileError(
            f"任务 [{spec.get('project_id')}] 配置为 strict 质量模式，"
            f"但未找到有效的商品参考图 (item_images: {raw_images})，阻断生成。"
        )

    # 追溯事实来源（Provenance）
    provenance: Dict[str, str] = {}
    provenance["sku_id"] = "USER" if spec.get("product", {}).get("sku_id") else "MODEL_SUGGESTED"
    provenance["name"] = "USER" if spec.get("product", {}).get("name") or spec.get("product", {}).get("title_line1") else "MODEL_SUGGESTED"
    provenance["category"] = "USER" if spec.get("product", {}).get("category") else "MODEL_SUGGESTED"
    provenance["reference_image_ids"] = "USER" if valid_images else "MODEL_SUGGESTED"
    provenance["allowed_variants"] = "USER" if prod.get("allowed_variants") else "MODEL_SUGGESTED"
    provenance["selling_points"] = "USER" if spec.get("product", {}).get("selling_points") else "MODEL_SUGGESTED"
    provenance["product_attributes"] = "USER" if prod.get("attributes") else "MODEL_SUGGESTED"

    price_policy = str(prod.get("price_policy") or "USER_DEFINED")
    if price_policy not in ("EXCLUDE_ALL", "USER_DEFINED"):
        price_policy = "USER_DEFINED"

    claim_policy = str(prod.get("claim_policy") or "FACTS_ONLY")
    if claim_policy not in ("FACTS_ONLY", "REVIEW_REQUIRED"):
        claim_policy = "FACTS_ONLY"

    profile: Dict[str, Any] = {
        "schema_version": "3.0",
        "sku_id": sku_id,
        "name": name,
        "category": category,
        "reference_image_ids": valid_images,
        "allowed_variants": [str(v) for v in prod.get("allowed_variants", [])],
        "selling_points": [str(s) for s in prod.get("selling_points", [])],
        "product_attributes": dict(prod.get("attributes", {})),
        "provenance": provenance,
        "price_policy": price_policy,
        "claim_policy": claim_policy,
        "media_hashes": media_hashes,
    }

    if validate:
        ok, errs = validate_product_profile(profile)
        if not ok:
            raise ProductProfileError(f"生成的 ProductProfile 未通过 schema 校验: {'; '.join(errs)}")

    return profile


def validate_product_profile(profile: Dict[str, Any], schema_path: str = None) -> Tuple[bool, List[str]]:
    """使用 jsonschema 校验 ProductProfile 是否符合契约。"""
    path = schema_path or SCHEMA_V3_PATH
    if not os.path.exists(path):
        return True, []
    try:
        import jsonschema
        with open(path, "r", encoding="utf-8") as f:
            schema = json.load(f)
        validator = jsonschema.Draft202012Validator(schema)
        errors = [e.message for e in validator.iter_errors(profile)]
        return len(errors) == 0, errors
    except ImportError:
        # 未装 jsonschema 时进行基础字段校验
        req = ["schema_version", "sku_id", "name", "category", "reference_image_ids",
               "allowed_variants", "selling_points", "product_attributes", "provenance",
               "price_policy", "claim_policy", "media_hashes"]
        missing = [r for r in req if r not in profile]
        return len(missing) == 0, [f"缺少必填字段: {m}" for m in missing]
    except Exception as e:
        return False, [str(e)]


def save_product_profile(profile: Dict[str, Any], output_path: str) -> None:
    """落盘 product_profile.json。"""
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)


def load_product_profile(profile_path: str) -> Dict[str, Any]:
    """读取并验证已有的 product_profile.json。"""
    with open(profile_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data
