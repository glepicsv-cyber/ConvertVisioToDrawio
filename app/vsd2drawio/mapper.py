"""Bước map: model.json → .drawio (mxfile > diagram > mxGraphModel, không nén).

- Vị trí/kích thước lấy nguyên từ model (px), không tự dàn layout.
- Pool/lane là ``swimlane``; node con đặt parent = lane với tọa độ tương đối.
- Mỗi phần tử bọc trong ``<object>`` mang metadata (vsd_kind, ad_type, vsd_id…)
  để verify đối chiếu và để người dùng xem ngữ nghĩa trong draw.io.
- Hình không khớp hình chuẩn draw.io được vẽ đúng hình gốc bằng stencil.
"""
from __future__ import annotations

import base64
import html as htmlmod
import urllib.parse
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

from .model import Edge, Font, Lane, Model, Node, Page, Pool, Style, dist_point_polyline, point_at

ARROWS = {
    "none": ("none", 0), "block": ("block", 1), "open": ("open", 0), "oval": ("oval", 1),
    "diamond": ("diamond", 1),
}


def _n(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _style(d: dict) -> str:
    return "".join(f"{k};" if v is None else f"{k}={v};" for k, v in d.items())


def font_style(f: Font) -> dict:
    fs = (1 if f.bold else 0) | (2 if f.italic else 0) | (4 if f.underline else 0)
    return {"fontFamily": f.family or "Arial", "fontSize": _n(f.size), "fontColor": f.color or "#000000",
            "fontStyle": fs, "align": f.align or "center", "verticalAlign": f.valign or "middle"}


def paint_style(s: Style, min_width: float = 1.0) -> dict:
    d = {"fillColor": s.fill or "none", "strokeColor": s.stroke or "none",
         "strokeWidth": _n(max(min_width, s.stroke_width or 0))}
    if s.dashed:
        d["dashed"] = 1
    return d


def label_html(text: str, html: str) -> str:
    if html:
        return html
    return htmlmod.escape(text or "").replace("\n", "<br>")


# ------------------------------------------------------------- stencil

def compress_stencil(xml: str) -> str:
    """Nén giống Graph.compress của draw.io: encodeURIComponent → deflateRaw → base64."""
    q = urllib.parse.quote(xml, safe="-_.!~*'()")
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return base64.b64encode(c.compress(q.encode("utf-8")) + c.flush()).decode("ascii")


def stencil_for(node: Node) -> str | None:
    x0, y0, w, h = node.bbox
    w = max(w, 0.01)
    h = max(h, 0.01)
    parts = []
    for sp in node.geometry:
        if len(sp.points) < 2 or not (sp.fill or sp.stroke):
            continue
        parts.append(f'<fillcolor color="{sp.fill_color or "none"}"/>' if sp.fill and sp.fill_color else "")
        parts.append(f'<strokecolor color="{sp.stroke_color}"/>' if sp.stroke and sp.stroke_color else "")
        parts.append(f'<dashed dashed="{1 if sp.dashed else 0}"/>')
        pts = sp.points
        seg = [f'<move x="{_n(pts[0][0] - x0)}" y="{_n(pts[0][1] - y0)}"/>']
        seg += [f'<line x="{_n(p[0] - x0)}" y="{_n(p[1] - y0)}"/>' for p in pts[1:]]
        if sp.closed:
            seg.append("<close/>")
        parts.append("<path>" + "".join(seg) + "</path>")
        if sp.closed and sp.fill and sp.fill_color and sp.stroke and sp.stroke_color:
            parts.append("<fillstroke/>")
        elif sp.closed and sp.fill and sp.fill_color:
            parts.append("<fill/>")
        elif sp.stroke and sp.stroke_color:
            parts.append("<stroke/>")
    if not parts:
        return None
    xml = (f'<shape name="vsd" w="{_n(w)}" h="{_n(h)}" aspect="variable" strokewidth="inherit">'
           f'<foreground>{"".join(parts)}</foreground></shape>')
    return compress_stencil(xml)


# ------------------------------------------------------------- styles theo loại


def node_style(n: Node) -> dict:
    d: dict = {}
    shape = n.shape
    if n.type == "text" or shape == "none":
        d.update({"text": None, "html": 1, "whiteSpace": "wrap", "strokeColor": "none", "fillColor": "none"})
        d.update(font_style(n.font))
        return d
    if shape == "custom":
        st = stencil_for(n)
        if st:
            d["shape"] = f"stencil({st})"
        else:
            d.update({"text": None, "strokeColor": "none", "fillColor": "none"})
    elif shape == "rect":
        d["rounded"] = 0
    elif shape == "roundrect":
        d.update({"rounded": 1, "absoluteArcSize": 1, "arcSize": _n(max(n.style.rounded, 1) * 2)})
    elif shape == "stadium":
        d.update({"rounded": 1, "arcSize": 50})
    elif shape == "ellipse":
        d["ellipse"] = None
    elif shape == "rhombus":
        d["rhombus"] = None
    elif shape == "bar":
        d["rounded"] = 0
    d.update({"whiteSpace": "wrap", "html": 1})
    if "text" not in d:
        d.update(paint_style(n.style))
    if n.type == "note" and shape == "custom":
        d["spacingLeft"] = 4
    d.update(font_style(n.font))
    if n.text_vertical:
        d["horizontal"] = 0
    if n.rotation:
        d["rotation"] = _n(n.rotation)
    if n.flip_h:
        d["flipH"] = 1
    if n.flip_v:
        d["flipV"] = 1
    if n.type == "initial" and shape == "ellipse":
        d["aspect"] = "fixed"
    return d


def swimlane_style(name_font: Font, st: Style, header: float, orientation: str) -> dict:
    d = {"swimlane": None, "html": 1, "whiteSpace": "wrap", "startSize": _n(max(header, 0)),
         "horizontal": 1 if orientation == "vertical" else 0, "collapsible": 0, "container": 1,
         "swimlaneFillColor": "none", "fillColor": st.fill or "none", "strokeColor": st.stroke or "none",
         "strokeWidth": _n(max(1.0, st.stroke_width or 1))}
    d.update(font_style(name_font))
    d["verticalAlign"] = "middle"
    if name_font.align == "left":
        d["spacingLeft"] = 6
    return d


def is_orthogonal(points, tol: float = 0.6) -> bool:
    """Mọi đoạn đều nằm ngang hoặc thẳng đứng (connector vuông góc kiểu Visio)."""
    return len(points) >= 2 and all(abs(a[0] - b[0]) <= tol or abs(a[1] - b[1]) <= tol
                                    for a, b in zip(points, points[1:]))


def edge_style(e: Edge) -> dict:
    s, sf = ARROWS.get(e.arrow_start, ("block", 1))
    t, tf = ARROWS.get(e.arrow_end, ("block", 1))
    # Connector vuông góc → orthogonalEdgeStyle: các điểm gãy là gợi ý đoạn (SegmentConnector),
    # kéo object thì đường vẫn vuông góc thay vì bị xéo. Đường xiên (dẫn ghi chú…) giữ thẳng.
    orth = is_orthogonal(e.points)
    d = {"edgeStyle": "orthogonalEdgeStyle" if orth else "none", "rounded": 0, "html": 1}
    if orth:
        d.update({"orthogonalLoop": 1, "jettySize": "auto"})
    d.update({"startArrow": s, "startFill": sf, "endArrow": t, "endFill": tf,
              "strokeColor": e.style.stroke or "#000000", "strokeWidth": _n(max(1.0, e.style.stroke_width or 1)),
              "labelBackgroundColor": "#ffffff", "whiteSpace": "wrap"})
    if e.style.dashed:
        d["dashed"] = 1
    lf = font_style(e.label_font)
    lf.pop("align", None)
    lf.pop("verticalAlign", None)
    d.update(lf)
    return d


# ------------------------------------------------------------- dựng mxGraphModel


def _lane_overlap_ratio(page: Page, n: Node) -> float:
    from .model import overlap_area
    a = max(n.bbox[2] * n.bbox[3], 1.0)
    return sum(overlap_area(l.bbox, n.bbox) for l in page.lanes) / a


def node_parent(page: Page, n: Node) -> str:
    """Cha của node trong draw.io (verify dùng lại hàm này).

    - Node thuộc lane → lane (tọa độ tương đối, kéo lane thì node đi theo).
    - Dải trang trí của pool nằm ngoài mọi lane và cao/rộng đúng bằng các lane (dải lề/phase
      của Visio, đã căn ở bước reconstruct) → pool, là một phần tử của stack layout nên
      giãn/co cùng pool như lane.
    - Còn lại → lớp gốc "1" (không làm hỏng stack layout của pool).
    """
    if n.lane and page.lane(n.lane):
        return n.lane
    pl = page.pool(n.pool) if n.pool else None
    if pl and n.type == "decor" and _lane_overlap_ratio(page, n) < 0.05:
        lanes = [l for l in page.lanes if l.pool == pl.id]
        if lanes:
            from .model import bbox_union
            span = bbox_union([l.bbox for l in lanes])
            a = 1 if pl.orientation == "vertical" else 0
            if abs(n.bbox[a] - span[a]) <= 1 and abs(n.bbox[a + 2] - span[a + 2]) <= 1:
                return pl.id
    return "1"


def is_frame(page: Page, n: Node) -> bool:
    """Khung không tô màu bao các node khác (vd. khung nét đứt gom nhóm):
    chỉ nhận click ở viền và nằm dưới các node bên trong để chúng sửa được."""
    from .model import contains_box
    if n.style.fill or n.text.strip() or any(sp.fill and sp.fill_color for sp in n.geometry):
        return False
    return any(m is not n and contains_box(n.bbox, m.bbox, 2) for m in page.nodes)


class _PageMapper:
    def __init__(self, page: Page):
        self.p = page
        self.origin: dict[str, tuple[float, float]] = {"1": (0.0, 0.0)}
        self.parent_of: dict[str, str] = {}

    def container_of_node(self, n: Node) -> str:
        return node_parent(self.p, n)

    def pool_layout(self, pl: Pool, children: list) -> dict:
        """Stack layout cho pool: các lane xếp liền khối, kéo/đổi cỡ một lane thì các lane
        còn lại dịch theo. Lề tính sao cho layout tái tạo đúng vị trí gốc."""
        horiz = pl.orientation == "vertical"   # lane dạng cột → xếp theo chiều ngang
        lanes = [c for c in children if isinstance(c, Lane)]
        d = {"childLayout": "stackLayout", "horizontalStack": 1 if horiz else 0, "resizeParent": 1,
             "resizeParentMax": 0, "resizeLast": 0}
        if not lanes:
            return d
        px, py, pw, ph = pl.bbox
        hs = pl.header_size
        first, last = children[0], children[-1]
        fb, lb = first.bbox, last.bbox
        if horiz:
            d["marginLeft"] = _n(max(0.0, fb[0] - px))
            d["marginRight"] = _n(max(0.0, px + pw - (lb[0] + lb[2])))
            d["marginTop"] = _n(max(0.0, lanes[0].bbox[1] - py - hs))
            d["marginBottom"] = _n(max(0.0, py + ph - (lanes[0].bbox[1] + lanes[0].bbox[3])))
        else:
            d["marginTop"] = _n(max(0.0, fb[1] - py))
            d["marginBottom"] = _n(max(0.0, py + ph - (lb[1] + lb[3])))
            d["marginLeft"] = _n(max(0.0, lanes[0].bbox[0] - px - hs))
            d["marginRight"] = _n(max(0.0, px + pw - (lanes[0].bbox[0] + lanes[0].bbox[2])))
        ax = 0 if horiz else 1
        gaps = [b.bbox[ax] - (a.bbox[ax] + a.bbox[ax + 2]) for a, b in zip(children, children[1:])]
        if any(abs(g) > 1.0 for g in gaps):
            d.update({"allowGaps": 1, "stackUnitSize": 1})
        return d

    def chain(self, cid: str) -> list[str]:
        out = [cid]
        while cid in self.parent_of:
            cid = self.parent_of[cid]
            out.append(cid)
        if out[-1] != "1":
            out.append("1")
        return out

    def build(self) -> ET.Element:
        p = self.p
        model = ET.Element("mxGraphModel", {
            "dx": "1200", "dy": "800", "grid": "1", "gridSize": "10", "guides": "1", "tooltips": "1", "connect": "1",
            "arrows": "1", "fold": "1", "page": "1", "pageScale": "1", "pageWidth": _n(p.width),
            "pageHeight": _n(p.height), "math": "0", "shadow": "0"})
        root = ET.SubElement(model, "root")
        ET.SubElement(root, "mxCell", {"id": "0"})
        ET.SubElement(root, "mxCell", {"id": "1", "parent": "0"})
        for n in p.nodes:
            self.parent_of[n.id] = self.container_of_node(n)
        done: set[str] = set()
        # pool + con trực tiếp của pool (lane, dải trang trí) theo thứ tự xếp chồng
        for pl in sorted(p.pools, key=lambda x: x.z):
            ax = 0 if pl.orientation == "vertical" else 1
            children = [l for l in p.lanes if l.pool == pl.id] + \
                [n for n in p.nodes if self.parent_of[n.id] == pl.id]
            children.sort(key=lambda c: (c.bbox[ax], c.bbox[1 - ax]))
            self.origin[pl.id] = (pl.bbox[0], pl.bbox[1])
            self.parent_of[pl.id] = "1"
            st = swimlane_style(pl.font, pl.style, pl.header_size, pl.orientation)
            st.update(self.pool_layout(pl, children))
            self._vertex(root, pl.id, "1", pl.bbox, label_html(pl.name, pl.html), st,
                         {"vsd_kind": "pool", "vsd_src": " ".join(pl.src[:3]), "confidence": pl.confidence})
            for c in children:
                if isinstance(c, Lane):
                    self._lane(root, c, pl.id)
                else:
                    self._node(root, c)
                done.add(c.id)
        # lane không thuộc pool nào
        for ln in sorted(p.lanes, key=lambda x: (x.order, x.z)):
            if ln.id not in done:
                self._lane(root, ln, "1")
        # node + edge: khung bao nhóm vẽ trước (nằm dưới), còn lại theo thứ tự z
        frames = {n.id for n in p.nodes if is_frame(p, n)}
        items = [("n", n.z, n) for n in p.nodes if n.id not in done] + [("e", e.z, e) for e in p.edges]
        items.sort(key=lambda t: (0 if t[2].id in frames else 1, t[1], 0 if t[0] == "n" else 1))
        for kind, _, obj in items:
            if kind == "n":
                self._node(root, obj, extra_style={"pointerEvents": 0} if obj.id in frames else None)
            else:
                self._edge(root, obj)
        return model

    def _lane(self, root, ln: Lane, par: str):
        self.origin[ln.id] = (ln.bbox[0], ln.bbox[1])
        self.parent_of[ln.id] = par
        self._vertex(root, ln.id, par, ln.bbox, label_html(ln.name, ln.html),
                     swimlane_style(ln.font, ln.style, ln.header_size, ln.orientation),
                     {"vsd_kind": "lane", "lane_order": ln.order, "vsd_src": " ".join(ln.src[:3]),
                      "confidence": ln.confidence})

    def _vertex(self, root, cid, parent, bbox, label, style, attrs):
        ox, oy = self.origin.get(parent, (0.0, 0.0))
        obj = ET.SubElement(root, "object", {"label": label, "id": cid,
                                             **{k: str(v) for k, v in attrs.items() if v is not None}})
        cell = ET.SubElement(obj, "mxCell", {"style": _style(style), "vertex": "1", "parent": parent})
        ET.SubElement(cell, "mxGeometry", {"x": _n(bbox[0] - ox), "y": _n(bbox[1] - oy), "width": _n(bbox[2]),
                                           "height": _n(bbox[3]), "as": "geometry"})

    def _node(self, root, n: Node, extra_style: dict | None = None):
        par = self.parent_of[n.id]
        style = node_style(n)
        if extra_style:
            style.update(extra_style)
        attrs = {"vsd_kind": "node", "ad_type": n.type, "vsd_src": " ".join(n.src[:4]),
                 "confidence": n.confidence}
        if n.lane:
            attrs["lane"] = n.lane
        if n.orientation:
            attrs["orientation"] = n.orientation
        if n.layer != "main":
            attrs["layer"] = n.layer
        self._vertex(root, n.id, par, n.bbox, label_html(n.text, n.html), style, attrs)

    def _edge(self, root, e: Edge):
        ends = [x for x in (e.source, e.target) if x and self.p.node(x)]
        if ends:
            chains = [self.chain(self.parent_of.get(x, "1")) for x in ends]
            common = [c for c in chains[0] if all(c in ch for ch in chains[1:])]
            par = common[0] if common else "1"
        else:
            par = "1"
        if len(ends) < 2:
            par = "1" if not ends else par
        ox, oy = self.origin.get(par, (0.0, 0.0))
        style = edge_style(e)
        pts = e.points
        src = self.p.node(e.source) if e.source else None
        tgt = self.p.node(e.target) if e.target else None
        if src and pts:
            style.update(self._constraint("exit", src.bbox, pts[0]))
        if tgt and pts:
            style.update(self._constraint("entry", tgt.bbox, pts[-1]))
        attrs = {"label": label_html(e.label, e.html), "id": e.id, "vsd_kind": "edge", "edge_kind": e.kind,
                 "vsd_src": " ".join(e.src[:3]), "confidence": str(e.confidence)}
        obj = ET.SubElement(root, "object", attrs)
        cattrs = {"style": _style(style), "edge": "1", "parent": par}
        if src:
            cattrs["source"] = src.id
        if tgt:
            cattrs["target"] = tgt.id
        cell = ET.SubElement(obj, "mxCell", cattrs)
        gattrs = {"relative": "1", "as": "geometry"}
        offset = None
        if e.label and e.label_pos and len(pts) >= 2:
            _, pos = dist_point_polyline(e.label_pos[0], e.label_pos[1], pts)
            px, py = point_at(pts, pos)
            gattrs["x"] = _n(pos * 2 - 1)
            gattrs["y"] = "0"
            offset = (e.label_pos[0] - px, e.label_pos[1] - py)
        geo = ET.SubElement(cell, "mxGeometry", gattrs)
        if pts:
            ET.SubElement(geo, "mxPoint", {"x": _n(pts[0][0] - ox), "y": _n(pts[0][1] - oy), "as": "sourcePoint"})
            ET.SubElement(geo, "mxPoint", {"x": _n(pts[-1][0] - ox), "y": _n(pts[-1][1] - oy), "as": "targetPoint"})
        if offset:
            ET.SubElement(geo, "mxPoint", {"x": _n(offset[0]), "y": _n(offset[1]), "as": "offset"})
        if len(pts) > 2:
            arr = ET.SubElement(geo, "Array", {"as": "points"})
            for q in pts[1:-1]:
                ET.SubElement(arr, "mxPoint", {"x": _n(q[0] - ox), "y": _n(q[1] - oy)})

    @staticmethod
    def _constraint(prefix, b, pt) -> dict:
        w = b[2] or 1.0
        h = b[3] or 1.0
        fx = min(1.0, max(0.0, (pt[0] - b[0]) / w))
        fy = min(1.0, max(0.0, (pt[1] - b[1]) / h))
        return {f"{prefix}X": f"{fx:.4f}".rstrip("0").rstrip(".") or "0",
                f"{prefix}Y": f"{fy:.4f}".rstrip("0").rstrip(".") or "0", f"{prefix}Dx": 0, f"{prefix}Dy": 0,
                f"{prefix}Perimeter": 0}


def model_to_mxfile(model: Model) -> ET.Element:
    mx = ET.Element("mxfile", {"host": "vsd2drawio", "agent": "vsd2drawio", "version": "24.0.0", "type": "device"})
    for p in model.pages:
        d = ET.SubElement(mx, "diagram", {"id": f"page-{p.index}", "name": p.name})
        d.append(_PageMapper(p).build())
    return mx


def write_drawio(model: Model, path: Path) -> Path:
    mx = model_to_mxfile(model)
    ET.indent(mx, space=" ")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ET.tostring(mx, encoding="unicode"), encoding="utf-8")
    return path
