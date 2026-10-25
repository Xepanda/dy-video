import json, os, glob, sys
sys.stdout.reconfigure(encoding="utf-8")
res = {}
for pid in sorted(os.listdir("output")):
    d = os.path.join("output", pid)
    if not os.path.isdir(d):
        continue
    qc = os.path.join(d, "qc_report.json")
    mf = os.path.join(d, "agent_decision_manifest.json")
    if not (os.path.exists(qc) and os.path.exists(mf)):
        continue
    q = json.load(open(qc, encoding="utf-8"))
    m = json.load(open(mf, encoding="utf-8"))
    clips = m.get("chosen_clips", [])
    srcs = sorted({(c.get("source_tag") or "main")[-9:].strip() for c in clips})
    asp = os.path.join("data/work_dirs", pid, "subtitles.ass")
    subs = sum(1 for l in open(asp, encoding="utf-8-sig") if l.startswith("Dialogue")) if os.path.exists(asp) else 0
    # ASR 校对统计：遍历该项目的逐源 verified 与原始 whisper
    tot = cor = 0
    for wf in glob.glob(os.path.join("data/work_dirs", pid, "asr", "*.whisper.json")):
        vf = wf.replace(".whisper.json", ".verified.json")
        if os.path.exists(vf):
            tot += len(json.load(open(wf, encoding="utf-8")))
            cor += len(json.load(open(vf, encoding="utf-8")))
    fd = m.get("final_deliverables", {})
    vid, cov = fd.get("video", ""), fd.get("cover", "")
    res[pid] = {
        "duration_sec": round(m.get("total_duration", 0), 2),
        "clips": len(clips),
        "sources_used": srcs,
        "cross_source": len(srcs) > 1,
        "subtitle_lines": subs,
        "asr_segments": tot,
        "qc_passed": bool(q.get("all_passed")),
        "warnings": q.get("warnings", []),
        "video": vid, "video_mb": round(os.path.getsize(vid) / 1048576, 1) if os.path.exists(vid) else 0,
        "cover": cov, "cover_kb": round(os.path.getsize(cov) / 1024) if os.path.exists(cov) else 0,
    }
    print(pid, json.dumps(res[pid], ensure_ascii=False))
json.dump(res, open("logs/final_acceptance.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("\n共 %d 条素材，QC PASS %d 条" % (len(res), sum(1 for v in res.values() if v["qc_passed"])))