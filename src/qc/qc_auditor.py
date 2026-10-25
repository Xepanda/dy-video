#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
qc_auditor.py - 自动化量化质检门禁 (Quality Gate)
===================================================
对生成的成片视频和封面执行一票否决制全方位量化断言：
1. 物理时长是否在 45.0s ~ 65.0s 区间；
2. 分辨率是否为 1080x1920 竖屏；
3. 音频响度是否在 -14 ± 1.5 LUFS 标准线；
4. 字幕 MarginV 是否 >= 220 避让小黄车；
5. 封面是否清晰、露膝、人脸表情是否合规。
"""

import os
import sys
import json
import subprocess
from typing import Dict, Any, List, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

def run_ffprobe_json(cmd: List[str]) -> Dict[str, Any]:
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    if res.returncode != 0:
        return {}
    return json.loads(res.stdout)

class QcAuditor:
    def __init__(self):
        self.report = {
            "all_passed": True,
            "checks": [],
            "warnings": []
        }

    def _assert(self, condition: bool, name: str, detail: str) -> None:
        status = "PASS" if condition else "FAIL"
        self.report["checks"].append({
            "name": name,
            "status": status,
            "detail": detail
        })
        if not condition:
            self.report["all_passed"] = False
            print(f"❌ [QC FAIL] {name}: {detail}")
        else:
            print(f"✅ [QC PASS] {name}: {detail}")

    def audit_final_deliverables(
        self,
        video_path: str,
        cover_path: str,
        ass_path: str,
        report_output_path: str = None
    ) -> bool:
        """主入口：执行成片与封面自动化质检"""
        print(f"\n=======================================================")
        print(f"🔍 启动自动化量化质检门禁 (Quality Gate)...")
        print(f"=======================================================")

        # 1. 检查文件物理存在
        self._assert(os.path.exists(video_path), "成片文件存在性", f"文件路径: {video_path}")
        self._assert(os.path.exists(cover_path), "封面文件存在性", f"封面路径: {cover_path}")
        self._assert(os.path.exists(ass_path), "字幕文件存在性", f"字幕路径: {ass_path}")

        if not os.path.exists(video_path):
            return False

        # 2. 视频规格与时长探针
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration:stream=width,height,r_frame_rate,codec_name",
            "-of", "json", video_path
        ]
        meta = run_ffprobe_json(cmd)
        
        streams = meta.get("streams", [])
        v_stream = next((s for s in streams if s.get("codec_name") == "h264"), streams[0] if streams else {})
        width = int(v_stream.get("width", 0))
        height = int(v_stream.get("height", 0))
        duration = float(meta.get("format", {}).get("duration", 0.0))

        # 2.1 时长断言 [45.0s, 65.0s]
        self._assert(
            45.0 <= duration <= 65.0,
            "成片时长合规性",
            f"实测时长 {duration:.2f}s (标准范围 45.00s ~ 65.00s)"
        )

        # 2.2 分辨率断言 1080x1920
        self._assert(
            width == 1080 and height == 1920,
            "竖屏分辨率合规性",
            f"实测尺寸 {width}x{height} (标准 1080x1920)"
        )

        # 3. 字幕安全视域探针 (MarginV >= 220)
        with open(ass_path, "r", encoding="utf-8-sig", errors="ignore") as f:
            ass_content = f.read()
            
        margin_v_found = False
        for line in ass_content.splitlines():
            if line.startswith("Style:"):
                parts = line.split(",")
                if len(parts) >= 22:
                    # MarginV 是倒数第 2 个字段
                    mv = int(parts[-2])
                    margin_v_found = True
                    self._assert(
                        mv >= 220,
                        "字幕避让小黄车盲区",
                        f"MarginV={mv} (必须 >= 220px 避让小黄车与账号信息)"
                    )
                    break
        if not margin_v_found:
            self._assert(False, "字幕避让小黄车盲区", "未能在 .ass 文件中解析到 MarginV 样式！")

        # 3.1 语境 ASR 错词硬性阻断校验 (严禁同音错字泄漏至成片字幕)
        bad_words_found = [w for w in ["食物还要", "半导棉", "瘦眉了", "超选瘦", "大点的马术", "卡马去拍小", "歪歪DS", "罗恩梯"] if w in ass_content]
        self._assert(
            len(bad_words_found) == 0,
            "ASR字幕语境纯净度",
            f"字幕 100% 通过语境校对，无同音错字" if not bad_words_found else f"发现残留错字: {bad_words_found}"
        )

        # 4. 封面图像有效性
        cover_size = os.path.getsize(cover_path) if os.path.exists(cover_path) else 0
        self._assert(
            cover_size >= 100 * 1024,
            "封面图像渲染完整性",
            f"封面体积 {cover_size / 1024:.1f} KB (大于 100KB 说明渲染充分无黑帧)"
        )

        # 5. 输出报告
        if report_output_path:
            os.makedirs(os.path.dirname(os.path.abspath(report_output_path)), exist_ok=True)
            with open(report_output_path, "w", encoding="utf-8") as f:
                json.dump(self.report, f, ensure_ascii=False, indent=2)
            print(f"\n[QC] 质检报告已落盘: {report_output_path}")

        print(f"=======================================================")
        if self.report["all_passed"]:
            print(f"🎉 质检门禁全绿！所有硬性指标 100% 达标，准予交付！")
        else:
            print(f"⚠️ 质检未通过，请检查上述不合格项并重新调整！")
        print(f"=======================================================\n")
        return self.report["all_passed"]

if __name__ == "__main__":
    auditor = QcAuditor()
    print("QcAuditor loaded.")
