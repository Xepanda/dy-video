#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
test_video_effects.py - 视频特效（2026-10-24 新增）单元测试
============================================================
覆盖本轮新增的四类元素与两项决策改造：

1. 封面贴图布局：并排 → **靠右垂直堆叠**（需求变更）
2. 顶部平台标识条：生成 + 尺寸/文本正确
3. 挂角利益点贴纸：文案生成 + 出现/消失时序
4. 放大特效：**分镜内定时缓动推镜**（触发点 + smoothstep 过渡）
5. 镜像/分屏：effect 从常量改为**决策变量**（不再死代码）
6. 防反文字遮挡：镜像分镜的 OCR 定位 + 贴纸覆盖

运行: python -u tests/test_video_effects.py
"""
import os
import sys
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.graphics.video_overlays import (
    generate_platform_bar, generate_stage_sticker,
    generate_occlusion_patch, generate_split_labels,
)
from src.core.video_assembler import build_clip_filter
from src.tools.build_plan import _build_sticker_texts
from src.graphics.cover_renderer import DEFAULTS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONSTRAINTS = yaml.safe_load(
    open(os.path.join(ROOT, "contracts", "constraints.yaml"), encoding="utf-8"))
TMP = os.path.join(ROOT, "data", "_test_effects")
os.makedirs(TMP, exist_ok=True)


def case_sticker_layout_vertical():
    """需求 1：商品图改靠右垂直堆叠。"""
    st = CONSTRAINTS["sticker"]
    assert st["layout"] == "vertical", f"sticker.layout 应为 vertical，实为 {st['layout']}"
    assert DEFAULTS["sticker"]["layout"] == "vertical", \
        "渲染器内置默认也应为 vertical"
    assert st["side"] == "right", "应保持靠右"
    print("[PASS] 封面贴图布局 = vertical（靠右对齐、上下堆叠）")


def case_platform_bar():
    """需求 2：视频顶部平台名称标识条。"""
    bar_c = CONSTRAINTS["platform_bar"]
    out = os.path.join(TMP, "bar.png")
    generate_platform_bar(out, bar_c, shop_name="MIXCITY", account="@深圳高端女装")
    assert os.path.exists(out), "标识条未生成"
    from PIL import Image
    img = Image.open(out)
    assert img.width == 1080, f"宽度应为 1080，实为 {img.width}"
    assert img.height == bar_c["height"], f"高度应为 {bar_c['height']}，实为 {img.height}"
    # 底色不应完全透明（必须是不透明条，不能被视频穿透）
    alpha = img.split()[3]
    assert alpha.getextrema()[0] == bar_c["background_alpha"], \
        "底色不透明度与配置不符"
    print(f"[PASS] 顶部平台标识条: {img.width}x{img.height}，底色 alpha={bar_c['background_alpha']}")


def case_stage_sticker_text():
    """需求 3a：挂角贴纸文案生成。"""
    clips = [
        {"id": "c01_hook", "category": "hook", "desc": "这个真的绝了"},
        {"id": "c02_fabric", "category": "fabric", "desc": "3M新雪丽面料"},
        {"id": "c03_fit", "category": "fit", "desc": "很显瘦"},
        {"id": "c04_styling", "category": "styling", "desc": "随便搭"},
    ]
    sc = CONSTRAINTS["video_sticker"]
    texts = _build_sticker_texts(clips, {}, sc)
    assert texts, "应生成贴纸文案"
    assert texts.get("c02_fabric") == "3M新雪丽", \
        f"应命中具体卖点短语，实为 {texts.get('c02_fabric')}"
    assert texts.get("c03_fit") == "显瘦", \
        f"应命中卖点词，实为 {texts.get('c03_fit')}"
    assert texts.get("c04_styling") == "一衣多穿", \
        f"应回落到类目短语，实为 {texts.get('c04_styling')}"
    # 显式指定优先
    spec = {"options": {"sticker_texts": {"c01_hook": "指定文案"}}}
    t2 = _build_sticker_texts(clips, spec, sc)
    assert t2["c01_hook"] == "指定文案", "spec 显式指定应优先"
    print(f"[PASS] 挂角贴纸文案: {texts}")


def case_stage_sticker_render():
    """需求 3a：贴纸渲染。"""
    sc = CONSTRAINTS["video_sticker"]
    out = os.path.join(TMP, "stk.png")
    generate_stage_sticker("3M新雪丽", out, sc)
    from PIL import Image
    img = Image.open(out)
    assert img.width == sc["size"] and img.height == sc["size"], \
        f"应为 {sc['size']}x{sc['size']}，实为 {img.size}"
    alpha = img.split()[3]
    assert alpha.getextrema()[1] == 255, "贴纸应有完全不透明区域"
    print(f"[PASS] 挂角贴纸渲染: {img.size}")


def case_zoom_timed_transition():
    """需求 3b：放大特效的触发位置与过渡方式。"""
    zc = CONSTRAINTS["zoom_effect"]
    assert zc["enabled"] is True
    assert zc["trigger_sec"] > 0, "应有触发点"
    assert zc["transition_sec"] > 0, "应有过渡时长"
    assert zc["easing"] == "smoothstep", "应为 smoothstep 缓动"
    assert 1.25 <= zc["max_zoom"] <= 1.35, "倍率应在 SKILL 的 1.25~1.35 区间"

    # 普通分镜：无 zoompan
    vf = build_clip_filter({"zoom": 1.0}, zc)
    assert "zoompan" not in vf, f"无 zoom 的分镜不应有 zoompan: {vf}"
    # 推镜分镜：必须有 zoompan 且带触发点/缓动表达式
    vf2 = build_clip_filter({"zoom": 1.3, "y_anchor": 0.35}, zc)
    assert "zoompan" in vf2, f"推镜分镜应有 zoompan: {vf2}"
    trig_f = int(round(zc["trigger_sec"] * 30))
    trans_f = int(round(zc["transition_sec"] * 30))
    assert f"on-{trig_f}" in vf2, f"应含触发帧号 on-{trig_f}: {vf2}"
    assert f"/{trans_f}" in vf2, f"应含过渡帧数 /{trans_f}: {vf2}"
    assert "pow(" in vf2, "应使用 smoothstep 缓动（pow 表达式）"
    print(f"[PASS] 放大特效: 触发 {zc['trigger_sec']}s(frame {trig_f}) → "
          f"过渡 {zc['transition_sec']}s({trans_f}帧, smoothstep)")


def case_effect_is_decision_variable():
    """需求 4：effect 必须是决策变量，不能是写死常量。"""
    import inspect
    from src.tools import build_plan as bp
    src = inspect.getsource(bp)
    # 决策逻辑必须存在：effect 被按规则改写，而不是只有初始化值
    assert 'clip["effect"] = "hflip"' in src, \
        "build_plan 缺少镜像决策逻辑（effect 仍是死值）"
    assert '["effect"] = "split"' in src, \
        "build_plan 缺少分屏决策逻辑"
    assert "EFFECT" in src or "特效决策" in src, "应存在特效决策段落"
    # 装配器仍应支持三种 effect
    from src.core.video_assembler import build_clip_filter as bcf
    assert "hflip" in bcf({"effect": "hflip", "zoom": 1.0}, {})
    print("[PASS] effect 已改为决策变量（镜像/分屏可被触发）")


def case_occlusion_patch():
    """需求 4：镜像防反文字遮挡贴纸。"""
    mc = CONSTRAINTS["mirror_effect"]
    assert mc["occlusion_required"] is True
    out = os.path.join(TMP, "occ.png")
    regions = [(100, 200, 400, 320), (600, 150, 900, 260)]
    generate_occlusion_patch(out, 1080, 1920, regions, mc)
    from PIL import Image
    import numpy as np
    img = Image.open(out)
    arr = np.array(img)
    assert arr.shape[:2] == (1920, 1080), f"尺寸应 1080x1920，实为 {arr.shape}"
    covered = int((arr[:, :, 3] > 0).sum())
    expect = sum((x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in regions)
    assert covered > expect * 0.9, f"遮挡面积过小: {covered} vs {expect}"
    print(f"[PASS] 镜像遮挡贴纸: 覆盖 {covered}px（{len(regions)} 处）")


def case_split_labels():
    """需求 4：分屏双侧胶囊标签。"""
    sc = CONSTRAINTS["split_effect"]
    out = os.path.join(TMP, "split.png")
    generate_split_labels(out, "藏青显瘦", "米白百搭", sc, half_w=540, height=1920)
    from PIL import Image
    import numpy as np
    img = Image.open(out)
    arr = np.array(img)
    left = (arr[:, :540, 3] > 0).sum()
    right = (arr[:, 540:, 3] > 0).sum()
    assert left > 0 and right > 0, f"左右标签都应有内容: L={left} R={right}"
    print(f"[PASS] 分屏双侧标签: 左 {left}px / 右 {right}px")


def case_qc_overlay_audit():
    """QC 必须新增叠加元素复测。"""
    import inspect
    from src.qc.qc_auditor import QcAuditor
    assert hasattr(QcAuditor, "_audit_overlays"), "QC 缺少 _audit_overlays"
    assert hasattr(QcAuditor, "_audit_av_sync"), "QC 缺少 _audit_av_sync"
    sig = inspect.signature(QcAuditor.audit_final_deliverables)
    assert "overlay_meta_path" in sig.parameters, \
        "audit_final_deliverables 应接受 overlay_meta_path"
    print("[PASS] QC 已接入叠加元素复测 + 音画同步抽检")


def main():
    failures = []
    for fn in (case_sticker_layout_vertical, case_platform_bar,
               case_stage_sticker_text, case_stage_sticker_render,
               case_zoom_timed_transition, case_effect_is_decision_variable,
               case_occlusion_patch, case_split_labels,
               case_qc_overlay_audit):
        try:
            fn()
        except AssertionError as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"[FAIL] {e}")
        except Exception as e:
            failures.append(f"{fn.__name__}: {type(e).__name__}: {e}")
            print(f"[ERROR] {type(e).__name__}: {e}")
    # 清理
    try:
        import shutil
        shutil.rmtree(TMP, ignore_errors=True)
    except OSError:
        pass
    print(f"\n[TEST-EFFECTS] {'全部通过' if not failures else '失败: ' + str(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
