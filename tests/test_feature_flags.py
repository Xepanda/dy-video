# -*- coding: utf-8 -*-
"""tests/test_feature_flags.py — 统一能力开关（contracts/features.yaml）回归测试

覆盖：
  1. 默认值（不写 spec 时全部取推荐值）
  2. spec 覆盖生效
  3. 组合规则强制收敛（plain → record_only；compact → strict）
  4. 校验：未知开关 / 非法值 / 未开放项
  5. apply() 派生到既有配置键，且不污染原 constraints
  6. GUI 单一事实源：get_html_content() 的开关面板从 yaml 渲染（无占位符残留）
  7. QC 在 plain 形态下把字幕类断言记为 SKIP（不是 PASS）

运行：python -u tests/test_feature_flags.py   （CI / run_regression 入口）
      python -m pytest tests/test_feature_flags.py -q
"""
import os
import sys
import json
import yaml

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.tools import feature_flags as ff

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _constraints() -> dict:
    with open(os.path.join(PROJECT_ROOT, "contracts", "constraints.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_defaults() -> None:
    """不写 features 时取全部推荐默认值。"""
    res = ff.resolve({"options": {}})
    v = res["values"]
    assert v["render_profile"] == "branded", v
    assert v["duration_preset"] == "standard"
    assert v["fabric_ratio"] == "strict"
    assert v["promo_words"] == "allow_welfare"
    assert v["violation_action"] == "delete"
    assert v["transition"] == "on" and v["magnifier"] == "on"
    assert v["manual_review"] == "on"
    assert not res["forced"], "默认值不应触发组合规则"
    print("  [PASS] 默认值 = 推荐配置")


def test_spec_override() -> None:
    """spec 显式值覆盖默认值，并标记为 explicit。"""
    res = ff.resolve({"options": {"features": {"render_profile": "plain",
                                               "magnifier": "off"}}})
    assert res["values"]["render_profile"] == "plain"
    assert res["values"]["magnifier"] == "off"
    assert set(res["explicit"]) == {"render_profile", "magnifier"}
    print("  [PASS] spec 覆盖生效")


def test_combo_forces_record_only() -> None:
    """plain 形态下违禁词处置必须收敛为 record_only（无字幕可删）。"""
    res = ff.resolve({"options": {"features": {"render_profile": "plain",
                                               "violation_action": "delete"}}})
    assert res["values"]["violation_action"] == "record_only", res["values"]
    forced_keys = {f["key"] for f in res["forced"]}
    assert "violation_action" in forced_keys
    print(f"  [PASS] plain 强制收敛：{res['forced'][0]['from']} → {res['forced'][0]['to']}")


def test_combo_forces_strict_fabric() -> None:
    """紧凑时长档下参数占比必须锁定严格档。"""
    res = ff.resolve({"options": {"features": {"duration_preset": "compact",
                                               "fabric_ratio": "loose"}}})
    assert res["values"]["fabric_ratio"] == "strict"
    assert any(f["key"] == "fabric_ratio" for f in res["forced"])
    print("  [PASS] compact 强制 fabric_ratio=strict")


def test_validate_errors() -> None:
    """未知键 / 非法值 / 未开放项都必须被拦。"""
    assert ff.validate({"features": {"not_a_key": "x"}}), "未知开关未被拦"
    assert ff.validate({"features": {"render_profile": "pure"}}), "非法值未被拦"
    errs = ff.validate({"features": {"speech_rate": "1.2"}})
    assert errs and "未开放" in errs[0], errs
    assert not ff.validate({"features": {"render_profile": "plain"}}), "合法值被误拦"
    assert not ff.validate({}), "空 payload 应合法"
    print("  [PASS] 校验：未知/非法/未开放全部拦截")


def test_apply_derives_without_pollution() -> None:
    """开关派生到既有配置键，且不修改传入的 constraints。"""
    c = _constraints()
    before = c["video"]["funnel"]["fabric"]["max"]
    out = ff.apply(c, {"options": {"features": {"render_profile": "plain",
                                                "duration_preset": "compact",
                                                "transition": "off",
                                                "magnifier": "off",
                                                "fabric_ratio": "strict"}}})
    assert out["video"]["duration_range"] == [48, 55]
    assert out["video"]["funnel"]["fabric"]["max"] == 11
    assert out["transition_effect"]["enabled"] is False
    assert out["magnifier_effect"]["enabled"] is False
    assert out["violation_words"]["handling"]["subtitle_action"] == "off"
    assert out["features"]["render_profile"] == "plain"
    assert c["video"]["funnel"]["fabric"]["max"] == before, "原 constraints 被污染"
    print(f"  [PASS] 派生正确，原对象未变（fabric.max 仍为 {before}）")


def test_features_of_fallback() -> None:
    """未经 apply 的 constraints 读取时回落默认，不抛异常。"""
    f = ff.features_of({})
    assert f["render_profile"] == "branded"
    assert ff.is_plain({}) is False
    print("  [PASS] 未注入时安全回落")


def test_gui_single_source() -> None:
    """GUI 面板必须来自 features.yaml（占位符被替换、含关键开关）。"""
    from src.tools.desktop_spec_creator import get_html_content
    html = get_html_content()
    assert "__FEATURES_JSON__" not in html, "占位符未被替换"
    assert "featurePanel" in html
    for key in ("render_profile", "fabric_ratio", "violation_action", "manual_review"):
        assert key in html, f"面板缺少开关 {key}"
    # 未开放项必须带原因说明，避免用户误以为可选
    assert "未开放" in html
    print("  [PASS] GUI 面板由 yaml 渲染（单一事实源）")


def test_qc_plain_skips_subtitles() -> None:
    """plain 形态下 QC 的 7 项字幕断言记为 SKIP，并披露覆盖率。"""
    from src.qc.qc_auditor import QcAuditor
    c = _constraints()
    plain = ff.apply(c, {"options": {"features": {"render_profile": "plain"}}})
    branded = ff.apply(c, {"options": {"features": {"render_profile": "branded"}}})
    a_plain = QcAuditor(plain)
    a_branded = QcAuditor(branded)
    assert a_plain.subtitles_active is False
    assert a_branded.subtitles_active is True
    assert a_plain.report["disclaimers"], "plain 必须写入免责声明"
    # SKIP 原语：写 status=SKIP，且不影响 all_passed
    a_plain._skip("字幕零价格断言", "plain 无字幕")
    last = a_plain.report["checks"][-1]
    assert last["status"] == "SKIP" and a_plain.report["all_passed"] is True
    print("  [PASS] plain 模式 SKIP 语义正确（不误判为 PASS）")


def test_qc_plain_keeps_visual_checks() -> None:
    """plain 只跳过文字/贴纸类断言：放大镜/转场/镜像必须照检（防静默漏检）。"""
    import tempfile
    from src.qc.qc_auditor import QcAuditor
    c = _constraints()
    plain = ff.apply(c, {"options": {"features": {"render_profile": "plain"}}})
    a = QcAuditor(plain)
    meta = {"platform_bar": {"applied": False}, "sticker_events": [],
            "zoom_events": [], "mirror_clips": [], "transition": {},
            "magnifier_events": []}
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "video_overlay_meta.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        a._audit_overlays("nonexistent.mp4", p, plain)
    names = {c["name"]: c["status"] for c in a.report["checks"]}
    # 文字/贴纸两项 → SKIP
    assert names.get("视频顶部平台标识") == "SKIP", names
    assert names.get("挂角贴纸时序") == "SKIP", names
    # 画面技法四项 → 必须存在且不是 SKIP（曾因整体 return 而整项消失）
    for nm in ("放大特效时序", "镜像防反文字遮挡", "切片转场复测", "放大镟特效复测"):
        assert nm in names, f"plain 漏检画面项：{nm}（{sorted(names)}）"
        assert names[nm] != "SKIP", f"{nm} 不应在 plain 下跳过"
    print("  [PASS] plain 仅跳文字/贴纸，画面技法 4 项照检")


def test_frozen_root_resolution() -> None:
    """打包（PyInstaller onefile）后必须按 exe 目录找 features.yaml。

    历史坑：按 __file__ 推三层会落到 _MEIPASS 的祖先目录，面板静默为空。
    """
    fake_exe = os.path.join(os.path.abspath(os.sep), "fake", "SpecCreator.exe")
    had = hasattr(sys, "frozen")
    old_frozen = getattr(sys, "frozen", None)
    old_exe = sys.executable
    sys.frozen = True                    # type: ignore[attr-defined]
    sys.executable = fake_exe
    try:
        assert ff._project_root() == os.path.dirname(fake_exe)
        p = ff.default_definitions_path()
        assert p.endswith(os.path.join("contracts", "features.yaml")), p
    finally:
        sys.executable = old_exe
        if had:
            sys.frozen = old_frozen      # type: ignore[attr-defined]
        else:
            del sys.frozen               # type: ignore[attr-defined]
    # 还原后仍指向仓库内的真实文件
    assert os.path.exists(ff.default_definitions_path())
    print("  [PASS] 打包后按 exe 目录解析 features.yaml，源码态回落到仓库根")


def test_fabric_ratio_assertion() -> None:
    """决策 6：参数类内容占比 ≤25%（严格档 BLOCK / 宽松档仅记录 / 无分镜表 SKIP）。"""
    import tempfile
    from src.qc.qc_auditor import QcAuditor
    c = _constraints()

    def run(clips, features):
        cons = ff.apply(c, {"options": {"features": features}})
        a = QcAuditor(cons)
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "plan.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"clips": clips}, f, ensure_ascii=False)
            a._audit_fabric_ratio(p)
        return a.report["checks"][-1]

    over = [{"category": "fabric", "dur": 12.0}, {"category": "hook", "dur": 5.0},
            {"category": "fit", "dur": 10.0}, {"category": "styling", "dur": 10.0}]
    ok = [{"category": "fabric", "dur": 8.0}, {"category": "hook", "dur": 5.0},
          {"category": "fit", "dur": 10.0}, {"category": "styling", "dur": 10.0}]
    strict = {"render_profile": "branded", "fabric_ratio": "strict"}
    chk = run(over, strict)
    assert chk["status"] == "FAIL" and chk["measured"] > 0.25, chk
    chk = run(ok, strict)
    assert chk["status"] == "PASS", chk
    # 宽松档：只记录不判定（PASS + level=WARN，且 measured 仍落盘）
    chk = run(over, {"render_profile": "branded", "fabric_ratio": "loose"})
    assert chk["status"] == "PASS" and chk["level"] == "WARN", chk
    assert chk["measured"] > 0.25, chk
    # 没有分镜表 → SKIP，不冒充 PASS
    a = QcAuditor(ff.apply(c, {"options": {"features": strict}}))
    a._audit_fabric_ratio(None)
    assert a.report["checks"][-1]["status"] == "SKIP"
    print("  [PASS] 参数类占比：32.4% 拦 / 21.6% 过 / 宽松只记录 / 无表 SKIP")


def main() -> int:
    tests = [
        test_defaults, test_spec_override,
        test_combo_forces_record_only, test_combo_forces_strict_fabric,
        test_validate_errors, test_apply_derives_without_pollution,
        test_features_of_fallback, test_gui_single_source,
        test_qc_plain_skips_subtitles, test_qc_plain_keeps_visual_checks,
        test_frozen_root_resolution, test_fabric_ratio_assertion,
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
    print(f"\n[TEST-FEATURES] {len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
