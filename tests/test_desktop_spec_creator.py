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


def test_api_save_spec_direct_to_material_dir() -> None:
    """GUI 默认模式下直接落盘至用户素材目录，绝对不修改仓库内文件。"""
    tmpdir = tempfile.mkdtemp()
    try:
        payload = _sample_payload()
        payload["project_id"] = "custom_silk_99"
        material_dir = os.path.join(tmpdir, "user_material_folder")
        os.makedirs(material_dir, exist_ok=True)
        payload["raw_video_path"] = material_dir

        repo_spec = os.path.join(PROJECT_ROOT, "job_spec.json")
        orig_mtime = os.path.getmtime(repo_spec) if os.path.exists(repo_spec) else None

        api = DesktopSpecApi()
        result = api.save_spec(payload)

        assert result["success"] is True, result.get("message")
        target_spec = os.path.join(material_dir, "job_spec.json")
        named_spec = os.path.join(material_dir, "custom_silk_99_job_spec.json")
        assert os.path.exists(target_spec), "素材目录下未生成 job_spec.json"
        assert os.path.exists(named_spec), "素材目录下未生成 custom_silk_99_job_spec.json"

        if orig_mtime is not None:
            assert os.path.getmtime(repo_spec) == orig_mtime, "仓库内 job_spec.json 被篡改！"
        print("  [PASS] DesktopSpecApi：直接生成至素材目录且仓库内零修改")
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


def test_api_start_pipeline_wires_job_runner() -> None:
    """测试 DesktopSpecApi.start_pipeline 正确链接 JobRunner 调度并返回状态。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        mat_file = os.path.join(tmpdir, "test.mp4")
        with open(mat_file, "wb") as f:
            f.write(b"0" * 1024)

        payload = _sample_payload()
        payload["raw_video_path"] = mat_file
        payload["project_id"] = "desktop_pipeline_test"

        api = DesktopSpecApi()
        from unittest.mock import patch
        with patch("src.runtime.job_runner.JobRunner.run_pipeline") as mock_run:
            mock_run.return_value = {"status": "DELIVERED", "final_video": "out.mp4"}
            res = api.start_pipeline(payload)
            assert res["success"] is True
            assert res["status"] == "DELIVERED"
            assert mock_run.called
        print("  [PASS] DesktopSpecApi.start_pipeline：成功打通 EXE 与 JobRunner 调用链")


def test_initial_data_has_no_fake_materials() -> None:
    """默认初始数据中严禁预填虚假材质关键词（桑蚕丝/羊绒/羊毛）。"""
    api = DesktopSpecApi()
    init_data = api.load_initial_data()
    keywords = init_data.get("keywords", "")
    assert "桑蚕丝" not in keywords and "羊绒" not in keywords and "羊毛" not in keywords, \
        "初始数据中残留了未经证实的虚假材质宣传词"
    print("  [PASS] load_initial_data：无虚假材质预填")


def test_api_scan_project_and_quarantine() -> None:
    """向导扫描项目：自动识别原始素材并严格隔离历史成品与报告。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        proj_dir = os.path.join(tmpdir, "demo_proj")
        mat_dir = os.path.join(proj_dir, "素材")
        os.makedirs(mat_dir, exist_ok=True)

        # 放入原始视频
        with open(os.path.join(mat_dir, "raw_chunk1.mp4"), "wb") as f:
            f.write(b"video1")
        with open(os.path.join(mat_dir, "cover.png"), "wb") as f:
            f.write(b"img1")

        # 放入历史成片及生成报告（应被隔离）
        with open(os.path.join(mat_dir, "demo_proj.mp4"), "wb") as f:
            f.write(b"historical_video")
        with open(os.path.join(mat_dir, "demo_proj_qc_report.json"), "w", encoding="utf-8") as f:
            json.dump({"status": "PASS"}, f)

        api = DesktopSpecApi()
        scan_res = api.scan_project(proj_dir)
        assert scan_res["success"] is True
        assert scan_res["project_id"] == "demo_proj"

        # 验证原始视频仅包含 raw_chunk1.mp4，demo_proj.mp4 被严格隔离
        source_names = [v["filename"] for v in scan_res["source_videos"]]
        assert "raw_chunk1.mp4" in source_names
        assert "demo_proj.mp4" not in source_names

        quarantine_names = [q["filename"] for q in scan_res["quarantined_files"]]
        assert "demo_proj.mp4" in quarantine_names

        # 验证商品图被正确识别 (不再因字段名 mismatch 丢失)
        image_names = [im["filename"] for im in scan_res["images"]]
        assert "cover.png" in image_names
        assert len(scan_res["images"]) == 1
        print("  [PASS] DesktopSpecApi.scan_project：向导素材发现与历史成品隔离正确")


def test_api_create_new_project() -> None:
    """测试向导新建项目目录与初始化 spec。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        new_proj_p = os.path.join(tmpdir, "my_new_project_001")
        api = DesktopSpecApi()
        res = api.create_new_project(new_proj_p, "my_new_project_001")
        assert res["success"] is True
        assert res["project_id"] == "my_new_project_001"
        assert os.path.isdir(os.path.join(new_proj_p, "素材"))
        assert os.path.isfile(os.path.join(new_proj_p, "素材", "job_spec.json"))
        print("  [PASS] DesktopSpecApi.create_new_project：新建项目初始化结构正确")


def test_api_create_new_project_with_parent_dir() -> None:
    """测试向导通过选择父目录 + 输入子目录名创建新项目。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        api = DesktopSpecApi()
        res = api.create_new_project(tmpdir, "child_project_002")
        assert res["success"] is True
        assert res["project_id"] == "child_project_002"
        expected_dir = os.path.join(tmpdir, "child_project_002")
        assert os.path.isdir(os.path.join(expected_dir, "素材"))
        assert os.path.isfile(os.path.join(expected_dir, "素材", "job_spec.json"))
        print("  [PASS] DesktopSpecApi.create_new_project(parent, name)：子目录创建正确")


def test_html_content_embedded_js_syntax() -> None:
    """验证向导内嵌 HTML 与 JavaScript 语法的正确性，绝不允许语法错误导致 WebView 白屏或死锁。"""
    from src.tools.desktop_spec_creator import get_html_content
    import re
    import subprocess
    import shutil

    html = get_html_content()
    assert html and "<!DOCTYPE html>" in html
    m = re.search(r"<script>(.*?)</script>", html, re.DOTALL)
    assert m, "HTML 模板中未找到 <script> 标签"
    script = m.group(1)

    # 1. 结构与闭合检查
    assert "create_new_project" in script
    assert "setProductImageByIndex" in script
    assert "generateAgentTaskAction" in script
    assert "collectPayload" in script
    assert "validateStep" not in script, "严禁调用未定义的 validateStep 函数"
    assert "collectFormData" not in script, "严禁调用未定义的 collectFormData 函数"
    assert "copyTaskPrompt" in script
    assert 'onclick="generateAgentTaskAction()"' in html
    assert 'id="agentTaskHandoffCard"' in html
    assert 'id="taskPromptText"' in html

    # 2. 若本地存在 node 环境，执行严格的编译器语法验证 (node -c)
    node_bin = shutil.which("node")
    if node_bin:
        res = subprocess.run([node_bin, "-c", "-"], input=script, text=True, capture_output=True)
        assert res.returncode == 0, f"内嵌 JavaScript 语法错误:\n{res.stderr}"
        print("  [PASS] get_html_content：内嵌 JavaScript 经 node -c 编译器语法验证 100% 通过")
    else:
        print("  [PASS] get_html_content：静态语法与关键字结构校验通过")


def test_spec_manager_subtitle_and_cover_strategy() -> None:
    """测试字幕策略与封面生成策略卡片点选存盘。"""
    mgr = SpecManager()
    payload = _sample_payload()
    payload["subtitle_strategy"] = "plain"
    payload["cover_strategy"] = "product_image"
    spec = mgr.build_spec_dict(payload)
    assert spec["options"]["features"]["render_profile"] == "plain"
    assert spec["options"]["cover_strategy"] == "product_image"
    print("  [PASS] SpecManager：字幕与封面策略映射正确")


def test_api_status_and_open_helpers() -> None:
    """测试状态获取与文件安全打开辅助函数。"""
    api = DesktopSpecApi()
    status = api.get_job_status()
    assert "status" in status
    assert "progress" in status

    # 路径不存在时返回 False
    res = api.open_path("C:\\non_existent_folder_xyz123")
    assert res["success"] is False
    print("  [PASS] DesktopSpecApi：状态机与打开路径防护正常")


def test_api_generate_agent_task_branded_unbranded_and_zero_selection() -> None:
    """测试 A02 核心改造：生成任务交接包、零勾选阻断、支持空品牌/有品牌。"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        api = DesktopSpecApi()
        mat_dir = os.path.join(td, "素材")
        os.makedirs(mat_dir, exist_ok=True)
        dummy_v1 = os.path.join(mat_dir, "v1.mp4")
        with open(dummy_v1, "wb") as f:
            f.write(b"dummy_video_bytes")

        # 1. 零勾选阻断
        zero_payload = {
            "project_id": "test_p",
            "project_root": td,
            "raw_video_paths": [],
            "product_title_l1": "商品第一行",
            "product_title_l2": "商品第二行",
        }
        res_zero = api.generate_agent_task(zero_payload)
        assert res_zero["success"] is False
        assert res_zero["status"] == "NEEDS_INPUT"

        # 2. 空品牌合法生成
        unbranded_payload = {
            "project_id": "test_unbranded",
            "project_root": td,
            "raw_video_paths": [dummy_v1],
            "brand_en": "",
            "brand_cn": "",
            "product_title_l1": "无品牌羊毛开衫",
            "product_title_l2": "质感百搭",
        }
        res_unbranded = api.generate_agent_task(unbranded_payload)
        assert res_unbranded["success"] is True
        assert os.path.isfile(res_unbranded["task_md_path"])
        with open(res_unbranded["task_md_path"], "r", encoding="utf-8") as f:
            content = f.read()
            assert "python -m src.cli prepare" in content

        with open(res_unbranded["spec_path"], "r", encoding="utf-8") as f:
            saved_spec = json.load(f)
            assert saved_spec["brand"]["title_en"] == ""
            assert saved_spec["brand"]["subtitle_cn"] == ""

        # 3. 有品牌合法生成
        branded_payload = {
            "project_id": "test_branded",
            "project_root": td,
            "raw_video_paths": [dummy_v1],
            "brand_en": "NEW_BRAND",
            "brand_cn": "高端设计",
            "product_title_l1": "品牌羊毛大衣",
            "product_title_l2": "优雅大气",
        }
        res_branded = api.generate_agent_task(branded_payload)
        assert res_branded["success"] is True
        with open(res_branded["spec_path"], "r", encoding="utf-8") as f:
            saved_spec_b = json.load(f)
            assert saved_spec_b["brand"]["title_en"] == "NEW_BRAND"
            assert saved_spec_b["brand"]["subtitle_cn"] == "高端设计"

        # 4. 非破坏性兼容：验证旧的 <project_id>_job_spec.json 绝不被删除
        mat_dir = os.path.join(td, "素材")
        old_named = os.path.join(mat_dir, "test_branded_job_spec.json")
        with open(old_named, "w", encoding="utf-8") as f:
            f.write('{"legacy": true}')
        res_non_destructive = api.generate_agent_task(branded_payload)
        assert res_non_destructive["success"] is True
        assert os.path.isfile(old_named), "非破坏性规范要求：旧的命名配置文件绝不允许被强制删除！"

    print("  [PASS] DesktopSpecApi：任务交接生成、零勾选阻断与空品牌/有品牌支持完全正确")


def main() -> int:
    tests = [
        test_spec_manager_build_spec_dict,
        test_spec_manager_validate_missing_required,
        test_spec_manager_save_and_load,
        test_api_save_spec,
        test_api_save_spec_rejects_invalid,
        test_api_save_spec_direct_to_material_dir,
        test_environment_helpers,
        test_api_start_pipeline_wires_job_runner,
        test_initial_data_has_no_fake_materials,
        test_api_scan_project_and_quarantine,
        test_api_create_new_project,
        test_api_create_new_project_with_parent_dir,
        test_spec_manager_subtitle_and_cover_strategy,
        test_api_status_and_open_helpers,
        test_api_generate_agent_task_branded_unbranded_and_zero_selection,
        test_html_content_embedded_js_syntax,
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
