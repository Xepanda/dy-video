# -*- coding: utf-8 -*-
"""
fetch_product_link.py - 抖音/抖店商品链接元数据与参考图抓取工具 (V3.4)
====================================================================
职责：
  1. 从用户粘贴的抖音商品链接或分享口令中提取合法的商品详情 URL（如 haohuo.jinritemai.com）；
  2. 通过本地无头浏览器（Playwright，自动回退 Chromium / Edge / Chrome）访问公开商品 H5 页面，
     拦截 `/shop/promotion/pack/detail` 响应并结合渲染后 DOM 提取结构化商品事实数据：
     - 商品标题、品牌英文/中文（店铺名）、SKU 货号、品类推断；
     - 核实属性字典（面料材质、里料材质、领型、版型、洗涤方式等，自动剥离价格与促销词）；
     - 主图与详情参考图 URL，并可下载至项目独立目录 `<project_root>/product_ref/`（绝不修改 raw/素材 目录）；
  3. 安全红线：仅读取公开页面数据，绝不绕过登录墙、验证码或人机校验；若页面被风控拦截，明确抛出
     `ProductFetchBlockedError` 并报告给调用方。
"""

import os
import re
import io
import json
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple


class ProductFetchError(RuntimeError):
    """商品链接解析或抓取失败。"""


class ProductFetchBlockedError(ProductFetchError):
    """商品页面被登录墙、验证码或反爬风控拦截（严禁绕过，直接上报）。"""


_URL_RE = re.compile(r"https?://[^\s<>\"'，。！？；（）()【】\[\]]+", re.IGNORECASE)

_BLOCKED_HOST_HINTS = (
    "passport.",
    "sso.",
    "verify.",
    "captcha.",
)

_BLOCKED_TEXT_HINTS = (
    "请完成安全验证",
    "滑动拼图",
    "拖动滑块",
    "点击图片中的",
    "行为验证",
    "人机验证",
    "请先登录",
    "扫码登录",
    "登录后查看",
    "访问频率过高",
)

_PRICE_OR_PROMO_KEYS = (
    "价格", "券后", "到手", "促销", "优惠", "补贴", "运费", "发货", "保障", "服务", "活动",
)

_SKU_CODE_RE = re.compile(r"\b([A-Za-z]{1,4}\d{3,6}[A-Za-z0-9\-]*)\b")
_BRACKET_SHOP_RE = re.compile(r"[【\[]([^】\]]{1,24})[】\]]")
_LEAD_EN_BRAND_RE = re.compile(r"^([A-Za-z][A-Za-z0-9&.\-]{1,24})(?=\s|[【\[（(]|[\u4e00-\u9fff])")

_KNOWN_COLORS = (
    "米白色", "象牙白", "奶白色", "本白色", "纯白色", "白色",
    "黑色", "炭黑色", "燕麦色", "驼色", "浅驼色", "深驼色",
    "花灰色", "浅灰色", "深灰色", "灰色", "藏青色", "藏蓝色",
    "酒红色", "安可拉红", "焦糖色", "卡其色", "杏色", "咖色",
    "棕色", "巧克力色", "雾霾蓝", "牛仔蓝", "军绿色", "墨绿色",
    "粉色", "香槟色", "紫色", "黄色", "红色",
)

_ATTR_KNOWN_LABELS = (
    "面料材质", "材质成分", "成分含量", "里料材质", "填充物", "含绒量",
    "领型", "袖型", "袖长", "衣长", "服装版型", "版型", "厚薄", "衣门襟",
    "风格", "适用季节", "年份季节", "洗涤方式", "洗涤", "图案", "流行元素",
    "货号", "款号", "品牌", "颜色", "颜色分类", "尺码",
)


def extract_product_url(raw_text: str) -> str:
    """从链接或分享文本中提取商品 URL。"""
    if not raw_text or not str(raw_text).strip():
        raise ProductFetchError("商品链接不能为空")
    s = str(raw_text).strip()
    m = _URL_RE.search(s)
    if not m:
        raise ProductFetchError(f"未在输入内容中识别到有效的 http/https 商品链接: {s[:120]}")
    url = m.group(0).rstrip(".,;!?")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ProductFetchError(f"无效的商品链接 URL: {url}")
    return url


def extract_product_id(url: str) -> str:
    """从商品 URL 中提取 promotion_id / product_id。"""
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    for key in ("id", "product_id", "promotion_id", "item_id"):
        vals = qs.get(key)
        if vals and str(vals[0]).strip():
            return str(vals[0]).strip()
    m = re.search(r"/(\d{10,24})(?:[/?#]|$)", parsed.path)
    if m:
        return m.group(1)
    return ""


def check_blocked_page(url: str, title: str = "", body_text: str = "") -> Optional[str]:
    """检测目标页面是否命中登录墙或人机验证码。返回阻断原因，未拦截返回 None。"""
    lower_url = (url or "").lower()
    for h in _BLOCKED_HOST_HINTS:
        if h in lower_url:
            return f"页面被重定向至认证/验证域 ({h})"
    combined = f"{title or ''}\n{(body_text or '')[:1500]}"
    # 若页面本身已经正常包含商品核心信息，仅底部弹窗出现登录按钮不应误报
    has_product_signals = any(
        tok in combined for tok in ("商品详情", "面料材质", "产品参数", "进店", "店铺", "参数")
    )
    for hint in _BLOCKED_TEXT_HINTS:
        if hint in combined:
            if hint in ("请先登录", "扫码登录", "登录后查看") and has_product_signals:
                continue
            return f"页面触发访问限制或验证码提示: '{hint}'"
    return None


def infer_category(title: str, attributes: Optional[Dict[str, str]] = None) -> str:
    """根据商品标题和属性推断标准电商类目。"""
    text = f"{title or ''} " + " ".join(f"{k}{v}" for k, v in (attributes or {}).items())
    low = text.lower()
    if any(k in low for k in ("羽绒", "大衣", "外套", "风衣", "棉服", "皮草", "夹克", "羊羔毛", "西装", "派克服", "冲锋衣")):
        return "女装/大衣外套"
    if any(k in low for k in ("毛衣", "针织", "羊绒", "开衫", "打底衫", "毛衫")):
        return "女装/毛针织"
    if any(k in low for k in ("连衣裙", "半身裙", "长裙", "短裙", "裙子")):
        return "女装/连衣裙"
    if any(k in low for k in ("牛仔裤", "西裤", "阔腿裤", "休闲裤", "打底裤", "裤子", "长裤")):
        return "女装/裤装裙装"
    if any(k in low for k in ("衬衫", "t恤", "卫衣", "雪纺衫", "衬衣")):
        return "女装/衬衫T恤"
    if any(k in low for k in ("包", "饰品", "项链", "耳环", "皮带", "鞋", "靴", "围巾", "帽子")):
        return "箱包配饰"
    if any(k in low for k in ("口红", "面霜", "护肤", "精华", "面膜", "粉底", "美妆", "洁面")):
        return "美妆护肤"
    return "通用电商"


def _clean_title_and_extract_meta(raw_title: str) -> Tuple[str, str, str, str]:
    """清洗商品原标题，返回 (clean_name, brand_en, shop_or_brand_cn, sku_code)。"""
    s = re.sub(r"\s+", " ", str(raw_title or "")).strip()
    if not s:
        return "", "", "", ""

    brand_en = ""
    brand_cn = ""
    sku_code = ""

    # 1. 提取英文前缀品牌，如 CONCISETXM【唐小木】...
    m_en = _LEAD_EN_BRAND_RE.match(s)
    if m_en:
        cand = m_en.group(1).strip()
        if len(cand) >= 2 and not re.fullmatch(r"\d+%?", cand):
            brand_en = cand
            s = s[m_en.end():].strip()

    # 2. 提取【店铺/品牌名】
    m_shop = _BRACKET_SHOP_RE.search(s)
    if m_shop:
        cand_shop = m_shop.group(1).strip()
        if not any(noise in cand_shop for noise in ("新品", "新款", "热卖", "包邮", "现货", "立减", "券", "折")):
            brand_cn = cand_shop
        s = (s[:m_shop.start()] + " " + s[m_shop.end():]).strip()

    # 3. 提取货号/SKU 编码，如 W1120、A2024
    sku_matches = _SKU_CODE_RE.findall(s)
    if sku_matches:
        sku_code = sku_matches[-1].upper()
        s = re.sub(rf"\s*{re.escape(sku_matches[-1])}\s*$", "", s, flags=re.IGNORECASE).strip()

    clean_name = re.sub(r"\s+", " ", s).strip(" -_|/")
    return clean_name, brand_en, brand_cn, sku_code


def _split_cover_titles(clean_name: str, attributes: Dict[str, str], category: str) -> Tuple[str, str]:
    """将完整商品名拆分为适合封面大字排版的 2 行精炼标题（每行约 4~11 字）。"""
    base = re.sub(r"\s+", "", clean_name or "")
    if not base:
        return "品质精选好物", "质感实拍"

    # 优先用「核心材质/款式 + 品类」构造高转化封面双行标题
    if len(base) <= 10:
        line2 = category.split("/")[-1] if "/" in category else "品质精选"
        if line2 in base:
            line2 = "实拍精选"
        return base, line2

    # 尝试按语义关键词切分（如 "100%羊羔毛外套新款时尚百搭绵羊毛大衣"）
    # 剔除冗长营销修饰词保留材质与核心品名
    trimmed = base
    for filler in ("新款", "时尚", "百搭", "爆款", "推荐", "秋冬季", "秋冬", "春秋", "春季", "冬季", "女装", "气质", "高级感"):
        if len(trimmed) > 14:
            trimmed = trimmed.replace(filler, "")
    trimmed = trimmed.strip() or base

    if len(trimmed) <= 10:
        attr_bits = []
        for k in ("领型", "服装版型", "版型", "风格"):
            v = str(attributes.get(k) or "").strip()
            if v and v not in trimmed and len(v) <= 5:
                attr_bits.append(v)
        l2 = "".join(attr_bits[:2]) or (category.split("/")[-1] if "/" in category else "实拍精选")
        return trimmed, l2[:10]

    # 对半找自然断点（外套/大衣/毛衣/开衫/连衣裙/裤/衫 后切分）
    mid = min(len(trimmed) // 2 + 2, 10)
    cut_pos = None
    for kw in ("外套", "大衣", "毛衣", "开衫", "连衣裙", "半身裙", "卫衣", "衬衫", "打底衫", "西裤", "长裤"):
        idx = trimmed.find(kw)
        if 2 <= idx <= 9:
            cut_pos = idx + len(kw)
            break
    if cut_pos and cut_pos < len(trimmed):
        l1 = trimmed[:cut_pos][:11]
        l2 = trimmed[cut_pos:][:11]
    else:
        l1 = trimmed[:mid]
        l2 = trimmed[mid:mid + 10]
    return l1 or base[:10], l2 or "实拍精选"


def _walk_json_for_fields(obj: Any, acc: Dict[str, Any], _path: str = "") -> None:
    """递归扫描抖店 pack/h5 与 pack/detail JSON，兼容多版本字段嵌套。"""
    if isinstance(obj, dict):
        # 优先精确提取 promotion_h5.head_figure_data.media_list[*].content_list[*].url
        if "head_figure_data" in obj and isinstance(obj["head_figure_data"], dict):
            for ml in obj["head_figure_data"].get("media_list") or []:
                if isinstance(ml, dict) and (ml.get("type") in (None, "image", "pic") or ml.get("name") == "商品"):
                    for item in ml.get("content_list") or []:
                        if isinstance(item, dict) and isinstance(item.get("url"), str) and item["url"].startswith("http"):
                            acc["main_imgs"].append(item["url"])

        # 精确提取 promotion_h5.shop_info.basic_info.shop_name
        if "shop_info" in obj and isinstance(obj["shop_info"], dict):
            si = obj["shop_info"]
            bi = si.get("basic_info") if isinstance(si.get("basic_info"), dict) else si
            sn = bi.get("shop_name") or bi.get("name")
            if isinstance(sn, str) and sn.strip() and not acc.get("shop_name"):
                acc["shop_name"] = sn.strip()

        # 精确提取 promotion_h5.basic_info_data.title_info.title
        if "basic_info_data" in obj and isinstance(obj["basic_info_data"], dict):
            ti = obj["basic_info_data"].get("title_info")
            if isinstance(ti, dict) and isinstance(ti.get("title"), str) and ti["title"].strip():
                acc["title"] = ti["title"].strip()

        # 标题（通用兜底）
        if not acc.get("title"):
            for k in ("title", "product_name", "short_title"):
                val = obj.get(k)
                if isinstance(val, str) and len(val.strip()) >= 8 and _path not in ("product_support_info_data", "comment_data"):
                    acc["title"] = val.strip()
                    break

        # 店铺名（通用兜底）
        for k in ("shop_name", "store_name"):
            val = obj.get(k)
            if isinstance(val, str) and val.strip() and not acc.get("shop_name"):
                acc["shop_name"] = val.strip()

        # 品牌
        for k in ("brand_name", "brand"):
            val = obj.get(k)
            if isinstance(val, str) and val.strip() and not acc.get("brand"):
                acc["brand"] = val.strip()

        # 主图 / 轮播图（排除评论与页脚图标）
        if _path not in ("comment_data", "footer_data", "product_support_info_data", "shop_info"):
            for k in ("imgs", "images", "img_list", "main_imgs", "pic_list"):
                val = obj.get(k)
                if isinstance(val, list):
                    for item in val:
                        if isinstance(item, str) and item.startswith("http"):
                            acc["main_imgs"].append(item)
                        elif isinstance(item, dict):
                            u = item.get("url") or (
                                item.get("url_list")[0]
                                if isinstance(item.get("url_list"), list) and item.get("url_list")
                                else None
                            )
                            if isinstance(u, str) and u.startswith("http"):
                                acc["main_imgs"].append(u)

        # 详情图
        for k in ("desc_imgs", "detail_imgs", "detail_images", "pic_desc", "detail_pic"):
            val = obj.get(k)
            if isinstance(val, list):
                for item in val:
                    if isinstance(item, str) and item.startswith("http"):
                        acc["detail_imgs"].append(item)
                    elif isinstance(item, dict):
                        u = item.get("url") or item.get("pic") or (
                            item.get("url_list")[0]
                            if isinstance(item.get("url_list"), list) and item.get("url_list")
                            else None
                        )
                        if isinstance(u, str) and u.startswith("http"):
                            acc["detail_imgs"].append(u)

        # 结构化参数 / product_format / format_list / props / params / property_list
        for k in ("product_format", "format_list", "product_params", "params", "props", "specs", "attributes", "property_list", "format"):
            val = obj.get(k)
            if isinstance(val, list):
                for row in val:
                    if isinstance(row, dict):
                        rk = row.get("name") or row.get("key") or row.get("title") or row.get("label")
                        rv = row.get("message") or row.get("value") or row.get("val") or row.get("desc") or row.get("content")
                        if isinstance(rv, list):
                            rv = " / ".join(
                                str(x.get("name") or x.get("value") or x.get("text") or x) if isinstance(x, dict) else str(x)
                                for x in rv if x
                            )
                        if isinstance(rk, str) and isinstance(rv, str) and rk.strip() and rv.strip():
                            acc["attrs"][rk.strip()] = rv.strip()
            elif isinstance(val, dict):
                for rk, rv in val.items():
                    if isinstance(rk, str) and isinstance(rv, (str, int, float)) and str(rv).strip():
                        acc["attrs"][rk.strip()] = str(rv).strip()

        # 颜色/规格选项（若有）
        for k in ("spec_list", "specs_info", "sku_specs", "sale_props"):
            val = obj.get(k)
            if isinstance(val, list):
                for sp in val:
                    if isinstance(sp, dict):
                        sname = str(sp.get("name") or "")
                        if any(c in sname for c in ("颜色", "花色", "款式")):
                            sub = sp.get("specs") or sp.get("values") or sp.get("spec_items") or []
                            for it in sub:
                                vname = it.get("name") if isinstance(it, dict) else str(it)
                                if vname and len(str(vname).strip()) <= 16:
                                    acc["variants"].append(str(vname).strip())

        for k, v in obj.items():
            if k in ("comment_data", "footer_data", "product_support_info_data", "shop_info"):
                continue
            if isinstance(v, (dict, list)):
                _walk_json_for_fields(v, acc, _path=k)
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                _walk_json_for_fields(item, acc, _path=_path)


def _extract_attributes_from_dom_text(body_text: str) -> Dict[str, str]:
    """从渲染后的页面文本中按行抽取常见商品参数键值对。"""
    attrs: Dict[str, str] = {}
    if not body_text:
        return attrs
    lines = [ln.strip() for ln in str(body_text).splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        # 形式 1: "面料材质：绵羊毛100%" 或 "面料材质 绵羊毛100%"
        m = re.match(r"^([^：:\s]{2,10})[：:]\s*(.{1,40})$", line)
        if m:
            k, v = m.group(1).strip(), m.group(2).strip()
            if k in _ATTR_KNOWN_LABELS and not any(p in k for p in _PRICE_OR_PROMO_KEYS):
                attrs.setdefault(k, v)
                continue
        # 形式 2: 标签独占一行，值在下一行
        if line in _ATTR_KNOWN_LABELS and i + 1 < len(lines):
            nxt = lines[i + 1]
            if nxt not in _ATTR_KNOWN_LABELS and len(nxt) <= 45 and not nxt.startswith("￥"):
                attrs.setdefault(line, nxt)
    return attrs


def _filter_image_urls(urls: List[str]) -> List[str]:
    """清洗并去重商品图片 URL，排除头像、图标和水印角标。"""
    seen = set()
    out: List[str] = []
    for raw in urls or []:
        u = str(raw or "").strip()
        if not u.startswith(("http://", "https://")):
            continue
        low = u.lower()
        if any(skip in low for skip in (
            "avatar", "icon", "logo", "badge", "tag_", "watermark", "emoji",
            "tos-cn-i-6w9my0ksvp", "static", "favicon", ".svg", ".gif",
        )):
            continue
        # 以路径主体做去重（避免同一张图因不同缩略参数重复）
        parsed = urllib.parse.urlparse(u)
        norm_key = parsed.netloc + parsed.path.split("~")[0]
        if norm_key in seen:
            continue
        seen.add(norm_key)
        out.append(u)
    return out


def _derive_selling_points_and_keywords(
    clean_name: str,
    attributes: Dict[str, str],
) -> Tuple[List[str], List[str]]:
    """仅从真实标题与参数属性中提炼事实卖点与字幕高亮词（绝不杜撰材质或价格）。"""
    selling_points: List[str] = []
    keywords: List[str] = []

    def _add_sp(val: str) -> None:
        v = str(val or "").strip()
        if v and 2 <= len(v) <= 18 and v not in selling_points:
            if not any(p in v for p in ("元", "块", "折", "￥", "¥", "秒杀", "福利")):
                selling_points.append(v)

    def _add_kw(val: str) -> None:
        v = str(val or "").strip()
        if v and 2 <= len(v) <= 8 and v not in keywords:
            keywords.append(v)

    # 1. 从核实属性提取（面料/领型/版型/工艺）
    for key in ("面料材质", "材质成分", "成分含量", "填充物", "领型", "服装版型", "版型", "厚薄", "里料材质", "洗涤方式", "洗涤"):
        val = str(attributes.get(key) or "").strip()
        if not val:
            continue
        if key in ("面料材质", "材质成分", "成分含量"):
            _add_sp(val)
            # 提取简短材质关键词用于字幕高亮
            for mat in ("100%绵羊毛", "绵羊毛", "100%羊羔毛", "羊羔毛", "100%羊毛", "羊绒", "桑蚕丝", "真丝", "纯棉", "亚麻", "醋酸", "白鸭绒", "白鹅绒"):
                if mat in val:
                    _add_kw(mat)
        elif key in ("领型", "服装版型", "版型"):
            _add_sp(val)
            if len(val) <= 4:
                _add_kw(val)
        elif key in ("洗涤方式", "洗涤"):
            _add_sp(f"建议{val}" if not val.startswith("建议") else val)

    # 2. 从真实标题提取事实材质/款式词（优先使用口播自然词根，避免电商标题复合词导致口播证据误拦）
    _MATERIAL_ROOT_MAP = (
        ("羊羔毛", "羊毛"),
        ("绵羊毛", "羊毛"),
        ("羊毛", "羊毛"),
        ("山羊绒", "羊绒"),
        ("羊绒", "羊绒"),
        ("桑蚕丝", "真丝"),
        ("真丝", "真丝"),
        ("白鸭绒", "羽绒"),
        ("白鹅绒", "羽绒"),
        ("鹅绒", "羽绒"),
        ("鸭绒", "羽绒"),
        ("蕾丝", "蕾丝"),
    )
    title_str = clean_name or ""
    for title_tok, spoken_root in _MATERIAL_ROOT_MAP:
        if title_tok in title_str:
            _add_sp(spoken_root)
            _add_kw(title_tok)
            _add_kw(spoken_root)

    for tok in ("100%羊羔毛", "立领", "宽松", "显瘦", "百搭", "加厚", "保暖", "大衣", "外套"):
        if tok in title_str:
            _add_kw(tok)

    return selling_points[:8], keywords[:6]


def parse_product_payload(
    pack_detail_json: Optional[Dict[str, Any]],
    dom_snapshot: Optional[Dict[str, Any]],
    url: str,
) -> Dict[str, Any]:
    """将拦截到的 pack/detail JSON 与 DOM 快照统一解析为标准商品字典（纯函数，支持离线单测）。"""
    acc: Dict[str, Any] = {
        "title": "",
        "shop_name": "",
        "brand": "",
        "attrs": {},
        "variants": [],
        "main_imgs": [],
        "detail_imgs": [],
    }
    if pack_detail_json:
        _walk_json_for_fields(pack_detail_json, acc)

    dom = dom_snapshot or {}
    dom_title = str(dom.get("product_title") or dom.get("title") or "").strip()
    body_text = str(dom.get("body_text") or "")
    dom_imgs = dom.get("image_urls") or []
    dom_main_imgs = dom.get("main_image_urls") or []
    dom_detail_imgs = dom.get("detail_image_urls") or []

    # 过滤掉纯平台标题（如 "抖店" / "商品详情"）
    if dom_title in ("商品详情", "抖店", "抖音商城", "抖音电商"):
        dom_title = ""

    raw_title = acc["title"] or dom_title
    if not raw_title and body_text:
        # 从页面文本首部寻找最像商品标题的长行
        for ln in body_text.splitlines()[:40]:
            t = ln.strip()
            if 10 <= len(t) <= 80 and not t.startswith(("￥", "¥", "已售", "保障", "发货", "参数", "服务")):
                raw_title = t
                break

    # 合并 DOM 参数
    dom_attrs = _extract_attributes_from_dom_text(body_text)
    attributes: Dict[str, str] = {}
    for k, v in {**dom_attrs, **acc["attrs"]}.items():
        if not k or not v:
            continue
        if any(pk in k for pk in _PRICE_OR_PROMO_KEYS):
            continue
        if "￥" in v or "¥" in v:
            continue
        attributes[k] = v

    clean_name, brand_en_t, brand_cn_t, sku_code = _clean_title_and_extract_meta(raw_title)

    # 品牌优先级：属性/JSON 显式品牌 > 标题提取
    brand_en = brand_en_t
    brand_cn = acc["shop_name"] or brand_cn_t
    if acc["brand"]:
        if re.fullmatch(r"[A-Za-z0-9&\s.\-]+", acc["brand"]):
            brand_en = brand_en or acc["brand"]
        else:
            brand_cn = brand_cn or acc["brand"]
    if attributes.get("品牌"):
        b_attr = attributes["品牌"]
        if re.fullmatch(r"[A-Za-z0-9&\s.\-]+", b_attr):
            brand_en = brand_en or b_attr
        elif not brand_cn:
            brand_cn = b_attr

    product_id = extract_product_id(url)
    sku_id = (
        str(attributes.get("货号") or attributes.get("款号") or "").strip()
        or sku_code
        or product_id
        or "SKU_LINK"
    )

    category = infer_category(raw_title or clean_name, attributes)
    title_line1, title_line2 = _split_cover_titles(clean_name or raw_title, attributes, category)

    # 颜色变体：优先来自 JSON 规格，其次检查属性"颜色"字段
    variants: List[str] = []
    for v in acc["variants"]:
        if v and v not in variants:
            variants.append(v)
    if not variants:
        color_attr = str(attributes.get("颜色") or attributes.get("颜色分类") or "")
        for c in _KNOWN_COLORS:
            if c in color_attr and c not in variants:
                variants.append(c)

    selling_points, highlight_keywords = _derive_selling_points_and_keywords(
        clean_name or raw_title, attributes
    )

    main_image_urls = _filter_image_urls(acc["main_imgs"] + list(dom_main_imgs))
    detail_image_urls = _filter_image_urls(acc["detail_imgs"] + list(dom_detail_imgs))
    all_image_urls = _filter_image_urls(main_image_urls + detail_image_urls + list(dom_imgs))
    if not main_image_urls and all_image_urls:
        main_image_urls = all_image_urls[:4]

    return {
        "product_url": url,
        "product_id": product_id,
        "sku_id": sku_id,
        "raw_title": raw_title,
        "name": clean_name or raw_title or f"商品_{product_id or 'link'}",
        "title_line1": title_line1,
        "title_line2": title_line2,
        "category": category,
        "brand": {
            "title_en": brand_en,
            "subtitle_cn": brand_cn,
        },
        "attributes": attributes,
        "allowed_variants": variants,
        "selling_points": selling_points,
        "highlight_keywords": highlight_keywords,
        "main_image_urls": main_image_urls,
        "detail_image_urls": detail_image_urls,
        "image_urls": all_image_urls,
    }


def download_product_images(
    image_urls: List[str],
    download_dir: str,
    max_images: int = 4,
    timeout_sec: int = 15,
) -> List[str]:
    """下载商品参考图到指定目录（自动将 webp 转为 jpg 以便 OpenCV/封面渲染读取）。"""
    if not image_urls or max_images <= 0:
        return []
    import requests

    os.makedirs(download_dir, exist_ok=True)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Referer": "https://haohuo.jinritemai.com/",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }

    saved: List[str] = []
    for url in image_urls:
        if len(saved) >= max_images:
            break
        try:
            resp = requests.get(url, headers=headers, timeout=timeout_sec)
            if resp.status_code != 200 or not resp.content or len(resp.content) < 2048:
                continue
            raw_bytes = resp.content
            idx = len(saved) + 1
            out_path = os.path.join(download_dir, f"product_ref_{idx:02d}.jpg")

            # 使用 PIL 校验分辨率并统一转 RGB JPEG（解决 OpenCV 部分版本 webp 兼容问题）
            converted = False
            try:
                from PIL import Image
                with Image.open(io.BytesIO(raw_bytes)) as im:
                    w, h = im.size
                    if w < 200 or h < 200:
                        continue
                    im.convert("RGB").save(out_path, format="JPEG", quality=92)
                    converted = True
            except Exception:
                converted = False

            if not converted:
                # 回退直接写入
                ct = (resp.headers.get("Content-Type") or "").lower()
                ext = ".png" if "png" in ct else (".webp" if "webp" in ct else ".jpg")
                out_path = os.path.join(download_dir, f"product_ref_{idx:02d}{ext}")
                with open(out_path, "wb") as f:
                    f.write(raw_bytes)

            if os.path.isfile(out_path) and os.path.getsize(out_path) > 1024:
                saved.append(os.path.abspath(out_path))
        except Exception:
            continue

    return saved


def _launch_chromium(playwright_obj):
    """依次尝试内置 Chromium、系统 Edge、系统 Chrome 启动无头浏览器。"""
    last_err = None
    attempts = [
        {"headless": True},
        {"channel": "msedge", "headless": True},
        {"channel": "chrome", "headless": True},
    ]
    for kwargs in attempts:
        try:
            return playwright_obj.chromium.launch(**kwargs)
        except Exception as e:
            last_err = e
            continue
    raise ProductFetchError(
        f"无法启动无头浏览器（已依次尝试 Playwright Chromium / Edge / Chrome）: {last_err}"
    )


def fetch_product_from_url(
    url_or_text: str,
    download_dir: Optional[str] = None,
    max_images: int = 4,
    timeout_ms: int = 30000,
) -> Dict[str, Any]:
    """抓取抖音/抖店公开商品链接的信息与参考图（不登录、不绕过验证码）。"""
    url = extract_product_url(url_or_text)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise ProductFetchError(
            "缺少 playwright 依赖，请先执行 `pip install playwright` 后重试"
        ) from e

    captured_payloads: List[Dict[str, Any]] = []
    dom_snapshot: Dict[str, Any] = {}
    final_url = url
    page_title = ""

    with sync_playwright() as p:
        browser = _launch_chromium(p)
        try:
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
                locale="zh-CN",
            )
            page = context.new_page()

            def _on_response(resp):
                try:
                    rurl = resp.url or ""
                    if any(tok in rurl for tok in (
                        "promotion/pack/detail",
                        "promotion/pack/h5",
                        "promotion/detail",
                        "shop/promotion",
                        "product/detail",
                    )):
                        if resp.status == 200:
                            ct = (resp.headers or {}).get("content-type", "")
                            if "json" in ct or "text" in ct:
                                data = resp.json()
                                if isinstance(data, dict):
                                    captured_payloads.append(data)
                except Exception:
                    pass

            page.on("response", _on_response)

            try:
                page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            except Exception:
                # 某些长轮询请求会阻塞 networkidle，退回 domcontentloaded + 固定等待
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(3500)

            # 使用鼠标滚轮向下滚动，兼容内部滚动容器并触发公开商品详情参数区渲染（不点击 App 弹窗）
            try:
                page.mouse.move(640, 450)
                for _ in range(4):
                    page.mouse.wheel(0, 1200)
                    page.wait_for_timeout(600)
            except Exception:
                pass

            final_url = page.url or url
            page_title = page.title() or ""

            dom_snapshot = page.evaluate(
                """() => {
                    const bodyText = document.body ? (document.body.innerText || '') : '';
                    const collected = [];
                    document.querySelectorAll('img').forEach(img => {
                        const src = img.currentSrc || img.src || img.getAttribute('data-src') || '';
                        const w = img.naturalWidth || img.width || 0;
                        const h = img.naturalHeight || img.height || 0;
                        const top = img.getBoundingClientRect ? img.getBoundingClientRect().top : 0;
                        if (src && src.startsWith('http') && (w >= 200 || h >= 200)) {
                            collected.push({src, top});
                        }
                    });
                    document.querySelectorAll('*').forEach(el => {
                        const w = el.clientWidth || 0;
                        const h = el.clientHeight || 0;
                        if (w < 200 && h < 200) return;
                        const bg = getComputedStyle(el).backgroundImage || '';
                        if (bg && bg.includes('http')) {
                            const m = bg.match(/url\\(["']?(https?:[^"')]+)["']?\\)/);
                            if (m && m[1]) {
                                const top = el.getBoundingClientRect ? el.getBoundingClientRect().top : 0;
                                collected.push({src: m[1], top});
                            }
                        }
                    });

                    const mainImgs = collected.filter(it => it.top < 750).map(it => it.src);
                    const detailImgs = collected.filter(it => it.top >= 750).map(it => it.src);
                    const allImgs = collected.map(it => it.src);

                    let prodTitle = '';
                    const hCandidates = document.querySelectorAll('h1, [class*="title"], [class*="Title"], [class*="name"], [class*="Name"]');
                    for (const el of hCandidates) {
                        const txt = (el.innerText || '').trim();
                        if (txt.length >= 10 && txt.length <= 90 && !txt.includes('￥') && !txt.includes('打开抖音')) {
                            prodTitle = txt;
                            break;
                        }
                    }
                    return {
                        title: document.title || '',
                        product_title: prodTitle,
                        body_text: bodyText,
                        main_image_urls: mainImgs,
                        detail_image_urls: detailImgs,
                        image_urls: allImgs,
                    };
                }"""
            ) or {}
        finally:
            browser.close()

    blocked_reason = check_blocked_page(
        final_url, page_title, str(dom_snapshot.get("body_text") or "")
    )
    if blocked_reason:
        raise ProductFetchBlockedError(f"BLOCKED: {blocked_reason}")

    merged_pack: Optional[Dict[str, Any]] = None
    if len(captured_payloads) == 1:
        merged_pack = captured_payloads[0]
    elif len(captured_payloads) > 1:
        merged_pack = {"payloads": captured_payloads}

    parsed = parse_product_payload(merged_pack, dom_snapshot, url)
    parsed["fetched_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    parsed["source_mode"] = "pack_json+dom" if merged_pack else "dom_only"

    if not parsed.get("raw_title") and not parsed.get("attributes") and not parsed.get("image_urls"):
        raise ProductFetchError("未能从商品链接中解析出有效商品信息，请检查链接是否有效或已下架")

    downloaded: List[str] = []
    if download_dir:
        # 优先下载主图，不足再用详情图补足
        cand_urls = parsed.get("main_image_urls") or parsed.get("image_urls") or []
        if len(cand_urls) < max_images:
            for u in parsed.get("detail_image_urls") or []:
                if u not in cand_urls:
                    cand_urls.append(u)
        downloaded = download_product_images(cand_urls, download_dir, max_images=max_images)

    parsed["downloaded_images"] = downloaded
    parsed["reference_images"] = list(downloaded)
    parsed["item_images"] = list(downloaded[:2])
    return parsed


def merge_product_into_spec(
    spec: Dict[str, Any],
    product_data: Dict[str, Any],
    overwrite: bool = True,
) -> Dict[str, Any]:
    """将抓取到的商品结构化信息合并写入 job_spec 字典（不改动 raw_video_paths 等素材字段）。"""
    out = json.loads(json.dumps(spec or {}, ensure_ascii=False))
    prod = out.setdefault("product", {})
    brand = out.setdefault("brand", {"title_en": "", "subtitle_cn": ""})
    opts = out.setdefault("options", {})

    def _set_if(target: Dict[str, Any], key: str, val: Any) -> None:
        if val is None or val == "" or val == [] or val == {}:
            return
        if overwrite or not target.get(key):
            target[key] = val

    _set_if(prod, "product_url", product_data.get("product_url"))
    _set_if(prod, "sku_id", product_data.get("sku_id"))
    _set_if(prod, "name", product_data.get("name"))
    _set_if(prod, "title_line1", product_data.get("title_line1"))
    _set_if(prod, "title_line2", product_data.get("title_line2"))
    _set_if(prod, "category", product_data.get("category"))

    if product_data.get("attributes"):
        if overwrite or not prod.get("attributes"):
            prod["attributes"] = dict(product_data["attributes"])
        else:
            merged_attrs = dict(product_data["attributes"])
            merged_attrs.update(prod["attributes"])
            prod["attributes"] = merged_attrs

    if product_data.get("allowed_variants"):
        _set_if(prod, "allowed_variants", list(product_data["allowed_variants"]))

    if product_data.get("selling_points"):
        _set_if(prod, "selling_points", list(product_data["selling_points"]))

    if product_data.get("reference_images"):
        _set_if(prod, "reference_images", list(product_data["reference_images"]))
    if product_data.get("item_images"):
        _set_if(prod, "item_images", list(product_data["item_images"])[:2])

    fetched_brand = product_data.get("brand") or {}
    if isinstance(brand, dict):
        _set_if(brand, "title_en", fetched_brand.get("title_en"))
        _set_if(brand, "subtitle_cn", fetched_brand.get("subtitle_cn"))

    if product_data.get("highlight_keywords"):
        _set_if(opts, "highlight_keywords", list(product_data["highlight_keywords"]))

    return out
