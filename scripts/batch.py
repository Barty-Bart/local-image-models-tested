#!/usr/bin/env python3
"""Run a list of builds through the locked cases.
usage: batch.py plan.json   (plan: [{"model": label, "tier": t, "t2i": api|null, "edit": {"1": api, "2": api, "4": api}|null, "set": [...]}])"""
import json, os, subprocess, sys, fcntl
HERE = os.path.dirname(os.path.abspath(__file__))
C = json.load(open(os.path.join(HERE, "cases.json")))
INP = os.path.join(os.path.dirname(HERE), "results", "images", "inputs", "z_image_turbo_bf16")
plan = json.load(open(sys.argv[1]))
# one GPU job at a time, so timings never overlap
_lock = open(os.path.join(os.path.dirname(HERE), "results", ".gpu.lock"), "w"); fcntl.flock(_lock, fcntl.LOCK_EX)
seed = sys.argv[2] if len(sys.argv) > 2 else "1001"
# SKIP_DONE=1: skip (model, case) already done for this seed, to resume a stopped pass
DONE = set()
if os.environ.get("SKIP_DONE"):
    for l in open(os.path.join(os.path.dirname(HERE), "results", "results.jsonl")):
        try: r = json.loads(l)
        except Exception: continue
        if str(r.get("seed")) == seed and r.get("status") == "success": DONE.add((r.get("model"), r.get("case")))
for b in plan:
    jobs = []
    if b.get("t2i"):
        jobs += [(k, b["t2i"], p, []) for k, p in C["t2i"].items()]
    if b.get("edit"):
        for grp in ("edit", "fuse"):
            for k, c in C[grp].items():
                n = str(len(c["images"]))
                if n in b["edit"]:
                    jobs.append((k, b["edit"][n], c["prompt"], [os.path.join(INP, f"{i}_s1001.png") for i in c["images"]]))
                else:
                    print(json.dumps({"model": b["model"], "case": k, "status": "skipped", "why": f"no {n}-ref workflow (max refs)"}), flush=True)
    only = os.environ.get("ONLY")  # e.g. ONLY=T2_photo runs just that case
    if only: jobs = [j for j in jobs if j[0] in only.split(",")]
    jobs = [j for j in jobs if (b["model"], j[0]) not in DONE]
    first = True
    for k, api, prompt, imgs in jobs:
        cmd = ["python3", os.path.join(HERE, "run_case.py"), "--api", os.path.join(HERE, api), "--model", b["model"],
               "--tier", b.get("tier", "uncapped"), "--seed", seed, "--case", k, "--prompt", prompt]
        if imgs: cmd += ["--images"] + imgs
        if b.get("set"): cmd += ["--set"] + b["set"]
        if b.get("prompt_node"): cmd += ["--prompt-node", b["prompt_node"]]
        if first: cmd.append("--free"); first = False
        r = subprocess.run(cmd, capture_output=True, text=True)
        try:
            d = json.loads(r.stdout.strip().splitlines()[-1])
            print(json.dumps({x: d.get(x) for x in ("model", "case", "status", "exec_s", "smi_delta_gb", "smi_peak_gb", "error")}), flush=True)
        except Exception:
            print(json.dumps({"model": b["model"], "case": k, "status": "runner-fail", "out": (r.stdout + r.stderr)[-600:]}), flush=True)
