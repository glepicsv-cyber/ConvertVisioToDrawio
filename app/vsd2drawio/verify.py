"""Bước verify: đối chiếu raw.json/model ↔ .drawio, render và so ảnh với bản gốc.

Kết quả ghi vào work/<tên>/report.json. Trạng thái:
- FAIL   : có lỗi cấu trúc (thiếu node/edge/lane/text, lệch bbox, sai lane cha, edge thiếu đầu…)
- REVIEW : không lỗi cấu trúc nhưng còn issue chờ AI/người quyết định
- PASS   : không lỗi, không còn issue
"""
from __future__ import annotations

import html as htmlmod
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .ingest import find_drawio, run_cmd
from .mapper import node_parent
from .model import Model, save_json


def _norm(s: str) -> str:
    """Chuẩn hóa label HTML của draw.io: bỏ thẻ, giải mã entity, bỏ khoảng trắng."""
    return re.sub(r"\s+", "", htmlmod.unescape(re.sub(r"<[^>]+>", " ", s or ""))).lower()


def _norm_plain(s: str) -> str:
    """Chuẩn hóa text thuần từ file gốc: không lọc thẻ, vì text có thể chứa '<->', 'a < b > c'…"""
    return re.sub(r"\s+", "", s or "").lower()


def parse_drawio(path: Path) -> list[dict]:
    """→ [{name, cells: {id: {...}}}] với bbox tuyệt đối cho vertex."""
    root = ET.parse(path).getroot()
    pages = []
    for d in root.findall("diagram"):
        gm = d.find("mxGraphModel")
        cells: dict[str, dict] = {}
        if gm is None:
            pages.append({"name": d.get("name"), "cells": cells})
            continue
        for el in gm.find("root"):
            if el.tag == "object":
                cell = el.find("mxCell")
                attrs = dict(el.attrib)
            else:
                cell = el
                attrs = {"label": el.get("value", "")}
            if cell is None:
                continue
            cid = el.get("id")
            geo = cell.find("mxGeometry")
            c = {"id": cid, "attrs": attrs, "parent": cell.get("parent"), "vertex": cell.get("vertex") == "1",
                 "edge": cell.get("edge") == "1", "source": cell.get("source"), "target": cell.get("target"),
                 "style": cell.get("style", ""), "label": attrs.get("label", "")}
            if geo is not None:
                c["geo"] = [float(geo.get(k, 0)) for k in ("x", "y", "width", "height")]
                for pt in geo.findall("mxPoint"):
                    c[pt.get("as")] = [float(pt.get("x", 0)), float(pt.get("y", 0))]
            cells[cid] = c
        # bbox tuyệt đối
        def absxy(cid, depth=0):
            c = cells.get(cid)
            if c is None or depth > 20 or "geo" not in c or not c["vertex"]:
                return (0.0, 0.0)
            px, py = absxy(c["parent"], depth + 1)
            return (px + c["geo"][0], py + c["geo"][1])
        for c in cells.values():
            if c["vertex"] and "geo" in c:
                x, y = absxy(c["id"])
                c["abs"] = [x, y, c["geo"][2], c["geo"][3]]
        pages.append({"name": d.get("name"), "cells": cells})
    return pages


# ------------------------------------------------------------- kiểm tra cấu trúc


def structural_checks(model: Model, raw: dict, drawio_pages: list[dict], rules: dict) -> tuple[list[dict], list[str]]:
    tol = rules["verify"]["bbox_tol"]
    errors: list[str] = []
    stats = []
    raw_pages = {p["name"]: p for p in raw.get("pages", [])}
    ref_texts = raw.get("reference_texts") or {}
    dmap = {p["name"]: p for p in drawio_pages}
    for mp in model.pages:
        dp = dmap.get(mp.name)
        st = {"name": mp.name, "background": mp.background, "nodes": len(mp.nodes), "edges": len(mp.edges),
              "lanes": len(mp.lanes), "pools": len(mp.pools), "errors": []}
        stats.append(st)

        def err(msg):
            st["errors"].append(msg)
            errors.append(f"[{mp.name}] {msg}")
        if dp is None:
            err("trang không có trong .drawio")
            continue
        cells = dp["cells"]
        kinds: dict[str, list[dict]] = {}
        for c in cells.values():
            kinds.setdefault(c["attrs"].get("vsd_kind", ""), []).append(c)
        # 1) số lượng
        for k, expect in (("node", len(mp.nodes)), ("edge", len(mp.edges)), ("lane", len(mp.lanes)), ("pool", len(mp.pools))):
            got = len(kinds.get(k, []))
            if got != expect:
                err(f"số {k}: model {expect} ≠ drawio {got}")
        st["drawio_counts"] = {k: len(kinds.get(k, [])) for k in ("node", "edge", "lane", "pool")}
        # đối chiếu với raw (vsdx: đếm theo master)
        rp = raw_pages.get(mp.name)
        if rp and raw.get("backend") == "vsdx":
            lane_like = [e for e in rp["elements"] if e.get("parent") is None and e.get("master") and
                         re.sub(r"\.\d+$", "", e["master"]).lower() in {k.lower() for k in rules["vsdx"]["lane_masters"]}]
            if len(lane_like) != len(kinds.get("lane", [])):
                err(f"số lane: raw {len(lane_like)} ≠ drawio {len(kinds.get('lane', []))}")
            one_d = [e for e in rp["elements"] if e.get("parent") is None and e.get("one_d") and e.get("subpaths")]
            st["raw_1d_shapes"] = len(one_d)
            if len(one_d) != len(kinds.get("edge", [])):
                err(f"số connector/callout 1D: raw {len(one_d)} ≠ drawio edge {len(kinds.get('edge', []))}")
        # 2) text không thiếu
        labels = "".join(_norm(c["label"]) for c in cells.values())
        if rp:
            raw_texts = {e["text"] for e in rp["elements"] if (e.get("text") or "").strip()
                         and (mp.background or e.get("layer") != "background")}
            missing = [t for t in raw_texts if _norm_plain(t) and _norm_plain(t) not in labels]
            if missing:
                err(f"thiếu {len(missing)} đoạn text: " + "; ".join(repr(t[:40]) for t in missing[:5]))
            st["texts"] = len(raw_texts)
        if mp.name in ref_texts:
            miss2 = [t for t in ref_texts[mp.name] if _norm_plain(t) and _norm_plain(t) not in labels]
            if miss2:
                err(f"thiếu {len(miss2)} đoạn text so với bản .vsd gốc: " + "; ".join(repr(t[:40]) for t in miss2[:5]))
            st["reference_texts"] = len(ref_texts[mp.name])
        # 3) bbox + lane cha
        max_dev = 0.0
        for n in mp.nodes:
            c = cells.get(n.id)
            if c is None or "abs" not in c:
                err(f"node {n.id} không có trong drawio")
                continue
            dev = max(abs(a - b) for a, b in zip(c["abs"], n.bbox))
            max_dev = max(max_dev, dev)
            if dev > tol:
                err(f"node {n.id} lệch bbox {dev:.1f}px")
            expect_parent = node_parent(mp, n)
            if c["parent"] != expect_parent:
                err(f"node {n.id} có parent {c['parent']} nhưng model là {expect_parent}")
        for l in mp.lanes:
            c = cells.get(l.id)
            if c is None or "abs" not in c:
                err(f"lane {l.id} không có trong drawio")
                continue
            dev = max(abs(a - b) for a, b in zip(c["abs"], l.bbox))
            max_dev = max(max_dev, dev)
            if dev > tol:
                err(f"lane {l.id} lệch bbox {dev:.1f}px")
        st["max_bbox_dev"] = round(max_dev, 3)
        # 4) edge đủ hai đầu
        for e in mp.edges:
            c = cells.get(e.id)
            if c is None:
                err(f"edge {e.id} không có trong drawio")
                continue
            for end, pt in (("source", "sourcePoint"), ("target", "targetPoint")):
                ref = c.get(end)
                if ref and ref not in cells:
                    err(f"edge {e.id}: {end} trỏ tới {ref} không tồn tại")
                if not ref and pt not in c:
                    err(f"edge {e.id}: thiếu đầu {end}")
                if (getattr(e, end) or None) != (ref or None):
                    err(f"edge {e.id}: {end} drawio={ref} ≠ model={getattr(e, end)}")
    return stats, errors


# ------------------------------------------------------------- so ảnh


def _frame_copy(drawio: Path, out: Path, model: Model) -> Path:
    """Bản sao để so ảnh: thêm khung trắng đúng kích thước trang ở dưới cùng
    để ảnh xuất ra luôn bắt đầu tại gốc trang (0,0)."""
    tree = ET.parse(drawio)
    for d, mp in zip(tree.getroot().findall("diagram"), model.pages):
        root = d.find("mxGraphModel/root")
        frame = ET.Element("mxCell", {"id": "__page_frame", "value": "", "vertex": "1", "parent": "1",
                                      "style": "rounded=0;fillColor=#FFFFFF;strokeColor=#FFFFFF;strokeWidth=1;"})
        ET.SubElement(frame, "mxGeometry", {"x": "0", "y": "0", "width": f"{mp.width:.2f}", "height": f"{mp.height:.2f}",
                                            "as": "geometry"})
        root.insert(2, frame)
    tree.write(out, encoding="utf-8")
    return out


def render_drawio_pages(drawio: Path, out_dir: Path, model: Model, scale: float, timeout: int = 180) -> dict[int, Path]:
    exe = find_drawio()
    if not exe:
        raise RuntimeError("Không tìm thấy draw.io Desktop. Cài: winget install --id JGraph.Draw -e")
    out_dir.mkdir(parents=True, exist_ok=True)
    cmp_file = _frame_copy(drawio, out_dir / "_compare.drawio", model)
    res = {}
    for i, mp in enumerate(model.pages, start=1):
        png = out_dir / f"page{i}_drawio.png"
        if png.exists():
            png.unlink()
        r = run_cmd([exe, "-x", "-f", "png", "-p", str(i), "-b", "0", "-s", f"{scale:.4f}", "-o", str(png), str(cmp_file)],
                    timeout=timeout)
        if not png.exists():
            # draw.io < 27 đánh số trang từ 0
            run_cmd([exe, "-x", "-f", "png", "-p", str(i - 1), "-b", "0", "-s", f"{scale:.4f}", "-o", str(png), str(cmp_file)],
                    timeout=timeout)
        if png.exists():
            res[i] = png
    return res


def _pad_crop(img, w, h):
    from PIL import Image
    img = img.crop((0, 0, min(w, img.width), min(h, img.height)))
    if img.size != (w, h):
        bg = Image.new("L", (w, h), 255)
        bg.paste(img, (0, 0))
        img = bg
    return img


def compare_images(orig_png: Path, drawio_png: Path, page_size_px: tuple[float, float], model_scale: float,
                   page_width_in: float, render_scale: float, out_prefix: Path, rules: dict) -> dict:
    """So ảnh gốc (LibreOffice) với ảnh render từ .drawio, cùng hệ tọa độ trang."""
    from PIL import Image, ImageChops, ImageFilter
    v = rules["verify"]
    o = Image.open(orig_png).convert("L")
    dpi = o.width / page_width_in if page_width_in else o.width / (page_size_px[0] / model_scale)
    W = int(round(page_size_px[0] / model_scale * dpi))
    H = int(round(page_size_px[1] / model_scale * dpi))
    o = _pad_crop(o, W, H)
    # ảnh draw.io có khung trắng đúng cỡ trang ở gốc (0,0) → cắt đúng khung rồi đưa về cùng cỡ
    d = Image.open(drawio_png).convert("L")
    d = _pad_crop(d, int(round(page_size_px[0] * render_scale)), int(round(page_size_px[1] * render_scale)))
    if d.size != (W, H):
        d = d.resize((W, H), Image.LANCZOS)
    thr = v["ink_threshold"]
    ink_o = o.point(lambda p: 255 if p < thr else 0)
    ink_d = d.point(lambda p: 255 if p < thr else 0)
    k = max(3, int(v["dilate"]) | 1)
    dil_o = ink_o.filter(ImageFilter.MaxFilter(k))
    dil_d = ink_d.filter(ImageFilter.MaxFilter(k))
    cov_o = ImageChops.multiply(ink_o, dil_d)   # mực gốc được bản drawio phủ
    cov_d = ImageChops.multiply(ink_d, dil_o)   # mực drawio có trong bản gốc
    cell = max(20, int(round(v["region_size"] / model_scale * dpi)))
    regions = []
    tot_w = tot_s = 0.0
    for y in range(0, H, cell):
        for x in range(0, W, cell):
            box = (x, y, min(x + cell, W), min(y + cell, H))
            a = ink_o.crop(box).histogram()[255]
            b = ink_d.crop(box).histogram()[255]
            if a + b < 15:
                continue
            ca = cov_o.crop(box).histogram()[255]
            cb = cov_d.crop(box).histogram()[255]
            p = ca / a if a else 0.0
            r = cb / b if b else 0.0
            s = (2 * p * r / (p + r)) if (p + r) else 0.0
            wgt = a + b
            tot_w += wgt
            tot_s += s * wgt
            f = model_scale / dpi
            regions.append({"bbox": [round(box[0] * f, 1), round(box[1] * f, 1), round((box[2] - box[0]) * f, 1),
                                     round((box[3] - box[1]) * f, 1)], "score": round(s, 3),
                            "ink_orig": a, "ink_drawio": b})
    score = round(tot_s / tot_w, 4) if tot_w else 1.0
    # ảnh minh họa: cạnh nhau + chồng màu
    side = Image.new("RGB", (W * 2 + 10, H), (255, 255, 255))
    side.paste(o.convert("RGB"), (0, 0))
    side.paste(d.convert("RGB"), (W + 10, 0))
    side.save(f"{out_prefix}_side.png")
    # đỏ = chỉ có ở bản gốc, xanh = chỉ có ở draw.io, đen = trùng nhau
    white = Image.new("L", (W, H), 255)
    overlay = Image.merge("RGB", (
        ImageChops.subtract(white, ink_d),
        ImageChops.subtract(ImageChops.subtract(white, ink_o), ink_d),
        ImageChops.subtract(white, ink_o)))
    overlay.save(f"{out_prefix}_diff.png")
    regions.sort(key=lambda r: r["score"])
    return {"score": score, "worst_regions": regions[: v["worst_regions"]], "dpi": round(dpi, 2),
            "side_by_side": Path(f"{out_prefix}_side.png").name, "diff": Path(f"{out_prefix}_diff.png").name}


# ------------------------------------------------------------- điều phối


def verify(work_dir: Path, drawio: Path, model: Model, raw: dict, open_issues: list[dict], rules: dict,
           render: bool = True, extra: dict | None = None, log=print) -> dict:
    work_dir = Path(work_dir)
    compare = work_dir / "compare"
    drawio_pages = parse_drawio(drawio)
    stats, errors = structural_checks(model, raw, drawio_pages, rules)
    warnings = list(raw.get("warnings", []))
    visual_issues = []
    if render:
        try:
            rscale = _render_scale(raw, model, compare)
            pngs = render_drawio_pages(drawio, compare, model, scale=rscale)
            raw_pages = {p["name"]: p for p in raw["pages"]}
            for i, (mp, st) in enumerate(zip(model.pages, stats), start=1):
                rp = raw_pages.get(mp.name, {})
                orig = compare / rp["orig_png"] if rp.get("orig_png") else None
                if not orig or not orig.exists() or i not in pngs:
                    st["visual"] = None
                    warnings.append(f"[{mp.name}] không có ảnh để so")
                    continue
                pw_in = (rp.get("orig_png_page_in") or [mp.width / model.scale])[0]
                cmpres = compare_images(orig, pngs[i], (mp.width, mp.height), model.scale, pw_in, rscale,
                                        compare / f"page{i}", rules)
                st["visual"] = cmpres
                if cmpres["score"] < rules["verify"]["min_page_score"]:
                    worst = cmpres["worst_regions"][0] if cmpres["worst_regions"] else None
                    visual_issues.append({"id": f"V{i:02d}", "page": mp.name, "kind": "visual", "target": f"page{i}",
                                          "message": f"Ảnh trang lệch nhiều so với gốc (điểm {cmpres['score']:.2f})",
                                          "confidence": cmpres["score"], "bbox": worst["bbox"] if worst else None,
                                          "candidates": [], "evidence": [f"xem compare/{cmpres['side_by_side']}"]})
        except Exception as ex:
            warnings.append(f"Không render/so ảnh được: {ex}")
            log(f"  ! {warnings[-1]}")
    issues_all = list(open_issues) + visual_issues
    status = "FAIL" if errors else ("REVIEW" if issues_all else "PASS")
    scores = [s["visual"]["score"] for s in stats if s.get("visual")]
    report = {
        "file": raw.get("source", {}).get("name"), "source": raw.get("source"), "backend": raw.get("backend"),
        "status": status, "score": round(min(scores), 4) if scores else None,
        "pages": stats, "errors": errors, "warnings": warnings, "open_issues": issues_all,
        "totals": {"nodes": sum(s["nodes"] for s in stats if not s["background"]),
                   "edges": sum(s["edges"] for s in stats if not s["background"]),
                   "lanes": sum(s["lanes"] for s in stats), "pages": len(stats)},
    }
    if extra:
        report.update(extra)
    save_json(work_dir / "report.json", report)
    return report


def _render_scale(raw: dict, model: Model, compare: Path) -> float:
    """Tỉ lệ render draw.io sao cho cùng độ phân giải với ảnh gốc."""
    from PIL import Image
    for p in raw.get("pages", []):
        f = compare / p["orig_png"] if p.get("orig_png") else None
        if f and f.exists() and p.get("orig_png_page_in") and p["orig_png_page_in"][0]:
            with Image.open(f) as im:
                dpi = im.width / p["orig_png_page_in"][0]
            return dpi / model.scale
    return 1.0
