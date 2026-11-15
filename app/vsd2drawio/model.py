"""Mô hình trung gian (model.json) và các tiện ích hình học dùng chung.

Mọi tọa độ trong model đều là px tuyệt đối trên trang, gốc ở góc trên-trái,
trục y hướng xuống (giống draw.io). bbox = [x, y, w, h].
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

# Các loại node Activity Diagram
NODE_TYPES = (
    "initial", "activity_final", "flow_final", "action", "decision", "merge",
    "fork_join", "object", "note", "send_signal", "receive_signal",
    "text", "decor", "unknown",
)
# Hình dạng dùng để vẽ lại (độc lập với ngữ nghĩa)
SHAPES = ("rect", "roundrect", "stadium", "ellipse", "rhombus", "bar", "custom", "none")


@dataclass
class Font:
    family: str = "Arial"
    size: float = 11.0          # px
    color: str = "#000000"
    bold: bool = False
    italic: bool = False
    underline: bool = False
    align: str = "center"       # left|center|right
    valign: str = "middle"      # top|middle|bottom


@dataclass
class Style:
    fill: str | None = "#ffffff"   # None = không tô
    stroke: str | None = "#000000"  # None = không viền
    stroke_width: float = 1.0
    dashed: bool = False
    rounded: float = 0.0             # bán kính bo góc (px), 0 = vuông


@dataclass
class SubPath:
    """Một đường con trong hình học gốc, tọa độ px tuyệt đối trên trang."""
    points: list[list[float]] = field(default_factory=list)
    closed: bool = False
    fill: bool = True
    stroke: bool = True
    fill_color: str | None = None
    stroke_color: str | None = None
    stroke_width: float = 1.0
    dashed: bool = False


@dataclass
class Node:
    id: str
    type: str = "unknown"
    bbox: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])
    shape: str = "custom"
    text: str = ""
    html: str = ""                  # nhãn dạng HTML nhiều màu/kiểu (nếu có)
    font: Font = field(default_factory=Font)
    style: Style = field(default_factory=Style)
    rotation: float = 0.0           # độ, theo chiều kim đồng hồ (draw.io)
    flip_h: bool = False
    flip_v: bool = False
    text_vertical: bool = False
    z: int = 0
    lane: str | None = None
    pool: str | None = None
    geometry: list[SubPath] = field(default_factory=list)
    orientation: str | None = None  # fork_join: horizontal|vertical
    confidence: float = 1.0
    evidence: list[str] = field(default_factory=list)
    src: list[str] = field(default_factory=list)
    layer: str = "main"


@dataclass
class Lane:
    id: str
    name: str = ""
    bbox: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])
    orientation: str = "vertical"   # vertical = các cột cạnh nhau, tiêu đề ở trên
    pool: str | None = None
    order: int = 0
    header_size: float = 0.0
    style: Style = field(default_factory=Style)
    font: Font = field(default_factory=Font)
    html: str = ""
    z: int = 0
    confidence: float = 1.0
    evidence: list[str] = field(default_factory=list)
    src: list[str] = field(default_factory=list)


@dataclass
class Pool:
    id: str
    name: str = ""
    bbox: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])
    orientation: str = "vertical"
    header_size: float = 0.0
    style: Style = field(default_factory=Style)
    font: Font = field(default_factory=Font)
    html: str = ""
    z: int = 0
    confidence: float = 1.0
    evidence: list[str] = field(default_factory=list)
    src: list[str] = field(default_factory=list)


@dataclass
class Edge:
    id: str
    source: str | None = None
    target: str | None = None
    points: list[list[float]] = field(default_factory=list)  # gồm cả 2 đầu mút
    label: str = ""
    html: str = ""
    label_pos: list[float] | None = None   # tâm nhãn (px tuyệt đối)
    label_font: Font = field(default_factory=Font)
    arrow_start: str = "none"
    arrow_end: str = "block"
    kind: str = "flow"                     # flow|annotation
    style: Style = field(default_factory=lambda: Style(fill=None))
    z: int = 0
    confidence: float = 1.0
    evidence: list[str] = field(default_factory=list)
    src: list[str] = field(default_factory=list)


@dataclass
class Page:
    index: int
    name: str
    width: float
    height: float
    background: bool = False
    pools: list[Pool] = field(default_factory=list)
    lanes: list[Lane] = field(default_factory=list)
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)

    def node(self, nid: str) -> Node | None:
        return next((n for n in self.nodes if n.id == nid), None)

    def lane(self, lid: str) -> Lane | None:
        return next((l for l in self.lanes if l.id == lid), None)

    def pool(self, pid: str) -> Pool | None:
        return next((p for p in self.pools if p.id == pid), None)

    def edge(self, eid: str) -> Edge | None:
        return next((e for e in self.edges if e.id == eid), None)


@dataclass
class Issue:
    id: str
    page: str
    kind: str              # node_type|lane|edge_end|edge_label|edge_direction|...
    target: str            # id node/edge/lane trong model
    message: str
    confidence: float
    bbox: list[float] | None = None
    candidates: list[Any] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)


@dataclass
class Model:
    source: dict = field(default_factory=dict)
    scale: float = 96.0
    pages: list[Page] = field(default_factory=list)

    def page(self, key: str | int) -> Page | None:
        for p in self.pages:
            if str(key) == p.name or str(key) == str(p.index):
                return p
        return None


# ---------------------------------------------------------------- (de)serialize

def _from_dict(cls, data):
    if data is None:
        return None
    kwargs = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        v = data[f.name]
        t = f.type if isinstance(f.type, str) else getattr(f.type, "__name__", "")
        if t.startswith("Font"):
            v = _from_dict(Font, v)
        elif t.startswith("Style"):
            v = _from_dict(Style, v)
        elif t.startswith("list[SubPath]"):
            v = [_from_dict(SubPath, x) for x in v]
        elif t.startswith("list[Pool]"):
            v = [_from_dict(Pool, x) for x in v]
        elif t.startswith("list[Lane]"):
            v = [_from_dict(Lane, x) for x in v]
        elif t.startswith("list[Node]"):
            v = [_from_dict(Node, x) for x in v]
        elif t.startswith("list[Edge]"):
            v = [_from_dict(Edge, x) for x in v]
        elif t.startswith("list[Page]"):
            v = [_from_dict(Page, x) for x in v]
        kwargs[f.name] = v
    return cls(**kwargs)


def model_from_dict(d: dict) -> Model:
    return _from_dict(Model, d)


def issue_from_dict(d: dict) -> Issue:
    return _from_dict(Issue, d)


def to_dict(obj) -> dict:
    return asdict(obj)


def save_json(path: Path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not isinstance(data, (dict, list)):
        data = asdict(data)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def load_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_model(path: Path) -> Model:
    return model_from_dict(load_json(path))


# ---------------------------------------------------------------- hình học

def bbox_of_points(pts) -> list[float]:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    if not xs:
        return [0.0, 0.0, 0.0, 0.0]
    return [min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)]


def bbox_union(boxes) -> list[float]:
    boxes = [b for b in boxes if b]
    if not boxes:
        return [0.0, 0.0, 0.0, 0.0]
    x1 = min(b[0] for b in boxes)
    y1 = min(b[1] for b in boxes)
    x2 = max(b[0] + b[2] for b in boxes)
    y2 = max(b[1] + b[3] for b in boxes)
    return [x1, y1, x2 - x1, y2 - y1]


def center(b) -> tuple[float, float]:
    return (b[0] + b[2] / 2.0, b[1] + b[3] / 2.0)


def contains_point(b, x, y, tol=0.0) -> bool:
    return b[0] - tol <= x <= b[0] + b[2] + tol and b[1] - tol <= y <= b[1] + b[3] + tol


def contains_box(outer, inner, tol=0.0) -> bool:
    return (inner[0] >= outer[0] - tol and inner[1] >= outer[1] - tol
            and inner[0] + inner[2] <= outer[0] + outer[2] + tol
            and inner[1] + inner[3] <= outer[1] + outer[3] + tol)


def overlap_area(a, b) -> float:
    w = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    h = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return max(0.0, w) * max(0.0, h)


def dist_point_rect_border(b, x, y) -> float:
    """Khoảng cách từ điểm tới biên hình chữ nhật (0 nếu nằm đúng trên biên)."""
    x1, y1, x2, y2 = b[0], b[1], b[0] + b[2], b[1] + b[3]
    if x1 <= x <= x2 and y1 <= y <= y2:
        return min(x - x1, x2 - x, y - y1, y2 - y)
    dx = max(x1 - x, 0.0, x - x2)
    dy = max(y1 - y, 0.0, y - y2)
    return math.hypot(dx, dy)


def dist_point_segment(px, py, ax, ay, bx, by) -> tuple[float, float]:
    """Trả về (khoảng cách, tham số t trên đoạn)."""
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return math.hypot(px - ax, py - ay), 0.0
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy)), t


def dist_point_polyline(px, py, pts) -> tuple[float, float]:
    """(khoảng cách nhỏ nhất, vị trí tương đối 0..1 theo chiều dài)."""
    if len(pts) < 2:
        return (math.hypot(px - pts[0][0], py - pts[0][1]) if pts else 1e9), 0.0
    lens = [math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]) for i in range(len(pts) - 1)]
    total = sum(lens) or 1.0
    best, best_pos, acc = 1e18, 0.0, 0.0
    for i in range(len(pts) - 1):
        d, t = dist_point_segment(px, py, *pts[i], *pts[i + 1])
        if d < best:
            best, best_pos = d, (acc + t * lens[i]) / total
        acc += lens[i]
    return best, best_pos


def point_at(pts, pos) -> tuple[float, float]:
    lens = [math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]) for i in range(len(pts) - 1)]
    total = sum(lens)
    if total == 0:
        return tuple(pts[0])
    target, acc = pos * total, 0.0
    for i, L in enumerate(lens):
        if acc + L >= target and L > 0:
            t = (target - acc) / L
            return (pts[i][0] + t * (pts[i + 1][0] - pts[i][0]), pts[i][1] + t * (pts[i + 1][1] - pts[i][1]))
        acc += L
    return tuple(pts[-1])


def rnd(v, nd=2):
    if isinstance(v, (list, tuple)):
        return [rnd(x, nd) for x in v]
    if isinstance(v, float):
        return round(v, nd)
    return v
