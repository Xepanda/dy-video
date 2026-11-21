# -*- coding: utf-8 -*-
"""
test_product_profile.py - PR-02 商品身份档案与契约迁移测试
"""

import os
import sys
import json
import tempfile
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.core.product_profile import (
    build_product_profile,
    migrate_job_spec,
    validate_product_profile,
    compute_file_hash,
    ProductProfileError,
)
from src.tools.desktop_spec_creator import SpecManager


def test_legacy_spec_migration():
    """旧五类 spec 迁移：
    测试只有 title_line1, title_line2 的老 spec，自动补齐 sku_id, name, category,
    且保留原全部字段不被破坏。
    """
    old_spec = {
        "project_id": "silk_dress_01",
        "raw_video_path": "data/raw.mp4",
        "brand": {"title_en": "MIXCITY", "subtitle_cn": "女装"},
        "product": {
            "title_line1": "重磅真丝",
            "title_line2": "桑蚕丝连衣裙",
            "item_images": ["img1.jpg"]
        },
        "options": {
            "duration_range": "45-55",
            "highlight_keywords": ["桑蚕丝", "显瘦"]
        }
    }
    migrated = migrate_job_spec(old_spec)
    assert migrated["project_id"] == "silk_dress_01"
    assert migrated["product"]["sku_id"] == "silk_dress_01"
    assert "重磅真丝" in migrated["product"]["name"]
    assert migrated["product"]["category"] == "桑蚕丝连衣裙"
    assert migrated["product"]["title_line1"] == "重磅真丝"
    assert migrated["options"]["automation"]["pipeline_version"] == 3
    assert migrated["raw_video_paths"] == ["data/raw.mp4"]


def test_empty_and_custom_sku():
    """测试空 SKU 和自定义 SKU 处理。"""
    spec_with_sku = {
        "project_id": "proj_123",
        "raw_video_path": "a.mp4",
        "product": {
            "sku_id": "SKU-9999",
            "name": "羊绒开衫",
            "category": "针织衫"
        }
    }
    profile = build_product_profile(spec_with_sku, validate=False)
    assert profile["sku_id"] == "SKU-9999"
    assert profile["provenance"]["sku_id"] == "USER"

    spec_without_sku = {
        "project_id": "proj_123",
        "raw_video_path": "a.mp4",
        "product": {"name": "羊绒开衫"}
    }
    profile2 = build_product_profile(spec_without_sku, validate=False)
    assert profile2["sku_id"] == "proj_123"
    assert profile2["provenance"]["sku_id"] == "MODEL_SUGGESTED"


def test_strict_mode_blocks_when_images_missing():
    """V3 严格模式在商品图不存在时必须阻断。"""
    spec_strict = {
        "project_id": "strict_test",
        "raw_video_path": "a.mp4",
        "product": {
            "sku_id": "SKU-1",
            "name": "大衣",
            "category": "外套",
            "item_images": ["non_existent_image_12345.jpg"]
        },
        "options": {
            "automation": {
                "quality_policy": "strict",
                "visual_verification": "required"
            }
        }
    }
    with pytest.raises(ProductProfileError) as exc_info:
        build_product_profile(spec_strict, strict_missing_images=True)
    assert "strict" in str(exc_info.value)


def test_limited_mode_allows_missing_images():
    """标准/受限模式允许无参考图，降级处理。"""
    spec_standard = {
        "project_id": "standard_test",
        "raw_video_path": "a.mp4",
        "product": {
            "sku_id": "SKU-2",
            "name": "大衣",
            "category": "外套",
            "item_images": []
        },
        "options": {
            "automation": {
                "quality_policy": "standard",
                "visual_verification": "optional"
            }
        }
    }
    profile = build_product_profile(spec_standard, strict_missing_images=False, validate=True)
    assert profile["reference_image_ids"] == []
    assert profile["media_hashes"] == {}
    assert profile["provenance"]["reference_image_ids"] == "MODEL_SUGGESTED"


def test_image_hash_changes_on_file_update():
    """商品参考图内容改变时，media_hashes 改变以使缓存失效。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        img_path = os.path.join(tmpdir, "test_item.jpg")
        with open(img_path, "wb") as f:
            f.write(b"image_content_version_1")

        spec = {
            "project_id": "hash_test",
            "raw_video_path": "a.mp4",
            "product": {
                "sku_id": "SKU-HASH",
                "name": "风衣",
                "category": "外套",
                "item_images": [img_path]
            }
        }
        profile_1 = build_product_profile(spec, spec_dir=tmpdir, validate=True)
        h1 = list(profile_1["media_hashes"].values())[0]

        # 更新图片内容
        with open(img_path, "wb") as f:
            f.write(b"image_content_version_2_changed")

        profile_2 = build_product_profile(spec, spec_dir=tmpdir, validate=True)
        h2 = list(profile_2["media_hashes"].values())[0]

        assert h1 != h2, "图片变更后其 hash 必须更新"


def test_desktop_spec_manager_roundtrip():
    """测试 desktop_spec_creator SpecManager 生成 V3 扩展字段。"""
    sm = SpecManager()
    payload = {
        "project_id": "desktop_test",
        "raw_video_path": "video.mp4",
        "brand_en": "BRAND",
        "brand_cn": "品牌",
        "product_title_l1": "立领",
        "product_title_l2": "羽绒服",
        "sku_id": "SKU-DESKTOP",
        "category": "羽绒服",
        "allowed_variants": ["白色", "黑色"],
        "selling_points": ["保暖", "轻盈"],
        "automation": {"auto_edit": True, "quality_policy": "strict"}
    }
    spec = sm.build_spec_dict(payload)
    assert spec["product"]["sku_id"] == "SKU-DESKTOP"
    assert spec["product"]["category"] == "羽绒服"
    assert spec["product"]["allowed_variants"] == ["白色", "黑色"]
    assert spec["options"]["automation"]["quality_policy"] == "strict"


def main():
    test_legacy_spec_migration()
    test_empty_and_custom_sku()
    test_strict_mode_blocks_when_images_missing()
    test_limited_mode_allows_missing_images()
    test_image_hash_changes_on_file_update()
    test_desktop_spec_manager_roundtrip()
    print("[TEST-PRODUCT-PROFILE] 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
