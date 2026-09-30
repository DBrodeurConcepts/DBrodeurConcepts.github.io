#!/usr/bin/env python3
"""Create stretched-canvas versions of every poster in a Printify shop.

For each poster it reuses the poster's artwork upload, picks the canvas sizes
that match the poster's aspect ratio, and creates a canvas product in
Printify. Nothing is created unless --apply is passed, and nothing is pushed
to Shopify unless --publish is passed too.

Usage:
  export PRINTIFY_API_TOKEN=...            # never commit this
  python3 canvas_sync.py discover          # list canvas blueprints / providers / sizes
  python3 canvas_sync.py plan              # dry run: what would be created
  python3 canvas_sync.py plan --apply --limit 3            # create 3 as unpublished
  python3 canvas_sync.py plan --apply --publish --limit 3  # create + push to Shopify
Settings live in config.json next to this file.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.printify.com/v1"
HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "created.json"


def api(method, path, body=None, retries=4):
    token = os.environ.get("PRINTIFY_API_TOKEN")
    if not token:
        sys.exit("PRINTIFY_API_TOKEN is not set")
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            API + path, data=data, method=method,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json",
                     "User-Agent": "dbrodeur-canvas-sync"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(2 ** (attempt + 1))
                continue
            sys.exit(f"{method} {path} -> {e.code}: {e.read().decode()[:500]}")
    sys.exit(f"{method} {path} failed after {retries} retries")


def paged(path):
    page = 1
    while True:
        sep = "&" if "?" in path else "?"
        res = api("GET", f"{path}{sep}limit=50&page={page}")
        yield from res.get("data", [])
        if page >= res.get("last_page", 1):
            return
        page += 1


SIZE_RE = re.compile(r'(\d+(?:\.\d+)?)\s*["″”]?\s*[x×]\s*(\d+(?:\.\d+)?)\s*["″”]?')


def parse_size(title):
    m = SIZE_RE.search(title or "")
    return (float(m.group(1)), float(m.group(2))) if m else None


def size_key(w, h):
    return f"{int(w)}x{int(h)}"


def classify(w, h, families):
    """Return (family_name, orientation) for a poster size, or None."""
    ratio = max(w, h) / min(w, h)
    orient = "portrait" if h > w else "landscape" if w > h else "square"
    best = min(families.items(), key=lambda kv: abs(kv[1]["ratio"] - ratio))
    name, fam = best
    if abs(fam["ratio"] - ratio) > fam.get("tolerance", 0.08):
        return None
    return name, orient


def orient_size(size, orient):
    """Config sizes are written portrait (short x long); flip for landscape."""
    a, b = sorted(size)
    return (b, a) if orient == "landscape" else (a, b)


def load_config():
    return json.loads((HERE / "config.json").read_text())


def get_shop_id(cfg):
    if cfg.get("shop_id"):
        return cfg["shop_id"]
    shops = api("GET", "/shops.json")
    for s in shops:
        if s.get("sales_channel") == "shopify":
            return s["id"]
    sys.exit(f"No Shopify shop found in {shops}")


def cmd_discover(cfg):
    for s in api("GET", "/shops.json"):
        print(f"shop {s['id']}: {s['title']} ({s.get('sales_channel')})")
    bps = [b for b in api("GET", "/catalog/blueprints.json")
           if "canvas" in b["title"].lower() and "stretch" in b["title"].lower()]
    for b in bps:
        print(f"\nblueprint {b['id']}: {b['title']}")
        for p in api("GET", f"/catalog/blueprints/{b['id']}/print_providers.json"):
            vs = api("GET", f"/catalog/blueprints/{b['id']}/print_providers/{p['id']}/variants.json")
            sizes = sorted({v["title"] for v in vs.get("variants", [])})
            loc = (p.get("location") or {}).get("country", "?")
            print(f"  provider {p['id']}: {p['title']} [{loc}] {len(sizes)} variants")
            print("    " + ", ".join(sizes))


def canvas_variants(cfg):
    bp, pp = cfg["blueprint_id"], cfg["print_provider_id"]
    vs = api("GET", f"/catalog/blueprints/{bp}/print_providers/{pp}/variants.json")["variants"]
    by_size = {}
    for v in vs:
        s = parse_size(v["title"])
        if s:
            by_size.setdefault(size_key(*s), v)
    return by_size


def poster_art(product):
    """Return (image_id, width_px, height_px) of the poster's front artwork."""
    for area in product.get("print_areas", []):
        for ph in area.get("placeholders", []):
            if ph.get("position") == "front" and ph.get("images"):
                img = ph["images"][0]
                return img["id"], img.get("width"), img.get("height")
    return None


def poster_sizes(product):
    sizes = set()
    for v in product.get("variants", []):
        if v.get("is_enabled"):
            s = parse_size(v.get("title"))
            if s:
                sizes.add(s)
    return sizes


def build_plan(cfg):
    shop = get_shop_id(cfg)
    variants = canvas_variants(cfg)
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    skip_bps = set(cfg.get("canvas_blueprint_ids_to_skip", [])) | {cfg["blueprint_id"]}
    plan, skipped = [], []
    for p in paged(f"/shops/{shop}/products.json"):
        if p["blueprint_id"] in skip_bps or "canvas" in [t.lower() for t in p.get("tags", [])]:
            continue
        if p["id"] in state:
            skipped.append((p["title"], "already has canvas"))
            continue
        art = poster_art(p)
        sizes = poster_sizes(p)
        if not art or not sizes:
            skipped.append((p["title"], "no artwork or sizes found"))
            continue
        w, h = max(sizes, key=lambda s: s[0] * s[1])
        cls = classify(w, h, cfg["families"])
        if not cls:
            skipped.append((p["title"], f"no size family for {w:g}x{h:g}"))
            continue
        fam_name, orient = cls
        fam = cfg["families"][fam_name]
        chosen, missing = [], []
        for size in fam["sizes"]:
            cw, ch = orient_size(size, orient)
            v = variants.get(size_key(cw, ch))
            if v:
                price = cfg["prices"][size_key(*sorted(size))]
                chosen.append((v, price, cw, ch))
            else:
                missing.append(size_key(cw, ch))
        if not chosen:
            skipped.append((p["title"], f"provider has none of {missing}"))
            continue
        plan.append({"source": p, "art": art, "family": fam_name,
                     "orient": orient, "variants": chosen, "missing": missing})
    return shop, plan, skipped


def canvas_title(poster_title, cfg):
    """'Texas Map Poster' -> 'Texas Map Canvas'; keeps the rest of the name."""
    t = re.sub(r"\b(Matte|Vertical|Horizontal)\s+", "", poster_title)
    t = re.sub(r"\b(Art\s+)?(Poster|Print)\b", cfg["title_word"], t, count=1)
    t = re.sub(r"\s{2,}", " ", t).strip(" -—–")
    if cfg["title_word"].lower() not in t.lower():
        t = f"{t} — {cfg['title_word']}"
    return t


def cover_scale(art_w, art_h, ph_w, ph_h):
    """Printify scale is relative to placeholder width; 1.0 = full width.
    Scale up so the art covers the whole placeholder (no blank bands)."""
    if not (art_w and art_h and ph_w and ph_h):
        return 1.0
    return max(1.0, (ph_h * art_w) / (ph_w * art_h))


def create(shop, item, cfg, publish):
    src = item["source"]
    img_id, art_w, art_h = item["art"]
    areas = []
    for v, price, cw, ch in item["variants"]:
        ph = next((x for x in v.get("placeholders", []) if x["position"] == "front"), {})
        scale = cover_scale(art_w, art_h, ph.get("width"), ph.get("height"))
        areas.append({"variant_ids": [v["id"]], "placeholders": [{
            "position": "front",
            "images": [{"id": img_id, "x": 0.5, "y": 0.5, "scale": round(scale, 4), "angle": 0}]}]})
    body = {
        "title": canvas_title(src["title"], cfg),
        "description": cfg["description_prefix"] + (src.get("description") or ""),
        "blueprint_id": cfg["blueprint_id"],
        "print_provider_id": cfg["print_provider_id"],
        "variants": [{"id": v["id"], "price": int(round(price * 100)), "is_enabled": True}
                     for v, price, _, _ in item["variants"]],
        "print_areas": areas,
        "tags": sorted(set(src.get("tags", [])) | set(cfg["extra_tags"])),
    }
    if cfg.get("print_on_side"):
        body["print_details"] = {"print_on_side": cfg["print_on_side"]}
    res = api("POST", f"/shops/{shop}/products.json", body)
    if publish:
        api("POST", f"/shops/{shop}/products/{res['id']}/publish.json", {
            "title": True, "description": True, "images": True, "variants": True,
            "tags": True, "keyFeatures": True, "shipping_template": True})
    return res


def cmd_plan(cfg, args):
    shop, plan, skipped = build_plan(cfg)
    if args.only:
        plan = [i for i in plan if i["source"]["id"] in args.only]
    if args.limit:
        plan = plan[:args.limit]
    for item in plan:
        sizes = ", ".join(f"{cw:g}x{ch:g} ${price}" for _, price, cw, ch in item["variants"])
        extra = f"  (provider lacks {', '.join(item['missing'])})" if item["missing"] else ""
        _, aw, ah = item["art"]
        print(f"[{item['family']}/{item['orient']}] {canvas_title(item['source']['title'], cfg)}"
              f"  <- {item['source']['id']}  art {aw}x{ah}px  |  {sizes}{extra}")
    print(f"\n{len(plan)} to create, {len(skipped)} skipped")
    for title, why in skipped:
        print(f"  skip: {title} — {why}")
    if not args.apply:
        print("\nDry run. Add --apply to create.")
        return
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    for item in plan:
        res = create(shop, item, cfg, args.publish)
        state[item["source"]["id"]] = {
            "canvas_id": res["id"], "title": res["title"],
            "source_title": item["source"]["title"],
            "source_shopify_id": (item["source"].get("external") or {}).get("id")}
        STATE_FILE.write_text(json.dumps(state, indent=2))
        print(f"created {res['id']}: {res['title']}")
        time.sleep(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["discover", "plan"])
    ap.add_argument("--apply", action="store_true", help="actually create products")
    ap.add_argument("--publish", action="store_true", help="also push to Shopify")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only", nargs="*", help="Printify product ids to include")
    args = ap.parse_args()
    cfg = load_config()
    if args.command == "discover":
        cmd_discover(cfg)
    else:
        cmd_plan(cfg, args)


if __name__ == "__main__":
    main()
