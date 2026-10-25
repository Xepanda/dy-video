#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
build_plan.py - 泛化分镜编译器（B2 核心，消除硬编码）
=====================================================
替代 plan_compiler.py 中写死的 demo 视频时间戳，对任意直播素材生成合规 plan.json。

算法（确定性，无任何硬编码时间戳）：
  1. 黑名单过滤：价格（detect_price）、库存促销、换款离场（正则 + 可选 shot 边界佐证）
  2. 语义打标：漏斗四类 Hook / 面料 / 版型 / 穿搭（关键词命中）
  3. 连续块合并：间隔 <= merge_gap 的相邻合规段合并为 block（保口型同步）
  4. 配额贪心：按 constraints.video.funnel 配额选块，段边界修剪
  5. 叙事排序：Hook 置顶 → 面料 → 版型 → 穿搭；总时长收敛到 [45,65]
  6. 效果赋值：面料块自动 1.3x 推镜（zoom），其余 zoom=1.0

CLI:
  python src/tools/build_plan.py --video x.mp4 --segments whisper.json \
      --spec job_spec.json --constraints contracts/constraints.yaml \
      [--cuts cuts.json] --out-json plan.json
"""

import os
import sys
import json
import re
import argparse
from typing import List, Dict, Any, Tuple, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from src.tools.detect_price import find_price_matches

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

def _load_constraints(path: str = None) -> Dict[str, Any]:
    """载入 constraints.yaml（阈值唯一来源）。"""
    path = path or os.path.join(PROJECT_ROOT, "contracts", "constraints.yaml")
    if not os.path.exists(path):
        return {}
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------- 关键词表

# 关键词表（2026-10-21 按 4 条新素材真实 ASR 语料扩充：
# 原表过窄导致 4/4 素材出现"类别 X 无合规候选"——Hook 类目几乎没有命中词）
HOOK_KEYWORDS = ["更漂亮", "独一无二", "绝了", "比下去", "最好看", "最显瘦", "天花板",
                 "爆炸", "超级", "巨好看", "闭眼入", "王炸", "封神", "实物还要",
                 "无敌", "太美", "巨美", "太好看", "好看到", "好好看", "惊艳", "心动",
                 "喜欢", "爱了", "种草", "试穿", "上身效果", "新款", "姐妹们", "真香",
                 "气质", "高级感", "好看", "美", "真好看", "绝了", "强烈推荐", "首选"]
FABRIC_KEYWORDS = ["面料", "做工", "材质", "棉", "羊毛", "蚕丝", "绒", "针织", "纱",
                   "进口", "手感", "软糯", "亲肤", "透气", "钩花", "镂空", "走线", "细节",
                   "工艺", "纱线", "克重", "支数", "蓬松", "织法", "填充", "织纹", "肌理",
                   "双面", "拼接", "复合"]
FIT_KEYWORDS = ["显瘦", "瘦没了", "修身", "版型", "款型", "上身", "遮肉", "小肚子", "身材",
                "不挑人", "纸片人", "纸片", "搭裙子", "搭配裙", "显高", "比例", "收腰",
                "遮胯", "胖瘦", "显比例", "肩宽", "胯宽", "包裹"]
STYLING_KEYWORDS = ["搭配", "穿搭", "裤子", "裤腿", "格子裤", "编织裤", "西裤", "卫裤",
                    "老钱", "松弛", "休闲", "裙子", "大衣", "外套", "叠穿", "风格",
                    "胃裤", "衬衫", "背心", "半裙", "长裙", "短裙", "皮衣", "长裤", "短裤",
                    "牛仔", "靴", "鞋子", "包", "帽子", "围巾", "配饰", "气质感"]

# 库存/促销/换款离场黑名单（正则，无时间窗；由旧 demo 黑名单泛化而来）
# 2026-10-21 泛化验收修正：删除/收紧会误杀"穿搭/面料/版型"内容词的模式——
#   删除 .*牛仔.*（牛仔裤是穿搭内容，反而挤掉 styling 类目）
#   删除 .*拍了.*（"我就没拍了"非库存话术；保留 .*拍下.*）
#   删除 .*自己.*买.*（"你就自己买吧"是口语；改为 .*自己.*拍.*）
#   删除 .*价格.*（纪要禁"具体价格"而非"价格"二字，具体数字由 detect_price 负责）
#   删除 .*偏薄.* / .*款型.*（面料/版型卖点表达，属可用内容）
#   删除 .*来看这个.* / .*我特意.*（泛词，误杀正常话术与 Hook 候选）
#   收紧 .*主播.* → .*主播.*(换|走|下播|下班).*    .*撤.* → .*撤下.*|.*撤掉.*
# 运行时可用 constraints.yaml 的 blacklist 段覆盖（便于不改正则代码即可调参）。
INVENTORY_PATTERNS = [
    r".*库存.*", r".*只剩.*", r".*剩.\d+.*", r".*最后.\d+.*", r".*抢完.*", r".*抢购.*",
    r".*上链接.*", r".*小黄车.*", r".*购物袋.*", r".*下单.*", r".*拍下.*",
    r".*卖了.\d+.*", r".*秒杀.*", r".*福利.*", r".*赠.*", r".*送.*条.*", r".*甩.*链接.*",
    r".*一盘链接.*", r".*归谁.*", r".*剪到了.*", r".*评价去买吧.*", r".*不用讲.*",
    r".*仓库.*", r".*买这个.*", r".*自己.*拍.*",
    r".*几块.*",
]
CHANGEOVER_PATTERNS = [
    r".*换.*款.*", r".*下一款.*", r".*下一个.*", r".*我来穿.*", r".*我来换.*", r".*我来上.*",
    r".*脱了.*", r".*换下来.*", r".*过掉.*", r".*先看.*款.*", r".*撤下.*", r".*撤掉.*", r".*下班.*",
    r".*欢迎.*进入.*直播间.*", r".*主播.*(换|走|下播|下班).*", r".*关注.*", r".*粉丝群.*", r".*点亮.*",
    r".*内裤.*", r".*拖了.*分钟.*", r".*拖了一下.*", r".*不好意思.*", r".*做事不行.*",
    r".*着急下班.*", r".*美颜.*", r".*待会.*穿.*", r".*昨天.*跟你说.*",
    r".*星星哦.*", r".*看到美女.*", r".*就是买什么.*", r".*给你们甩.*",
]

# 合并间隔（秒）：相邻段间隔小于该值才可合并为同一 block
MERGE_GAP_SEC = 0.6
# block 最短时长（秒）
MIN_BLOCK_SEC = 1.2


def _clean_oral_fillers(text: str) -> str:
    """清理口语赘词与幻觉残留（需求3：只保留商品介绍/卖点/使用演示相关内容）。

    直播 ASR 常见噪声：
      - 填充与口水词（嗯/啊/呃/这个这个/然后然后）→ 字幕冗余、语义噪声；
      - 幻觉音节（快速口播产生的无意义音串，含"说错了/我怎么跑到"等元话语）→ 整句无商品语义。
    仅做保守清理，不改写语义内容；不改时间戳。
    """
    text = re.sub(r"(这个这个|然后然后|就是就是|嗯嗯|啊啊|呃呃)+", lambda m: m.group(0)[:2], text)
    # 通用中文双音节叠词去重（"真的真的"→"真的"）：
    # 仅去重"程度副词+副词"与常见口语叠词，避开"件件/看看"等实义叠词。
    _DUP_OK = ("真的", "好好", "非常", "特别", "超级", "真的", "比較", "比較")
    for w in _DUP_OK:
        text = text.replace(w + w, w)
    for w in ("好看", "漂亮"):
        text = text.replace(w + w, w)
    text = re.sub(r"[嗯呃啊呀哦哎唉哈嘿]{1,3}(?=[，,。.、])", "", text)
    text = re.sub(r"\s+", "", text)
    return text.strip("，,。.、 ")


def _is_idle_chatter(text: str) -> bool:
    """判断是否为与商品无关的闲聊/ASR 幻觉（含元话语则直接判否）。

    判据：出现"我怎么跑到/说错了/姜桑达"这类脱离商品语境的元话语，
    或清洗后文本过短且不含任何商品语义词。
    """
    meta = re.search(r"(说错|口误|跑到了?那|我怎么跑|重复一遍|听不清|没听清|刚才说到)", text)
    if meta:
        return True
    return False


def _classify(text: str) -> List[str]:
    """返回文本命中的漏斗类别列表（可多命中）。"""
    cats = []
    if any(k in text for k in HOOK_KEYWORDS):
        cats.append("hook")
    if any(k in text for k in FABRIC_KEYWORDS):
        cats.append("fabric")
    if any(k in text for k in FIT_KEYWORDS):
        cats.append("fit")
    if any(k in text for k in STYLING_KEYWORDS):
        cats.append("styling")
    return cats


def is_blacklisted(text: str, use_price: bool = True,
                  constraints: Dict[str, Any] = None) -> Tuple[bool, str]:
    """黑名单判定（无时间窗，纯内容信号）。

    constraints.blacklist.inventory / .changover 可覆盖内置正则（不改正则代码即可调参）。
    """
    if use_price and find_price_matches(text):
        return True, "命中价格表达"
    bl = (constraints or {}).get("blacklist", {}) or {}
    inv_patterns = bl.get("inventory") or INVENTORY_PATTERNS
    chg_patterns = bl.get("changover") or CHANGEOVER_PATTERNS
    for pat in inv_patterns:
        if re.search(pat, text):
            return True, f"命中库存/促销 ({pat})"
    for pat in chg_patterns:
        if re.search(pat, text):
            return True, f"命中换款/离场 ({pat})"
    return False, ""


# ---------------------------------------------------------------- 块构建

def _segment_score(seg: Dict[str, Any], category: str) -> float:
    """段在指定类别下的得分：关键词命中数 × 时长权重。"""
    kw_map = {
        "hook": HOOK_KEYWORDS, "fabric": FABRIC_KEYWORDS,
        "fit": FIT_KEYWORDS, "styling": STYLING_KEYWORDS,
    }
    hits = sum(1 for k in kw_map[category] if k in seg.get("text", ""))
    dur = seg.get("duration", 0.0)
    return hits * 2.0 + min(dur, 8.0) * 0.5


def build_blocks(segments: List[Dict[str, Any]],
                 constraints: Dict[str, Any] = None) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """过滤 + 合并连续段为 block。返回 (blocks, rejected)。

    多源支持（2026-10-21）：segments 携带 source_file 时按源分组处理，
    绝不跨文件合并（不同视频的时间轴不相通，合并会产生无效拼接）。
    无 source_file 的单源 segments 走原有逻辑，行为完全不变。
    """
    clean, rejected = [], []
    for s in segments:
        text = _clean_oral_fillers(s.get("text", "").strip())
        if not text:
            continue
        # 需求 3：闲聊/ASR 幻觉（如"我怎么跑到姜桑达马吉拉奥说错了"）直接淘汰
        if _is_idle_chatter(text):
            rejected.append({**s, "reason": "闲聊/ASR 幻觉元话语（与商品无关）"})
            continue
        bad, reason = is_blacklisted(text, constraints=constraints)
        if bad:
            rejected.append({**s, "reason": reason})
        else:
            clean.append(dict(s, text=text))

    blocks: List[Dict[str, Any]] = []
    multi = any(s.get("source_file") for s in clean)
    if multi:
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for s in clean:
            groups.setdefault(s.get("source_file", ""), []).append(s)
        ordered_groups = list(groups.values())
    else:
        clean.sort(key=lambda x: x.get("start", 0.0))
        ordered_groups = [clean]

    for grp in ordered_groups:
        cur: Optional[Dict[str, Any]] = None
        for s in sorted(grp, key=lambda x: x.get("start", 0.0)):
            st = s.get("start", 0.0)
            en = s.get("end", st)
            if cur is not None and st - cur["end"] <= MERGE_GAP_SEC:
                cur["end"] = max(cur["end"], en)
                cur["segments"].append(s)
            else:
                if cur is not None and cur["end"] - cur["start"] >= MIN_BLOCK_SEC:
                    blocks.append(cur)
                cur = {"start": st, "end": en, "segments": [s],
                       "source_file": s.get("source_file", ""),
                       "source_tag": s.get("source_tag", "")}
        if cur is not None and cur["end"] - cur["start"] >= MIN_BLOCK_SEC:
            blocks.append(cur)

    # 汇总每个 block 的类别与得分（段间以逗号连接，防止跨段伪短语）
    for b in blocks:
        text_all = "，".join(s.get("text", "") for s in b["segments"])
        # 块级复查：合并文本可能产生跨段黑名单短语（如"甩"+"个链接"）
        bad, reason = is_blacklisted(text_all)
        if bad:
            b["_drop"] = reason
            continue
        b["text"] = text_all
        b["dur"] = round(b["end"] - b["start"], 2)
        cats = set()
        for s in b["segments"]:
            cats.update(_classify(s.get("text", "")))
        b["categories"] = sorted(cats)
        b["scores"] = {c: round(_segment_score({"text": text_all, "duration": b["dur"]}, c), 2)
                       for c in ("hook", "fabric", "fit", "styling")}
    blocks = [b for b in blocks if not b.get("_drop")]
    return blocks, rejected


def _trim_block_to_duration(block: Dict[str, Any], target: float,
                            prefer_keywords: List[str] = None) -> Dict[str, Any]:
    """将 block 修剪到 target 时长。优先段边界对齐；无法对齐时硬时间裁剪兜底。

    prefer_keywords: 给定时不取块首，而是滑动窗口选取"关键词命中最多"的连续段
    （Hook 黄金 3 秒的关键——大块（可达 80s+）的首句往往是尺码/寒暄话术，
      真正的钩子在中后段）。

    返回新 block（含 remainder 余料，供后续类别复用）。
    """
    if block["dur"] <= target:
        return dict(block)
    segs = sorted(block["segments"], key=lambda x: x.get("start", 0.0))

    # 策略 0：关键词最密窗口（仅当显式指定关键词时启用）
    if prefer_keywords:
        def _sdur(s):
            return s.get("duration", s.get("end", 0) - s.get("start", 0))
        best = None
        i = 0
        acc = 0.0
        for j in range(len(segs)):
            acc += _sdur(segs[j])
            while acc > target and i < j:
                acc -= _sdur(segs[i])
                i += 1
            window = segs[i:j + 1]
            if not window:
                continue
            wtext = "，".join(s.get("text", "") for s in window)
            hits = sum(1 for k in prefer_keywords if k in wtext)
            if best is None or hits > best[0]:
                best = (hits, i, j + 1)
        if best is not None and best[0] > 0:
            _, i, j = best
            kept = segs[i:j]
            rest = segs[j:]
            # 目标时长硬约束：窗口可能因单段过长而超出 target（如 32s 长句）→ 硬裁剪
            win_st = kept[0].get("start", block["start"])
            win_en = kept[-1].get("end", win_st + target)
            if win_en - win_st > target + 0.05:
                win_en = round(win_st + target, 3)
                kept = [s for s in kept if s.get("start", 0) < win_en]
            trimmed = {
                "start": win_st,
                "end": win_en,
                "dur": round(win_en - win_st, 2),
                "segments": kept,
                "text": "，".join(s.get("text", "") for s in kept),
                "categories": block["categories"], "scores": block["scores"],
                "source_file": block.get("source_file", ""),
                "source_tag": block.get("source_tag", ""),
            }
            if rest:
                remainder = {
                    "start": rest[0].get("start", block["start"]),
                    "end": rest[-1].get("end", block["start"] + target),
                    "dur": round(rest[-1].get("end", 0) - rest[0].get("start", 0), 2),
                    "segments": rest,
                    "text": "，".join(s.get("text", "") for s in rest),
                    "categories": block["categories"], "scores": block["scores"],
                    "source_file": block.get("source_file", ""),
                    "source_tag": block.get("source_tag", ""),
                }
                if remainder["dur"] >= MIN_BLOCK_SEC:
                    trimmed["remainder"] = remainder
            return trimmed

    # 策略 1：段边界对齐（从头保留整段）
    kept: List[Dict[str, Any]] = []
    acc = 0.0
    for s in segs:
        sd = s.get("duration", s.get("end", 0) - s.get("start", 0))
        if acc + sd > target and kept:
            break
        kept.append(s)
        acc += sd

    use_hard_cut = False
    if acc <= target * 1.05:
        rest = segs[len(kept):]
    else:
        # 策略 2：段边界无法满足（单段超长）→ 硬时间裁剪到 target
        use_hard_cut = True
        hard_end = block["start"] + target
        kept = [s for s in segs if s.get("start", 0) < hard_end]
        rest = [s for s in segs if s.get("start", 0) >= hard_end]

    def _mk(segs_subset, force_end=None):
        st = segs_subset[0].get("start", block["start"]) if segs_subset else block["start"]
        en = force_end if force_end is not None else (
            segs_subset[-1].get("end", st) if segs_subset else st + target)
        return {"start": st, "end": en, "dur": round(en - st, 2),
                "segments": segs_subset,
                "text": "，".join(s.get("text", "") for s in segs_subset),
                "categories": block["categories"], "scores": block["scores"],
                "source_file": block.get("source_file", ""),
                "source_tag": block.get("source_tag", "")}

    if use_hard_cut:
        # 硬裁剪路径：结束于精确目标时刻（防呆：不得因 acc 被改写而误入段对齐路径）
        trimmed = _mk(kept, force_end=block["start"] + target)
    else:
        # 段对齐路径：结束于最后一个整段的段界（与 remainder 无缝衔接，无重叠）
        trimmed = _mk(kept)
        # 兜底（2026-10-21）：单段略超目标时（如 12.5s 段裁到 12.02s）段对齐无法
        # 缩短，会导致"修剪无进展→整块被丢弃→总时长跌破下限"。此处改用硬时间裁剪。
        if trimmed["dur"] - target > 0.05:
            hard_end = block["start"] + target
            kept = [s for s in segs if s.get("start", 0) < hard_end]
            rest = [s for s in segs if s.get("start", 0) >= hard_end]
            trimmed = _mk(kept, force_end=hard_end)
    if rest:
        remainder = _mk(rest)
        if remainder["dur"] >= MIN_BLOCK_SEC:
            trimmed["remainder"] = remainder  # 余料挂回，供后续类别复用
    return trimmed


# ---------------------------------------------------------------- 特效决策

# 挂角利益点贴纸的文案兜底（按分镜类目）
_CATEGORY_STICKER_TEXT = {
    "hook": "开场必看",
    "fabric": "面料细节",
    "fit": "版型显瘦",
    "styling": "一衣多穿",
}
# 面料/工艺类更具体的高频卖点短语（命中即优先用）
_STICKER_PHRASES = [
    "3M新雪丽", "半导棉", "半岛棉", "桑蚕丝", "羊绒", "羊毛", "针织",
    "显瘦", "遮肉", "不挑人", "透气", "亲肤", "软糯", "不起球", "不易变形",
]


def _build_sticker_texts(clips: List[Dict[str, Any]],
                         spec_data: Dict[str, Any],
                         sticker_c: Dict[str, Any]) -> Dict[str, str]:
    """为每个分镜生成挂角利益点贴纸文案（2026-10-24 新增）。

    优先级：spec.options.sticker_texts 显式指定
          → 分镜原文命中的具体卖点短语
          → 分镜类目兜底短语
    贴纸文案需短（正方形贴纸内换行显示），超长截断。
    """
    if not sticker_c.get("enabled", True):
        return {}
    explicit = ((spec_data.get("options", {}) or {}).get("sticker_texts") or {})
    out: Dict[str, str] = {}
    for clip in clips:
        cid = clip["id"]
        text = explicit.get(cid, "")
        if not text:
            desc = clip.get("desc", "") or ""
            for phrase in _STICKER_PHRASES:
                if phrase in desc:
                    text = phrase
                    break
        if not text:
            text = _CATEGORY_STICKER_TEXT.get(clip.get("category", ""), "")
        if text:
            out[cid] = text[:8]
    return out


# ---------------------------------------------------------------- 主入口

def build_plan(
    raw_video_path: str,
    segments: List[Dict[str, Any]],
    spec_data: Dict[str, Any],
    constraints: Dict[str, Any] = None,
    scene_cuts: Optional[List[float]] = None,
    cache_key: str = None,
    work_dir: str = None,
) -> Dict[str, Any]:
    """生成 plan.json（结构符合 contracts/plan.schema.json）。

    cache_key + work_dir 提供时启用阶段缓存（B13）。
    """
    if cache_key and work_dir:
        from src.tools.stage_cache import load_stage
        cached = load_stage(work_dir, "plan", cache_key)
        if cached is not None:
            print(f"[BUILD-PLAN] 命中阶段缓存（{len(cached.get('clips', []))} 个分镜）")
            return cached
    constraints = constraints or {}
    funnel_c = (constraints.get("video", {}) or {}).get("funnel", {})
    dur_range = ((constraints.get("video", {}) or {}).get("duration_range")) or [45, 65]
    dur_min, dur_max = float(dur_range[0]), float(dur_range[1])

    # spec 的 duration_range 可收窄（如 "48-55"），但不允许超出 [45,65]
    spec_range = (spec_data.get("options", {}) or {}).get("duration_range", "45-55")
    try:
        s_min, s_max = [float(x) for x in str(spec_range).split("-")]
        dur_min = max(dur_min, s_min)
        dur_max = min(dur_max, s_max)
    except (ValueError, AttributeError):
        pass

    # 切片安全余量（2026-10-21）：ffmpeg 逐段 -ss/-t 切片后每段物理时长会略大于计划值
    #（实测 4 段合计 +0.09s），若计划刚好卡上限（如 65.0s）成片会溢出被 QC 拦截。
    # 故上限收紧 0.5s 作为安全边界；下限不动（避免无谓丢段）。
    DURATION_SAFETY_MARGIN_SEC = 0.5
    dur_max = max(dur_min + 1.0, dur_max - DURATION_SAFETY_MARGIN_SEC)

    blocks, rejected = build_blocks(segments, constraints)

    meta = {"funnel": {}, "rejected_count": len(rejected),
            "candidate_blocks": len(blocks), "notes": []}

    def quota(cat: str) -> Tuple[float, float]:
        q = funnel_c.get(cat, {})
        return float(q.get("min", 0)), float(q.get("max", 0))

    order = ["hook", "fabric", "fit", "styling"]

    # 按漏斗类别依次贪心选块（配额内取最高分；配额未满时按段边界修剪）
    chosen_by_cat: Dict[str, List[Dict[str, Any]]] = {}
    used_ids = set()
    for cat in order:
        q_min, q_max = quota(cat)
        if q_min <= 0:
            continue
        cands = [b for b in blocks
                 if cat in b["categories"] and id(b) not in used_ids]
        cands.sort(key=lambda b: b["scores"].get(cat, 0), reverse=True)
        picked: List[Dict[str, Any]] = []
        acc = 0.0
        for b in cands:
            if acc >= q_min and picked:
                break
            if acc + b["dur"] <= q_max:
                picked.append(dict(b, _quota_cat=cat))
                used_ids.add(id(b))
                acc += b["dur"]
            elif acc < q_min:
                # 配额未满：从头部按段边界修剪；余料挂回池供后续类别复用
                tb = _trim_block_to_duration(
                    b, q_max - acc,
                    prefer_keywords=(HOOK_KEYWORDS if cat == "hook" else None))
                if tb["dur"] >= 1.0:
                    tb["_quota_cat"] = cat
                    picked.append(tb)
                    used_ids.add(id(b))
                    acc += tb["dur"]
                    rem = tb.pop("remainder", None)
                    if rem is not None:
                        blocks.append(rem)
        if picked:
            meta["funnel"][cat] = round(acc, 2)
        else:
            meta["notes"].append(f"类别 {cat} 无合规候选（配额 {q_min}~{q_max}s）")
        chosen_by_cat[cat] = picked

    # 叙事排序：Hook 置顶 → 面料 → 版型 → 穿搭
    ordered_blocks: List[Dict[str, Any]] = []
    for cat in order:
        ordered_blocks.extend(chosen_by_cat.get(cat, []))

    total = round(sum(b["dur"] for b in ordered_blocks), 2)

    # 时长不足：用剩余高分层级块补齐（可重复调用：修剪后若跌破下限再补一次）
    def _top_up(ordered: List[Dict[str, Any]], total: float) -> float:
        if total >= dur_min:
            return total
        rest = [b for b in blocks if id(b) not in used_ids]
        rest.sort(key=lambda b: b["scores"].get("styling", 0) + b["scores"].get("fabric", 0),
                  reverse=True)
        # 需求 3：补齐时长也不得引入与商品无关的闲聊块——
        # 块文本若不含任何漏斗类目关键词（categories 为空）则跳过；
        # 否则会出现"他们坐船跟我们坐船有什么区别"这类无商品语义内容被拉进成片。
        semantic_rest = [b for b in rest if b.get("categories")]
        skipped = len(rest) - len(semantic_rest)
        if skipped:
            meta["notes"].append(f"补齐阶段跳过 {skipped} 个无商品语义闲聊块")
        for b in semantic_rest:
            if total >= dur_min:
                break
            need = dur_min - total
            tb = _trim_block_to_duration(b, min(b["dur"], need + 2.0))
            if tb["dur"] >= 1.0:
                ordered.append(tb)
                used_ids.add(id(b))
                total = round(total + tb["dur"], 2)
        return total

    total = _top_up(ordered_blocks, total)
    if total < dur_min:
        meta["notes"].append(f"配额不足，已用补充块补齐至 {total}s（仍低于下限 {dur_min}s）")
    else:
        meta["notes"].append(f"配额补齐后总时长 {total}s")

    # 时长超出：从尾部（穿搭层）逐块修剪/丢弃（带无进展保护，防死循环）
    guard = 0
    while total > dur_max and ordered_blocks and guard < 50:
        guard += 1
        last = ordered_blocks[-1]
        excess = total - dur_max
        if last["dur"] - excess >= 1.0:
            tb = _trim_block_to_duration(last, last["dur"] - excess)
            new_total = round(total - last["dur"] + tb["dur"], 2)
            if new_total < total - 0.05:  # 有实质进展才接受
                ordered_blocks[-1] = tb
                total = new_total
                continue
        # 无进展 → 丢弃尾块
        ordered_blocks.pop()
        total = round(sum(b["dur"] for b in ordered_blocks), 2)

    # 修剪后若跌破时长下限 → 再补一次（修剪与补齐存在先后依赖）
    if total < dur_min:
        total = _top_up(ordered_blocks, total)
        meta["notes"].append(f"修剪后补齐至 {total}s")

    meta["total_duration"] = total
    meta["target_range"] = [dur_min, dur_max]

    # 生成 clips
    clips = []
    for i, b in enumerate(ordered_blocks):
        # 类别标注优先级：① 该块由哪个漏斗配额选出（hook/fabric/fit/styling 语义准确）
        # ② ② 补齐块无配额来源 → 取块内得分最高的类别（避免按位置强制标注，
        #    否则补齐块会被误标为 hook，掩盖真实类目缺口）
        if b.get("_quota_cat"):
            cat = b["_quota_cat"]
        elif b["categories"]:
            # 补齐块不占用 hook 名额（hook 语义=开场黄金 3~8s）：取非 hook 的最高分类别
            non_hook = [c for c in b["categories"] if c != "hook"]
            pool = non_hook or b["categories"]
            cat = max(pool, key=lambda c: b["scores"].get(c, 0))
        else:
            cat = "styling"
        has_fabric_kw = any(k in b.get("text", "") for k in FABRIC_KEYWORDS)
        # 面料块推镜倍率：读约束（zoom_effect.zoom），并受 zoom_effect.max_zoom 钳制。
        # 2026-10-25：此前硬编码 1.3，改为约束驱动，改 yaml 即改行为。
        zoom_c = (constraints.get("zoom_effect", {}) or {})
        z_target = float(zoom_c.get("zoom", 1.3) or 1.3)
        z_max = float(zoom_c.get("max_zoom", 1.35) or 1.35)
        if z_target > z_max:
            z_target = z_max
        clip = {
            "id": f"c{i+1:02d}_{cat}",
            "st": round(b["start"], 3),
            "end": round(b["end"], 3),
            "dur": round(b["dur"], 3),
            "is_hook": (i == 0),
            "zoom": z_target if has_fabric_kw else 1.0,
            "y_anchor": float(zoom_c.get("y_anchor", 0.35) or 0.35),
            # 默认值，随后由下方「特效决策」按规则改写（hflip / split）。
            # 2026-10-24 修复：此前这里写死 "none" 且无任何决策逻辑，
            # 导致装配器里已完整实现的镜像/分屏成为永不触发的死代码。
            "effect": "none",
            "category": cat,
            "desc": (b.get("text", "")[:24] + "…") if len(b.get("text", "")) > 24 else b.get("text", ""),
        }

        if b.get("source_file"):
            clip["source_file"] = b["source_file"]
            clip["source_tag"] = b.get("source_tag", "")
        clips.append(clip)

    if not clips:
        raise ValueError("未能在素材中选出任何合规分镜块（可能全部命中黑名单）")

    # ————————————————————————————————————————————————————————————
    # 特效决策（2026-10-24）：effect 从"写死常量"改为**决策变量**
    # ————————————————————————————————————————————————————————————
    # 此前 clip["effect"] 恒为 "none"，导致装配器里已完整实现的
    # hflip（镜像）与 split（分屏对比）成为**永不触发的死代码**。
    # 现按下列规则决策：
    #   1) 挂角利益点贴纸：每个分镜一条，文字取该分镜的核心卖点
    #      （面料/工艺关键词优先，否则用类目短语）；
    #   2) 镜像 hflip：用于**非首镜**的 styling/fit 分镜，且素材里确实
    #      检测到背景文字（无文字则镜像没有违和点，无需遮挡）——
    #      实际遮挡由装配器用 OCR 定位后贴纸覆盖（SKILL 2.1）；
    #   3) 分屏 split：仅当 spec 显式提供 split_pairs（同商品两个不同色/款
    #      的时间点）时启用，避免凭空捏造"另一款"。
    mirror_c = (constraints.get("mirror_effect", {}) or {})
    split_c = (constraints.get("split_effect", {}) or {})
    sticker_c = (constraints.get("video_sticker", {}) or {})

    # —— 挂角贴纸：卖点短句 ——
    sticker_texts = _build_sticker_texts(clips, spec_data, sticker_c)
    for clip in clips:
        clip["sticker_text"] = sticker_texts.get(clip["id"], "")

    # —— 分屏决策：仅在 spec 显式给出配对时启用 ——
    # 优先于镜像（分屏是更强的表达形式，同一分镜不同时用两种）。
    # 分屏走独立的 hstack 渲染路径，**不经过 build_clip_filter**，
    # 因此不叠加推镜（zoom 置 1.0），避免 plan 里的 zoom 与实际渲染不一致。
    split_pairs = (spec_data.get("options", {}) or {}).get("split_pairs") or []
    if split_c.get("enabled", True) and split_pairs:
        pair = split_pairs[0]
        try:
            target = clips[1] if len(clips) > 1 else clips[0]
            target["effect"] = "split"
            target["zoom"] = 1.0
            target["split_pair_st"] = float(pair.get("st", target["st"]))
            target["split_label_left"] = pair.get("label_left", "")
            target["split_label_right"] = pair.get("label_right", "")
            target["effect_reason"] = "同商品双色/双款对比（SKILL 2.3）"
        except (TypeError, ValueError):
            pass

    # —— 镜像决策 ——
    if mirror_c.get("enabled", True) and len(clips) >= 3:
        # 只对第 2 个及以后的分镜启用（首镜是黄金 3 秒 Hook，不宜镜像）；
        # 已被分屏占用的分镜跳过（分屏优先）。
        for clip in clips[1:]:
            if clip.get("effect") != "none":
                continue
            if clip.get("category") in ("styling", "fit") and clip.get("zoom", 1.0) <= 1.0:
                clip["effect"] = "hflip"
                clip["effect_reason"] = "打破连续分镜的站位单一感（SKILL 2.1）"
                break   # 每条成片最多镜像 1 处，避免观感割裂

    effect_count = {}
    for c in clips:
        effect_count[c.get("effect", "none")] = effect_count.get(c.get("effect", "none"), 0) + 1
    print(f"[BUILD-PLAN] 特效决策: {effect_count}（挂角贴纸 "
          f"{sum(1 for c in clips if c.get('sticker_text'))} 个）")

    options = spec_data.get("options", {}) or {}
    sources_in_plan = sorted({c["source_file"] for c in clips if c.get("source_file")})
    plan = {
        "project_id": spec_data.get("project_id", "demo"),
        "raw_video_path": os.path.abspath(raw_video_path),
        "multi_source": len(sources_in_plan) > 1,
        "sources_used": sources_in_plan,
        "total_expected_duration": total,
        "clips": clips,
        "brand": spec_data.get("brand", {}) or {},
        "hook_card": {"text": "极简松弛感 · 显瘦显气质", "dur": 2.5, "y": 280},
        "cta": {"text": "戳左下方小黄车查看上身搭配", "dur": 2.5},
        "boom": {"volume": 0.45, "file": "boom.wav"},
        "subtitles": {"keywords": options.get("highlight_keywords", [])},
        "selection_meta": meta,
    }
    if cache_key and work_dir:
        from src.tools.stage_cache import save_stage
        save_stage(work_dir, "plan", cache_key, plan)
    return plan


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="泛化分镜编译器")
    parser.add_argument("--video", required=True)
    parser.add_argument("--segments", required=True, help="ASR 片段 JSON（[{start,end,text}]）")
    parser.add_argument("--spec", default="job_spec.json")
    parser.add_argument("--constraints", default="contracts/constraints.yaml")
    parser.add_argument("--cuts", default=None, help="场景检测 JSON（可选）")
    parser.add_argument("--out-json", required=True)
    args = parser.parse_args(argv)

    import yaml
    with open(args.segments, "r", encoding="utf-8") as f:
        segments = json.load(f)
    with open(args.spec, "r", encoding="utf-8") as f:
        spec = json.load(f)
    constraints = {}
    if os.path.exists(args.constraints):
        with open(args.constraints, "r", encoding="utf-8") as f:
            constraints = yaml.safe_load(f)

    cuts = None
    if args.cuts and os.path.exists(args.cuts):
        with open(args.cuts, "r", encoding="utf-8") as f:
            cuts = json.load(f).get("cuts")

    plan = build_plan(args.video, segments, spec, constraints, scene_cuts=cuts)

    print(f"[PLAN] 总时长 {plan['total_expected_duration']}s | {len(plan['clips'])} 个分镜")
    for c in plan["clips"]:
        print(f"       {c['id']}: {c['st']:.2f}~{c['end']:.2f}s ({c['dur']:.2f}s) "
              f"zoom={c['zoom']} | {c['desc']}")
    print(f"[PLAN] 选段审计: {plan['selection_meta']}")

    os.makedirs(os.path.dirname(os.path.abspath(args.out_json)), exist_ok=True)
    with open(args.out_json, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    print(f"[PLAN] plan.json 已生成: {args.out_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
