"""Bước reconstruct: hình học thô (raw.json) → mô hình có cấu trúc (model.json).

Mỗi kết luận kèm ``confidence`` (0–1) và ``evidence``. Mục nào dưới ngưỡng
``confidence_threshold`` trong rules.yaml được đưa vào issues.json.

- Backend ``vsdx``: dùng master, glue (Connects) và quan hệ container
  (Relationships) có sẵn trong file → độ tin cậy cao.
- Backend ``fodg``: chỉ có hình học → gộp phần tử, phân loại theo hình dạng,
  dò swimlane, bắt đầu mút connector, gán nhãn guard theo khoảng cách.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .model import (Edge, Font, Issue, Lane, Model, Node, Page, Pool, Style, SubPath, bbox_of_points,
                    bbox_union, center, contains_box, contains_point, dist_point_polyline,
                    dist_point_rect_border, overlap_area, save_json)

DEFAULT_RULES = Path(__file__).with_name("rules.yaml")


def load_rules(path: Path | None = None) -> dict:
    base = yaml.safe_load(DEFAULT_RULES.read_text(encoding="utf-8"))
    if path and Path(path).exists() and Path(path).resolve() != DEFAULT_RULES.resolve():
        user = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        _deep_update(base, user)
    return base


def _deep_update(a: dict, b: dict) -> None:
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            _deep_update(a[k], v)
        else:
            a[k] = v


# ============================================================== item trung gian

@dataclass
class Item:
    """Một "shape" sau khi gộp các phần tử thô thuộc về nó."""
    id: str
    src: list[str]
    bbox: list[float]
    subpaths: list[dict] = field(default_factory=list)   # có màu riêng: fill/stroke/sw/dashed
    text: str = ""
    html: str = ""
    font: dict | None = None
    text_bbox: list[float] | None = None
    text_angle: float = 0.0
    master: str | None = None
    one_d: bool = False
    begin: list[float] | None = None
    end: list[float] | None = None
    glue: dict = field(default_factory=dict)
    arrow_start: str = "none"
    arrow_end: str = "none"
    fill: str | None = None
    stroke: str | None = None
    stroke_width: float = 1.0
    dashed: bool = False
    rounding: float = 0.0
    z: int = 0
    rotation: float = 0.0
    relations: dict = field(default_factory=dict)
    user: dict = field(default_factory=dict)
    children: list[str] = field(default_factory=list)   # raw id con (cho group vsdx)
    labels: list["Item"] = field(default_factory=list)  # text cùng nhóm với connector (fodg)

    @property
    def closed_subpaths(self):
        return [s for s in self.subpaths if s["closed"] and (s["fill"] or s["stroke"])]

    @property
    def open_subpaths(self):
        return [s for s in self.subpaths if not s["closed"] and s["stroke"]]

    @property
    def visible(self) -> bool:
        return bool(self.closed_subpaths or self.open_subpaths or self.text.strip())


def _master_key(name: str | None) -> str:
    if not name:
        return ""
    return re.sub(r"\.\d+$", "", name.strip()).lower()


def _num(rid: str) -> str:
    """'s21' → '21', 'e57' → '57', 's14_23' → '14_23' (giữ phần phân biệt id trùng)."""
    s = re.sub(r"^[A-Za-z]+", "", rid or "")
    return s or rid


def _luma(color: str | None) -> float:
    if not color or not color.startswith("#") or len(color) != 7:
        return 255.0
    r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
    return 0.299 * r + 0.587 * g + 0.114 * b


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _style_subpaths(e: dict) -> list[dict]:
    out = []
    for sp in e.get("subpaths", []):
        fill = e.get("fill") if sp.get("fill") and sp.get("closed") else None
        stroke = e.get("stroke") if sp.get("stroke") else None
        if not fill and not stroke:
            continue
        out.append({"points": sp["points"], "closed": sp["closed"], "fill": fill, "stroke": stroke,
                    "sw": e.get("stroke_width", 1.0), "dashed": e.get("dashed", False), "src": e["id"]})
    return out


def _item_from_elem(e: dict, iid: str | None = None) -> Item:
    return Item(id=iid or e["id"], src=[e["id"]], bbox=list(e["bbox"]), subpaths=_style_subpaths(e),
                text=e.get("text") or "", html=e.get("html") or "", font=e.get("font"),
                text_bbox=e.get("text_bbox"), text_angle=e.get("text_angle", 0.0), master=e.get("master"),
                one_d=e.get("one_d", False), begin=e.get("begin"), end=e.get("end"), glue=e.get("glue") or {},
                arrow_start=e.get("arrow_start", "none"), arrow_end=e.get("arrow_end", "none"),
                fill=e.get("fill"), stroke=e.get("stroke"), stroke_width=e.get("stroke_width", 1.0),
                dashed=e.get("dashed", False), rounding=e.get("rounding", 0.0), z=e.get("z", 0),
                rotation=e.get("rotation", 0.0), relations=e.get("relations") or {}, user=e.get("user") or {})


# ============================================================== hình học hình dạng

def _simplify(pts, tol=0.8):
    """Bỏ điểm trùng và điểm thẳng hàng."""
    q = []
    for p in pts:
        if not q or math.hypot(p[0] - q[-1][0], p[1] - q[-1][1]) > tol:
            q.append(p)
    if len(q) > 2 and math.hypot(q[0][0] - q[-1][0], q[0][1] - q[-1][1]) <= tol:
        q = q[:-1]
    changed = True
    while changed and len(q) > 3:
        changed = False
        for i in range(len(q)):
            a, b, c = q[i - 1], q[i], q[(i + 1) % len(q)]
            cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
            L = math.hypot(c[0] - a[0], c[1] - a[1]) or 1.0
            if abs(cross) / L < tol * 0.5:
                q.pop(i)
                changed = True
                break
    return q


def _area(pts) -> float:
    s = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i - 1]
        x2, y2 = pts[i]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _is_convex(pts) -> bool:
    sign = 0
    n = len(pts)
    for i in range(n):
        a, b, c = pts[i], pts[(i + 1) % n], pts[(i + 2) % n]
        cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        if abs(cross) < 1e-6:
            continue
        s = 1 if cross > 0 else -1
        if sign and s != sign:
            return False
        sign = s
    return True


def _is_ellipse(pts, bbox) -> bool:
    if len(pts) < 12 or bbox[2] <= 0 or bbox[3] <= 0:
        return False
    cx, cy = center(bbox)
    rx, ry = bbox[2] / 2, bbox[3] / 2
    errs = [abs(((p[0] - cx) / rx) ** 2 + ((p[1] - cy) / ry) ** 2 - 1) for p in pts]
    return max(errs) < 0.12


def shape_features(sp: dict) -> dict:
    pts = sp["points"]
    b = bbox_of_points(pts)
    w, h = b[2], b[3]
    simp = _simplify(pts)
    area_ratio = _area(pts) / (w * h) if w > 0 and h > 0 else 0.0
    f = {"bbox": b, "w": w, "h": h, "nv": len(simp), "area_ratio": area_ratio, "convex": _is_convex(simp) if len(simp) >= 3 else True,
         "ellipse": _is_ellipse(pts, b), "rect": False, "diamond": False, "stadium": False, "roundrect": False,
         "simple": simp}
    if len(simp) == 4:
        axis = all(abs(simp[i][0] - simp[(i + 1) % 4][0]) < 1 or abs(simp[i][1] - simp[(i + 1) % 4][1]) < 1 for i in range(4))
        if axis and area_ratio > 0.97:
            f["rect"] = True
        cx, cy = center(b)
        mids = sum(1 for p in simp if (abs(p[0] - cx) < max(2, w * 0.06) and (abs(p[1] - b[1]) < 2 or abs(p[1] - b[1] - h) < 2))
                   or (abs(p[1] - cy) < max(2, h * 0.06) and (abs(p[0] - b[0]) < 2 or abs(p[0] - b[0] - w) < 2)))
        if mids == 4:
            f["diamond"] = True
    if not f["rect"] and not f["ellipse"] and len(pts) > 8 and f["convex"]:
        r = min(w, h) / 2
        stadium_ratio = (w * h - (4 - math.pi) * r * r) / (w * h) if w * h else 0
        if abs(area_ratio - stadium_ratio) < 0.025 and min(w, h) > 0:
            f["stadium"] = True
        elif 0.86 < area_ratio < 0.995:
            f["roundrect"] = True
    return f


# ============================================================== xây item (fodg)

def _same_geom(a: dict, b: dict, tol: float) -> bool:
    if a["kind"] != b["kind"] or any(abs(x - y) > tol for x, y in zip(a["bbox"], b["bbox"])):
        return False
    pa = [p for s in a["subpaths"] for p in s["points"]]
    pb = [p for s in b["subpaths"] for p in s["points"]]
    if len(pa) != len(pb):
        return False
    return all(abs(p[0] - q[0]) <= tol and abs(p[1] - q[1]) <= tol for p, q in zip(pa, pb))


def _boxes_touch(a, b, tol=1.0) -> bool:
    return not (a[0] > b[0] + b[2] + tol or b[0] > a[0] + a[2] + tol or a[1] > b[1] + b[3] + tol or b[1] > a[1] + a[3] + tol)


def build_items_fodg(page: dict, rules: dict) -> tuple[list[Item], list[str]]:
    """Gộp phần tử FODG thành item. Trả (items, nhật ký)."""
    tol = rules["geometry"]["duplicate_tol"]
    els = [e for e in page["elements"] if page.get("background") or e.get("layer") != "background"]
    by_id = {e["id"]: e for e in els}
    kids: dict[str | None, list[dict]] = {}
    for e in els:
        kids.setdefault(e.get("parent"), []).append(e)
    log: list[str] = []

    def leaves(gid):
        out = []
        for e in kids.get(gid, []):
            if e["kind"] == "group":
                out += leaves(e["id"])
            else:
                out.append(e)
        return out

    # 1) gộp bản sao fill/stroke cùng hình học trong cùng group
    merged_away: set[str] = set()
    for gid, lst in kids.items():
        geo = [e for e in lst if e["kind"] not in ("group", "text")]
        for i, a in enumerate(geo):
            if a["id"] in merged_away:
                continue
            for b in geo[i + 1:]:
                if b["id"] in merged_away or not _same_geom(a, b, tol):
                    continue
                a["fill"] = a["fill"] or b["fill"]
                if b["stroke"] and not a["stroke"]:
                    a["stroke"], a["stroke_width"], a["dashed"] = b["stroke"], b["stroke_width"], b["dashed"]
                if b["arrow_end"] != "none" and a["stroke"] == b["stroke"]:
                    a["arrow_end"] = b["arrow_end"]
                if b["arrow_start"] != "none" and a["stroke"] == b["stroke"]:
                    a["arrow_start"] = b["arrow_start"]
                a.setdefault("merged", []).append(b["id"])
                merged_away.add(b["id"])
    log.append(f"gộp {len(merged_away)} bản sao fill/stroke")

    items: list[Item] = []

    def is_closed(e):
        return any(s["closed"] for s in e["subpaths"])

    def visible(e):
        return bool(_style_subpaths(e))

    def make_item(members: list[dict], texts: list[dict]) -> Item:
        members = sorted(members, key=lambda e: e["z"])
        main = max(members, key=lambda e: e["bbox"][2] * e["bbox"][3]) if members else None
        it = _item_from_elem(main if main else texts[0])
        it.src = [e["id"] for e in members] + [t["id"] for t in texts]
        for e in members:
            it.src += e.get("merged", [])
        it.subpaths = [sp for e in members for sp in _style_subpaths(e)]
        it.bbox = bbox_union([e["bbox"] for e in members]) if members else list(texts[0]["bbox"])
        it.z = min(e["z"] for e in members + texts)
        if texts:
            texts = sorted(texts, key=lambda t: (t["bbox"][1], t["bbox"][0]))
            it.text = "\n".join(t["text"] for t in texts)
            it.html = texts[0]["html"] if len(texts) == 1 else ""
            it.font = texts[0]["font"]
            it.text_bbox = bbox_union([t["bbox"] for t in texts])
        elif members:
            it.text, it.html, it.font, it.text_bbox = "", "", None, None
        return it

    def process(gid):
        lst = [e for e in kids.get(gid, []) if e["id"] not in merged_away]
        for e in lst:
            if e["kind"] == "group":
                lv = [x for x in leaves(e["id"]) if x["id"] not in merged_away]
                geo = [x for x in lv if x["kind"] != "text" and visible(x)]
                texts = [x for x in lv if x["kind"] == "text"]
                closed = [x for x in geo if is_closed(x)]
                opened = [x for x in geo if not is_closed(x)]
                # group Visio chứa nhiều shape có chữ → tách thành từng shape để sửa được riêng
                subgroups = [x for x in kids.get(e["id"], []) if x["kind"] == "group"]
                if len(texts) >= 2 and len(subgroups) >= 2:
                    process(e["id"])
                    continue
                # đường hở nhỏ, không mũi tên, chạm hình kín → chi tiết trang trí của shape
                if closed and opened and _connected(closed):
                    cb = bbox_union([x["bbox"] for x in closed])
                    small = [o for o in opened if o["arrow_end"] == "none" and o["arrow_start"] == "none" and
                             ((max(o["bbox"][2], o["bbox"][3]) <= 12 and _boxes_touch(o["bbox"], cb, 2))
                              or contains_box(cb, o["bbox"], 1))]
                    if len(small) == len(opened):
                        closed, opened = closed + small, []
                # nhóm chỉ có hình kín liền nhau (+ text) → một shape
                if closed and not opened and _connected(closed):
                    items.append(make_item(closed, texts))
                    continue
                # nhóm chỉ có đường hở (+ text) → connector có nhãn / callout
                if opened and not closed:
                    callout_like = len(opened) == 1 and len(opened[0]["subpaths"]) >= 2 and texts and \
                        opened[0]["arrow_end"] == "none" and opened[0]["arrow_start"] == "none"
                    if len(opened) == 1 and not callout_like:
                        it = make_item(opened, [])
                        it.labels = [_item_from_elem(t) for t in texts]
                        it.src += [t["id"] for t in texts]
                        items.append(it)
                    else:
                        it = make_item(opened, texts)
                        items.append(it)
                    continue
                if not geo and texts:
                    for t in texts:
                        items.append(_item_from_elem(t))
                    continue
                process(e["id"])
            elif e["kind"] == "text":
                items.append(_item_from_elem(e))
            elif visible(e):
                items.append(make_item([e], []))
    process(None)
    # đường hở rất nhỏ không mũi tên sát một hình kín → chi tiết của hình đó
    shapes0 = [i for i in items if i.closed_subpaths]
    for t in [i for i in items if i.open_subpaths and not i.closed_subpaths and not i.text.strip()
              and max(i.bbox[2], i.bbox[3]) <= 12 and i.arrow_end == "none" and i.arrow_start == "none"]:
        near = [s for s in shapes0 if _boxes_touch(s.bbox, t.bbox, 3)]
        if near:
            s = min(near, key=lambda s: s.bbox[2] * s.bbox[3])
            s.subpaths += t.subpaths
            s.src += t.src
            s.bbox = bbox_union([s.bbox, t.bbox])
            items.remove(t)
    # gắn text tự do nằm trong một shape kín không có text
    free_texts = [i for i in items if not i.subpaths and i.text]
    shapes = [i for i in items if i.closed_subpaths]
    for t in free_texts:
        cx, cy = center(t.bbox)
        cands = [s for s in shapes if not s.text and contains_point(s.bbox, cx, cy) and
                 s.bbox[2] * s.bbox[3] < 4 * max(t.bbox[2] * t.bbox[3], 1)]
        if cands:
            s = min(cands, key=lambda s: s.bbox[2] * s.bbox[3])
            s.text, s.html, s.font, s.text_bbox = t.text, t.html, t.font, t.bbox
            s.src += t.src
            items.remove(t)
    log.append(f"{len(items)} item sau khi gộp")
    return items, log


def _connected(elems) -> bool:
    if len(elems) <= 1:
        return True
    seen = {0}
    stack = [0]
    while stack:
        i = stack.pop()
        for j in range(len(elems)):
            if j not in seen and _boxes_touch(elems[i]["bbox"], elems[j]["bbox"]):
                seen.add(j)
                stack.append(j)
    return len(seen) == len(elems)


# ============================================================== xây item (vsdx)

def build_items_vsdx(page: dict, rules: dict | None = None) -> tuple[list[Item], dict[str, str], dict[str, dict]]:
    """Mỗi shape cấp cao nhất → một item (gộp hình của các shape con).

    Group Visio chứa nhiều shape có chữ, hoặc có shape con được connector gắn vào,
    được TÁCH thành từng item riêng (để trong draw.io sửa được từng object).
    Pool/lane/khung cấu trúc luôn giữ nguyên một khối.
    Trả (items, map raw id → id item chứa nó, raw theo id)."""
    els = page["elements"]
    by_id = {e["id"]: e for e in els}
    kids: dict[str | None, list[dict]] = {}
    for e in els:
        kids.setdefault(e.get("parent"), []).append(e)
    glue_targets = {t for e in els for t in (e.get("glue") or {}).values()}
    vr = (rules or {}).get("vsdx", {})
    whole = {m.lower() for m in vr.get("pool_masters", [])} | {m.lower() for m in vr.get("lane_masters", {})} | \
        {m.lower() for m in vr.get("ignore_masters", [])} | {m.lower() for m in vr.get("callout_masters", [])}
    top_of: dict[str, str] = {}

    def desc(eid):
        out = []
        for c in kids.get(eid, []):
            out.append(c)
            out += desc(c["id"])
        return out

    def should_explode(e) -> bool:
        if e.get("kind") != "group" or _master_key(e.get("master")) in whole:
            return False
        d = desc(e["id"])
        with_text = [c for c in d if (c.get("text") or "").strip()]
        return len(with_text) >= 2 or any(c["id"] in glue_targets for c in d)

    def whole_item(e) -> Item:
        it = _item_from_elem(e)
        d = desc(e["id"])
        top_of[e["id"]] = e["id"]
        for c in d:
            top_of[c["id"]] = e["id"]
            it.subpaths += _style_subpaths(c)
        it.children = [c["id"] for c in d]
        if not it.text.strip():
            texts = [c for c in d if c.get("text", "").strip()]
            if texts:
                it.text = "\n".join(c["text"] for c in texts)
                it.html = texts[0].get("html", "") if len(texts) == 1 else ""
                it.font = texts[0]["font"]
                it.text_bbox = bbox_union([c["text_bbox"] for c in texts if c.get("text_bbox")])
        # bbox = phần NHÌN THẤY (không lấy khung group, tránh một ô trong suốt che các object khác)
        pts = [p for sp in it.subpaths for p in sp["points"]]
        if pts:
            it.bbox = bbox_of_points(pts)
        elif it.text.strip() and it.text_bbox:
            it.bbox = list(it.text_bbox)
        return it

    items = []

    def visit(e):
        if should_explode(e):
            own = _item_from_elem(e)  # khung riêng của group (nếu có hình)
            top_of[e["id"]] = e["id"]
            if own.subpaths or own.text.strip():
                own.bbox = bbox_of_points([p for sp in own.subpaths for p in sp["points"]]) if own.subpaths else own.bbox
                items.append(own)
            for c in kids.get(e["id"], []):
                visit(c)
            return
        items.append(whole_item(e))

    for e in kids.get(None, []):
        visit(e)
    return items, top_of, by_id


# ============================================================== phân loại

def classify_geometry(it: Item, rules: dict) -> tuple[str, str, float, list[str], str | None]:
    """→ (type, shape, confidence, evidence, orientation)."""
    g = rules["geometry"]
    c = rules["classify"]
    text = _norm(it.text)
    closed = it.closed_subpaths
    if not closed and not it.open_subpaths:
        if text:
            return "text", "none", 0.9, ["chỉ có text, không có hình"], None
        return "unknown", "custom", 0.3, ["không có hình hiển thị"], None
    if not closed:
        return "unknown", "custom", 0.4, ["chỉ có đường hở không phải connector"], None
    main = max(closed, key=lambda s: _area(s["points"]))
    f = shape_features(main)
    w, h = f["w"], f["h"]
    ev = [f"hình chính {w:.0f}x{h:.0f}px, {f['nv']} đỉnh, tỉ lệ diện tích {f['area_ratio']:.2f}"]
    dark = _luma(main["fill"]) <= g["dark_fill_max_luma"] if main["fill"] else False
    if dark:
        ev.append(f"tô đặc màu tối {main['fill']}")
    thin, long_ = min(w, h), max(w, h)
    # thanh fork/join
    if dark and thin > 0 and long_ / thin >= g["bar_min_aspect"] and thin <= g["bar_max_thickness"] and \
            (f["rect"] or f["area_ratio"] > 0.9):
        ori = "horizontal" if w >= h else "vertical"
        return "fork_join", ("bar" if len(closed) == 1 and not it.open_subpaths else "custom"), 0.9, \
            ev + [f"thanh đặc dài mảnh ({ori})"], ori
    # hình tròn
    round_ = f["ellipse"] and h > 0 and abs(w / h - 1) <= g["circle_aspect_tol"]
    if round_:
        inner = [s for s in closed if s is not main and contains_box(f["bbox"], bbox_of_points(s["points"]), 1)]
        crosses = [s for s in it.open_subpaths if contains_box(f["bbox"], bbox_of_points(s["points"]), 2)]
        if inner and any(_luma(s["fill"]) <= g["dark_fill_max_luma"] for s in inner if s["fill"]):
            return "activity_final", "custom", 0.9, ev + ["tròn có vòng ngoài, lõi tô đặc"], None
        if len(crosses) >= 2 or (crosses and len(crosses[0]["points"]) >= 4):
            return "flow_final", "custom", 0.85, ev + ["tròn có dấu X"], None
        if dark and not text:
            return "initial", "ellipse", 0.9, ev + ["tròn tô đặc"], None
        return "unknown", "ellipse", 0.45, ev + ["hình tròn sáng màu, không rõ ký hiệu"], None
    if f["diamond"]:
        return "decision", "rhombus", 0.85, ev + ["hình thoi"], None
    words_i = [w_.lower() for w_ in c["terminator_initial_words"]]
    words_f = [w_.lower() for w_ in c["terminator_final_words"]]
    if f["stadium"] or (f["ellipse"] and text):
        low = text.lower()
        if low in words_i:
            return "initial", "stadium", 0.8, ev + [f"hình viên thuốc, chữ '{text}'"], None
        if low in words_f:
            return "activity_final", "stadium", 0.8, ev + [f"hình viên thuốc, chữ '{text}'"], None
        if text:
            r = c["stadium_with_text"]
            return r["type"], "stadium", r["confidence"], ev + ["hình viên thuốc có text"], None
    if f["roundrect"] and text:
        r = c["rect_rounded_with_text"]
        return r["type"], "roundrect", r["confidence"], ev + ["chữ nhật bo góc có text"], None
    if f["rect"]:
        if it.rounding > 0.5 and text:
            r = c["rect_rounded_with_text"]
            return r["type"], "roundrect", r["confidence"], ev + [f"chữ nhật bo góc {it.rounding:.1f}px"], None
        if text:
            r = c["rect_square_with_text"]
            return r["type"], "rect", r["confidence"], ev + ["chữ nhật góc vuông có text"], None
        return "unknown", "rect", 0.5, ev + ["chữ nhật không có text"], None
    if f["nv"] == 5:
        if f["area_ratio"] > 0.93 and text:
            return "note", "custom", 0.8, ev + ["ngũ giác gập góc (note)"], None
        if f["convex"]:
            return "send_signal", "custom", 0.75, ev + ["ngũ giác lồi (send signal)"], None
        return "receive_signal", "custom", 0.75, ev + ["ngũ giác lõm (receive signal)"], None
    if f["nv"] == 6 and not f["convex"] and text:
        return "note", "custom", 0.65, ev + ["hình gập góc"], None
    return "unknown", "custom", 0.4, ev + ["không khớp luật nào"], None


# ============================================================== swimlane


def detect_swimlanes(items: list[Item], rules: dict, page_box) -> tuple[list[Pool], list[Lane], set[str], list[str]]:
    """Dò pool/lane theo hình học. Trả (pools, lanes, id item đã dùng, nhật ký)."""
    s = rules["swimlane"]
    tol = s["edge_tol"]
    log = []
    rects = [i for i in items if len(i.closed_subpaths) >= 1 and shape_features(max(i.closed_subpaths, key=lambda x: _area(x["points"])))["rect"]]
    content = bbox_union([i.bbox for i in items if i.visible]) or page_box
    used: set[str] = set()
    pools, lanes = [], []
    for orient in ("vertical", "horizontal"):
        span_idx = 3 if orient == "vertical" else 2
        cand = [r for r in rects if r.id not in used and r.bbox[span_idx] >= s["min_span_ratio"] * content[span_idx]
                and not r.text.strip()]
        # nhóm theo cạnh đầu và độ dài
        groups: list[list[Item]] = []
        for r in sorted(cand, key=lambda r: (r.bbox[1 if orient == "vertical" else 0], r.bbox[0 if orient == "vertical" else 1])):
            placed = False
            for gr in groups:
                ref = gr[0].bbox
                if orient == "vertical":
                    same = abs(ref[1] - r.bbox[1]) <= tol and abs(ref[3] - r.bbox[3]) <= tol
                else:
                    same = abs(ref[0] - r.bbox[0]) <= tol and abs(ref[2] - r.bbox[2]) <= tol
                if same:
                    gr.append(r)
                    placed = True
                    break
            if not placed:
                groups.append([r])
        # nhóm ngắn trước: lane nằm trong pool nên luôn ngắn hơn khung pool
        groups.sort(key=lambda gr: gr[0].bbox[span_idx])
        for gr in groups:
            # bỏ hình đang bao các lane đã tìm (đó là khung pool, xét sau)
            gr = [r for r in gr if not any(contains_box(r.bbox, l.bbox, tol) for l in lanes)]
            # giữ các hình kề nhau (không chồng lấn) liên tiếp
            ax = 0 if orient == "vertical" else 1
            gr = sorted(gr, key=lambda r: r.bbox[ax])
            chain = []
            for r in gr:
                if chain and r.bbox[ax] < chain[-1].bbox[ax] + chain[-1].bbox[ax + 2] - tol:
                    # chồng lấn: giữ hình nhỏ hơn (lane), hình to có thể là pool
                    if r.bbox[ax + 2] < chain[-1].bbox[ax + 2]:
                        chain[-1] = r
                    continue
                chain.append(r)
            # tìm header có text cho từng thân lane; thân không có tiêu đề không phải lane
            found = []
            for body in chain:
                hdr = _find_header(body, items, orient, s, used)
                if hdr is not None:
                    found.append((body, hdr))
            if len(found) < max(1, s["min_lanes"]):
                continue
            with_hdr = found
            adjacent = all(abs(found[i + 1][0].bbox[ax] - (found[i][0].bbox[ax] + found[i][0].bbox[ax + 2])) <= tol * 2
                           for i in range(len(found) - 1))
            conf = 0.9 if adjacent else 0.6
            for order, (body, hdr) in enumerate(found):
                hsize = (hdr.bbox[3] if orient == "vertical" else hdr.bbox[2]) if hdr else 0.0
                ln = Lane(id=f"L{_num(body.id)}", name=_norm(hdr.text) if hdr else "", bbox=list(body.bbox),
                          orientation=orient, order=order, header_size=hsize,
                          style=_style_of(hdr or body), font=_font_of(hdr) if hdr else Font(),
                          html=hdr.html if hdr else "", z=body.z, confidence=conf,
                          evidence=[f"{len(found)} hình chữ nhật {'cao' if orient == 'vertical' else 'rộng'} kề nhau, cùng "
                                    f"{'đỉnh và chiều cao' if orient == 'vertical' else 'cạnh trái và chiều rộng'}"]
                          + ([f"dải tiêu đề '{_norm(hdr.text)}'"] if hdr else ["không thấy dải tiêu đề"]),
                          src=body.src + (hdr.src if hdr else []))
                if not adjacent:
                    ln.evidence.append("các lane không liền nhau")
                lanes.append(ln)
                used.add(body.id)
                if hdr:
                    used.add(hdr.id)
            log.append(f"dò được {len(found)} lane {orient}")
            # pool bao ngoài
            ub = bbox_union([b.bbox for b, _ in found])
            outer = [r for r in rects if r.id not in used and contains_box(r.bbox, ub, tol) and
                     r.bbox[2] * r.bbox[3] >= ub[2] * ub[3] * 0.95]
            title = None
            pool_box = None
            if outer:
                pb = min(outer, key=lambda r: r.bbox[2] * r.bbox[3])
                pool_box = pb
                used.add(pb.id)
            # dải tiêu đề pool: hình có text, cùng bề ngang (vertical) nằm ngay trên các lane
            tcand = [i for i in items if i.id not in used and i.text.strip() and i.closed_subpaths]
            for t in tcand:
                if orient == "vertical":
                    ok = t.bbox[1] + t.bbox[3] <= ub[1] + tol and t.bbox[1] + t.bbox[3] >= ub[1] - 3 * tol and \
                        t.bbox[0] <= ub[0] + tol and t.bbox[0] + t.bbox[2] >= ub[0] + ub[2] - tol
                else:
                    ok = t.bbox[0] + t.bbox[2] <= ub[0] + tol and t.bbox[0] + t.bbox[2] >= ub[0] - 3 * tol and \
                        t.bbox[1] <= ub[1] + tol and t.bbox[1] + t.bbox[3] >= ub[1] + ub[3] - tol
                if ok:
                    title = t
                    break
            if pool_box or title:
                pbx = pool_box.bbox if pool_box else bbox_union([ub, title.bbox])
                if title:
                    pbx = bbox_union([pbx, title.bbox])
                    used.add(title.id)
                hs = 0.0
                if title:
                    hs = (title.bbox[1] + title.bbox[3] - pbx[1]) if orient == "vertical" else (title.bbox[0] + title.bbox[2] - pbx[0])
                pl = Pool(id=f"P{_num((pool_box or title).id)}", name=_norm(title.text) if title else "", bbox=list(pbx),
                          orientation=orient, header_size=hs, style=_style_of(title or pool_box),
                          font=_font_of(title) if title else Font(), html=title.html if title else "",
                          z=min((pool_box or title).z, (title or pool_box).z), confidence=0.85 if title else 0.7,
                          evidence=(["hình chữ nhật bao các lane"] if pool_box else []) + ([f"tiêu đề '{_norm(title.text)}'"] if title else []),
                          src=(pool_box.src if pool_box else []) + (title.src if title else []))
                pools.append(pl)
                for ln in lanes:
                    if ln.pool is None and contains_box(pl.bbox, ln.bbox, tol):
                        ln.pool = pl.id
    return pools, lanes, used, log


def _find_header(body: Item, items: list[Item], orient: str, s: dict, used: set) -> Item | None:
    tol = s["edge_tol"]
    b = body.bbox
    best = None
    for i in items:
        if i is body or i.id in used or not i.text.strip():
            continue
        x = i.bbox
        if orient == "vertical":
            ok = abs(x[0] - b[0]) <= tol and abs(x[2] - b[2]) <= tol and abs(x[1] - b[1]) <= tol * 3 and \
                x[3] <= s["header_max_ratio"] * b[3]
        else:
            ok = abs(x[1] - b[1]) <= tol and abs(x[3] - b[3]) <= tol and abs(x[0] - b[0]) <= tol * 3 and \
                x[2] <= s["header_max_ratio"] * b[2]
        if ok and (best is None or x[2] * x[3] < best.bbox[2] * best.bbox[3]):
            best = i
    return best


# ============================================================== tiện ích style


def _style_of(it: Item | None) -> Style:
    if it is None:
        return Style()
    cs = it.closed_subpaths
    main = max(cs, key=lambda s: _area(s["points"])) if cs else (it.open_subpaths[0] if it.open_subpaths else None)
    fill = None
    stroke = None
    sw = it.stroke_width
    dashed = it.dashed
    for sp in (cs or it.open_subpaths):
        fill = fill or sp["fill"]
    if main is not None:
        stroke = main["stroke"] or next((sp["stroke"] for sp in it.subpaths if sp["stroke"]), None)
        sw = main["sw"] if main["stroke"] else sw
        dashed = main["dashed"]
    return Style(fill=fill, stroke=stroke, stroke_width=round(max(sw, 0.0), 2), dashed=dashed,
                 rounded=round(it.rounding, 2))


def _font_of(it: Item | None) -> Font:
    if it is None or not it.font:
        return Font()
    f = it.font
    return Font(family=f.get("family") or "Arial", size=float(f.get("size") or 11), color=f.get("color") or "#000000",
                bold=bool(f.get("bold")), italic=bool(f.get("italic")), underline=bool(f.get("underline")),
                align=f.get("align") or "center", valign=f.get("valign") or "middle")


def _geometry_of(it: Item) -> list[SubPath]:
    return [SubPath(points=[[round(p[0], 2), round(p[1], 2)] for p in sp["points"]], closed=sp["closed"],
                    fill=bool(sp["fill"]), stroke=bool(sp["stroke"]), fill_color=sp["fill"], stroke_color=sp["stroke"],
                    stroke_width=round(sp["sw"], 3), dashed=bool(sp["dashed"])) for sp in it.subpaths]


def _make_node(it: Item, ntype: str, shape: str, conf: float, ev: list[str], ori=None, bbox=None,
               subpaths=None) -> Node:
    if subpaths is not None:
        it = Item(**{**it.__dict__, "subpaths": subpaths})
    n = Node(id=f"n{_num(it.id)}", type=ntype, bbox=[round(v, 2) for v in (bbox or it.bbox)], shape=shape,
             text=it.text.strip("\n"), html=it.html, font=_font_of(it), style=_style_of(it), z=it.z,
             geometry=_geometry_of(it), orientation=ori, confidence=round(conf, 3), evidence=ev, src=list(it.src))
    ta = (it.text_angle or 0.0) % 360
    if n.text and (abs(ta - 90) < 5 or abs(ta - 270) < 5):
        n.text_vertical = True
    return n


# ============================================================== connector


def snap_end(pt, nodes: list[Node], rules: dict, exclude: str | None = None):
    """Tìm node có biên gần điểm nhất. Trả (node_id|None, conf, evidence, candidates)."""
    g = rules["geometry"]
    cands = []
    for n in nodes:
        if n.type in ("text", "decor") or n.id == exclude:
            continue
        b = n.bbox
        d = dist_point_rect_border(b, pt[0], pt[1])
        inside = contains_point(b, pt[0], pt[1])
        if inside and d > g["snap_tol"]:
            d = d * 0.5  # điểm nằm sâu trong node vẫn coi là bắt vào node đó
        if d <= g["snap_tol"]:
            cands.append((d, n.id))
    cands.sort()
    if not cands:
        return None, 0.4, [f"không có node nào trong {g['snap_tol']}px"], []
    if len(cands) > 1 and cands[1][0] - cands[0][0] < g["snap_ambiguity"]:
        return cands[0][1], 0.5, [f"2 node gần như nhau: {cands[0][1]} ({cands[0][0]:.1f}px), {cands[1][1]} ({cands[1][0]:.1f}px)"], \
            [c[1] for c in cands[:3]]
    conf = 0.9 if cands[0][0] <= g["snap_tol"] / 2 else 0.75
    return cands[0][1], conf, [f"bắt vào {cands[0][1]} cách biên {cands[0][0]:.1f}px"], [c[1] for c in cands[:3]]


def _orient_points(points, begin, end):
    if begin and end and len(points) >= 2:
        d_keep = math.hypot(points[0][0] - begin[0], points[0][1] - begin[1])
        d_flip = math.hypot(points[-1][0] - begin[0], points[-1][1] - begin[1])
        if d_flip < d_keep:
            return list(reversed(points))
    return points


# ============================================================== reconstruct trang


def reconstruct_page(page: dict, backend: str, rules: dict) -> tuple[Page, list[str]]:
    thr = rules["confidence_threshold"]
    log: list[str] = []
    P = Page(index=page["index"], name=page["name"], width=page["width"], height=page["height"],
             background=bool(page.get("background")))
    page_box = [0, 0, page["width"], page["height"]]
    vmap = {k.lower(): v for k, v in (rules["vsdx"]["masters"] or {}).items()}
    vr = rules["vsdx"]
    callouts = {m.lower() for m in vr["callout_masters"]}
    connectors = {m.lower() for m in vr["connector_masters"]}
    pool_m = {m.lower() for m in vr["pool_masters"]}
    lane_m = {k.lower(): v for k, v in vr["lane_masters"].items()}
    ignore_m = {m.lower() for m in vr["ignore_masters"]}

    top_of: dict[str, str] = {}
    by_raw: dict[str, dict] = {}
    if backend == "vsdx":
        items, top_of, by_raw = build_items_vsdx(page, rules)
    else:
        items, l2 = build_items_fodg(page, rules)
        log += l2
    items = [i for i in items if i.visible or i.relations]
    item_by_id = {i.id: i for i in items}

    # ---------- pool & lane
    used: set[str] = set()
    if backend == "vsdx":
        for it in items:
            mk = _master_key(it.master)
            if mk in pool_m:
                hdr = _child_header(it, by_raw, "top")
                ori = "vertical"
                pl = Pool(id=f"P{_num(it.id)}", name=_norm(hdr["text"]) if hdr else _norm(it.text),
                          bbox=[round(v, 2) for v in by_raw[it.id]["shape_box"]], orientation=ori,
                          header_size=round(hdr["bbox"][3], 2) if hdr else 0.0,
                          style=_style_of(_item_from_elem(hdr) if hdr else it), font=_font_of(_item_from_elem(hdr)) if hdr else Font(),
                          html=hdr.get("html", "") if hdr else "", z=it.z, confidence=vr["master_confidence"],
                          evidence=[f"master '{it.master}'"], src=[it.id] + it.children)
                P.pools.append(pl)
                used.add(it.id)
            elif mk in lane_m:
                raw = by_raw[it.id]
                box = raw["shape_box"]
                hdr = _child_header(it, by_raw, None)
                ori = lane_m[mk]
                if hdr:  # xác nhận hướng theo vị trí dải tiêu đề
                    hb = hdr["bbox"]
                    if abs(hb[2] - box[2]) < 2 and hb[3] < box[3] * 0.5:
                        ori = "vertical"
                    elif abs(hb[3] - box[3]) < 2 and hb[2] < box[2] * 0.5:
                        ori = "horizontal"
                name = _norm(hdr["text"]) if hdr else _norm(it.user.get("User.visHeadingText", ""))
                hs = (hdr["bbox"][3] if ori == "vertical" else hdr["bbox"][2]) if hdr else 0.0
                ln = Lane(id=f"L{_num(it.id)}", name=name, bbox=[round(v, 2) for v in box], orientation=ori,
                          header_size=round(hs, 2), style=_style_of(_item_from_elem(hdr) if hdr else it),
                          font=_font_of(_item_from_elem(hdr)) if hdr else Font(), html=hdr.get("html", "") if hdr else "",
                          z=it.z, confidence=vr["master_confidence"], evidence=[f"master '{it.master}'"],
                          src=[it.id] + it.children)
                ln.members = [m for m in it.relations.get("1", [])]  # type: ignore[attr-defined]
                P.lanes.append(ln)
                used.add(it.id)
        for pl in P.pools:
            pl.orientation = next((l.orientation for l in P.lanes if contains_box(pl.bbox, l.bbox, 3)), pl.orientation)
            for ln in P.lanes:
                if ln.pool is None and contains_box(pl.bbox, ln.bbox, 3):
                    ln.pool = pl.id
        if not P.lanes:  # vẽ lane bằng hình chữ nhật thường → dò hình học
            pools, lanes, u2, l3 = detect_swimlanes([i for i in items if not i.one_d], rules, page_box)
            P.pools += pools
            P.lanes += lanes
            used |= u2
            log += l3
    else:
        pools, lanes, u2, l3 = detect_swimlanes(items, rules, page_box)
        P.pools, P.lanes = pools, lanes
        used |= u2
        log += l3
    # lane không có pool → tạo khung chứa vô hình để các lane xếp liền khối trong draw.io
    for orient in ("vertical", "horizontal"):
        loose = [l for l in P.lanes if l.pool is None and l.orientation == orient]
        if loose:
            pl = Pool(id=f"P{_num(loose[0].id)}", name="", bbox=[round(v, 2) for v in bbox_union([l.bbox for l in loose])],
                      orientation=orient, header_size=0.0, style=Style(fill=None, stroke=None), z=min(l.z for l in loose),
                      confidence=0.9, evidence=["không có pool trong bản gốc: tạo khung chứa vô hình để lane liền khối"],
                      src=[])
            P.pools.append(pl)
            for l in loose:
                l.pool = pl.id
    # thứ tự lane
    for orient in ("vertical", "horizontal"):
        ls = sorted([l for l in P.lanes if l.orientation == orient], key=lambda l: l.bbox[0] if orient == "vertical" else l.bbox[1])
        for k, l in enumerate(ls):
            l.order = k

    # ---------- phân loại item còn lại
    edges_src: list[Item] = []
    notes_leader: list[tuple[Node, dict, Item]] = []
    for it in items:
        if it.id in used:
            continue
        mk = _master_key(it.master)
        if backend == "vsdx" and mk in ignore_m:
            if it.closed_subpaths or it.open_subpaths:
                P.nodes.append(_make_node(it, "decor", "custom", 0.95, [f"master cấu trúc '{it.master}' giữ nguyên hình"]))
            continue
        # callout (vsdx theo master; fodg theo mẫu hình)
        if (backend == "vsdx" and mk in callouts) or (backend != "vsdx" and _looks_callout(it)):
            node, leader = _make_callout(it, mk in callouts)
            if node:
                P.nodes.append(node)
                if leader is not None:
                    notes_leader.append((node, leader, it))
                continue
        is_conn = (backend == "vsdx" and (mk in connectors or (it.one_d and it.open_subpaths and not it.closed_subpaths))) or \
                  (backend != "vsdx" and it.open_subpaths and not it.closed_subpaths)
        if is_conn:
            edges_src.append(it)
            continue
        if backend == "vsdx" and mk in vmap and vmap[mk] != "unknown":
            t = vmap[mk]
            ttype, shape, conf, ev, ori = classify_geometry(it, rules)
            if t == "terminator":
                low = _norm(it.text).lower()
                if low in [w.lower() for w in rules["classify"]["terminator_initial_words"]]:
                    t, conf = "initial", vr["master_confidence"]
                elif low in [w.lower() for w in rules["classify"]["terminator_final_words"]]:
                    t, conf = "activity_final", vr["master_confidence"]
                else:
                    t, conf = ("action", 0.6) if it.text.strip() else ("unknown", 0.4)
            else:
                conf = vr["master_confidence"]
            if t == "fork_join":
                ori = "horizontal" if it.bbox[2] >= it.bbox[3] else "vertical"
            shape = _render_shape(it, shape)
            P.nodes.append(_make_node(it, t, shape, conf, [f"master '{it.master}' → {t}"] + ev, ori))
            continue
        ttype, shape, conf, ev, ori = classify_geometry(it, rules)
        # hình không chữ nằm trong pool nhưng ngoài mọi lane (dải lề/phase của pool) → trang trí
        if ttype == "unknown" and not it.text.strip() and P.lanes and \
                any(contains_box(p.bbox, it.bbox, 3) for p in P.pools) and \
                sum(overlap_area(l.bbox, it.bbox) for l in P.lanes) < 0.05 * max(it.bbox[2] * it.bbox[3], 1):
            P.nodes.append(_make_node(it, "decor", _render_shape(it, shape), 0.85,
                                      ev + ["nằm trong pool nhưng ngoài các lane → dải trang trí của pool"]))
            continue
        if backend == "vsdx" and it.master:
            ev = [f"master '{it.master}' chưa có trong bảng ánh xạ"] + ev
        P.nodes.append(_make_node(it, ttype, _render_shape(it, shape), conf, ev, ori))

    # ---------- connector
    node_of_raw: dict[str, str] = {}
    for n in P.nodes:
        for s in n.src:
            node_of_raw[s] = n.id
    lane_ids = {s for l in P.lanes for s in l.src} | {s for p in P.pools for s in p.src}
    for it in edges_src:
        sp = max(it.open_subpaths, key=lambda s: len(s["points"]))
        pts = [list(p) for p in sp["points"]]
        if backend == "vsdx":
            pts = _orient_points(pts, it.begin, it.end)
        e = Edge(id=f"c{_num(it.id)}", points=[[round(p[0], 2), round(p[1], 2)] for p in pts], z=it.z,
                 arrow_start=it.arrow_start, arrow_end=it.arrow_end,
                 style=Style(fill=None, stroke=sp["stroke"], stroke_width=round(sp["sw"], 2), dashed=sp["dashed"]),
                 src=list(it.src))
        confs = []
        for end_name, pt in (("begin", pts[0]), ("end", pts[-1])):
            nid = None
            if backend == "vsdx" and it.glue.get(end_name):
                raw_t = it.glue[end_name]
                top = top_of.get(raw_t, raw_t)
                nid = node_of_raw.get(raw_t) or node_of_raw.get(top)
                if nid:
                    confs.append(0.98)
                    e.evidence.append(f"đầu {end_name} gắn (glue) vào {nid}")
                elif raw_t in lane_ids or top in lane_ids:
                    e.evidence.append(f"đầu {end_name} gắn vào lane/pool → để tự do")
            if not nid:
                nid, c, ev, cands = snap_end(pt, P.nodes, rules)
                confs.append(c)
                e.evidence += [f"đầu {end_name}: " + x for x in ev]
                if c < thr:
                    e.__dict__.setdefault("_cands", {})[end_name] = cands
            if end_name == "begin":
                e.source = nid
            else:
                e.target = nid
        # hướng theo mũi tên
        if it.arrow_end == "none" and it.arrow_start != "none":
            e.source, e.target = e.target, e.source
            e.points = list(reversed(e.points))
            e.arrow_start, e.arrow_end = "none", it.arrow_start
            e.evidence.append("mũi tên ở đầu Begin → đảo hướng")
        elif it.arrow_end == "none" and it.arrow_start == "none":
            e.kind = "annotation"
            confs.append(0.6)
            e.evidence.append("đường không có mũi tên")
        e.confidence = round(min(confs) if confs else 0.5, 3)
        # nhãn
        lbl = it.labels[0] if it.labels else (it if it.text.strip() else None)
        if lbl is not None and lbl.text.strip():
            e.label = lbl.text.strip("\n")
            e.html = lbl.html
            e.label_font = _font_of(lbl)
            e.label_pos = [round(v, 2) for v in center(lbl.text_bbox or lbl.bbox)]
            e.evidence.append("nhãn thuộc cùng shape/nhóm với connector")
        P.edges.append(e)

    # callout: đường dẫn → cạnh annotation
    for node, leader, it in notes_leader:
        pts = [list(p) for p in leader["points"]]
        # điểm xa ghi chú nhất là đầu chỉ vào đối tượng
        nb = node.bbox
        if dist_point_rect_border(nb, *pts[0]) < dist_point_rect_border(nb, *pts[-1]):
            pts = list(reversed(pts))
        target = None
        conf = 0.6
        ev = []
        if it.glue.get("begin"):
            raw_t = it.glue["begin"]
            target = node_of_raw.get(raw_t) or node_of_raw.get(top_of.get(raw_t, raw_t))
            if target:
                conf = 0.98
                ev.append(f"callout gắn (glue) vào {target}")
        if not target:
            target, conf, ev2, _ = snap_end(pts[0], P.nodes, rules, exclude=node.id)
            ev += ev2
        e = Edge(id=f"a{_num(it.id)}", source=node.id, target=target, points=list(reversed(pts)),
                 arrow_start="none", arrow_end="none", kind="annotation", z=it.z,
                 style=Style(fill=None, stroke=leader["stroke"], stroke_width=round(leader["sw"], 2), dashed=leader["dashed"]),
                 confidence=round(conf, 3), evidence=["đường dẫn của callout"] + ev, src=list(it.src))
        P.edges.append(e)

    # ---------- trang nền: giữ nguyên hình, không suy luận ký hiệu
    if P.background:
        for n in P.nodes:
            n.type, n.confidence = "decor", 1.0
            n.evidence.append("phần tử của trang nền")

    # ---------- nhãn guard tự do (fodg) và text tự do
    _attach_free_labels(P, rules)

    # ---------- decision / merge theo số cạnh
    for n in P.nodes:
        if n.type == "decision" and not n.text.strip():
            ins = sum(1 for e in P.edges if e.target == n.id and e.kind == "flow")
            outs = sum(1 for e in P.edges if e.source == n.id and e.kind == "flow")
            if ins >= 2 and outs == 1:
                n.type = "merge"
                n.evidence.append(f"{ins} cạnh vào, 1 cạnh ra → merge")

    # ---------- thành viên lane
    _assign_lanes(P, rules, backend, by_raw, node_of_raw)
    _align_pool_strips(P)
    P.nodes.sort(key=lambda n: n.z)
    P.edges.sort(key=lambda e: e.z)
    return P, log


def _render_shape(it: Item, shape: str) -> str:
    """Chọn hình vẽ: hình chuẩn draw.io nếu khớp chính xác, ngược lại custom (vẽ đúng hình gốc)."""
    if len(it.subpaths) != 1 or abs(it.rotation % 90) > 0.5:
        return "custom" if shape not in ("none",) else shape
    sp = it.subpaths[0]
    if not sp["closed"]:
        return "custom"
    f = shape_features(sp)
    if f["rect"]:
        return "roundrect" if it.rounding > 0.5 else "rect"
    if f["diamond"]:
        return "rhombus"
    if f["ellipse"]:
        return "ellipse"
    if f["stadium"]:
        return "stadium"
    return "custom"


def _child_header(it: Item, by_raw: dict, where) -> dict | None:
    """Shape con có text làm dải tiêu đề (lane/pool .vsdx)."""
    cands = [by_raw[c] for c in it.children if by_raw.get(c, {}).get("text", "").strip()]
    if not cands:
        return None
    if where == "top":
        return min(cands, key=lambda c: c["bbox"][1])
    return min(cands, key=lambda c: c["bbox"][2] * c["bbox"][3])


def _looks_callout(it: Item) -> bool:
    """fodg: nhóm gồm đường hở không mũi tên + text không nằm trong hình kín."""
    return bool(it.text.strip() and len(it.open_subpaths) >= 2 and not it.closed_subpaths
                and it.arrow_end == "none" and it.arrow_start == "none")


def _make_callout(it: Item, by_master: bool):
    """Tách callout thành note (thanh bên + text) và đường dẫn tới đối tượng."""
    tb = it.text_bbox or it.bbox
    subs = it.open_subpaths
    if not subs or not it.text.strip():
        return None, None
    leader = None
    if it.begin:
        leader = min(subs, key=lambda s: min(math.hypot(p[0] - it.begin[0], p[1] - it.begin[1]) for p in (s["points"][0], s["points"][-1])))
    else:
        # đường không chạm khung text là đường dẫn
        far = [s for s in subs if dist_point_rect_border(tb, *s["points"][0]) > 3 or dist_point_rect_border(tb, *s["points"][-1]) > 3]
        leader = max(far, key=lambda s: max(dist_point_rect_border(tb, *p) for p in s["points"])) if far else None
    bar = [s for s in subs if s is not leader]
    bbox = bbox_union([tb] + [bbox_of_points(s["points"]) for s in bar])
    node = _make_node(it, "note", "custom", 0.95 if by_master else 0.8,
                      [f"callout{' (master ' + it.master + ')' if it.master else ''}: thanh bên + text"], bbox=bbox,
                      subpaths=bar)
    return node, leader


def _attach_free_labels(P: Page, rules: dict) -> None:
    g = rules["geometry"]
    thr = rules["confidence_threshold"]
    flows = [e for e in P.edges if e.kind == "flow"]
    for n in list(P.nodes):
        if n.type != "text":
            continue
        cx, cy = center(n.bbox)
        # nằm trong node khác → không phải nhãn cạnh
        if any(m is not n and m.type not in ("text", "decor") and contains_box(m.bbox, n.bbox, 2) for m in P.nodes):
            continue
        dists = sorted(((dist_point_polyline(cx, cy, e.points)[0], e.id) for e in flows if not e.label), key=lambda x: x[0])
        if not dists or dists[0][0] > g["label_max_dist"]:
            continue
        e = P.edge(dists[0][1])
        conf = 0.85
        ev = [f"text '{_norm(n.text)}' cách cạnh {e.id} {dists[0][0]:.1f}px"]
        if len(dists) > 1 and dists[1][0] - dists[0][0] < g["label_ambiguity"]:
            conf = 0.5
            ev.append(f"cạnh {dists[1][1]} cũng gần ({dists[1][0]:.1f}px)")
            e.__dict__.setdefault("_label_cands", [d[1] for d in dists[:3]])
        e.label, e.html, e.label_font = n.text, n.html, n.font
        e.label_pos = [round(v, 2) for v in center(n.bbox)]
        e.evidence += ev
        e.src += n.src
        e.__dict__["_label_conf"] = conf
        if conf < thr:
            e.__dict__["_label_issue"] = ev
        P.nodes.remove(n)


def _align_pool_strips(P: Page) -> None:
    """Dải trang trí của pool nằm ngoài các lane (dải lề/phase của Visio) được căn đúng
    chiều dọc (lane cột) hoặc chiều ngang (lane hàng) theo các lane. Trong draw.io nó là
    một phần tử của stack layout nên giãn/co cùng pool như các lane."""
    for pl in P.pools:
        lanes = [l for l in P.lanes if l.pool == pl.id]
        if not lanes:
            continue
        span = bbox_union([l.bbox for l in lanes])
        vert = pl.orientation == "vertical"
        a = 1 if vert else 0          # trục cần căn: y với lane cột, x với lane hàng
        for n in P.nodes:
            if n.type != "decor" or n.lane or n.pool != pl.id:
                continue
            if sum(overlap_area(l.bbox, n.bbox) for l in lanes) >= 0.05 * max(n.bbox[2] * n.bbox[3], 1):
                continue
            old0, oldL = n.bbox[a], n.bbox[a + 2]
            if oldL < 0.5 * span[a + 2] or oldL <= 0:
                continue
            new0, newL = span[a], span[a + 2]
            if abs(new0 - old0) < 0.01 and abs(newL - oldL) < 0.01:
                continue
            k = newL / oldL
            for sp in n.geometry:
                for p in sp.points:
                    p[a] = round(new0 + (p[a] - old0) * k, 2)
            n.bbox[a], n.bbox[a + 2] = round(new0, 2), round(newL, 2)
            n.evidence.append(f"căn theo các lane ({'dọc' if vert else 'ngang'}) để giãn cùng pool trong draw.io")


def _assign_lanes(P: Page, rules: dict, backend: str, by_raw: dict, node_of_raw: dict) -> None:
    tol = rules["geometry"]["lane_boundary_tol"]
    member_of: dict[str, list[str]] = {}
    if backend == "vsdx":
        for l in P.lanes:
            for m in getattr(l, "members", []) or []:
                nid = node_of_raw.get(m)
                if nid:
                    member_of.setdefault(nid, []).append(l.id)
    for n in P.nodes:
        cx, cy = center(n.bbox)
        geo = [l for l in P.lanes if contains_point(l.bbox, cx, cy)]
        geo_lane = min(geo, key=lambda l: l.bbox[2] * l.bbox[3]) if geo else None
        crossing = [l for l in P.lanes if overlap_area(l.bbox, n.bbox) > 0 and l is not geo_lane and
                    _intrusion(n.bbox, l.bbox) > tol]
        rel = member_of.get(n.id, [])
        if rel:
            lid = rel[0] if len(rel) == 1 else (geo_lane.id if geo_lane and geo_lane.id in rel else rel[0])
            n.lane = lid
            n.evidence.append(f"thuộc lane {lid} theo quan hệ container của Visio")
            if geo_lane and geo_lane.id != lid:
                n.evidence.append(f"nhưng tâm nằm trong lane {geo_lane.id}")
                if n.type not in ("note", "text", "decor"):
                    n.confidence = min(n.confidence, 0.6)
                    n.__dict__["_lane_issue"] = [lid, geo_lane.id]
        elif geo_lane:
            n.lane = geo_lane.id
            n.evidence.append(f"tâm nằm trong lane {geo_lane.id}")
            if crossing and n.type not in ("decor", "text", "note"):
                n.__dict__["_lane_issue"] = [geo_lane.id] + [l.id for l in crossing]
                n.evidence.append("đè ranh giới lane " + ", ".join(l.id for l in crossing))
        if n.lane:
            ln = P.lane(n.lane)
            n.pool = ln.pool if ln else None
        else:
            pools = [p for p in P.pools if contains_point(p.bbox, cx, cy)]
            n.pool = pools[0].id if pools else None


def _intrusion(nb, lb) -> float:
    """Phần node lấn sang lane lb (px theo chiều nhỏ hơn)."""
    w = min(nb[0] + nb[2], lb[0] + lb[2]) - max(nb[0], lb[0])
    h = min(nb[1] + nb[3], lb[1] + lb[3]) - max(nb[1], lb[1])
    if w <= 0 or h <= 0:
        return 0.0
    return min(w, h)


# ============================================================== issues


def collect_issues(model: Model, rules: dict) -> list[Issue]:
    thr = rules["confidence_threshold"]
    issues: list[Issue] = []

    def add(page, kind, target, msg, conf, bbox, cands=None, ev=None):
        issues.append(Issue(id=f"I{len(issues) + 1:03d}", page=page.name, kind=kind, target=target, message=msg,
                            confidence=round(conf, 3), bbox=[round(v, 1) for v in bbox] if bbox else None,
                            candidates=cands or [], evidence=ev or []))
    for p in model.pages:
        for l in p.lanes:
            if l.confidence < thr:
                add(p, "lane", l.id, f"Lane '{l.name}' nhận dạng chưa chắc", l.confidence, l.bbox, ev=l.evidence)
        for n in p.nodes:
            if n.type in ("decor",):
                continue
            if n.confidence < thr and "_lane_issue" not in n.__dict__:
                add(p, "node_type", n.id, f"Không chắc loại ký hiệu của '{_norm(n.text)[:40]}' (đang là {n.type})",
                    n.confidence, n.bbox, ev=n.evidence)
            if "_lane_issue" in n.__dict__:
                add(p, "lane_membership", n.id, f"Node '{_norm(n.text)[:40]}' đè ranh giới lane", 0.5, n.bbox,
                    cands=n.__dict__["_lane_issue"], ev=n.evidence)
        for e in p.edges:
            box = bbox_of_points(e.points)
            if e.confidence < thr:
                kind = "edge_end" if (e.source is None or e.target is None or "_cands" in e.__dict__) else "edge_direction"
                cands = e.__dict__.get("_cands", {})
                add(p, kind, e.id, f"Connector {e.id}: {'đầu chưa bắt được node' if kind == 'edge_end' else 'không rõ hướng'}"
                    f" (nguồn={e.source}, đích={e.target})", e.confidence, box, cands=[cands] if cands else [], ev=e.evidence)
            if "_label_issue" in e.__dict__:
                add(p, "edge_label", e.id, f"Nhãn '{_norm(e.label)}' không rõ thuộc cạnh nào", e.__dict__.get("_label_conf", 0.5),
                    box, cands=e.__dict__.get("_label_cands", []), ev=e.__dict__["_label_issue"])
    return issues


def _strip_private(model: Model) -> None:
    for p in model.pages:
        for obj in list(p.nodes) + list(p.edges) + list(p.lanes):
            for k in [k for k in obj.__dict__ if k.startswith("_") or k in ("members",)]:
                obj.__dict__.pop(k, None)


def reconstruct(raw: dict, rules: dict) -> tuple[Model, list[Issue], list[str]]:
    model = Model(source=dict(raw.get("source", {}), backend=raw.get("backend")), scale=raw.get("scale", 96.0))
    logs = []
    for page in raw["pages"]:
        P, log = reconstruct_page(page, raw.get("backend", "fodg"), rules)
        model.pages.append(P)
        logs += [f"[{P.name}] {x}" for x in log]
    issues = collect_issues(model, rules)
    _strip_private(model)
    return model, issues, logs


def model_to_dict(model: Model) -> dict:
    from dataclasses import asdict
    return asdict(model)


def run_reconstruct(work_dir: Path, rules: dict) -> tuple[Model, list[Issue]]:
    from .model import load_json
    raw = load_json(work_dir / "raw.json")
    model, issues, logs = reconstruct(raw, rules)
    save_json(work_dir / "model.json", model_to_dict(model))
    from dataclasses import asdict
    save_json(work_dir / "issues.json", [asdict(i) for i in issues])
    (work_dir / "reconstruct.log").write_text("\n".join(logs), encoding="utf-8")
    return model, issues
