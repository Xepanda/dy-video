# -*- coding: utf-8 -*-
"""
tests/test_fetch_product_link.py
抖音商品链接抓取器、raw 目录隔离、简化向导与 CLI 单元测试（全离线、无需联网）。
"""

import os
import sys
import json
import tempfile
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.tools.fetch_product_link import (
    extract_product_url,
    extract_product_id,
    check_blocked_page,
    parse_product_payload,
    merge_product_into_spec,
    ProductFetchError,
)
from src.core.project_context import ProjectPaths
from src.tools.desktop_spec_creator import SpecManager, DesktopSpecApi


SAMPLE_URL = (
    "https://haohuo.jinritemai.com/ecommerce/trade/detail/index.html"
    "?id=3849509400821760021&ins_activity_param=jfo9SKLcSow&origin_type=pc_buyin_group"
)

SAMPLE_PACK_H5 = {
    "status_code": 0,
    "promotion_h5": {
        "basic_info_data": {
            "price_info": {"price": {"min_price": "3??"}},
            "title_info": {
                "title": "CONCISETXM【唐小木】100%羊羔毛外套新款时尚百搭绵羊毛大衣 W1120"
            },
            "product_id": "3849509400821760021",
        },
        "shop_info": {
            "basic_info": {
                "shop_name": "唐小木",
                "shop_logo": "https://p26-item.ecombdimg.com/img/logo~resize:74:74.png",
            }
        },
        "head_figure_data": {
            "media_list": [
                {
                    "name": "商品",
                    "type": "image",
                    "content_list": [
                        {"url": "https://p3-item.ecombdimg.com/img/ecom-shop-material/img_01~tplv-5mmsx3fupr-resize_q:1080:1080:q90.webp"},
                        {"url": "https://p3-item.ecombdimg.com/img/ecom-shop-material/img_02~tplv-5mmsx3fupr-resize_q:1080:1080:q90.webp"},
                    ],
                }
            ]
        },
        "comment_data": {
            "good_comment": {
                "media_list": [
                    {"image_info": {"url": "https://p9-product-comment-sign.byteimg.com/comment_only.webp"}}
                ]
            }
        },
    },
}


def test_extract_product_url_and_id() -> None:
    """支持直接粘贴 URL 或含分享口令的文本。"""
    share_copy = f"复制打开抖音查看好物 {SAMPLE_URL} 立享优惠"
    extracted = extract_product_url(share_copy)
    assert extracted == SAMPLE_URL
    assert extract_product_id(extracted) == "3849509400821760021"

    raised = False
    try:
        extract_product_url("这段文字里没有任何网址")
    except ProductFetchError:
        raised = True
    assert raised, "无 URL 输入应当抛出 ProductFetchError"
    print("  [PASS] extract_product_url / extract_product_id：口令提取与异常拦截正确")


def test_parse_product_payload_structured() -> None:
    """准确解析 promotion_h5 JSON + DOM 属性，不混入价格与评论图。"""
    dom = {
        "title": "商品详情",
        "body_text": "产品参数\n面料材质\n绵羊毛100%\n领型\n立领\n服装版型\n宽松型\n洗涤\n干洗\n价格\n￥399",
        "image_urls": [],
    }
    parsed = parse_product_payload(SAMPLE_PACK_H5, dom, SAMPLE_URL)
    assert parsed["product_id"] == "3849509400821760021"
    assert parsed["sku_id"] == "W1120"
    assert parsed["brand"]["title_en"] == "CONCISETXM"
    assert parsed["brand"]["subtitle_cn"] == "唐小木"
    assert parsed["category"] == "女装/大衣外套"
    assert "羊羔毛" in parsed["name"]
    assert len(parsed["title_line1"]) >= 3 and len(parsed["title_line2"]) >= 2
    assert parsed["attributes"].get("面料材质") == "绵羊毛100%"
    assert parsed["attributes"].get("领型") == "立领"
    assert "价格" not in parsed["attributes"], "禁止将价格字段混入商品属性"
    assert len(parsed["main_image_urls"]) == 2
    assert all("comment_only" not in u for u in parsed["image_urls"]), "评论区图片不应混入主图"
    assert "羊毛" in parsed["selling_points"], "卖点应提取口播自然词根 '羊毛' 以通过口播证据门禁"
    print("  [PASS] parse_product_payload：标题/品牌/SKU/属性/主图提取准确")


def test_check_blocked_page_detection() -> None:
    """登录重定向与验证码页应触发明确阻断，正常商品页不误报。"""
    assert check_blocked_page("https://passport.douyin.com/login", "登录", "") is not None
    assert check_blocked_page(SAMPLE_URL, "安全验证", "请完成安全验证 滑动拼图") is not None
    # 正常页面底部存在“请先登录”弹窗文案但不应误报
    normal_body = "商品详情\n面料材质\n绵羊毛100%\n进店\n请先登录查看更多"
    assert check_blocked_page(SAMPLE_URL, "商品详情", normal_body) is None
    print("  [PASS] check_blocked_page：风控/登录墙精准识别，无误报")


def test_merge_product_into_spec() -> None:
    """merge_product_into_spec 保持 raw_video_paths 不变并补全商品元数据。"""
    base_spec = {
        "project_id": "projet-1",
        "raw_video_paths": ["D:/demo/raw/a.mp4"],
        "product": {},
        "options": {"duration_range": "45-65"},
    }
    parsed = parse_product_payload(SAMPLE_PACK_H5, None, SAMPLE_URL)
    parsed["item_images"] = ["D:/demo/product_ref/product_ref_01.jpg"]
    parsed["reference_images"] = ["D:/demo/product_ref/product_ref_01.jpg", "D:/demo/product_ref/product_ref_02.jpg"]

    merged = merge_product_into_spec(base_spec, parsed)
    assert merged["raw_video_paths"] == ["D:/demo/raw/a.mp4"]
    assert merged["product"]["product_url"] == SAMPLE_URL
    assert merged["product"]["sku_id"] == "W1120"
    assert merged["brand"]["title_en"] == "CONCISETXM"
    assert merged["product"]["item_images"] == ["D:/demo/product_ref/product_ref_01.jpg"]
    assert len(merged["product"]["reference_images"]) == 2
    print("  [PASS] merge_product_into_spec：无损合并商品元数据")


def test_project_paths_supports_raw_folder() -> None:
    """ProjectPaths 识别 raw/ 素材目录，保证 work/ 与 output/ 位于项目根而非 raw/ 内。"""
    with tempfile.TemporaryDirectory() as td:
        proj = os.path.join(td, "projet-1")
        raw_dir = os.path.join(proj, "raw")
        os.makedirs(raw_dir, exist_ok=True)

        p_from_raw = ProjectPaths(project_root=raw_dir, run_id="run_test_01")
        assert os.path.normcase(p_from_raw.project_root) == os.path.normcase(proj)
        assert os.path.normcase(p_from_raw.materials_dir) == os.path.normcase(raw_dir)
        assert p_from_raw.project_id == "projet-1"
        assert os.path.normcase(os.path.dirname(p_from_raw.work_dir)) == os.path.normcase(os.path.join(proj, "work"))

        p_from_root = ProjectPaths(project_root=proj, run_id="run_test_02")
        assert os.path.normcase(p_from_root.materials_dir) == os.path.normcase(raw_dir)
        print("  [PASS] ProjectPaths：raw/ 素材子目录识别与运行时隔离正确")


def test_spec_manager_accepts_product_url_and_api_fetch() -> None:
    """SpecManager 允许仅提供 product_url，且 DesktopSpecApi.fetch_product_link 正常工作。"""
    mgr = SpecManager()
    payload = {
        "project_id": "link_only_proj",
        "raw_video_paths": ["sample.mp4"],
        "product_url": SAMPLE_URL,
    }
    errors = mgr.validate(payload)
    assert not errors, f"仅填 product_url 应通过校验，却报错: {errors}"
    spec = mgr.build_spec_dict(payload)
    assert spec["product"]["product_url"] == SAMPLE_URL

    with tempfile.TemporaryDirectory() as td:
        img_file = os.path.join(td, "product_ref_01.jpg")
        with open(img_file, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 128)
        mock_parsed = parse_product_payload(SAMPLE_PACK_H5, None, SAMPLE_URL)
        mock_parsed["downloaded_images"] = [img_file]
        mock_parsed["item_images"] = [img_file]
        mock_parsed["reference_images"] = [img_file]

        api = DesktopSpecApi()
        with patch("src.tools.fetch_product_link.fetch_product_from_url", return_value=mock_parsed):
            res = api.fetch_product_link(SAMPLE_URL, project_root=td)
            assert res["success"] is True
            assert res["product"]["sku_id"] == "W1120"
            assert len(res["images"]) == 1
    print("  [PASS] SpecManager / DesktopSpecApi.fetch_product_link：链接优先配置流通过")


def test_prepare_autofetches_when_only_product_url_provided() -> None:
    """stage_prepare 在 spec 仅提供 product_url 时自动抓取商品元数据并构建 ProductProfile。"""
    from src.runtime.stages.prepare import stage_prepare

    with tempfile.TemporaryDirectory() as td:
        proj = os.path.join(td, "auto_link_proj")
        raw_dir = os.path.join(proj, "raw")
        os.makedirs(raw_dir, exist_ok=True)
        vid = os.path.join(raw_dir, "clip_01.mp4")
        with open(vid, "wb") as f:
            f.write(b"fake_mp4_content_123456")

        spec_path = os.path.join(proj, "job_spec.json")
        with open(spec_path, "w", encoding="utf-8") as f:
            json.dump({
                "project_id": "auto_link_proj",
                "raw_video_paths": [vid],
                "product": {"product_url": SAMPLE_URL},
            }, f, ensure_ascii=False, indent=2)

        mock_parsed = parse_product_payload(SAMPLE_PACK_H5, None, SAMPLE_URL)
        mock_parsed["downloaded_images"] = []
        mock_parsed["item_images"] = []
        mock_parsed["reference_images"] = []

        with patch("src.tools.fetch_product_link.fetch_product_from_url", return_value=mock_parsed) as m_fetch:
            res = stage_prepare(spec_path=spec_path, run_id="run_auto_fetch")
            assert m_fetch.called, "stage_prepare 应对仅含 product_url 的 spec 自动触发抓取"
            assert res["status"] == "PREPARED"
            assert "羊羔毛" in res["product_profile"]
            snap = os.path.join(res["work_dir"], "spec_snapshot.json")
            with open(snap, "r", encoding="utf-8") as sf:
                snap_data = json.load(sf)
            assert snap_data["product"]["sku_id"] == "W1120"
            assert snap_data["brand"]["title_en"] == "CONCISETXM"
    print("  [PASS] stage_prepare：仅填 product_url 自动补全商品档案正确")


def main() -> int:
    tests = [
        test_extract_product_url_and_id,
        test_parse_product_payload_structured,
        test_check_blocked_page_detection,
        test_merge_product_into_spec,
        test_project_paths_supports_raw_folder,
        test_spec_manager_accepts_product_url_and_api_fetch,
        test_prepare_autofetches_when_only_product_url_provided,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"  [FAIL] {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  [ERROR] {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n[TEST-FETCH-PRODUCT] {len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
