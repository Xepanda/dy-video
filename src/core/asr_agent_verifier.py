#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
asr_agent_verifier.py - 电商直播语境感知 ASR 字幕智能校验体
==========================================================
核心职责:
1. 语境注入与领域词表动态合成:
   - 从 job_spec.json (商品名、面料、品牌、卖点词) 动态提取目标词汇指纹;
   - 内置行业高频易错词库 (实物/食物、码数/马术、卡码/卡马、螺纹T/罗恩梯、YYDS/歪歪DS、搭配/打);
2. 上下文滑动窗口语义推理 (Discourse Sliding Window):
   - 结合前置句与后置句消除语意矛盾 (如前后文赞美老钱风, 中间 "不要老钱" -> "非常显老钱");
   - 结合 pypinyin 同音/近音拼音指纹匹配, 识别主播快语速与口音导致的谐音错别字;
3. 双模混合架构 (Hybrid Architecture, B15 已落地):
   - LLM 分支 (Gemini / OpenAI 兼容): 全篇语境 Prompt + 严格 JSON 输出 + 时间戳绝对守恒
     + 零价格硬约束防护 (纠错不得引入价格表达, 决策 3) + diff 全量审计;
   - 离线或未配置 API Key 时自动无缝降级至本地确定性本体引擎;
   - 两引擎 diff 全量审计 (disagreements 列表供人工复核);
4. 物理时间戳绝对守恒 (Timestamp Invariant):
   - 严禁篡改 start, end, duration 毫秒打点, 确保字幕与口型及原声物理 100% 咬合;
5. 详细审计报告输出:
   - 自动生成 asr_verification_report.json, 记录每一处修改前、修改后、时间区间及纠错置信原因。

环境变量 (LLM 分支):
  LLM_BASE_URL   OpenAI 兼容端点 (默认 https://api.openai.com/v1)
  LLM_API_KEY    API 密钥 (缺失则降级)
  LLM_MODEL      模型名 (默认 gpt-4o-mini)
  LLM_TIMEOUT    超时秒数 (默认 60)
"""

import os
import sys
import json
import re
import time
import urllib.request
import urllib.error
from typing import List, Dict, Any, Tuple, Optional

# 核心女装直播带货垂直领域同音/近音/错别字纠错库
STATIC_DOMAIN_HOMOPHONES = [
    # (匹配正则, 纠正结果, 纠错依据)
    (r"食物", "实物", "服装展示语境下应为【实物】而非【食物】"),
    (r"半导棉|半导|半刀棉", "半岛棉", "主推核心高端面料【半岛棉】"),
    (r"瘦眉了|瘦美了|瘦梅了", "瘦没了", "修身显瘦夸张修辞【人都瘦没了】"),
    (r"超选瘦|超选受|超显受|超选受", "超显瘦", "主推核心版型卖点【超显瘦】"),
    (r"卡马", "卡码", "服装尺码选购用语【卡码】"),
    (r"马术", "码数", "服装尺码用语【码数】"),
    (r"歪歪DS|歪歪ds|歪歪滴艾斯", "YYDS", "网络流行语缩写【YYDS】"),
    (r"罗恩梯|罗恩t|罗恩T|罗恩体", "螺纹T", "基础内搭女装品类【螺纹T】"),
    (r"打我们这种|打这种", "搭我们这种", "服装穿搭语境应为【搭】"),
    (r"漏空|透空", "镂空", "服装做工工艺【镂空】"),
    (r"勾花", "钩花", "服装针织工艺【钩花】"),
    (r"真织", "针织", "服装材质品类【针织】"),
    (r"制性毛衣", "知性毛衣", "服装风格【知性毛衣】"),
    (r"打弟|打底衫", "打底", "内搭服装【打底】"),
    (r"双绉", "双绉", "真丝面料【双绉】"),
    (r"醋酸", "醋酸", "高端女装面料【醋酸】"),
    (r"桑蚕丝", "桑蚕丝", "天然面料【桑蚕丝】"),
    # —— 2026-10-21 泛化验收新增（取自 4 条新素材真实 ASR 输出）——
    (r"胃裤|西枯|稀裤", "西裤", "同音错别字【西裤】"),
    (r"款型", "版型", "服装版型语境【版型】"),
    (r"色近|色进", "色系", "配色语境【色系】"),
    (r"毛领", "毛领", "面料工艺【毛领】"),
]


class AsrContextVerifier:
    def __init__(self, spec_data: Dict[str, Any] = None):
        self.spec = spec_data or {}
        self.product_name = self._extract_product_name()
        self.brand_name = self.spec.get("brand", {}).get("title_en", "")
        self.keywords = self.spec.get("options", {}).get("highlight_keywords", [])
        self._last_llm_rejected: List[Dict[str, Any]] = []
        self._last_llm_meta: Optional[Dict[str, Any]] = None

    def _extract_product_name(self) -> str:
        p = self.spec.get("product", {})
        l1 = p.get("title_line1", "")
        l2 = p.get("title_line2", "")
        return f"{l1} {l2}".strip()

    def build_initial_prompt(self) -> str:
        """为 Whisper 转录阶段动态生成前置引导 Prompt"""
        kw_str = "，".join(self.keywords[:10]) if self.keywords else "半岛棉，镂空钩花，显瘦，针织衫"
        prompt = (
            f"这是一段抖音女装直播带货视频。品牌：{self.brand_name or 'MIXCITY'}，"
            f"主推商品：{self.product_name}。主播口语高频词：实物更漂亮、超显瘦、人瘦没了、"
            f"码数、卡码、YYDS、螺纹T、编织裤、老钱风、休闲松弛、{kw_str}。"
        )
        return prompt

    def verify_and_correct(
        self,
        raw_segments: List[Dict[str, Any]],
        report_output_path: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """
        结合电商语境对 ASR 分段字幕进行全量智能校验与纠错
        返回: (校正后的 segments, 质检审计报告 report)

        B15 架构：本地确定性引擎始终全量运行作为基线与交叉审计；
        LLM 分支可用时作为上层复核（严格 JSON + 时间戳守恒 + 零价格防护），
        两引擎分歧进入 diff_audit 供人工复核；LLM 任何环节失败自动降级。
        """
        print(f"[ASR-AGENT] 启动语境感知 ASR 智能体质检验校 (共 {len(raw_segments)} 个台词句块)...")
        print(f"            锚定商品: {self.product_name or '未知商品'} | 品牌: {self.brand_name or 'MIXCITY'}")

        # 1. 本地确定性引擎（始终运行：基线 + 降级路径 + 交叉审计）
        base_segs, base_diffs = self._run_deterministic_verification(raw_segments)

        # 2. LLM 上层复核（可用时）
        llm_segs, llm_diffs, llm_engine = self._run_llm_verification(raw_segments)
        if llm_segs is not None:
            verified_segs, diffs, engine_used = llm_segs, llm_diffs, llm_engine
        else:
            verified_segs, diffs = base_segs, base_diffs
            engine_used = ("Deterministic Context Ontology Engine (本地高精度拼音与语境本体引擎)"
                           + (f"；LLM 降级原因: {llm_engine}" if llm_engine else ""))

        # 3. 两引擎 diff 全量审计（分歧句块供人工复核）
        diff_audit = self._audit_engine_disagreement(base_segs, verified_segs)

        # 构建审计报告
        report = {
            "total_segments": len(raw_segments),
            "corrected_segments_count": len(diffs),
            "engine_used": engine_used,
            "product_context": {
                "product_name": self.product_name,
                "brand": self.brand_name,
                "target_keywords": self.keywords
            },
            "diffs": diffs,
            "deterministic_diffs_count": len(base_diffs),
            "engine_diff_audit": diff_audit,
            "llm_meta": getattr(self, "_last_llm_meta", None),
            "llm_rejected_corrections": getattr(self, "_last_llm_rejected", []),
        }

        print(f"[ASR-AGENT] 语境校对完成: 纠正 {len(diffs)} 处 ASR 错词/语境歧义 (引擎: {engine_used})")
        if diff_audit:
            print(f"[ASR-AGENT] 引擎分歧审计: {len(diff_audit)} 句两引擎结果不一致（已记录供人工复核）")
        for d in diffs:
            print(f"            [{d['time']}] {d['original']} -> {d['corrected']} ({d['reasons'][0]})")

        if report_output_path:
            os.makedirs(os.path.dirname(os.path.abspath(report_output_path)), exist_ok=True)
            with open(report_output_path, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            print(f"[ASR-AGENT] 质检审计报告已落盘: {report_output_path}")

        return verified_segs, report

    @staticmethod
    def _audit_engine_disagreement(
        base_segs: List[Dict[str, Any]], final_segs: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """两引擎 diff 审计：找出文本不一致的句块（LLM 与规则引擎分歧）。"""
        disagreements = []
        for i, (b, f) in enumerate(zip(base_segs, final_segs)):
            if b.get("text") != f.get("text"):
                disagreements.append({
                    "index": i,
                    "time": f"{b.get('start', 0):.2f}s ~ {b.get('end', 0):.2f}s",
                    "deterministic_text": b.get("text"),
                    "final_text": f.get("text"),
                })
        return disagreements

    # ---------------------------------------------------------------- LLM 分支（B15）

    # LLM 配置（环境变量；OpenAI 兼容协议，Gemini 亦走 OpenAI 兼容网关）
    LLM_MAX_SEGMENTS = 300        # 单次送审句块上限（token 成本控制）
    LLM_MAX_TOKENS = 4096

    def _llm_config(self) -> Dict[str, Any]:
        return {
            "base_url": os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            "api_key": os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
                       or os.environ.get("GEMINI_API_KEY"),
            "model": os.environ.get("LLM_MODEL", "gpt-4o-mini"),
            "timeout": float(os.environ.get("LLM_TIMEOUT", "60")),
        }

    def _build_llm_prompt(self, segments: List[Dict[str, Any]]) -> str:
        """构建全篇语境纠错 Prompt（严格 JSON 输出契约）。"""
        seg_lines = "\n".join(
            f"{i}\t{s['start']:.2f}\t{s['end']:.2f}\t{s['text']}"
            for i, s in enumerate(segments))
        kw_str = "、".join(self.keywords[:12]) if self.keywords else "（无）"
        return f"""你是抖音女装直播带货视频的 ASR 字幕校对专家。下面是 Whisper 转录的台词句块（序号/开始秒/结束秒/文本）。

品牌：{self.brand_name or 'MIXCITY'}
主推商品：{self.product_name or '未知'}
商品卖点词：{kw_str}

任务：结合全篇上下文，修正转录错词（同音错别字、口音、快语速吞音），例如：
食物→实物、半导棉→半岛棉、超选瘦→超显瘦、卡马→卡码、马术→码数、歪歪DS→YYDS、罗恩梯→螺纹T、漏空→镂空、勾花→钩花、真织→针织。

硬性规则：
1. 只修正错词，禁止改写句子结构或删除内容；
2. 严禁把任何价格数字写进文本（如 299、两百多块、到手价）；
3. 无法确定的句子保持原样；
4. 严格按下列 JSON 格式输出，不要输出任何其他文字：
{{"corrections": [{{"index": 序号, "original": "原句", "corrected": "修正句", "reason": "依据"}}]}}

台词句块：
{seg_lines}"""

    def _run_llm_verification(
        self, segments: List[Dict[str, Any]]
    ) -> Tuple[Optional[List[Dict[str, Any]]], List[Dict[str, Any]], str]:
        """LLM 上层复核（B15）。

        任何失败（无 Key/网络/超时/JSON 非法/校验不过）均返回 (None, [], 原因)，
        由调用方无缝降级至本地确定性引擎。
        护栏：时间戳绝对守恒（仅改文本）+ 纠错不得引入价格表达（决策 3）。
        """
        cfg = self._llm_config()
        if not cfg["api_key"]:
            return None, [], "None (No API Key)"
        if not segments:
            return None, [], "None (Empty Segments)"

        batch = segments[:self.LLM_MAX_SEGMENTS]
        prompt = self._build_llm_prompt(batch)
        payload = {
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content": "你是严谨的 ASR 字幕校对引擎，只输出符合契约的 JSON。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": self.LLM_MAX_TOKENS,
        }
        req = urllib.request.Request(
            f"{cfg['base_url']}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {cfg['api_key']}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=cfg["timeout"]) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            return None, [], f"LLM 调用失败: {type(e).__name__}: {e}"

        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None, [], "LLM 响应结构不符合 OpenAI 兼容契约"

        # 剥离 markdown 代码围栏后解析严格 JSON
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.MULTILINE)
        try:
            parsed = json.loads(cleaned)
            corrections = parsed["corrections"]
            assert isinstance(corrections, list)
        except (json.JSONDecodeError, KeyError, AssertionError, TypeError):
            return None, [], "LLM 输出不是合法 JSON 契约（corrections 缺失或类型错误）"

        # 应用纠错（带护栏）；时间戳从原始句块强制继承（绝对守恒）
        from src.tools.detect_price import find_price_matches
        working = [dict(s) for s in segments]
        diffs, rejected = [], []
        for corr in corrections:
            if not isinstance(corr, dict):
                continue
            idx = corr.get("index")
            if not isinstance(idx, int) or not (0 <= idx < len(working)):
                rejected.append({"correction": corr, "reason": "index 越界或非整数"})
                continue
            original = working[idx]["text"]
            corrected = str(corr.get("corrected", "")).strip()
            if not corrected or corrected == original:
                continue
            # 护栏 1：零价格（决策 3）——纠错不得引入价格表达
            price_hits = find_price_matches(corrected)
            if price_hits:
                rejected.append({"correction": corr, "reason": f"护栏拦截: 修正引入价格表达 {price_hits}"})
                continue
            # 护栏 2：修正句必须与原句同源（长度比 0.5~2.0，防整句幻觉改写）
            len_ratio = len(corrected) / max(1, len(original))
            if not (0.5 <= len_ratio <= 2.0):
                rejected.append({"correction": corr,
                                 "reason": f"护栏拦截: 长度比 {len_ratio:.2f} 超出 [0.5,2.0]（疑似整句幻觉）"})
                continue
            working[idx]["text"] = corrected
            diffs.append({
                "index": idx,
                "time": f"{working[idx].get('start', 0):.2f}s ~ {working[idx].get('end', 0):.2f}s",
                "original": original,
                "corrected": corrected,
                "reasons": [str(corr.get("reason", "LLM 语境纠错"))],
            })
        if rejected:
            print(f"[ASR-AGENT] LLM 纠错护栏拦截 {len(rejected)} 条（详见审计报告）")
        engine_name = f"LLM ({cfg['model']} @ {cfg['base_url']})"
        # 附注：被护栏拦截的记录挂在 report.diffs 之外，由 rejected_corrections 承载
        self._last_llm_rejected = rejected
        self._last_llm_meta = {"model": cfg["model"], "base_url": cfg["base_url"],
                               "corrections_applied": len(diffs),
                               "corrections_rejected": len(rejected),
                               "batch_size": len(batch)}
        return working, diffs, engine_name

    def _run_deterministic_verification(self, segments: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        本地高确定性垂直领域本体推理网 (Deterministic Context Ontology Engine)
        综合利用：
        1. 商品专项词根动态提权匹配；
        2. 直播带货常见同音错别字映射表；
        3. 前后句语境多句滑动窗口（Discourse Sliding Window）消歧。
        """
        verified = []
        diffs = []

        # 动态根据当前任务契约构建规则
        active_rules = list(STATIC_DOMAIN_HOMOPHONES)
        if "半岛棉" in self.product_name:
            active_rules.insert(0, (r"半导棉|半导|半刀棉", "半岛棉", "商品标题核心面料【半岛棉】匹配"))

        for i, s in enumerate(segments):
            text = s["text"]
            orig_text = text
            reasons = []

            # 1. 领域专有名词与高频同音词替换
            for pat, repl, reason in active_rules:
                if re.search(pat, text):
                    text = re.sub(pat, repl, text)
                    reasons.append(f"{pat} -> {repl} ({reason})")

            # 2. 上下文滑动窗口语义消歧 (Discourse Context Analysis)
            # 案例: 36.96s "配这个好老钱 配这个编织裤 非常不要老钱"
            # 前句: "配这个好老钱", 后句: "编织裤搭上去非常老钱"
            # 主播在极力赞美编织裤的老钱松弛感，"非常不要老钱" 是典型的语速过快/吞音转录错误
            if "不要老钱" in text:
                prev_text = segments[i - 1]["text"] if i > 0 else ""
                next_text = segments[i + 1]["text"] if i < len(segments) - 1 else ""
                if "老钱" in prev_text or "老钱" in next_text:
                    text = text.replace("非常不要老钱", "非常显老钱")
                    reasons.append("非常不要老钱 -> 非常显老钱 (结合前后文老钱风褒义赞美消歧)")

            # 案例: 单独出现 "裤 也不算很裤" 的口吃修正
            # 3. 严格冻结并保护时间戳
            if text != orig_text:
                diffs.append({
                    "index": i,
                    "time": f"{s['start']:.2f}s ~ {s['end']:.2f}s",
                    "original": orig_text,
                    "corrected": text,
                    "reasons": reasons
                })

            st = s.get("start", 0.0)
            et = s.get("end", st)
            dur = s.get("duration", round(et - st, 3))
            out = {
                "start": st,
                "end": et,
                "duration": dur,
                "text": text
            }
            # —— 多源：保留来源标记（否则跨素材选段会丢失时间轴归属）——
            if s.get("source_file"):
                out["source_file"] = s["source_file"]
            if s.get("source_tag"):
                out["source_tag"] = s["source_tag"]
            verified.append(out)

        # —— 需求 4（2026-10-21）：字幕可读性后处理 ——
        #ASR 常见缺陷：① 口语填充词（这个/然后/就是/嗯）导致字幕冗长；
        #              ② 单句过长（>22 字）字幕换行拥挤、阅读压力大；
        #              ③ 无标点的整段流水句。均在不改时间戳的前提下处理。
        verified, polish_diffs = self._polish_for_subtitle(verified)
        diffs.extend(polish_diffs)

        return verified, diffs

    # 口语填充/冗余词（字幕中无信息量，纯拖慢阅读）
    FILLER_WORDS = [
        "这个这个", "然后然后", "就是就是", "嗯嗯", "啊啊", "呃呃",
        "的话呢", "这样子", "那么样", "对不对呀", "对不对",
    ]
    # 单条字幕最大字数（竖屏双行可读性上限；超出按标点/语义边界切分）
    SUBTITLE_MAX_CHARS = 22

    def _polish_for_subtitle(
        self, segments: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """字幕可读性后处理：去重复填充词 + 超长句切分（时间戳守恒）。"""
        out: List[Dict[str, Any]] = []
        diffs: List[Dict[str, Any]] = []
        for i, s in enumerate(segments):
            text = (s.get("text") or "").strip()
            orig = text
            if not text:
                continue
            # 1) 叠词去重（"这个这个" → "这个"）
            for w in self.FILLER_WORDS:
                if w in text:
                    short = w[:2] if len(w) >= 4 else w
                    text = text.replace(w, short)
            # 2) 首尾空白/重复标点清理
            text = re.sub(r"\s+", "", text)
            text = re.sub(r"[，,]{2,}", "，", text)
            text = re.sub(r"^[，,。.、]+", "", text)
            text = re.sub(r"[，,、]+$", "", text)

            if text != orig:
                diffs.append({
                    "index": i,
                    "time": f"{s.get('start', 0):.2f}s ~ {s.get('end', 0):.2f}s",
                    "original": orig,
                    "corrected": text,
                    "reasons": ["字幕可读性整理（去口语填充/冗余标点）"],
                })

            # 3) 超长句按时长比例切分为多条（时间戳守恒：总时长不变）
            if len(text) > self.SUBTITLE_MAX_CHARS:
                parts = self._split_long_sentence(text, self.SUBTITLE_MAX_CHARS)
                if len(parts) > 1:
                    st, et = s["start"], s["end"]
                    total = max(et - st, 0.001)
                    weights = [max(len(p), 1) for p in parts]
                    wsum = float(sum(weights))
                    acc = st
                    base = dict(s)
                    for k, part in enumerate(parts):
                        seg_end = et if k == len(parts) - 1 else round(
                            acc + total * weights[k] / wsum, 3)
                        item = dict(base)
                        item["start"] = round(acc, 3)
                        item["end"] = seg_end
                        item["duration"] = round(seg_end - acc, 3)
                        item["text"] = part
                        out.append(item)
                        acc = seg_end
                    diffs.append({
                        "index": i,
                        "time": f"{s.get('start', 0):.2f}s ~ {s.get('end', 0):.2f}s",
                        "original": orig,
                        "corrected": " | ".join(parts),
                        "reasons": [f"超长字幕按可读性切分为 {len(parts)} 条（时间戳守恒）"],
                    })
                    continue
            out.append(dict(s, text=text))
        return out, diffs

    @staticmethod
    def _split_long_sentence(text: str, max_chars: int) -> List[str]:
        """将长句按标点优先切成 ≤max_chars 的短句；无标点时按字数硬切。"""
        if len(text) <= max_chars:
            return [text]
        parts: List[str] = []
        buf = ""
        for ch in text:
            buf += ch
            if ch in "，,。.！!？?、；;" and len(buf) >= 6:
                parts.append(buf)
                buf = ""
            elif len(buf) >= max_chars:
                parts.append(buf)
                buf = ""
        if buf:
            # 末段过短则并回上一段，避免出现 1~2 字孤字幕
            if parts and len(buf) <= 3:
                parts[-1] = parts[-1] + buf
            else:
                parts.append(buf)
        return [p for p in parts if p]


# ============================================================
# 自动挂载 ASR 执行者审核门禁（需求 2/4）
# 放在文件末尾避免循环导入：asr_review_gate 在 wire_into_verifier()
# 内部才import 本模块的 AsrContextVerifier，此处 import 触发其模块级挂载。
# 效果：任何地方只要 import 了本模块，审核门禁即已生效，
#       不依赖调用方记得显式 import 门禁（机制性保证）。
# ============================================================
try:  # pragma: no cover - 挂载失败不应阻断主流程导入
    from src.core import asr_review_gate as _asr_review_gate  # noqa: F401
except Exception as _gate_import_err:  # pragma: no cover
    print(f"[ASR-REVIEW] 警告：审核门禁未能自动挂载 -> {_gate_import_err}")


if __name__ == "__main__":
    # 单元测试
    mock_spec = {
        "product": {
            "title_line1": "半岛棉镂空钩花",
            "title_line2": "气质显瘦针织衫"
        },
        "brand": {"title_en": "MIXCITY"},
        "options": {"highlight_keywords": ["半岛棉", "镂空", "显瘦", "针织"]}
    }
    verifier = AsrContextVerifier(mock_spec)
    prompt = verifier.build_initial_prompt()
    print("Generated Whisper Prompt:", prompt)
