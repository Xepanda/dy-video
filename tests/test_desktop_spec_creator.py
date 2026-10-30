# -*- coding: utf-8 -*-
"""
tests/test_desktop_spec_creator.py
桌面任务配置器业务逻辑单元测试（SpecManager / DesktopSpecApi / 环境探测）。

运行方式（两种都支持）：
  python -u tests/test_desktop_spec_creator.py     # CI / run_regression 入口
  python -m pytest tests/test_desktop_spec_creator.py -q

2026-10-30：改为不依赖 pytest fixture（原版用 tmp_path/sample_payload，
run_regression 以 `python <file>` 直接执行时 fixture 不会被解析，等于空跑）。
"""
import os
import sys
import json
import tempfile
import shutil

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.tools.desktop_spec_creator import (
    SpecManager,
    DesktopSpecApi,
    get_project_root,
    find_python_interpreter,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _sample_payload() -> dict:
    return {
        "project_id": "test_proj_001",
        "raw_video_path": "data/raw_samples/sample.mp4",
        "brand_en": "MIXCITY",
        "brand_cn": "高端女装",
        "product_title_l1": "羊绒针织衫",
        "product_title_l2": "圆领套头",
        "product_img_1": "data/raw_samples/img1.png",
        "product_img_2": "",
        "duration_range": "45-65",
        "split_strategy": "force_off",
        "keywords": "羊绒, 显瘦, 百搭, 闭眼入",
    }


def test_spec_manager_build_spec_dict() -> None:
    """表单字段 → 标准 spec 结构（含关键词多分隔符解析）。"""
    spec = SpecManager().build_spec_dict(_sample_payload())

    assert spec["project_id"] == "test_proj_001"
    assert spec["raw_video_path"] == "data/raw_samples/sample.mp4"
    assert spec["brand"]["title_en"] == "MIXCITY"
    assert spec["brand"]["subtitle_cn"] == "高端女装"
    assert spec["product"]["title_line1"] == "羊绒针织衫"
    assert spec["product"]["title_line2"] == "圆领套头"
    assert spec["product"]["item_images"] == ["data/raw_samples/img1.png"]
    assert spec["options"]["duration_range"] == "45-65"
    assert spec["options"]["split_strategy"] == "force_off"
    assert spec["options"]["highlight_keywords"] == ["羊绒", "显瘦", "百搭", "闭眼入"]
    print("  [PASS] build_spec_dict：字段映射与关键词解析正确")


def test_spec_manager_validate_missing_required() -> None:
    """必填项缺失时应返回明确错误。"""
    errors = SpecManager().validate({"project_id": "", "raw_video_path": ""})
    assert errors, "缺失必填项却未报错"
    assert any("任务代号" in e or "project_id" in e for e in errors)
    print(f"  [PASS] validate：缺失必填项报错 {len(errors)} 条")


def test_spec_manager_save_and_load() -> None:
    """保存后能原样读回（UTF-8、中文不转义丢失）。"""
    tmpdir = tempfile.mkdtemp()
    try:
        manager = SpecManager()
        out = os.path.join(tmpdir, "job_spec.json")
        spec = manager.build_spec_dict(_sample_payload())
        assert manager.save_spec(spec, out) is True
        assert os.path.exists(out)

        loaded = manager.load_spec(out)
        assert loaded["project_id"] == "test_proj_001"
        assert loaded["product"]["title_line1"] == "羊绒针织衫"
        print("  [PASS] save/load：往返一致")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_api_save_spec() -> None:
    """桌面 API 入口：校验通过即落盘，且内容可被 json 解析。"""
    tmpdir = tempfile.mkdtemp()
    try:
        target = os.path.join(tmpdir, "job_spec.json")
        result = DesktopSpecApi(spec_path=target).save_spec(_sample_payload())
        assert result["success"] is True, result.get("message")
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["project_id"] == "test_proj_001"
        assert data["options"]["highlight_keywords"] == ["羊绒", "显瘦", "百搭", "闭眼入"]
        print("  [PASS] DesktopSpecApi.save_spec：落盘成功")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_api_save_spec_rejects_invalid() -> None:
    """校验失败时不得写盘（防脏 spec 覆盖用户当前任务）。"""
    tmpdir = tempfile.mkdtemp()
    try:
        target = os.path.join(tmpdir, "job_spec.json")
        bad = {"project_id": "", "raw_video_path": "", "brand_en": "",
               "product_title_l1": "", "product_title_l2": ""}
        result = DesktopSpecApi(spec_path=target).save_spec(bad)
        assert result["success"] is False
        assert not os.path.exists(target), "校验失败却写出了文件"
        print("  [PASS] DesktopSpecApi.save_spec：非法输入被拒且未写盘")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_environment_helpers() -> None:
    """项目根目录与解释器探测可用，且不再硬编码个人路径。"""
    root = get_project_root()
    assert os.path.isdir(root)
    assert os.path.exists(os.path.join(root, "requirements.txt"))

    py = find_python_interpreter()
    assert py and len(py) > 0

    # 回归保护：源码里不得出现硬编码的用户目录
    src = os.path.join(root, "src", "tools", "desktop_spec_creator.py")
    with open(src, "r", encoding="utf-8") as f:
        code = f.read()
    assert "C:\\Users\\" not in code and "/home/" not in code.split("def get_project_root")[0], \
        "find_python_interpreter 仍硬编码了个人绝对路径"
    print(f"  [PASS] 环境探测：root={os.path.basename(root)}，解释器可解析")


def main() -> int:
    tests = [
        test_spec_manager_build_spec_dict,
        test_spec_manager_validate_missing_required,
        test_spec_manager_save_and_load,
        test_api_save_spec,
        test_api_save_spec_rejects_invalid,
        test_environment_helpers,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"  [FAIL] {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  [ERROR] {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n[TEST-DESKTOP] {len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
