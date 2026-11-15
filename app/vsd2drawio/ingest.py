"""Bước ingest: đọc file Visio → hình học thô (raw.json) + ảnh gốc PNG từng trang.

Hai backend:
- ``vsdx``: đọc thẳng XML trong gói zip (.vsdx) — giữ master, glue, container.
- ``fodg``: .vsd nhị phân → LibreOffice headless → Flat ODF Drawing (.fodg)
  → đọc từng phần tử hình học (mất quan hệ, cần reconstruct suy luận).

Ảnh gốc PNG luôn được render bằng LibreOffice từ file gốc (.vsd nếu có,
ngược lại .vsdx). File gốc được chép vào work/ trước khi đưa cho LibreOffice
để không có file tạm/lock nào được ghi cạnh file gốc.
"""
from __future__ import annotations

import html as htmlmod
import math
import os
import platform
import re
import shutil
import subprocess
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .model import bbox_of_points, bbox_union, save_json

# ============================================================== công cụ ngoài


def find_soffice() -> str | None:
    """Tự dò đường dẫn LibreOffice trên Windows/macOS/Linux."""
    env = os.environ.get("VSD2DRAWIO_SOFFICE")
    if env and Path(env).exists():
        return env
    for name in ("soffice", "soffice.exe", "libreoffice"):
        p = shutil.which(name)
        if p:
            return p
    cands = []
    sysname = platform.system()
    if sysname == "Windows":
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     os.environ.get("LOCALAPPDATA", "")):
            if base:
                cands.append(Path(base) / "LibreOffice" / "program" / "soffice.exe")
    elif sysname == "Darwin":
        cands.append(Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"))
    else:
        cands += [Path("/usr/bin/soffice"), Path("/usr/lib/libreoffice/program/soffice"),
                  Path("/opt/libreoffice/program/soffice"), Path("/snap/bin/libreoffice")]
    for c in cands:
        if c.exists():
            return str(c)
    return None


def find_drawio() -> str | None:
    """Tự dò draw.io Desktop (dùng CLI để render PNG khi verify)."""
    env = os.environ.get("VSD2DRAWIO_DRAWIO")
    if env and Path(env).exists():
        return env
    for name in ("draw.io", "drawio", "draw.io.exe"):
        p = shutil.which(name)
        if p:
            return p
    cands = []
    sysname = platform.system()
    if sysname == "Windows":
        for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            if base:
                cands += [Path(base) / "Programs" / "draw.io" / "draw.io.exe", Path(base) / "draw.io" / "draw.io.exe"]
    elif sysname == "Darwin":
        cands.append(Path("/Applications/draw.io.app/Contents/MacOS/draw.io"))
    else:
        cands += [Path("/opt/drawio/drawio"), Path("/usr/bin/drawio"), Path("/snap/bin/drawio")]
    for c in cands:
        if c.exists():
            return str(c)
    return None


def run_cmd(args: list[str], timeout: int = 180, cwd: str | None = None) -> subprocess.CompletedProcess:
    """Chạy lệnh có timeout; khi treo thì giết cả cây tiến trình."""
    kw = {}
    if platform.system() == "Windows":
        kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kw["start_new_session"] = True
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, **kw)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        out, err = proc.communicate()
        raise TimeoutError(f"Lệnh quá {timeout}s và đã bị dừng: {' '.join(args[:3])} ...")
    return subprocess.CompletedProcess(args, proc.returncode, out, err)


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if platform.system() == "Windows":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def soffice_convert(src: Path, fmt: str, outdir: Path, timeout: int = 240) -> Path:
    """Chuyển đổi một file bằng LibreOffice headless (profile riêng nằm trong work/)."""
    return soffice_convert_many([src], fmt, outdir, timeout)[0]


def soffice_convert_many(srcs: list[Path], fmt: str, outdir: Path, timeout: int = 300,
                         profile_dir: Path | None = None) -> list[Path]:
    """Chuyển nhiều file trong một lần gọi LibreOffice.

    Dùng profile riêng của tool (không đụng profile của người dùng). Lần đầu chạy
    với profile mới LibreOffice có thể tự khởi động lại và bỏ qua lệnh chuyển →
    tự thử lại một lần.
    """
    soffice = find_soffice()
    if not soffice:
        raise RuntimeError("Không tìm thấy LibreOffice (soffice). Cài: winget install --id TheDocumentFoundation.LibreOffice -e")
    outdir.mkdir(parents=True, exist_ok=True)
    profile = Path(profile_dir) if profile_dir else outdir / ".lo_profile"
    profile.mkdir(parents=True, exist_ok=True)
    ext = fmt.split(":")[0]
    outs = [outdir / (s.stem + "." + ext) for s in srcs]
    res = None
    for _attempt in range(2):
        missing = [s for s, o in zip(srcs, outs) if not o.exists()]
        if not missing:
            break
        args = [soffice, f"-env:UserInstallation={profile.resolve().as_uri()}", "--headless", "--norestore",
                "--nolockcheck", "--nodefault", "--convert-to", fmt, "--outdir", str(outdir)] + [str(s) for s in missing]
        res = run_cmd(args, timeout=timeout)
    bad = [o.name for o in outs if not o.exists()]
    if bad:
        err = res.stderr.decode(errors="ignore")[:300] if res else ""
        raise RuntimeError(f"LibreOffice không tạo được {', '.join(bad)}: {err}")
    return outs


# ============================================================== nguồn


@dataclass
class SourceSet:
    """Một sơ đồ cần chuyển: có thể gồm .vsdx (cấu trúc) và/hoặc .vsd (bản gốc)."""
    name: str                 # tên đầu ra (không đuôi)
    rel_dir: str              # thư mục con tương đối so với thư mục vào ("" nếu ở gốc)
    vsdx: Path | None = None
    vsd: Path | None = None
    note: str = ""

    @property
    def key(self) -> str:
        return f"{self.rel_dir}/{self.name}".lstrip("/")

    @property
    def sources(self) -> list[Path]:
        return [p for p in (self.vsd, self.vsdx) if p]

    @property
    def original(self) -> Path:
        return self.vsd or self.vsdx


VISIO_EXT = {".vsd", ".vsdx"}


def discover(in_dir: Path) -> list[SourceSet]:
    """Tìm các sơ đồ trong thư mục vào và ghép cặp .vsd ↔ .vsdx.

    Quy tắc ghép (theo từng thư mục):
    1. Cùng tên (không phân biệt hoa thường) → một cặp.
    2. Còn đúng 1 .vsd và 1 .vsdx chưa ghép trong cùng thư mục project
       → coi là cùng một sơ đồ (tên đầu ra lấy theo file .vsd gốc).
    3. Còn lại: mỗi file là một sơ đồ riêng.
    """
    in_dir = Path(in_dir)
    by_dir: dict[Path, list[Path]] = {}
    for p in sorted(in_dir.rglob("*")):
        if p.is_file() and p.suffix.lower() in VISIO_EXT and not p.name.startswith(("~$", ".~lock")):
            by_dir.setdefault(p.parent, []).append(p)
    out: list[SourceSet] = []
    for d, files in sorted(by_dir.items()):
        rel = d.relative_to(in_dir).as_posix()
        rel = "" if rel == "." else rel
        vsd = {p.stem.lower(): p for p in files if p.suffix.lower() == ".vsd"}
        vsdx = {p.stem.lower(): p for p in files if p.suffix.lower() == ".vsdx"}
        for k in sorted(set(vsd) & set(vsdx)):
            out.append(SourceSet(vsd[k].stem, rel, vsdx=vsdx.pop(k), vsd=vsd.pop(k), note="ghép theo tên"))
        if len(vsd) == 1 and len(vsdx) == 1 and rel:
            (_, a), (_, b) = vsd.popitem(), vsdx.popitem()
            out.append(SourceSet(a.stem, rel, vsdx=b, vsd=a, note=f"ghép theo thư mục project '{rel}'"))
        for p in vsd.values():
            out.append(SourceSet(p.stem, rel, vsd=p, note="chỉ có .vsd"))
        for p in vsdx.values():
            out.append(SourceSet(p.stem, rel, vsdx=p, note="chỉ có .vsdx"))
    return out


def source_for_file(path: Path, in_dir: Path) -> SourceSet:
    """Tìm SourceSet chứa một file cụ thể (để chạy convert một file)."""
    path = Path(path).resolve()
    for s in discover(in_dir):
        if any(p.resolve() == path for p in s.sources):
            return s
    try:
        rel = path.parent.relative_to(Path(in_dir).resolve()).as_posix()
        rel = "" if rel == "." else rel
    except ValueError:
        rel = ""
    if path.suffix.lower() == ".vsdx":
        return SourceSet(path.stem, rel, vsdx=path)
    return SourceSet(path.stem, rel, vsd=path)


# ============================================================== tiện ích chung

ARROW_OPEN = {1, 3, 9, 16, 17, 18, 19}
ARROW_OVAL = {10, 20, 21}
ARROW_DIAMOND = {22, 41}


def visio_arrow(code) -> str:
    try:
        c = int(float(code))
    except (TypeError, ValueError):
        return "none"
    if c == 0:
        return "none"
    if c in ARROW_OPEN:
        return "open"
    if c in ARROW_OVAL:
        return "oval"
    if c in ARROW_DIAMOND:
        return "diamond"
    return "block"


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _color(v) -> str | None:
    if not v:
        return None
    v = str(v).strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", v):
        return v.lower()
    if re.fullmatch(r"#[0-9a-fA-F]{3}", v):
        return "#" + "".join(c * 2 for c in v[1:]).lower()
    return None


# Bảng màu chỉ số Visio (dùng khi màu ghi dạng số)
VISIO_COLORS = ["#000000", "#ffffff", "#ff0000", "#00ff00", "#0000ff", "#ffff00", "#ff00ff", "#00ffff",
                "#800000", "#008000", "#000080", "#808000", "#800080", "#008080", "#c0c0c0", "#e6e6e6",
                "#cdcdcd", "#b3b3b3", "#9a9a9a", "#808080", "#666666", "#4d4d4d", "#333333", "#1a1a1a"]


def _vcolor(v) -> str | None:
    c = _color(v)
    if c:
        return c
    try:
        i = int(float(v))
        if 0 <= i < len(VISIO_COLORS):
            return VISIO_COLORS[i]
    except (TypeError, ValueError):
        pass
    return None


def _arc3(p0, pc, p1, n=16):
    """Các điểm trên cung tròn đi qua p0 → pc → p1."""
    (x1, y1), (x2, y2), (x3, y3) = p0, pc, p1
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return [p1]
    ux = ((x1 * x1 + y1 * y1) * (y2 - y3) + (x2 * x2 + y2 * y2) * (y3 - y1) + (x3 * x3 + y3 * y3) * (y1 - y2)) / d
    uy = ((x1 * x1 + y1 * y1) * (x3 - x2) + (x2 * x2 + y2 * y2) * (x1 - x3) + (x3 * x3 + y3 * y3) * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a1 = math.atan2(y1 - uy, x1 - ux)
    a2 = math.atan2(y2 - uy, x2 - ux)
    a3 = math.atan2(y3 - uy, x3 - ux)

    def norm(a):
        while a < 0:
            a += 2 * math.pi
        while a >= 2 * math.pi:
            a -= 2 * math.pi
        return a
    ccw_mid = norm(a2 - a1)
    ccw_end = norm(a3 - a1)
    if ccw_mid <= ccw_end:
        sweep = ccw_end
    else:
        sweep = ccw_end - 2 * math.pi
    pts = []
    for i in range(1, n + 1):
        a = a1 + sweep * i / n
        pts.append((ux + r * math.cos(a), uy + r * math.sin(a)))
    pts[-1] = p1
    return pts


def _bezier(p0, c1, c2, p1, n=12):
    out = []
    for i in range(1, n + 1):
        t = i / n
        mt = 1 - t
        out.append((mt ** 3 * p0[0] + 3 * mt * mt * t * c1[0] + 3 * mt * t * t * c2[0] + t ** 3 * p1[0],
                    mt ** 3 * p0[1] + 3 * mt * mt * t * c1[1] + 3 * mt * t * t * c2[1] + t ** 3 * p1[1]))
    return out


def _quad(p0, c, p1, n=10):
    out = []
    for i in range(1, n + 1):
        t = i / n
        mt = 1 - t
        out.append((mt * mt * p0[0] + 2 * mt * t * c[0] + t * t * p1[0], mt * mt * p0[1] + 2 * mt * t * c[1] + t * t * p1[1]))
    return out


def _is_axis_rect(pts, tol=0.6) -> bool:
    """4 đỉnh, mọi cạnh song song trục."""
    q = _dedupe(pts, 1e-3)
    if len(q) > 1 and math.hypot(q[0][0] - q[-1][0], q[0][1] - q[-1][1]) < tol:
        q = q[:-1]
    if len(q) != 4:
        return False
    for i in range(4):
        a, b = q[i], q[(i + 1) % 4]
        if abs(a[0] - b[0]) > tol and abs(a[1] - b[1]) > tol:
            return False
    return True


def _dedupe(pts, tol=1e-6):
    out = []
    for p in pts:
        if not out or abs(out[-1][0] - p[0]) > tol or abs(out[-1][1] - p[1]) > tol:
            out.append(p)
    return out


def _classify_kind(subpaths, has_ellipse=False) -> str:
    vis = [s for s in subpaths if s["points"]]
    if not vis:
        return "text"
    if has_ellipse and len(vis) == 1:
        return "ellipse"
    if all(not s["closed"] for s in vis):
        if len(vis) == 1 and len(vis[0]["points"]) == 2:
            return "line"
        return "path"
    if len(vis) == 1 and _is_axis_rect(vis[0]["points"]):
        return "rect"
    if len(vis) == 1 and len(_dedupe(vis[0]["points"])) <= 12:
        return "polygon"
    return "path"


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


# ============================================================== VSDX

NS = "{http://schemas.microsoft.com/office/visio/2012/main}"
RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_RNS = "{http://schemas.openxmlformats.org/package/2006/relationships}"

LINE_CELLS = {"LineWeight", "LineColor", "LinePattern", "BeginArrow", "EndArrow", "BeginArrowSize", "EndArrowSize",
              "Rounding", "LineCap", "LineColorTrans"}
FILL_CELLS = {"FillForegnd", "FillBkgnd", "FillPattern", "FillForegndTrans", "FillBkgndTrans",
              "ShdwForegnd", "ShdwPattern"}
TEXT_CELLS = {"LeftMargin", "RightMargin", "TopMargin", "BottomMargin", "VerticalAlign", "TextBkgnd", "TextDirection"}


class _VsdxDoc:
    def __init__(self, path: Path):
        self.z = zipfile.ZipFile(path)
        self.names = set(self.z.namelist())
        self.doc = self._xml("visio/document.xml")
        self.styles: dict[str, ET.Element] = {}
        ss = self.doc.find(NS + "StyleSheets")
        if ss is not None:
            for s in ss:
                self.styles[s.get("ID")] = s
        ds = self.doc.find(NS + "DocumentSettings")
        self.default_styles = {
            "LineStyle": ds.get("DefaultLineStyle", "0") if ds is not None else "0",
            "FillStyle": ds.get("DefaultFillStyle", "0") if ds is not None else "0",
            "TextStyle": ds.get("DefaultTextStyle", "0") if ds is not None else "0",
        }
        self.masters: dict[str, dict] = {}
        if "visio/masters/masters.xml" in self.names:
            mroot = self._xml("visio/masters/masters.xml")
            rels = self._rels("visio/masters/_rels/masters.xml.rels")
            for m in mroot:
                rel = m.find(NS + "Rel")
                target = rels.get(rel.get(RNS + "id")) if rel is not None else None
                self.masters[m.get("ID")] = {"name": m.get("NameU") or m.get("Name") or "",
                                             "file": f"visio/masters/{target}" if target else None, "root": None}

    def _xml(self, name):
        return ET.fromstring(self.z.read(name))

    def _rels(self, name):
        if name not in self.names:
            return {}
        return {r.get("Id"): r.get("Target") for r in self._xml(name)}

    def master_shapes(self, mid: str) -> dict[str, ET.Element]:
        m = self.masters.get(mid)
        if not m or not m["file"] or m["file"] not in self.names:
            return {}
        if m["root"] is None:
            root = self._xml(m["file"])
            shapes = {}
            top = root.find(NS + "Shapes")
            m["top"] = list(top) if top is not None else []
            for s in root.iter(NS + "Shape"):
                shapes[s.get("ID")] = s
            m["root"] = shapes
        return m["root"]

    def master_top(self, mid: str) -> ET.Element | None:
        self.master_shapes(mid)
        m = self.masters.get(mid)
        tops = m.get("top") if m else None
        return tops[0] if tops else None

    def pages(self):
        root = self._xml("visio/pages/pages.xml")
        rels = self._rels("visio/pages/_rels/pages.xml.rels")
        out = []
        for p in root:
            rel = p.find(NS + "Rel")
            target = rels.get(rel.get(RNS + "id")) if rel is not None else None
            out.append((p, f"visio/pages/{target}" if target else None))
        return out


class _VShape:
    """Shape Visio có kế thừa: instance → master shape → stylesheet."""

    def __init__(self, doc: _VsdxDoc, el: ET.Element, master_id: str | None):
        self.doc = doc
        self.el = el
        self.master_id = el.get("Master") or master_id
        self.mel = None
        if self.master_id:
            if el.get("MasterShape"):
                self.mel = doc.master_shapes(self.master_id).get(el.get("MasterShape"))
            elif el.get("Master"):
                self.mel = doc.master_top(self.master_id)
        self._cells = None

    @property
    def id(self):
        return self.el.get("ID")

    @property
    def master_name(self) -> str | None:
        if self.el.get("Master"):
            return self.doc.masters.get(self.el.get("Master"), {}).get("name")
        return None

    def _own_cells(self, el):
        return {c.get("N"): c for c in el.findall(NS + "Cell")} if el is not None else {}

    def _style_attr(self, which):
        v = self.el.get(which)
        if v is None and self.mel is not None:
            v = self.mel.get(which)
        return v if v is not None else self.doc.default_styles[which]

    def _style_cell(self, sid, name, which, depth=0):
        if sid is None or depth > 10:
            return None
        st = self.doc.styles.get(sid)
        if st is None:
            return None
        for c in st.findall(NS + "Cell"):
            if c.get("N") == name:
                return c.get("V")
        return self._style_cell(st.get(which), name, which, depth + 1)

    def cell(self, name, default=None):
        for el in (self.el, self.mel):
            if el is None:
                continue
            for c in el.findall(NS + "Cell"):
                if c.get("N") == name and c.get("V") is not None:
                    return c.get("V")
        which = "LineStyle" if name in LINE_CELLS else "FillStyle" if name in FILL_CELLS else \
            "TextStyle" if name in TEXT_CELLS else None
        if which:
            v = self._style_cell(self._style_attr(which), name, which)
            if v is not None:
                return v
        return default

    def fcell(self, name, default=0.0):
        return _f(self.cell(name), default)

    def sections(self, sname) -> dict[str, dict]:
        """Section đã gộp kế thừa: {IX: {'cells':{}, 'rows':{IX:{'T':..,'cells':{}}}}}."""
        merged: dict[str, dict] = {}
        for el in (self.mel, self.el):
            if el is None:
                continue
            for sec in el.findall(NS + "Section"):
                if sec.get("N") != sname:
                    continue
                six = sec.get("IX", "0")
                if sec.get("Del") == "1":
                    merged.pop(six, None)
                    continue
                tgt = merged.setdefault(six, {"cells": {}, "rows": {}})
                for c in sec.findall(NS + "Cell"):
                    tgt["cells"][c.get("N")] = c.get("V")
                for i, r in enumerate(sec.findall(NS + "Row")):
                    rix = r.get("IX") or r.get("N") or str(i)
                    if r.get("Del") == "1":
                        tgt["rows"].pop(rix, None)
                        continue
                    row = tgt["rows"].setdefault(rix, {"T": r.get("T"), "cells": {}})
                    if r.get("T"):
                        row["T"] = r.get("T")
                    for c in r.findall(NS + "Cell"):
                        row["cells"][c.get("N")] = c.get("V") if c.get("V") is not None else c.get("F")
        return merged

    def text_el(self):
        t = self.el.find(NS + "Text")
        if t is None and self.mel is not None:
            t = self.mel.find(NS + "Text")
        return t

    def style_sheet_char(self):
        """Ký tự mặc định từ stylesheet (khi không có section Character)."""
        sid = self._style_attr("TextStyle")
        for _ in range(10):
            st = self.doc.styles.get(sid)
            if st is None:
                return {}
            for sec in st.findall(NS + "Section"):
                if sec.get("N") == "Character":
                    r = sec.find(NS + "Row")
                    if r is not None:
                        return {c.get("N"): c.get("V") for c in r.findall(NS + "Cell")}
            sid = st.get("TextStyle")
        return {}


def _geom_local(vs: _VShape, w: float, h: float):
    """Chuyển các section Geometry thành danh sách subpath (tọa độ cục bộ, inch, y lên)."""
    out = []
    has_ellipse = False
    secs = vs.sections("Geometry")
    for six in sorted(secs, key=lambda k: int(k) if k.isdigit() else 0):
        sec = secs[six]
        sc = sec["cells"]
        if _f(sc.get("NoShow")) == 1:
            continue
        no_fill = _f(sc.get("NoFill")) == 1
        no_line = _f(sc.get("NoLine")) == 1
        cur: list[tuple] = []
        subs: list[list[tuple]] = []
        start = None

        def flush():
            nonlocal cur
            if len(cur) > 1:
                subs.append(cur)
            cur = []
        rows = sec["rows"]
        for rix in sorted(rows, key=lambda k: int(k) if str(k).isdigit() else 0):
            r = rows[rix]
            t = r["T"] or ""
            c = r["cells"]
            X, Y = _f(c.get("X")), _f(c.get("Y"))
            if t.startswith("Rel"):
                X, Y = X * w, Y * h
            if t in ("MoveTo", "RelMoveTo"):
                flush()
                cur = [(X, Y)]
                start = (X, Y)
            elif t in ("LineTo", "RelLineTo"):
                if not cur:
                    cur = [start or (0.0, 0.0)]
                cur.append((X, Y))
            elif t == "ArcTo":
                p0 = cur[-1] if cur else (0.0, 0.0)
                A = _f(c.get("A"))
                if abs(A) < 1e-9:
                    cur.append((X, Y))
                else:
                    dx, dy = X - p0[0], Y - p0[1]
                    L = math.hypot(dx, dy) or 1.0
                    mid = ((p0[0] + X) / 2 + A * dy / L, (p0[1] + Y) / 2 - A * dx / L)
                    cur += _arc3(p0, mid, (X, Y))
            elif t in ("EllipticalArcTo", "RelEllipticalArcTo"):
                p0 = cur[-1] if cur else (0.0, 0.0)
                A, B = _f(c.get("A")), _f(c.get("B"))
                if t.startswith("Rel"):
                    A, B = A * w, B * h
                C, D = _f(c.get("C")), _f(c.get("D"), 1.0) or 1.0
                cosc, sinc = math.cos(-C), math.sin(-C)

                def fw(p):
                    x, y = p[0] * cosc - p[1] * sinc, p[0] * sinc + p[1] * cosc
                    return (x / D, y)

                def bw(p):
                    x, y = p[0] * D, p[1]
                    return (x * math.cos(C) - y * math.sin(C), x * math.sin(C) + y * math.cos(C))
                pts = _arc3(fw(p0), fw((A, B)), fw((X, Y)))
                cur += [bw(p) for p in pts]
                cur[-1] = (X, Y)
            elif t in ("RelCubBezTo", "CubicBezTo"):
                p0 = cur[-1] if cur else (0.0, 0.0)
                A, B, C2, D2 = _f(c.get("A")), _f(c.get("B")), _f(c.get("C")), _f(c.get("D"))
                if t.startswith("Rel"):
                    A, B, C2, D2 = A * w, B * h, C2 * w, D2 * h
                cur += _bezier(p0, (A, B), (C2, D2), (X, Y))
            elif t in ("RelQuadBezTo",):
                p0 = cur[-1] if cur else (0.0, 0.0)
                cur += _quad(p0, (_f(c.get("A")) * w, _f(c.get("B")) * h), (X, Y))
            elif t == "Ellipse":
                flush()
                A, B, C2, D2 = _f(c.get("A")), _f(c.get("B")), _f(c.get("C")), _f(c.get("D"))
                ax, ay = A - X, B - Y
                bx, by = C2 - X, D2 - Y
                pts = [(X + ax * math.cos(a) + bx * math.sin(a), Y + ay * math.cos(a) + by * math.sin(a))
                       for a in (2 * math.pi * i / 48 for i in range(49))]
                subs.append(pts)
                has_ellipse = True
            elif t == "NURBSTo":
                # Xấp xỉ: đi qua các điểm điều khiển trong công thức NURBS(...)
                e = str(c.get("E") or "")
                nums = [_f(v) for v in re.findall(r"-?\d+(?:\.\d+)?(?:[eE]-?\d+)?", e)]
                if len(nums) >= 4:
                    xt, yt = nums[2], nums[3]
                    ctrl = nums[4:]
                    for i in range(0, len(ctrl) - 3, 4):
                        px, py = ctrl[i], ctrl[i + 1]
                        if xt == 0:
                            px *= w
                        if yt == 0:
                            py *= h
                        cur.append((px, py))
                cur.append((X, Y))
            elif t == "PolylineTo":
                a = str(c.get("A") or "")
                nums = [_f(v) for v in re.findall(r"-?\d+(?:\.\d+)?(?:[eE]-?\d+)?", a)]
                if len(nums) >= 2:
                    xt, yt = nums[0], nums[1]
                    pts = nums[2:]
                    for i in range(0, len(pts) - 1, 2):
                        px, py = pts[i], pts[i + 1]
                        cur.append((px * w if xt == 0 else px, py * h if yt == 0 else py))
                cur.append((X, Y))
            elif t in ("SplineStart", "SplineKnot"):
                cur.append((X, Y))
            # InfiniteLine và loại khác: bỏ qua
        flush()
        for sp in subs:
            closed = len(sp) > 2 and math.hypot(sp[0][0] - sp[-1][0], sp[0][1] - sp[-1][1]) < 1e-6
            out.append({"points": sp, "closed": closed, "fill": (not no_fill) and closed, "stroke": not no_line})
    return out, has_ellipse


class _Xf:
    """Biến đổi cục bộ → cha cho 1 shape Visio."""

    def __init__(self, vs: _VShape):
        self.pinx, self.piny = vs.fcell("PinX"), vs.fcell("PinY")
        self.w, self.h = vs.fcell("Width"), vs.fcell("Height")
        self.lpx, self.lpy = vs.fcell("LocPinX", self.w / 2), vs.fcell("LocPinY", self.h / 2)
        self.ang = vs.fcell("Angle")
        self.fx = vs.fcell("FlipX") == 1
        self.fy = vs.fcell("FlipY") == 1

    def apply(self, p):
        x, y = p
        if self.fx:
            x = self.w - x
        if self.fy:
            y = self.h - y
        x -= self.lpx
        y -= self.lpy
        c, s = math.cos(self.ang), math.sin(self.ang)
        return (self.pinx + x * c - y * s, self.piny + x * s + y * c)


def _text_runs(vs: _VShape):
    """Đọc text + các đoạn định dạng (cp) → (plain, runs[(text, char_ix, para_ix)])."""
    t = vs.text_el()
    if t is None:
        return "", []
    runs = []
    cp, pp = "0", "0"

    def add(s):
        if s:
            runs.append([s, cp, pp])
    add(t.text)
    for child in t:
        tag = child.tag.replace(NS, "")
        if tag == "cp":
            cp = child.get("IX", "0")
        elif tag == "pp":
            pp = child.get("IX", "0")
        elif tag == "fld":
            add("".join(child.itertext()))
        add(child.tail)
    plain = "".join(r[0] for r in runs)
    plain = plain.replace("\u2028", "\n").replace("\r", "")
    # bỏ ký tự điều khiển (trừ xuống dòng)
    plain = "".join(ch for ch in plain if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    return plain.strip("\n"), runs


def _char_fmt(vs: _VShape, scale: float):
    chars = vs.sections("Character")
    rows = chars.get("0", {}).get("rows", {}) if chars else {}
    base = vs.style_sheet_char()
    fmts = {}
    for ix, r in rows.items():
        c = dict(base)
        c.update({k: v for k, v in r["cells"].items() if v is not None})
        fmts[str(ix)] = c
    if not fmts:
        fmts["0"] = base
    out = {}
    for ix, c in fmts.items():
        st = int(_f(c.get("Style"), 0))
        out[ix] = {
            "family": c.get("Font") if c.get("Font") and not re.fullmatch(r"\d+", str(c.get("Font"))) else "Arial",
            "size": round(_f(c.get("Size"), 0.1111) * scale, 2),
            "color": _vcolor(c.get("Color")) or "#000000",
            "bold": bool(st & 1), "italic": bool(st & 2), "underline": bool(st & 4),
        }
    return out


def _para_align(vs: _VShape) -> str:
    paras = vs.sections("Paragraph")
    rows = paras.get("0", {}).get("rows", {}) if paras else {}
    r = rows.get("0") or (next(iter(rows.values())) if rows else None)
    h = int(_f(r["cells"].get("HorzAlign"), 1)) if r else 1
    return {0: "left", 1: "center", 2: "right", 3: "left"}.get(h, "center")


def _runs_html(runs, fmts, base) -> str:
    if not runs:
        return ""
    parts = []
    multi = len({json_key(fmts.get(r[1], base)) for r in runs if r[0].strip()}) > 1
    if not multi:
        return ""
    for text, cp, _pp in runs:
        f = fmts.get(cp, base)
        s = htmlmod.escape(text.replace("\r", "")).replace("\n", "<br>")
        if not s:
            continue
        style = f"color:{f['color']};"
        if f.get("bold"):
            s = f"<b>{s}</b>"
        if f.get("italic"):
            s = f"<i>{s}</i>"
        if f.get("underline"):
            s = f"<u>{s}</u>"
        parts.append(f'<span style="{style}">{s}</span>')
    h = "".join(parts)
    while h.endswith("<br></span>"):
        h = h[: -len("<br></span>")] + "</span>"
    return h


def json_key(d) -> str:
    return repr(sorted(d.items())) if isinstance(d, dict) else repr(d)


def read_vsdx(path: Path, scale: float = 96.0) -> dict:
    """Đọc .vsdx → raw dict (tọa độ px, gốc trên-trái)."""
    doc = _VsdxDoc(path)
    pages = []
    for pidx, (pel, pfile) in enumerate(doc.pages(), start=1):
        ps = pel.find(NS + "PageSheet")
        pcells = {c.get("N"): c.get("V") for c in ps.findall(NS + "Cell")} if ps is not None else {}
        pw_in, ph_in = _f(pcells.get("PageWidth"), 8.5), _f(pcells.get("PageHeight"), 11)
        page = {"index": pidx, "name": pel.get("Name") or pel.get("NameU") or f"Page-{pidx}",
                "background": pel.get("Background") == "1", "back_page": pel.get("BackPage"),
                "visio_page_id": pel.get("ID"),
                "width": round(pw_in * scale, 2), "height": round(ph_in * scale, 2),
                "width_in": pw_in, "height_in": ph_in, "elements": []}
        if not pfile or pfile not in doc.names:
            pages.append(page)
            continue
        root = doc._xml(pfile)
        z = [0]
        seen_ids: set[str] = set()

        def to_px(p):
            return [round(p[0] * scale, 3), round((ph_in - p[1]) * scale, 3)]

        def walk(el, parent_chain: list[_Xf], parent_id, master_ctx):
            vs = _VShape(doc, el, master_ctx)
            xf = _Xf(vs)
            chain = [xf] + parent_chain

            def tf(p):
                for x in chain:
                    p = x.apply(p)
                return p
            subs_local, has_ell = _geom_local(vs, xf.w, xf.h)
            subpaths = []
            for sp in subs_local:
                subpaths.append({"points": [to_px(tf(p)) for p in sp["points"]], "closed": sp["closed"],
                                 "fill": sp["fill"], "stroke": sp["stroke"]})
            corners = [to_px(tf(p)) for p in ((0, 0), (xf.w, 0), (xf.w, xf.h), (0, xf.h))]
            box = bbox_of_points(corners)
            gb = bbox_of_points([p for s in subpaths for p in s["points"]]) if subpaths else box
            # tổng góc xoay (độ, ngược chiều kim đồng hồ trong hệ y-lên)
            ang = sum(x.ang for x in chain)
            # text
            plain, runs = _text_runs(vs)
            fmts = _char_fmt(vs, scale)
            base = fmts.get("0") or next(iter(fmts.values()))
            font = dict(base)
            font["align"] = _para_align(vs)
            va = int(_f(vs.cell("VerticalAlign"), 1))
            font["valign"] = {0: "top", 1: "middle", 2: "bottom"}.get(va, "middle")
            text_bbox = None
            text_angle = 0.0
            if plain.strip():
                if vs.cell("TxtWidth") is not None:
                    tw, th = vs.fcell("TxtWidth"), vs.fcell("TxtHeight")
                    tpx, tpy = vs.fcell("TxtPinX", xf.w / 2), vs.fcell("TxtPinY", xf.h / 2)
                    tlx, tly = vs.fcell("TxtLocPinX", tw / 2), vs.fcell("TxtLocPinY", th / 2)
                    ta = vs.fcell("TxtAngle")
                    c, s = math.cos(ta), math.sin(ta)
                    tc = [(tpx + (x - tlx) * c - (y - tly) * s, tpy + (x - tlx) * s + (y - tly) * c)
                          for x, y in ((0, 0), (tw, 0), (tw, th), (0, th))]
                    text_bbox = bbox_of_points([to_px(tf(p)) for p in tc])
                    text_angle = math.degrees(ang + ta)
                else:
                    text_bbox = box
                    text_angle = math.degrees(ang)
            # style
            fp = int(_f(vs.cell("FillPattern"), 1))
            ftrans = _f(vs.cell("FillForegndTrans"), 0)
            fill = _vcolor(vs.cell("FillForegnd")) if fp != 0 and ftrans < 0.99 else None
            lp = int(_f(vs.cell("LinePattern"), 1))
            stroke = _vcolor(vs.cell("LineColor")) if lp != 0 else None
            lw = _f(vs.cell("LineWeight"), 0.01) * scale
            one_d = vs.cell("BeginX") is not None and vs.cell("EndX") is not None and \
                (el.find(NS + "Cell[@N='BeginX']") is not None or (vs.mel is not None and vs.mel.find(NS + "Cell[@N='BeginX']") is not None))
            user = {}
            for sec in el.findall(NS + "Section"):
                if sec.get("N") in ("User", "Property"):
                    for r in sec.findall(NS + "Row"):
                        v = r.find(NS + "Cell[@N='Value']")
                        if v is not None and v.get("V") not in (None, ""):
                            user[f"{sec.get('N')}.{r.get('N')}"] = v.get("V")
            rel_cell = el.find(NS + "Cell[@N='Relationships']")
            relations = {}
            if rel_cell is not None and rel_cell.get("F"):
                for chunk in rel_cell.get("F").split("DEPENDSON(")[1:]:
                    m = re.match(r"\s*(\d+)\s*,", chunk)
                    if m:
                        ids = re.findall(r"Sheet\.(\d+)!", chunk)
                        relations.setdefault(m.group(1), []).extend(f"s{i}" for i in ids)
            kind = "group" if el.get("Type") == "Group" else _classify_kind(subpaths, has_ell)
            z[0] += 1
            rid = f"s{vs.id}"
            if rid in seen_ids:  # file lỗi có thể trùng ID shape
                rid = f"s{vs.id}_{z[0]}"
            seen_ids.add(rid)
            elem = {
                "id": rid, "vsdx_id": vs.id, "kind": kind, "type_attr": el.get("Type"),
                "master": vs.master_name, "name": el.get("NameU") or el.get("Name"),
                "bbox": [round(v, 3) for v in (gb if kind != "group" else box)], "shape_box": [round(v, 3) for v in box],
                "subpaths": subpaths, "fill": fill, "stroke": stroke, "stroke_width": round(max(lw, 0.0), 3),
                "dashed": lp > 1, "arrow_start": visio_arrow(vs.cell("BeginArrow")),
                "arrow_end": visio_arrow(vs.cell("EndArrow")),
                "rounding": round(_f(vs.cell("Rounding")) * scale, 3),
                "text": plain, "html": _runs_html(runs, fmts, base), "font": font,
                "text_bbox": [round(v, 3) for v in text_bbox] if text_bbox else None,
                "text_angle": round(text_angle, 3), "rotation": round(math.degrees(ang), 3),
                "flip_x": xf.fx, "flip_y": xf.fy,
                "z": z[0], "parent": parent_id, "one_d": bool(one_d),
                "hidden": False, "user": user, "relations": relations,
            }
            if one_d:
                b = (vs.fcell("BeginX"), vs.fcell("BeginY"))
                e = (vs.fcell("EndX"), vs.fcell("EndY"))
                # BeginX/EndX tính trong hệ tọa độ của cha
                for x in parent_chain:
                    b, e = x.apply(b), x.apply(e)
                elem["begin"], elem["end"] = to_px(b), to_px(e)
            page["elements"].append(elem)
            shapes = el.find(NS + "Shapes")
            if shapes is not None:
                for ch in shapes:
                    walk(ch, chain, elem["id"], vs.master_id)

        top = root.find(NS + "Shapes")
        if top is not None:
            for el in top:
                walk(el, [], None, None)
        glue = {}
        conns = root.find(NS + "Connects")
        if conns is not None:
            for c in conns:
                fc = c.get("FromCell") or ""
                end = "begin" if fc.startswith("Begin") else "end" if fc.startswith("End") else None
                if end:
                    glue.setdefault(f"s{c.get('FromSheet')}", {})[end] = f"s{c.get('ToSheet')}"
        for e in page["elements"]:
            if e["id"] in glue:
                e["glue"] = glue[e["id"]]
        pages.append(page)
    return {"backend": "vsdx", "scale": scale, "pages": pages}


# ============================================================== FODG (.vsd qua LibreOffice)

ODF = {
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "svg": "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "fo": "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
}


def _q(prefix, name):
    return "{%s}%s" % (ODF[prefix], name)


def _len_px(v, scale) -> float:
    if v is None:
        return 0.0
    m = re.fullmatch(r"\s*(-?[\d.]+(?:[eE]-?\d+)?)\s*([a-z%]*)\s*", str(v))
    if not m:
        return 0.0
    n, u = float(m.group(1)), m.group(2)
    factor = {"cm": 1 / 2.54, "mm": 1 / 25.4, "in": 1.0, "pt": 1 / 72, "pc": 1 / 6, "px": 1 / 96, "": 1 / 96}.get(u, 1 / 96)
    return n * factor * scale


class _OdfStyles:
    def __init__(self, root):
        self.by_name: dict[str, ET.Element] = {}
        for st in root.iter(_q("style", "style")):
            fam = st.get(_q("style", "family"))
            self.by_name[(fam, st.get(_q("style", "name")))] = st
        self.defaults: dict[str, dict] = {}
        for st in root.iter(_q("style", "default-style")):
            self.defaults[st.get(_q("style", "family"))] = self._props(st)
        self.markers = {m.get(_q("draw", "name")) for m in root.iter(_q("draw", "marker"))}

    @staticmethod
    def _props(st):
        d = {}
        for child in st:
            for k, v in child.attrib.items():
                local = k.split("}")[1] if "}" in k else k
                ns = k.split("}")[0][1:] if "}" in k else ""
                pfx = next((p for p, u in ODF.items() if u == ns), "")
                d[f"{pfx}:{local}"] = v
        return d

    def resolve(self, family, name) -> dict:
        chain = []
        cur = name
        seen = set()
        while cur and (family, cur) in self.by_name and cur not in seen:
            seen.add(cur)
            st = self.by_name[(family, cur)]
            chain.append(st)
            cur = st.get(_q("style", "parent-style-name"))
        d = dict(self.defaults.get(family, {}))
        for st in reversed(chain):
            d.update(self._props(st))
        return d


_PATH_TOKEN = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]|-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def _parse_svg_path(d: str):
    toks = _PATH_TOKEN.findall(d or "")
    subs, cur = [], []
    x = y = sx = sy = 0.0
    cmd = None
    i = 0
    last_ctrl = None

    def num():
        nonlocal i
        v = float(toks[i])
        i += 1
        return v
    while i < len(toks):
        t = toks[i]
        if re.fullmatch(r"[A-Za-z]", t):
            cmd = t
            i += 1
            if cmd in "Zz":
                if cur:
                    cur.append((sx, sy))
                    subs.append((cur, True))
                cur = []
                x, y = sx, sy
                continue
        if cmd is None:
            break
        rel = cmd.islower()
        c = cmd.upper()
        try:
            if c == "M":
                nx, ny = num(), num()
                if rel:
                    nx, ny = x + nx, y + ny
                if cur:
                    subs.append((cur, False))
                cur = [(nx, ny)]
                x, y, sx, sy = nx, ny, nx, ny
                cmd = "l" if rel else "L"
            elif c == "L":
                nx, ny = num(), num()
                if rel:
                    nx, ny = x + nx, y + ny
                cur.append((nx, ny))
                x, y = nx, ny
            elif c == "H":
                nx = num()
                x = x + nx if rel else nx
                cur.append((x, y))
            elif c == "V":
                ny = num()
                y = y + ny if rel else ny
                cur.append((x, y))
            elif c == "C":
                a = [num() for _ in range(6)]
                if rel:
                    a = [a[k] + (x if k % 2 == 0 else y) for k in range(6)]
                cur += _bezier((x, y), (a[0], a[1]), (a[2], a[3]), (a[4], a[5]))
                last_ctrl = (a[2], a[3])
                x, y = a[4], a[5]
            elif c == "S":
                a = [num() for _ in range(4)]
                if rel:
                    a = [a[k] + (x if k % 2 == 0 else y) for k in range(4)]
                c1 = (2 * x - last_ctrl[0], 2 * y - last_ctrl[1]) if last_ctrl else (x, y)
                cur += _bezier((x, y), c1, (a[0], a[1]), (a[2], a[3]))
                last_ctrl = (a[0], a[1])
                x, y = a[2], a[3]
            elif c == "Q":
                a = [num() for _ in range(4)]
                if rel:
                    a = [a[k] + (x if k % 2 == 0 else y) for k in range(4)]
                cur += _quad((x, y), (a[0], a[1]), (a[2], a[3]))
                x, y = a[2], a[3]
            elif c == "T":
                a = [num() for _ in range(2)]
                if rel:
                    a = [a[0] + x, a[1] + y]
                cur.append((a[0], a[1]))
                x, y = a
            elif c == "A":
                a = [num() for _ in range(7)]
                ex, ey = (a[5] + x, a[6] + y) if rel else (a[5], a[6])
                cur.append((ex, ey))  # xấp xỉ thô; hiếm gặp trong xuất của LibreOffice
                x, y = ex, ey
            else:
                i += 1
        except (IndexError, ValueError):
            break
        if c not in "CS":
            last_ctrl = None
    if cur:
        subs.append((cur, False))
    return subs


def _parse_points(s: str):
    pts = []
    for pair in (s or "").split():
        if "," in pair:
            a, b = pair.split(",", 1)
            pts.append((float(a), float(b)))
    return pts


def _parse_transform(s: str):
    """draw:transform → hàm biến đổi (đơn vị px đã đổi ở ngoài)."""
    ops = re.findall(r"(rotate|translate|scale|skewX|skewY)\s*\(([^)]*)\)", s or "")
    return [(op, [v for v in re.split(r"[\s,]+", args.strip()) if v]) for op, args in ops]


def _odf_text(frame, styles: _OdfStyles, scale):
    paras = []
    runs = []
    first_t = None
    first_p = None
    for p in frame.iter():
        if p.tag in (_q("text", "p"), _q("text", "h")):
            if first_p is None:
                first_p = p.get(_q("text", "style-name"))
            buf = []

            def walk(n):
                nonlocal first_t
                for ch in n:
                    tag = ch.tag
                    if tag == _q("text", "span"):
                        if first_t is None and "".join(ch.itertext()).strip():
                            first_t = ch.get(_q("text", "style-name"))
                        t = "".join(_iter_text(ch))
                        buf.append(t)
                        runs.append((t, ch.get(_q("text", "style-name"))))
                    elif tag == _q("text", "s"):
                        buf.append(" " * int(ch.get(_q("text", "c"), "1")))
                    elif tag == _q("text", "line-break"):
                        buf.append("\n")
                    elif tag == _q("text", "tab"):
                        buf.append("\t")
                    if ch.tail:
                        buf.append(ch.tail)
            if p.text:
                buf.append(p.text)
            walk(p)
            paras.append("".join(buf))
            runs.append(("\n", None))
    text = "\n".join(paras).strip("\n")
    tprops = styles.resolve("text", first_t) if first_t else {}
    pprops = styles.resolve("paragraph", first_p) if first_p else {}
    font = {
        "family": tprops.get("style:font-name", "Arial"),
        "size": round(_len_px(tprops.get("fo:font-size", "10pt"), scale), 2),
        "color": _color(tprops.get("fo:color")) or "#000000",
        "bold": tprops.get("fo:font-weight") == "bold",
        "italic": tprops.get("fo:font-style") == "italic",
        "underline": tprops.get("style:text-underline-style", "none") != "none",
        "align": {"start": "left", "left": "left", "end": "right", "right": "right", "justify": "left"}.get(
            pprops.get("fo:text-align", "center"), "center"),
        "valign": "middle",
    }
    # html nhiều màu
    colors = set()
    parts = []
    for t, sname in runs:
        if t == "\n":
            parts.append("<br>")
            continue
        pr = styles.resolve("text", sname) if sname else {}
        col = _color(pr.get("fo:color")) or font["color"]
        if t.strip():
            colors.add((col, pr.get("fo:font-weight") == "bold", pr.get("fo:font-style") == "italic"))
        s = htmlmod.escape(t)
        if pr.get("fo:font-weight") == "bold":
            s = f"<b>{s}</b>"
        if pr.get("fo:font-style") == "italic":
            s = f"<i>{s}</i>"
        parts.append(f'<span style="color:{col};">{s}</span>')
    h = "".join(parts)
    while h.endswith("<br>"):
        h = h[:-4]
    return text, font, (h if len(colors) > 1 else "")


def _iter_text(n):
    if n.text:
        yield n.text
    for ch in n:
        if ch.tag == _q("text", "s"):
            yield " " * int(ch.get(_q("text", "c"), "1"))
        elif ch.tag == _q("text", "line-break"):
            yield "\n"
        else:
            yield from _iter_text(ch)
        if ch.tail:
            yield ch.tail


def read_fodg(path: Path, scale: float = 96.0) -> dict:
    """Đọc Flat ODF Drawing do LibreOffice xuất → raw dict."""
    root = ET.parse(path).getroot()
    styles = _OdfStyles(root)
    pl = next(root.iter(_q("style", "page-layout-properties")), None)
    pw = _len_px(pl.get(_q("fo", "page-width")), scale) if pl is not None else 0
    ph = _len_px(pl.get(_q("fo", "page-height")), scale) if pl is not None else 0
    pages = []
    counter = [0]
    for pidx, page_el in enumerate(root.iter(_q("draw", "page")), start=1):
        page = {"index": pidx, "name": page_el.get(_q("draw", "name")) or f"Page-{pidx}",
                "background": False, "width": round(pw, 2), "height": round(ph, 2), "elements": []}
        z = [0]

        def new_id():
            counter[0] += 1
            return f"e{counter[0]}"

        def gstyle(el):
            props = styles.resolve("graphic", el.get(_q("draw", "style-name")))
            fill = None
            if props.get("draw:fill", "solid") not in ("none",):
                op = props.get("draw:opacity", "100%")
                if not op.startswith("0"):
                    fill = _color(props.get("draw:fill-color")) or "#729fcf"
            stroke = None
            dashed = False
            if props.get("draw:stroke", "solid") != "none":
                stroke = _color(props.get("svg:stroke-color")) or "#000000"
                dashed = props.get("draw:stroke") == "dash"
            sw = _len_px(props.get("svg:stroke-width", "0"), scale)
            ms = "block" if props.get("draw:marker-start") else "none"
            me = "block" if props.get("draw:marker-end") else "none"
            return fill, stroke, sw, dashed, ms, me

        def geom_xform(el):
            """Hàm đổi tọa độ viewBox → px trang."""
            x = _len_px(el.get(_q("svg", "x")), scale)
            y = _len_px(el.get(_q("svg", "y")), scale)
            w = _len_px(el.get(_q("svg", "width")), scale)
            h = _len_px(el.get(_q("svg", "height")), scale)
            vb = [float(v) for v in (el.get(_q("svg", "viewBox")) or "0 0 1 1").split()]
            sx = w / vb[2] if vb[2] else 1.0
            sy = h / vb[3] if vb[3] else 1.0
            tr = _parse_transform(el.get(_q("draw", "transform")))

            def f(p):
                px = (p[0] - vb[0]) * sx
                py = (p[1] - vb[1]) * sy
                if not tr:
                    return [round(px + x, 3), round(py + y, 3)]
                for op, args in tr:
                    if op == "rotate":
                        a = float(args[0])
                        c, s = math.cos(-a), math.sin(-a)
                        px, py = px * c - py * s, px * s + py * c
                    elif op == "translate":
                        px += _len_px(args[0], scale)
                        py += _len_px(args[1] if len(args) > 1 else "0", scale)
                    elif op == "scale":
                        px *= float(args[0])
                        py *= float(args[1] if len(args) > 1 else args[0])
                return [round(px, 3), round(py, 3)]
            return f

        def add(kind, el, parent, subpaths=None, text="", font=None, html="", bbox=None):
            fill, stroke, sw, dashed, ms, me = gstyle(el)
            if subpaths is not None and bbox is None:
                bbox = bbox_of_points([p for s in subpaths for p in s["points"]])
            z[0] += 1
            e = {"id": new_id(), "kind": kind, "bbox": [round(v, 3) for v in bbox], "subpaths": subpaths or [],
                 "fill": fill, "stroke": stroke, "stroke_width": round(sw, 3), "dashed": dashed,
                 "arrow_start": ms, "arrow_end": me, "text": text, "html": html,
                 "font": font, "text_bbox": [round(v, 3) for v in bbox] if text else None, "text_angle": 0.0,
                 "rotation": 0.0, "z": z[0], "parent": parent, "one_d": False, "hidden": False,
                 "master": None, "user": {}, "relations": {}}
            page["elements"].append(e)
            return e

        def walk(el, parent):
            tag = el.tag
            if tag == _q("draw", "g"):
                z[0] += 1
                gid = new_id()
                gel = {"id": gid, "kind": "group", "bbox": [0, 0, 0, 0], "subpaths": [], "fill": None, "stroke": None,
                       "stroke_width": 0, "dashed": False, "arrow_start": "none", "arrow_end": "none", "text": "",
                       "html": "", "font": None, "text_bbox": None, "text_angle": 0.0, "rotation": 0.0, "z": z[0],
                       "parent": parent, "one_d": False, "hidden": False, "master": None, "user": {}, "relations": {}}
                page["elements"].append(gel)
                start = len(page["elements"])
                for ch in el:
                    walk(ch, gid)
                kids = [e for e in page["elements"][start:] if e["kind"] != "group"]
                gel["bbox"] = [round(v, 3) for v in bbox_union([k["bbox"] for k in kids])]
                return
            if tag == _q("draw", "polygon") or tag == _q("draw", "polyline"):
                f = geom_xform(el)
                pts = [f(p) for p in _parse_points(el.get(_q("draw", "points")))]
                closed = tag == _q("draw", "polygon")
                if closed and pts:
                    pts = pts + [pts[0]]
                sp = [{"points": pts, "closed": closed, "fill": closed, "stroke": True}]
                kind = _classify_kind(sp) if closed else ("line" if len(pts) == 2 else "path")
                add(kind, el, parent, sp)
            elif tag == _q("draw", "path"):
                f = geom_xform(el)
                subs = []
                for pts, closed in _parse_svg_path(el.get(_q("svg", "d"))):
                    q = [f(p) for p in pts]
                    if len(q) >= 2:
                        cl = closed or math.hypot(q[0][0] - q[-1][0], q[0][1] - q[-1][1]) < 0.5
                        subs.append({"points": q, "closed": cl, "fill": cl, "stroke": True})
                if subs:
                    add(_classify_kind(subs), el, parent, subs)
            elif tag == _q("draw", "line"):
                p1 = [_len_px(el.get(_q("svg", "x1")), scale), _len_px(el.get(_q("svg", "y1")), scale)]
                p2 = [_len_px(el.get(_q("svg", "x2")), scale), _len_px(el.get(_q("svg", "y2")), scale)]
                add("line", el, parent, [{"points": [p1, p2], "closed": False, "fill": False, "stroke": True}])
            elif tag in (_q("draw", "rect"), _q("draw", "ellipse"), _q("draw", "circle"), _q("draw", "custom-shape")):
                x = _len_px(el.get(_q("svg", "x")), scale)
                y = _len_px(el.get(_q("svg", "y")), scale)
                w = _len_px(el.get(_q("svg", "width")), scale)
                h = _len_px(el.get(_q("svg", "height")), scale)
                if tag in (_q("draw", "ellipse"), _q("draw", "circle")):
                    pts = [[round(x + w / 2 + w / 2 * math.cos(a), 3), round(y + h / 2 + h / 2 * math.sin(a), 3)]
                           for a in (2 * math.pi * i / 48 for i in range(49))]
                    kind = "ellipse"
                else:
                    pts = [[x, y], [x + w, y], [x + w, y + h], [x, y + h], [x, y]]
                    kind = "rect"
                text, font, h2 = _odf_text(el, styles, scale)
                add(kind, el, parent, [{"points": pts, "closed": True, "fill": True, "stroke": True}],
                    text=text, font=font, html=h2)
            elif tag == _q("draw", "frame"):
                x = _len_px(el.get(_q("svg", "x")), scale)
                y = _len_px(el.get(_q("svg", "y")), scale)
                w = _len_px(el.get(_q("svg", "width")), scale)
                h = _len_px(el.get(_q("svg", "height")), scale)
                tb = el.find(_q("draw", "text-box"))
                if tb is not None:
                    text, font, h2 = _odf_text(tb, styles, scale)
                    if text.strip():
                        props = styles.resolve("graphic", el.get(_q("draw", "style-name")))
                        font["valign"] = {"top": "top", "bottom": "bottom"}.get(props.get("draw:textarea-vertical-align"), "middle")
                        e = add("text", el, parent, [], text=text, font=font, html=h2, bbox=[x, y, w, h])
                        e["fill"] = None
                        e["stroke"] = None
                elif el.find(_q("draw", "image")) is not None:
                    add("rect", el, parent, [{"points": [[x, y], [x + w, y], [x + w, y + h], [x, y + h], [x, y]],
                                              "closed": True, "fill": False, "stroke": True}])
            elif tag == _q("draw", "connector"):
                p1 = [_len_px(el.get(_q("svg", "x1")), scale), _len_px(el.get(_q("svg", "y1")), scale)]
                p2 = [_len_px(el.get(_q("svg", "x2")), scale), _len_px(el.get(_q("svg", "y2")), scale)]
                add("line", el, parent, [{"points": [p1, p2], "closed": False, "fill": False, "stroke": True}])
        for ch in page_el:
            walk(ch, None)
        pages.append(page)
    _mark_background_fodg(pages)
    return {"backend": "fodg", "scale": scale, "pages": pages}


def _sig(e) -> tuple:
    return (e["kind"], tuple(round(v) for v in e["bbox"]), _norm_text(e.get("text", "")))


def _mark_background_fodg(pages: list[dict]) -> None:
    """LibreOffice chép nội dung trang nền vào mọi trang. Nếu một trang có toàn
    bộ phần tử xuất hiện y hệt trên mọi trang khác → đó là trang nền; các bản
    chép trên trang khác được đánh dấu layer=background."""
    if len(pages) < 2:
        return
    sigs = [{_sig(e) for e in p["elements"] if e["kind"] != "group"} for p in pages]
    for i, p in enumerate(pages):
        if not sigs[i]:
            continue
        others = [s for j, s in enumerate(sigs) if j != i]
        if all(sigs[i] <= s for s in others) and any(len(s) > len(sigs[i]) for s in others):
            p["background"] = True
            for j, q in enumerate(pages):
                if j == i:
                    continue
                for e in q["elements"]:
                    if e["kind"] != "group" and _sig(e) in sigs[i]:
                        e["layer"] = "background"
                # group chỉ chứa phần tử nền cũng thuộc nền
                kids: dict[str, list] = {}
                for e in q["elements"]:
                    if e.get("parent"):
                        kids.setdefault(e["parent"], []).append(e)
                for e in reversed(q["elements"]):
                    if e["kind"] == "group" and kids.get(e["id"]) and all(k.get("layer") == "background" for k in kids[e["id"]]):
                        e["layer"] = "background"


# ============================================================== ảnh gốc PNG


def split_fodg_pages(fodg: Path, outdir: Path) -> list[Path]:
    """Tách FODG nhiều trang thành nhiều FODG một trang (để render PNG từng trang)."""
    x = fodg.read_text(encoding="utf-8")
    pages = list(re.finditer(r"<draw:page\b.*?</draw:page>", x, re.S))
    out = []
    if not pages:
        return out
    head, tail = x[: pages[0].start()], x[pages[-1].end():]
    for i, m in enumerate(pages, start=1):
        p = outdir / f"page{i}.fodg"
        p.write_text(head + m.group() + tail, encoding="utf-8")
        out.append(p)
    return out


def render_original_pngs(original: Path, lo_dir: Path, compare_dir: Path) -> tuple[Path, list[dict]]:
    """Chép file gốc vào work/, chuyển sang FODG rồi render PNG từng trang."""
    lo_dir.mkdir(parents=True, exist_ok=True)
    compare_dir.mkdir(parents=True, exist_ok=True)
    local = lo_dir / ("source" + original.suffix.lower())
    shutil.copy2(original, local)
    fodg = soffice_convert(local, "fodg", lo_dir)
    split_dir = lo_dir / "pages"
    if split_dir.exists():
        shutil.rmtree(split_dir)
    split_dir.mkdir(parents=True)
    page_files = split_fodg_pages(fodg, split_dir)
    names = [m.group(1) for m in re.finditer(r'<draw:page\b[^>]*draw:name="([^"]*)"', fodg.read_text(encoding="utf-8"))]
    root = ET.parse(fodg).getroot()
    pl = next(root.iter(_q("style", "page-layout-properties")), None)
    pw_in = _len_px(pl.get(_q("fo", "page-width")), 1.0) if pl is not None else 0
    ph_in = _len_px(pl.get(_q("fo", "page-height")), 1.0) if pl is not None else 0
    infos = []
    pngs = soffice_convert_many(page_files, "png", split_dir, profile_dir=lo_dir / ".lo_profile")
    for i, png in enumerate(pngs, start=1):
        dst = compare_dir / f"page{i}_orig.png"
        shutil.copy2(png, dst)
        infos.append({"index": i, "name": htmlmod.unescape(names[i - 1]) if i - 1 < len(names) else f"Page-{i}",
                      "png": dst.name, "page_width_in": pw_in, "page_height_in": ph_in})
    return fodg, infos


# ============================================================== điều phối


def auto_backend(src: SourceSet) -> str:
    """vsdx nếu có; .vsd thì qua LibreOffice (fodg) khi có, không thì đọc nhị phân trực tiếp (vsdbin)."""
    if src.vsdx:
        return "vsdx"
    return "fodg" if find_soffice() else "vsdbin"


def ingest(src: SourceSet, work_dir: Path, scale: float = 96.0, backend: str = "auto",
           render_png: bool = True, log=print) -> dict:
    """Chạy ingest cho một sơ đồ: ghi raw.json và ảnh gốc vào work_dir."""
    work_dir.mkdir(parents=True, exist_ok=True)
    lo_dir = work_dir / "lo"
    compare_dir = work_dir / "compare"
    warnings = []
    fodg = None
    png_infos: list[dict] = []
    if render_png:
        try:
            fodg, png_infos = render_original_pngs(src.original, lo_dir, compare_dir)
        except Exception as ex:  # thiếu LibreOffice vẫn cho phép đi tiếp với .vsdx
            warnings.append(f"Không render được ảnh gốc: {ex}")
            log(f"  ! {warnings[-1]}")
    use = backend
    if use == "auto":
        use = auto_backend(src)
    if use == "vsdx":
        if not src.vsdx:
            raise RuntimeError("Không có file .vsdx cho backend vsdx")
        raw = read_vsdx(src.vsdx, scale)
    elif use == "vsdbin":
        if not src.vsd:
            raise RuntimeError("Không có file .vsd cho backend vsdbin")
        from .vsdbin import read_vsd
        raw = read_vsd(src.vsd, scale)
    else:
        if fodg is None:  # chưa có (không render ảnh) → chuyển riêng
            local = lo_dir / ("source" + src.original.suffix.lower())
            lo_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src.original, local)
            fodg = soffice_convert(local, "fodg", lo_dir)
        raw = read_fodg(fodg, scale)
    raw["source"] = {"name": src.name, "rel_dir": src.rel_dir, "vsd": str(src.vsd) if src.vsd else None,
                     "vsdx": str(src.vsdx) if src.vsdx else None, "pairing": src.note}
    raw["backend"] = use
    raw["warnings"] = warnings
    # gắn ảnh gốc theo tên trang (ưu tiên) hoặc thứ tự
    by_name = {i["name"]: i for i in png_infos}
    for p in raw["pages"]:
        info = by_name.get(p["name"]) or next((i for i in png_infos if i["index"] == p["index"]), None)
        if info:
            p["orig_png"] = info["png"]
            p["orig_png_page_in"] = [info["page_width_in"], info["page_height_in"]]
    # văn bản tham chiếu từ bản .vsd gốc (để verify khi dùng .vsdx)
    if use == "vsdx" and fodg is not None and src.vsd:
        try:
            ref = read_fodg(fodg, scale)
            raw["reference_texts"] = {
                p["name"]: sorted({_norm_text(e["text"]) for e in p["elements"]
                                   if e.get("text", "").strip() and e.get("layer") != "background"})
                for p in ref["pages"] if not p.get("background")}
        except Exception as ex:
            warnings.append(f"Không đọc được văn bản tham chiếu từ .vsd: {ex}")
    save_json(work_dir / "raw.json", raw)
    return raw
