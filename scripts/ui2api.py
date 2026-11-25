#!/usr/bin/env python3
"""Convert a ComfyUI UI-format workflow (official template, with subgraphs) to API format.

usage: ui2api.py template.json object_info.json > api.json
Keeps every widget value exactly as the template sets it. Notes are dropped, reroutes and
bypassed nodes are passed through, muted nodes are dropped, subgraphs are flattened
(node ids become "outer:inner").
"""
import json, sys

SKIP = {"Note", "MarkdownNote"}
WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO"}


def is_widget(spec):
    t = spec[0]
    return isinstance(t, list) or t in WIDGET_TYPES or t == "COMFY_DYNAMICCOMBO_V3"


class Graph:
    def __init__(self, nodes, links, parent=None, inst=None, sgdef=None, prefix=""):
        self.nodes = {n["id"]: n for n in nodes}
        self.links = {}
        for l in links or []:
            if isinstance(l, list):
                self.links[l[0]] = (l[1], l[2])
            else:
                self.links[l["id"]] = (l["origin_id"], l["origin_slot"])
        self.parent, self.inst, self.sgdef, self.prefix = parent, inst, sgdef, prefix


class Conv:
    def __init__(self, wf, oi):
        self.oi = oi
        self.defs = {s["id"]: s for s in wf.get("definitions", {}).get("subgraphs", [])}
        self.out = {}
        self.root = Graph(wf["nodes"], wf["links"])

    def nid(self, g, i):
        return f"{g.prefix}{i}"

    # value of a subgraph input slot, seen from inside graph g
    def sg_input(self, g, slot):
        name = g.sgdef["inputs"][slot]["name"]
        inst = g.inst
        for inp in inst.get("inputs", []):
            if inp["name"] == name and inp.get("link") is not None:
                o, s = g.parent.links[inp["link"]]
                return self.resolve(g.parent, o, s)
        wv = inst.get("widgets_values") or []
        if wv:
            nonlink = [x for x in g.sgdef["inputs"] if x["type"] in WIDGET_TYPES or x["type"] not in
                       {"IMAGE", "MASK", "LATENT", "MODEL", "CLIP", "VAE", "CONDITIONING", "AUDIO", "VIDEO", "*"}]
            names = [x["name"] for x in nonlink]
            if name in names and names.index(name) < len(wv):
                return ("lit", wv[names.index(name)])
        return ("default", None)

    def resolve(self, g, node_id, slot):
        if node_id == -10:
            return self.sg_input(g, slot)
        n = g.nodes[node_id]
        t = n["type"]
        if t in self.defs:
            sub = self.sub(g, n)
            lid = sub.sgdef["outputs"][slot]["linkIds"][0]
            o, s = sub.links[lid]
            return self.resolve(sub, o, s)
        if t == "Reroute" or n.get("mode") == 4:
            want = (n.get("outputs") or [{}])[slot].get("type") if t != "Reroute" else None
            for inp in n.get("inputs", []):
                if inp.get("link") is not None and (want is None or inp.get("type") == want):
                    o, s = g.links[inp["link"]]
                    return self.resolve(g, o, s)
            return ("default", None)
        if t == "PrimitiveNode":
            return ("lit", (n.get("widgets_values") or [None])[0])
        return ("ref", [self.nid(g, node_id), slot])

    def sub(self, g, n):
        key = (g.prefix, n["id"])
        if not hasattr(self, "_subs"):
            self._subs = {}
        if key not in self._subs:
            d = self.defs[n["type"]]
            self._subs[key] = Graph(d["nodes"], d.get("links"), parent=g, inst=n, sgdef=d,
                                    prefix=f"{g.prefix}{n['id']}:")
        return self._subs[key]

    def emit(self, g):
        for i, n in g.nodes.items():
            t = n["type"]
            if t in SKIP or t in ("Reroute", "PrimitiveNode") or n.get("mode") in (2, 4):
                continue
            if t in self.defs:
                self.emit(self.sub(g, n))
                continue
            spec = self.oi.get(t)
            if spec is None:
                raise SystemExit(f"node type not in object_info: {t} (id {self.nid(g, i)})")
            inputs = {}
            wv = n.get("widgets_values")
            if isinstance(wv, dict):
                inputs.update({k: v for k, v in wv.items() if k != "videopreview"})
            elif wv:
                k = 0
                allin = list(spec["input"].get("required", {}).items()) + list(spec["input"].get("optional", {}).items())
                for name, sp in allin:
                    if not is_widget(sp):
                        continue
                    if k >= len(wv):
                        break
                    inputs[name] = wv[k]
                    k += 1
                    if sp[0] == "COMFY_DYNAMICCOMBO_V3":  # the chosen option's own settings follow, in order
                        opt = next((o for o in sp[1]["options"] if o["key"] == inputs[name]), None)
                        if opt:
                            for sub, ssp in list(opt["inputs"].get("required", {}).items()) + list(opt["inputs"].get("optional", {}).items()):
                                if is_widget(ssp) and k < len(wv):
                                    inputs[f"{name}.{sub}"] = wv[k]; k += 1
                        continue
                    opts = sp[1] if len(sp) > 1 and isinstance(sp[1], dict) else {}
                    if opts.get("control_after_generate") or (name in ("seed", "noise_seed") and k < len(wv) and wv[k] in ("fixed", "randomize", "increment", "decrement")):
                        k += 1
            for inp in n.get("inputs", []):
                if inp.get("link") is None:
                    continue
                o, s = g.links[inp["link"]]
                r = self.resolve(g, o, s)
                if r[0] == "ref":
                    inputs[inp["name"]] = r[1]
                elif r[0] == "lit":
                    inputs[inp["name"]] = r[1]
            self.out[self.nid(g, i)] = {"class_type": t, "inputs": inputs, "_meta": {"title": n.get("title") or t}}

    def run(self):
        self.emit(self.root)
        return simplify(self.out)


SAVE = {"SaveImage", "SaveImageAdvanced"}


def simplify(g, fixed_primitives=False):
    """Collapse switches with a fixed switch value and pass-through previews, then keep only
    what the save nodes need (so a switched-off prompt rewriter is never loaded or validated)."""
    def target(v):
        while isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and v[0] in g:
            n = g[v[0]]
            if fixed_primitives and n["class_type"] in ("PrimitiveBoolean", "PrimitiveInt", "PrimitiveFloat") and not isinstance(n["inputs"].get("value"), list):
                v = n["inputs"]["value"]; break
            sw = n["inputs"].get("switch")
            if fixed_primitives and isinstance(sw, list) and sw[0] in g and g[sw[0]]["class_type"] == "PrimitiveBoolean":
                sw = g[sw[0]]["inputs"]["value"]
            if n["class_type"] == "ComfySwitchNode" and isinstance(sw, bool):
                n["inputs"]["switch"] = sw
                v = n["inputs"].get("on_true" if n["inputs"]["switch"] else "on_false")
            elif n["class_type"] == "PreviewAny":
                v = n["inputs"].get("source")
            else:
                break
        return v
    for n in g.values():
        for k, v in list(n["inputs"].items()):
            n["inputs"][k] = target(v)
    keep, stack = set(), [k for k, n in g.items() if n["class_type"] in SAVE]
    while stack:
        k = stack.pop()
        if k in keep or k not in g:
            continue
        keep.add(k)
        for v in g[k]["inputs"].values():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
                stack.append(v[0])
    return {k: g[k] for k in g if k in keep}


if __name__ == "__main__":
    wf = json.load(open(sys.argv[1]))
    oi = json.load(open(sys.argv[2]))
    print(json.dumps(Conv(wf, oi).run(), indent=1, ensure_ascii=False))
