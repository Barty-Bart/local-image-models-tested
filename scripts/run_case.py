#!/usr/bin/env python3
"""Run one test case against a ComfyUI server and log quality inputs, time and peak memory.

usage:
  run_case.py --api api.json --case T1 --prompt "..." --seed 1001 --model z_image_turbo_bf16 \
      [--images a.png b.png] [--set NODE.input=value ...] [--free] [--url http://127.0.0.1:8188]
Writes the output image(s) to results/images/ and appends one line to results/results.jsonl.
"""
import argparse, json, os, sys, threading, time, urllib.request, uuid, mimetypes

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(os.path.dirname(HERE), "results")
TEXT_NODES = ("CLIPTextEncode", "TextEncodeQwenImage21", "CLIPTextEncodeFlux", "TextEncodeQwenImageEditPlus",
              "TextEncodeZImageOmni")


def req(url, data=None, headers=None, method=None):
    r = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(r, timeout=600) as f:
        return f.read()


def jget(url):
    return json.loads(req(url))


def jpost(url, obj):
    return json.loads(req(url, json.dumps(obj).encode(), {"Content-Type": "application/json"}) or b"{}")


def upload(url, path):
    name = os.path.basename(path)
    b = uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n"
            f"--{b}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\n"
            f"Content-Type: {mimetypes.guess_type(name)[0] or 'image/png'}\r\n\r\n").encode() + open(path, "rb").read() + f"\r\n--{b}--\r\n".encode()
    return json.loads(req(url + "/upload/image", body, {"Content-Type": f"multipart/form-data; boundary={b}"}))["name"]


def set_prompt(g, text):
    """Put the test prompt into the encoder that feeds the sampler's positive input (traced, not guessed)."""
    def is_ref(v): return isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and v[0] in g
    starts = [n["inputs"]["positive"] for n in g.values() if is_ref(n["inputs"].get("positive"))]
    starts += [n["inputs"]["conditioning"] for n in g.values() if n["class_type"] == "BasicGuider" and is_ref(n["inputs"].get("conditioning"))]
    seen, stack, hits = set(), list(starts), []
    while stack:
        v = stack.pop()
        if not is_ref(v) or v[0] in seen: continue
        seen.add(v[0]); n = g[v[0]]
        for f in ("text", "prompt"):
            if f in n["inputs"]:
                hits.append((v[0], f)); break
        else:
            for k, x in n["inputs"].items():
                if k in ("conditioning", "conditioning_1", "conditioning_2", "positive", "conditioning_to", "conditioning_from"): stack.append(x)
    if not hits:
        sys.exit("could not trace the positive prompt")
    for nid, f in hits:
        v = g[nid]["inputs"][f]
        # follow a wired prompt back to the string node that holds it
        while is_ref(v) and g[v[0]]["class_type"] in ("PrimitiveStringMultiline", "PrimitiveString", "StringReplace", "JsonExtractString", "ComfySwitchNode"):
            if g[v[0]]["class_type"].startswith("PrimitiveString"):
                g[v[0]]["inputs"]["value"] = text; break
            v = None; break
        if v is None or not is_ref(g[nid]["inputs"][f]) or not g[g[nid]["inputs"][f][0]]["class_type"].startswith("PrimitiveString"):
            g[nid]["inputs"][f] = text  # literal, cuts out any rewriter chain
    # drop nodes nothing needs any more (e.g. the prompt rewriter)
    keep, st = set(), [k for k, n in g.items() if n["class_type"] in ("SaveImage", "SaveImageAdvanced")]
    while st:
        k = st.pop()
        if k in keep: continue
        keep.add(k)
        st += [x[0] for x in g[k]["inputs"].values() if is_ref(x)]
    for k in [k for k in g if k not in keep]: g.pop(k)
    return hits


def mem_used(url):
    d = jget(url + "/system_stats")["devices"][0]
    return (d["vram_total"] - d["vram_free"]) / 2**30, (d["torch_vram_total"] - d["torch_vram_free"]) / 2**30, d["vram_total"] / 2**30


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", required=True)
    ap.add_argument("--case", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--seed", type=int, default=1001)
    ap.add_argument("--model", required=True, help="label for the build, e.g. z_image_turbo_bf16")
    ap.add_argument("--tier", default="uncapped")
    ap.add_argument("--images", nargs="*", default=[])
    ap.add_argument("--set", nargs="*", default=[], help="NODE.input=value (value parsed as JSON if possible)")
    ap.add_argument("--free", action="store_true", help="unload all models first (load time counted)")
    ap.add_argument("--url", default="http://127.0.0.1:8188")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--prompt-node", default=None, help="write the prompt into this string node (keeps the model's own rewriter)")
    ap.add_argument("--ssh", default="user@your-gpu-machine")
    a = ap.parse_args()
    g = json.load(open(a.api))

    for s_ in a.set:
        lhs, v = s_.split("=", 1)
        node, inp = lhs.rsplit(".", 1)
        try:
            v = json.loads(v)
        except ValueError:
            pass
        g[node]["inputs"][inp] = v
    sys.path.insert(0, HERE); from ui2api import simplify
    g = simplify(g, fixed_primitives=True)
    if a.prompt_node:
        g[a.prompt_node]["inputs"]["value"] = a.prompt
    else:
        set_prompt(g, a.prompt)
    for n in g.values():
        for s in ("seed", "noise_seed"):
            if isinstance(n["inputs"].get(s), int):
                n["inputs"][s] = a.seed
        if n["class_type"] == "SaveImageAdvanced":  # same PNG, plain node (its dynamic format widget doesn't convert)
            n["class_type"] = "SaveImage"; n["inputs"] = {"images": n["inputs"]["images"]}
        if n["class_type"] in ("SaveImage", "SaveImageAdvanced"):
            n["inputs"]["filename_prefix"] = f"vramtest/{a.tier}/{a.model}/{a.case}_s{a.seed}"
    loads = sorted([k for k, n in g.items() if n["class_type"] == "LoadImage"], key=lambda x: [int(p) if p.isdigit() else p for p in __import__("re").split(r"(\d+)", x)])
    if len(a.images) != len(loads) and not a.dry:
        sys.exit(f"workflow has {len(loads)} LoadImage nodes, got {len(a.images)} images (use --set or a ref-variant api)")
    if a.dry:
        print(json.dumps(g, indent=1)); return
    for k, p in zip(loads, a.images):
        g[k]["inputs"]["image"] = upload(a.url, p)

    if a.free:
        jpost(a.url + "/free", {"unload_models": True, "free_memory": True}); time.sleep(3)
    base_used, base_torch, total = mem_used(a.url)
    peak = {"used": base_used, "torch": base_torch}
    stop = threading.Event()

    def poll():
        while not stop.is_set():
            try:
                u, t, _ = mem_used(a.url)
                peak["used"] = max(peak["used"], u); peak["torch"] = max(peak["torch"], t)
            except Exception:
                pass
            time.sleep(0.5)
    th = threading.Thread(target=poll, daemon=True); th.start()
    # true GPU memory on the P16: nvidia-smi streamed over one SSH connection (ComfyUI's own counter misses the weights)
    import subprocess
    smi = subprocess.Popen(["ssh", "-o", "BatchMode=yes", a.ssh, "nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits -lms 250"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    smi_vals = []
    def smi_read():
        for line in smi.stdout:
            try: smi_vals.append(int(line.strip().split(",")[0]))
            except ValueError: pass
    th2 = threading.Thread(target=smi_read, daemon=True); th2.start()
    time.sleep(1.5)
    smi_base = min(smi_vals) if smi_vals else None
    t0 = time.time()
    wdir = os.path.join(RES, "workflows_used", a.tier, a.model); os.makedirs(wdir, exist_ok=True)
    json.dump(g, open(os.path.join(wdir, f"{a.case}_s{a.seed}.json"), "w"), indent=1, ensure_ascii=False)
    pid = jpost(a.url + "/prompt", {"prompt": g, "client_id": uuid.uuid4().hex})
    if "prompt_id" not in pid:
        stop.set(); sys.exit("rejected: " + json.dumps(pid)[:2000])
    pid = pid["prompt_id"]
    while True:
        h = jget(f"{a.url}/history/{pid}")
        if pid in h and h[pid].get("status", {}).get("completed") is not None or (pid in h and h[pid]["status"].get("status_str") == "error"):
            break
        time.sleep(0.5)
    wall = time.time() - t0
    stop.set(); th.join()
    time.sleep(0.6); smi.terminate()
    smi_peak = max(smi_vals) if smi_vals else None
    h = h[pid]
    st = h["status"]
    msgs = {m[0]: m[1].get("timestamp") for m in st.get("messages", [])}
    exec_s = None
    if "execution_start" in msgs and ("execution_success" in msgs):
        exec_s = (msgs["execution_success"] - msgs["execution_start"]) / 1000
    err = None
    if st.get("status_str") == "error":
        err = [m for m in st["messages"] if m[0] == "execution_error"]
        err = (err[0][1].get("exception_message") if err else "error")[:500]
    outs = []
    os.makedirs(os.path.join(RES, "images", a.tier, a.model), exist_ok=True)
    for node_out in h.get("outputs", {}).values():
        for im in node_out.get("images", []):
            q = urllib.parse.urlencode({"filename": im["filename"], "subfolder": im["subfolder"], "type": im["type"]})
            dst = os.path.join(RES, "images", a.tier, a.model, f"{a.case}_s{a.seed}{os.path.splitext(im['filename'])[1]}")
            open(dst, "wb").write(req(f"{a.url}/view?{q}")); outs.append(os.path.relpath(dst, RES))
    row = {"date": time.strftime("%Y-%m-%d %H:%M:%S"), "tier": a.tier, "model": a.model, "case": a.case, "seed": a.seed,
           "prompt": a.prompt, "images_in": [os.path.basename(p) for p in a.images], "free_first": a.free,
           "wall_s": round(wall, 2), "exec_s": round(exec_s, 2) if exec_s else None,
           "peak_used_gib": round(peak["used"], 2), "peak_torch_gib": round(peak["torch"], 2),
           "base_used_gib": round(base_used, 2), "gpu_total_gib": round(total, 2),
           "smi_base_gb": round(smi_base / 1024, 2) if smi_base else None, "smi_peak_gb": round(smi_peak / 1024, 2) if smi_peak else None,
           "smi_delta_gb": round((smi_peak - smi_base) / 1024, 2) if smi_base and smi_peak else None,
           "status": st.get("status_str"), "error": err, "outputs": outs, "api": os.path.basename(a.api),
           "workflow_used": f"workflows_used/{a.tier}/{a.model}/{a.case}_s{a.seed}.json",
           "files": sorted({v for n in g.values() for k, v in n["inputs"].items() if isinstance(v, str) and v.endswith((".safetensors", ".gguf"))}),
           "settings": {k: v for n in g.values() if n["class_type"].startswith("KSampler") for k, v in n["inputs"].items() if k in ("steps", "cfg", "sampler_name", "scheduler", "denoise")}}
    os.makedirs(RES, exist_ok=True)
    with open(os.path.join(RES, "results.jsonl"), "a") as f:
        f.write(json.dumps(row) + "\n")
    print(json.dumps(row))


if __name__ == "__main__":
    import urllib.parse
    main()
