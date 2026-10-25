#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
test_multi_source.py - 跨素材选段核心回归（B17）
=========================================================
用例：
  1. build_blocks 不跨文件合并：相邻时间戳的两源 segment 不得被并成一个 block
  2. block 继承 source_file；修剪路径（关键词窗口 / 段对齐 / 余料）均不丢来源
  3. build_plan 产出的 clips 带source_file，且时长收敛到 [45,65]
  4. 时长补齐阶段不引入无商品语义闲聊块（需求3）
  5. 字幕后处理：时间戳守恒 + 不丢 source_file（需求4）

背景（2026-10-21）：多源选段为本轮新增能力，此前零单测覆盖；
而 build_blocks/字幕后处理都是**重建 segment 对象**的地方，最易丢 source_file，
一旦丢失 video_assembler 会从默认文件切片，画面与口型全错且不报错。

运行: python -u tests/test_multi_source.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.tools.build_plan import (
    build_blocks, build_plan, _is_idle_chatter, _clean_oral_fillers)
from src.core.asr_agent_verifier import AsrContextVerifier

SRC_A = "D:/raw/A_001.mp4"
SRC_B = "D:/raw/A_002.mp4"

SPEC = {
    "project_id": "multi_demo",
    "product": {"title_line1": "半岛棉镂空钩花", "title_line2": "气质显瘦针织衫"},
    "brand": {"title_en": "MIXCITY"},
    "options": {"duration_range": "45-65",
                "highlight_keywords": ["半岛棉", "显瘦", "针织", "镂空"]},
}


def _seg(start, end, text, src):
    return {"start": start, "end": end, "duration": round(end - start, 3),
            "text": text, "source_file": src,
            "source_tag": os.path.basename(src)}


def _mk_segments():
    """构造两源语料：源 A 面料+显瘦，源 B 开场钩子+穿搭。

    注意 A 的末段与 B 的首段时间戳刻意重叠（0.8s 处），
    若代码错误地跨文件合并，会产出 start/end 混乱的巨型块。
    """
    return [
        _seg(0.0, 3.0, "这个半岛棉的面料真的非常柔软亲肤", SRC_A),
        _seg(3.2, 6.0, "镂空钩花的做工超级显瘦上身", SRC_A),
        _seg(6.2, 9.0, "针织的版型非常修身遮肉", SRC_A),
        _seg(9.2, 12.0, "我今天穿这件真的太好看了气质绝了", SRC_A),
        # 源 B —— 时间轴从 0 重新开始
        _seg(0.5, 3.5, "姐妹们看这个上身效果真的绝了独一无二", SRC_B),
        _seg(3.7, 7.0, "搭配西裤老钱风松弛感拉满", SRC_B),
        _seg(7.2, 10.0, "再配个毛衣叠穿特别好看", SRC_B),
        _seg(10.2, 13.0, "气质超级显瘦显高比例很好", SRC_B),
        _seg(13.2, 17.0, "这个针织衫的面料是半岛棉的非常亲肤柔软", SRC_B),
        _seg(17.2, 21.0, "镂空钩花的工艺特别透气不闷汗", SRC_B),
        _seg(21.2, 25.0, "上班通勤穿超级合适显瘦又气质", SRC_B),
        _seg(9.2, 15.0, "搭配牛仔裤也是超级好看很百搭", SRC_A),
        _seg(15.2, 21.0, "外面套个风衣气场就出来了非常高级", SRC_A),
        _seg(21.2, 27.0, "老钱风松弛感拿捏得刚刚好", SRC_A),
        _seg(27.2, 31.0, "半导棉的材质摸起来软糯亲肤很透气", SRC_A),
        _seg(31.2, 35.0, "这个镂空钩花超级显瘦遮肉又百搭", SRC_A),
        _seg(35.2, 39.0, "上班逛街都能穿非常非常非常实用", SRC_A),
        _seg(25.2, 29.0, "气质显瘦显高穿起来非常舒服", SRC_B),
        _seg(29.2, 33.0, "西裤衬衫裙子都能搭配很百搭", SRC_B),
    ]


def case_no_cross_file_merge():
    blocks, _ = build_blocks(_mk_segments(), {})
    assert blocks, "应产出 block"
    for b in blocks:
        srcs = {s["source_file"] for s in b["segments"]}
        assert len(srcs) == 1, \
            f"block 跨文件合并了: {srcs}（时间轴不通，合并即错）"
        assert b.get("source_file"), "block 未继承 source_file"
    by_src = {}
    for b in blocks:
        by_src.setdefault(b["source_file"], []).append(b)
    assert set(by_src) == {SRC_A, SRC_B}, f"两源都应有块，实得 {list(by_src)}"
    print(f"[PASS] 用例1 不跨文件合并（{len(blocks)} 块，"
          f"A={len(by_src.get(SRC_A, []))} B={len(by_src.get(SRC_B, []))}）")


def case_trim_keeps_source():
    """修剪路径（关键词最密窗口）必须继承 source。"""
    segs = [_seg(i * 2.0, i * 2.0 + 1.9,
                 "显瘦显瘦非常好看" if i % 2 == 0 else "镂空半岛棉面料",
                 SRC_B) for i in range(14)]
    blocks, _ = build_blocks(segs, {})
    assert blocks, "长块场景应产出 block"
    for b in blocks:
        assert b.get("source_file") == SRC_B, "block 未继承来源"
    # 验证 build_plan 产出的每个 clip 都带来源（含关键词窗口修剪路径）
    plan = build_plan(SRC_B, segs, SPEC, {}, cache_key=None, work_dir=None)
    for c in plan["clips"]:
        assert c.get("source_file") == SRC_B, \
            f"clip {c['id']} 丢失 source_file（got={c.get('source_file')}）"
    print(f"[PASS] 用例2 修剪后仍保留来源（{len(plan['clips'])} 个 clip）")


def case_plan_duration_and_clips():
    plan = build_plan(SRC_A, _mk_segments(), SPEC, {}, cache_key=None, work_dir=None)
    dur = plan["total_expected_duration"]
    # 契约：时长必须收敛进 [45,65]；若素材语义内容确实不足 45s，
    # 允许"尽力补齐后仍略低"但必须在 notes 中显式记录（不得静默短于下限）。
    if dur < 45:
        notes = " ".join(plan["selection_meta"].get("notes", []))
        assert "仍低于下限" in notes, f"总时长 {dur}s < 45s 且未在 notes 说明原因"
    assert dur <= 65, f"总时长 {dur}s 超出上限 65s"
    assert dur > 40, f"总时长 {dur}s 明显偏短，补齐逻辑可能失效"
    assert plan.get("multi_source") is True, "应标记 multi_source"
    assert len(plan.get("sources_used", [])) == 2, f"应使用 2 个源，实得 {plan.get('sources_used')}"
    for c in plan["clips"]:
        assert c["source_file"] in (SRC_A, SRC_B)
        assert c["dur"] > 0 and c["end"] > c["st"]
    print(f"[PASS] 用例3 计划合规（{dur}s / {len(plan['clips'])} clip / 2 源）")


def case_no_idle_chatter_in_topup():
    """闲聊/ASR 幻觉不得进入成片（需求3）。"""
    assert _is_idle_chatter("我怎么跑到姜桑达马吉拉奥说错了")
    assert _is_idle_chatter("刚才说到那一半了")
    assert not _is_idle_chatter("这个半岛棉的面料非常柔软亲肤")
    # 混入闲聊后，总时长仍应只由有效语义块构成
    segs = _mk_segments() + [
        _seg(20.0, 24.0, "我怎么跑到姜桑达马吉拉奥说错了", SRC_B),
        _seg(24.2, 28.0, "他们坐船跟我们坐船有什么区别", SRC_B),
    ]
    plan = build_plan(SRC_A, segs, SPEC, {}, cache_key=None, work_dir=None)
    joined = " ".join(c["desc"] for c in plan["clips"])
    assert "姜桑达" not in joined, "ASR 幻觉句进入了成片"
    assert "坐船" not in joined, "闲聊句进入了成片"
    print("[PASS] 用例4 闲聊/幻觉被过滤")


def case_subtitle_polish_keeps_source():
    """字幕后处理不得丢 source_file，且时间戳守恒（需求4）。"""
    v = AsrContextVerifier(SPEC)
    segs = [
        _seg(0.0, 3.0, "这个这个食物真的超级好看半岛棉", SRC_A),
        _seg(3.0, 6.0, "上身之后人都瘦眉了而且超选瘦非常非常非常非常好看的一个款式", SRC_B),
    ]
    out, diffs = v._run_deterministic_verification(segs)
    assert out, "应产出校对后 segments"
    for o in out:
        assert o.get("source_file"), f"字幕后处理丢失 source_file: {o}"
    # 时间戳守恒：首段起止不变，末段终点为原终点
    assert abs(out[0]["start"] - 0.0) < 1e-6 and abs(out[0]["end"] - 3.0) < 1e-6
    assert abs(out[-1]["end"] - 6.0) < 1e-6, f"末段终点漂移: {out[-1]['end']}"
    # 错词修正生效
    txt = "".join(o["text"] for o in out)
    assert "实物" in txt and "食物" not in txt, "食物→实物 纠错未生效"
    assert "半岛棉" in txt, "错词修正丢失"
    # 长句被切分为多条
    assert len(out) > len(segs), "超长句应被切分为多条"
    assert diffs, "应记录校对 diff"
    print(f"[PASS] 用例5 字幕后处理守恒且保来源（{len(segs)}→{len(out)} 条）")


def case_filler_clean():
    assert _clean_oral_fillers("这个这个真的好看") == "这个真的好看"
    assert _clean_oral_fillers("然后然后就是这样") == "然后就是这样"
    assert _clean_oral_fillers("真的真的好看") == "真的好看"
    assert _clean_oral_fillers("非常非常实用") == "非常实用"
    # 实义叠词不应被误删
    assert _clean_oral_fillers("件件都是爆款") == "件件都是爆款"
    # 填充词清理不得删掉实义内容
    assert "半岛棉" in _clean_oral_fillers("这个这个半岛棉真的好看")
    print("[PASS] 用例6 口语填充词清理")


def main():
    failures = []
    for fn in (case_no_cross_file_merge, case_trim_keeps_source,
               case_plan_duration_and_clips, case_no_idle_chatter_in_topup,
               case_subtitle_polish_keeps_source, case_filler_clean):
        try:
            fn()
        except AssertionError as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"[FAIL] {e}")
        except Exception as e:
            failures.append(f"{fn.__name__}: {type(e).__name__}: {e}")
            print(f"[ERROR] {type(e).__name__}: {e}")
    print(f"\n[TEST-B17] {'全部通过' if not failures else '失败: ' + str(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())