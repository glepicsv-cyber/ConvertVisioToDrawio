"""Áp file sửa ``work/<tên>/overrides.yaml`` vào model (sau reconstruct, trước mapper).

Đây là kênh DUY NHẤT để sửa kết quả. Không sửa tay file .drawio.
Overrides chỉ đổi NGỮ NGHĨA (loại, lane, đầu nối, nhãn, text). Tọa độ luôn do
tool tính; khi tách/gộp node, bbox được tính lại từ phần tử thô (raw.json).

Định dạng (mọi mục đều có ``page`` = tên hoặc số thứ tự trang, và ``reason``):

nodes:
  - {page: Trang 1, id: n21, type: action, reason: "..."}
  - {page: 1, id: n45, lane: L8, reason: "..."}
  - {page: 1, id: n46, text: "Từ chối đơn", reason: "..."}
edges:
  - {page: 1, id: c42, source: n39, target: n113, reason: "..."}
  - {page: 1, id: c73, reverse: true, reason: "..."}
  - {page: 1, id: c128, label: "[No]", reason: "..."}     # gán/đổi nhãn
  - {page: 1, id: c9, kind: annotation, reason: "..."}
  - {page: 1, id: c10, label_from: c11, reason: "..."}     # chuyển nhãn từ cạnh khác
merge:
  - {page: 1, ids: [n68, n70], type: fork_join, reason: "..."}
split:
  - {page: 1, id: n48, parts: [{src: [e12, e13], type: action}, {src: [e14], type: note}], reason: "..."}
accept:            # giữ nguyên kết quả của tool cho issue này
  - {id: I003, reason: "..."}
  - {page: 1, target: n133, reason: "..."}   # hoặc theo đối tượng
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .model import (Edge, Model, Node, NODE_TYPES, SubPath, bbox_of_points, bbox_union, center,
                    dist_point_rect_border)

TEMPLATE = """# overrides.yaml — quyết định ngữ nghĩa cho file này (AI hoặc người viết).
# Chỉ đổi loại/lane/đầu nối/nhãn/text; KHÔNG ghi tọa độ. Xem mô tả định dạng trong
# src/vsd2drawio/overrides.py. Mỗi mục nên có 'reason' ngắn.
nodes: []
edges: []
merge: []
split: []
accept: []
"""


@dataclass
class OverrideResult:
    applied: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    resolved_targets: set[tuple[str, str]] = field(default_factory=set)   # (page, id)
    accepted_issues: set[str] = field(default_factory=set)
    count: int = 0


def load_overrides(path: Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("overrides.yaml phải là một mapping (nodes/edges/merge/split/accept)")
    return data


def ensure_template(path: Path) -> None:
    path = Path(path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(TEMPLATE, encoding="utf-8")


def _page(model: Model, key, res: OverrideResult, what: str):
    p = model.page(key if key is not None else 1)
    if p is None:
        res.errors.append(f"{what}: không có trang '{key}'")
    return p


def apply_overrides(model: Model, ov: dict, raw: dict | None = None) -> OverrideResult:
    res = OverrideResult()
    raw_pages = {p["name"]: p for p in (raw or {}).get("pages", [])}

    for o in ov.get("nodes") or []:
        p = _page(model, o.get("page"), res, f"node {o.get('id')}")
        if not p:
            continue
        n = p.node(str(o.get("id")))
        if n is None:
            res.errors.append(f"[{p.name}] không có node {o.get('id')}")
            continue
        changed = []
        if "type" in o:
            t = str(o["type"])
            if t not in NODE_TYPES:
                res.errors.append(f"[{p.name}] {n.id}: loại '{t}' không hợp lệ ({', '.join(NODE_TYPES)})")
                continue
            n.type = t
            if t == "fork_join" and not n.orientation:
                n.orientation = "horizontal" if n.bbox[2] >= n.bbox[3] else "vertical"
            changed.append(f"type={t}")
        if "lane" in o:
            lid = o["lane"]
            if lid is not None and p.lane(str(lid)) is None:
                res.errors.append(f"[{p.name}] {n.id}: không có lane {lid}")
                continue
            n.lane = str(lid) if lid is not None else None
            ln = p.lane(n.lane) if n.lane else None
            n.pool = ln.pool if ln else n.pool
            changed.append(f"lane={lid}")
        if "text" in o:
            n.text = str(o["text"])
            n.html = ""
            changed.append("text")
        if "orientation" in o:
            n.orientation = str(o["orientation"])
            changed.append(f"orientation={n.orientation}")
        n.confidence = 1.0
        n.evidence.append(f"override: {', '.join(changed)} — {o.get('reason', '')}".strip())
        res.applied.append(f"[{p.name}] node {n.id}: {', '.join(changed)}")
        res.resolved_targets.add((p.name, n.id))

    for o in ov.get("edges") or []:
        p = _page(model, o.get("page"), res, f"edge {o.get('id')}")
        if not p:
            continue
        e = p.edge(str(o.get("id")))
        if e is None:
            res.errors.append(f"[{p.name}] không có connector {o.get('id')}")
            continue
        changed = []
        if o.get("reverse"):
            e.source, e.target = e.target, e.source
            e.points = list(reversed(e.points))
            e.arrow_start, e.arrow_end = e.arrow_end, e.arrow_start
            if e.arrow_end == "none":
                e.arrow_end, e.arrow_start = e.arrow_start if e.arrow_start != "none" else "block", "none"
            changed.append("reverse")
        for end in ("source", "target"):
            if end in o:
                nid = o[end]
                if nid is not None and p.node(str(nid)) is None:
                    res.errors.append(f"[{p.name}] {e.id}: không có node {nid}")
                    continue
                setattr(e, end, str(nid) if nid is not None else None)
                changed.append(f"{end}={nid}")
        if "label_from" in o:
            other = p.edge(str(o["label_from"]))
            if other is None:
                res.errors.append(f"[{p.name}] {e.id}: không có connector {o['label_from']}")
            else:
                e.label, e.html, e.label_pos, e.label_font = other.label, other.html, other.label_pos, other.label_font
                other.label, other.html, other.label_pos = "", "", None
                res.resolved_targets.add((p.name, other.id))
                changed.append(f"label_from={other.id}")
        if "label" in o:
            e.label = str(o["label"] or "")
            e.html = ""
            if e.label and e.label_pos is None and e.points:
                mid = e.points[len(e.points) // 2]
                e.label_pos = list(mid)
            changed.append("label")
        if "kind" in o:
            e.kind = str(o["kind"])
            changed.append(f"kind={e.kind}")
        if "arrow_end" in o:
            e.arrow_end = str(o["arrow_end"])
            changed.append("arrow_end")
        e.confidence = 1.0
        e.evidence.append(f"override: {', '.join(changed)} — {o.get('reason', '')}".strip())
        res.applied.append(f"[{p.name}] edge {e.id}: {', '.join(changed)}")
        res.resolved_targets.add((p.name, e.id))

    for o in ov.get("merge") or []:
        p = _page(model, o.get("page"), res, "merge")
        if not p:
            continue
        ids = [str(x) for x in o.get("ids") or []]
        nodes = [p.node(i) for i in ids]
        if len(ids) < 2 or any(n is None for n in nodes):
            res.errors.append(f"[{p.name}] merge {ids}: cần ≥2 node có thật")
            continue
        keep = nodes[0]
        keep.bbox = [round(v, 2) for v in bbox_union([n.bbox for n in nodes])]
        keep.geometry = [g for n in nodes for g in n.geometry]
        keep.src = [s for n in nodes for s in n.src]
        keep.text = "\n".join(n.text for n in nodes if n.text.strip())
        keep.html = ""
        keep.shape = "custom"
        if o.get("type"):
            keep.type = str(o["type"])
        keep.confidence = 1.0
        keep.evidence.append(f"override: gộp {ids} — {o.get('reason', '')}")
        for n in nodes[1:]:
            p.nodes.remove(n)
            for e in p.edges:
                if e.source == n.id:
                    e.source = keep.id
                if e.target == n.id:
                    e.target = keep.id
            res.resolved_targets.add((p.name, n.id))
        res.applied.append(f"[{p.name}] merge {ids} → {keep.id}")
        res.resolved_targets.add((p.name, keep.id))

    for o in ov.get("split") or []:
        p = _page(model, o.get("page"), res, "split")
        if not p:
            continue
        n = p.node(str(o.get("id")))
        rp = raw_pages.get(p.name)
        if n is None or rp is None:
            res.errors.append(f"[{p.name}] split {o.get('id')}: không có node hoặc thiếu raw.json")
            continue
        by_id = {e["id"]: e for e in rp["elements"]}
        parts = o.get("parts") or []
        new_nodes = []
        ok = True
        for k, part in enumerate(parts, start=1):
            els = [by_id.get(str(s)) for s in part.get("src") or []]
            if not els or any(e is None for e in els):
                res.errors.append(f"[{p.name}] split {n.id}: phần {k} có src không tồn tại")
                ok = False
                break
            geom = []
            for e in els:
                for sp in e.get("subpaths", []):
                    geom.append(SubPath(points=sp["points"], closed=sp["closed"], fill=sp["fill"] and bool(e.get("fill")),
                                        stroke=sp["stroke"] and bool(e.get("stroke")), fill_color=e.get("fill"),
                                        stroke_color=e.get("stroke"), stroke_width=e.get("stroke_width", 1.0),
                                        dashed=e.get("dashed", False)))
            nn = Node(id=f"{n.id}_{k}", type=str(part.get("type", "unknown")),
                      bbox=[round(v, 2) for v in bbox_union([e["bbox"] for e in els])], shape="custom",
                      text=" ".join(e.get("text", "") for e in els if e.get("text")).strip(), font=n.font,
                      style=n.style, z=n.z, lane=n.lane, pool=n.pool, geometry=geom, confidence=1.0,
                      evidence=[f"override: tách từ {n.id} — {o.get('reason', '')}"], src=[e["id"] for e in els])
            new_nodes.append(nn)
        if not ok or not new_nodes:
            continue
        idx = p.nodes.index(n)
        p.nodes[idx:idx + 1] = new_nodes
        # nối lại các cạnh vào phần gần nhất
        for e in p.edges:
            for end, pt in (("source", e.points[0] if e.points else None), ("target", e.points[-1] if e.points else None)):
                if getattr(e, end) == n.id and pt:
                    best = min(new_nodes, key=lambda m: dist_point_rect_border(m.bbox, pt[0], pt[1]))
                    setattr(e, end, best.id)
        res.applied.append(f"[{p.name}] split {n.id} → {[m.id for m in new_nodes]}")
        res.resolved_targets.add((p.name, n.id))

    for o in ov.get("accept") or []:
        if isinstance(o, dict) and o.get("target"):
            p = _page(model, o.get("page"), res, "accept")
            if p:
                res.resolved_targets.add((p.name, str(o["target"])))
                res.applied.append(f"accept [{p.name}] {o['target']}")
            continue
        iid = str(o.get("id") if isinstance(o, dict) else o)
        res.accepted_issues.add(iid)
        res.applied.append(f"accept {iid}")

    res.count = len(res.applied)
    return res


def open_issues(issues: list[dict], res: OverrideResult) -> list[dict]:
    """Các issue còn mở sau khi áp overrides."""
    out = []
    for i in issues:
        if i["id"] in res.accepted_issues or (i["page"], i["target"]) in res.resolved_targets:
            continue
        out.append(i)
    return out
