"""Đọc trực tiếp file Visio nhị phân .vsd (Visio 2003–2010, định dạng v11) — không cần Visio hay LibreOffice.

Thử nghiệm: cấu trúc được giải mã bằng cách đối chiếu dữ liệu thật (tham khảo mô tả định dạng
của dự án libvisio). Chỉ đọc phần cần cho sơ đồ: trang, shape/group, XForm, XForm1D, TextXForm,
Geometry (MoveTo/LineTo/ArcTo/EllipticalArcTo/Ellipse…), Line, Fill, TextBlock, Char/Para, Text,
master (kế thừa hình học, line, fill, text), stylesheet, tên trang/master.

Không đọc: ảnh nhúng (shape Foreign chỉ giữ khung), field/công thức, layer, connection points,
Connects (đầu nối được suy ra theo hình học như backend fodg).

Kết quả: raw dict cùng schema với ``ingest.read_vsdx``/``read_fodg`` (px, gốc trên-trái),
``backend = "vsdbin"`` → reconstruct dùng nhánh suy luận theo hình học.
"""
from __future__ import annotations

import math
import struct
from pathlib import Path

# ============================================================== Compound File (OLE2)

_FREE, _END = 0xFFFFFFFF, 0xFFFFFFFE


class _Cfb:
    """Bộ đọc tối giản Compound File Binary — chỉ đọc."""

    def __init__(self, data: bytes):
        if data[:8] != bytes.fromhex("D0CF11E0A1B11AE1"):
            raise ValueError("không phải file OLE2 (.vsd)")
        self.d = data
        self.ss = 1 << struct.unpack_from("<H", data, 0x1E)[0]
        self.mss = 1 << struct.unpack_from("<H", data, 0x20)[0]
        nfat, dir0 = struct.unpack_from("<II", data, 0x2C)
        self.cutoff, mf0, nmf, dif0, ndif = struct.unpack_from("<IIIII", data, 0x38)
        difat = list(struct.unpack_from("<109I", data, 0x4C))
        s = dif0
        for _ in range(ndif):
            vals = struct.unpack_from(f"<{self.ss // 4}I", self._sec(s))
            difat += vals[:-1]
            s = vals[-1]
        self.fat: list[int] = []
        for fs in difat[:nfat]:
            self.fat += struct.unpack_from(f"<{self.ss // 4}I", self._sec(fs))
        self.minifat: list[int] = []
        if nmf:
            mfd = self._chain(mf0)
            self.minifat = list(struct.unpack_from(f"<{len(mfd) // 4}I", mfd))
        dd = self._chain(dir0)
        self.entries = []
        for i in range(len(dd) // 128):
            e = dd[i * 128:(i + 1) * 128]
            nl = struct.unpack_from("<H", e, 64)[0]
            left, right, child = struct.unpack_from("<III", e, 68)
            start, size = struct.unpack_from("<IQ", e, 116)
            self.entries.append({"i": i, "name": e[:max(nl - 2, 0)].decode("utf-16-le", "replace"), "type": e[66],
                                 "left": left, "right": right, "child": child, "start": start, "size": size})
        root = self.entries[0]
        self.ministream = self._chain(root["start"])[:root["size"]] if root["start"] not in (_END, _FREE) else b""

    def _sec(self, n):
        o = (n + 1) * self.ss
        return self.d[o:o + self.ss]

    def _chain(self, s):
        out, guard = [], 0
        while s not in (_END, _FREE) and s < len(self.fat) and guard <= len(self.fat):
            out.append(self._sec(s))
            s = self.fat[s]
            guard += 1
        return b"".join(out)

    def _minichain(self, s):
        out, guard = [], 0
        while s not in (_END, _FREE) and s < len(self.minifat) and guard <= len(self.minifat):
            o = s * self.mss
            out.append(self.ministream[o:o + self.mss])
            s = self.minifat[s]
            guard += 1
        return b"".join(out)

    def stream(self, name: str) -> bytes:
        for e in self.entries[1:]:
            if e["type"] == 2 and e["name"] == name:
                if e["size"] < self.cutoff:
                    return self._minichain(e["start"])[:e["size"]]
                return self._chain(e["start"])[:e["size"]]
        raise KeyError(name)


# ============================================================== stream con bên trong VisioDocument


def lzss(src: bytes) -> bytes:
    """Giải nén kiểu LZSS dùng trong .vsd (cửa sổ 4096 byte, độ dài 3–18)."""
    buf = bytearray(4096)
    pos = 0
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        flag = src[i]
        i += 1
        for b in range(8):
            if i >= n:
                break
            if flag & (1 << b):
                c = src[i]
                i += 1
                out.append(c)
                buf[pos] = c
                pos = (pos + 1) & 0xFFF
            else:
                if i + 1 >= n:
                    i = n
                    break
                b1, b2 = src[i], src[i + 1]
                i += 2
                off = ((b1 | ((b2 & 0xF0) << 4)) + 18) & 0xFFF
                for k in range((b2 & 0x0F) + 3):
                    c = buf[(off + k) & 0xFFF]
                    out.append(c)
                    buf[pos] = c
                    pos = (pos + 1) & 0xFFF
    return bytes(out)


class _Ptr:
    __slots__ = ("type", "offset", "length", "format")

    def __init__(self, t, o, ln, f):
        self.type, self.offset, self.length, self.format = t, o, ln, f

    @property
    def compressed(self) -> bool:
        return bool(self.format & 2)


def _read_ptr(b: bytes, o: int) -> _Ptr:
    t, _x, off, ln, f = struct.unpack_from("<IIIIH", b, o)
    return _Ptr(t, off, ln, f)


class _Doc:
    def __init__(self, data: bytes):
        self.d = data
        ver = data[0x1A]
        if ver != 11:
            raise ValueError(f"chỉ hỗ trợ .vsd Visio 2003–2010 (phiên bản 11), file này là phiên bản {ver}")
        self.trailer = _read_ptr(data, 0x24)
        self._cache: dict[tuple, bytes] = {}

    def data(self, p: _Ptr) -> bytes:
        k = (p.offset, p.length, p.format)
        if k not in self._cache:
            raw = self.d[p.offset:p.offset + p.length]
            self._cache[k] = lzss(raw) if p.compressed else raw
        return self._cache[k]

    def kids(self, p: _Ptr) -> list[_Ptr]:
        """Danh sách con trỏ của stream dạng 'pointer list'."""
        d = self.data(p)
        shift = 4 if p.compressed else 0
        if len(d) < shift + 4:
            return []
        base = struct.unpack_from("<I", d, shift)[0] + shift
        if base + 8 > len(d):
            return []
        cnt = struct.unpack_from("<I", d, base)[0]
        o = base + 8
        if cnt > 100000 or o + cnt * 18 > len(d):
            return []
        return [_read_ptr(d, o + 18 * i) for i in range(cnt)]


# ============================================================== chunk

_TRAILER_TYPES = {0x64, 0x65, 0x66, 0x69, 0x6a, 0x6b, 0x6f, 0x71, 0x92, 0xa9, 0xb4, 0xb6, 0xb9, 0xc7}
_NO_TRAILER = {0x1f, 0xc9, 0x2d, 0xd1}


class _Chunk:
    __slots__ = ("type", "id", "list", "level", "data")


_UNKNOWN_OK = {0x50, 0x51, 0x52, 0x53, 0x54, 0x55}  # byte thứ 19 của header ở các chunk thật đã quan sát


def _plausible(d, o, first=False):
    """Header hợp lệ: type 1–255, cấp ≤ 3, byte 'unknown' quen thuộc (riêng chunk đầu stream được 0)."""
    if o + 19 > len(d):
        return False
    t, _i, _l, ln, lv, u = struct.unpack_from("<IIIIHB", d, o)
    if first:
        return 0 < t < 0x100 and lv <= 3 and ln <= len(d) - o - 19 and u in (_UNKNOWN_OK | {0})
    # cấp 0 chỉ gặp ở header sheet (stream shape của master); chunk khác cấp 0 là byte rác
    return 0 < t < 0x100 and 1 <= lv + (t in (0x46, 0x47, 0x48, 0x4a, 0x4e)) and lv <= 3 and \
        ln <= len(d) - o - 19 and u in _UNKNOWN_OK


def iter_chunks(d: bytes, start: int = 0):
    """Duyệt các chunk (header 19 byte: type, id, list, dataLength, level, unknown)."""
    o, n = start, len(d)
    first = True
    while o < n:
        while o < n and d[o] == 0:
            o += 1
        if o + 19 > n:
            break
        if not _plausible(d, o, first):
            o += 1  # lệch đồng bộ: dò header hợp lệ kế tiếp
            continue
        first = False
        t, cid, lst, ln, lv, _u = struct.unpack_from("<IIIIHB", d, o)
        c = _Chunk()
        c.type, c.id, c.list, c.level = t, cid, lst, lv
        c.data = d[o + 19:o + 19 + ln]
        trailer = 8 if (lst != 0 or t in _TRAILER_TYPES) else 0
        if t in _NO_TRAILER:
            trailer = 0
        end = o + 19 + ln
        # trailer không cố định theo loại chunk: thử các độ dài thường gặp, chọn cái dẫn tới header hợp lệ
        nxt = end + trailer
        for tr in dict.fromkeys((trailer, 0, 8, 4, 12)):
            k = end + tr
            while k < n and d[k] == 0:
                k += 1
            if k >= n or _plausible(d, k):
                nxt = end + tr
                break
        yield c
        o = nxt


# ============================================================== giải mã giá trị


def _dbl(b: bytes, i: int, default=0.0) -> float:
    """Giá trị thứ i dạng [đơn vị 1 byte][double 8 byte]."""
    o = i * 9 + 1
    if o + 8 > len(b):
        return default
    v = struct.unpack_from("<d", b, o)[0]
    return v if math.isfinite(v) else default


def _u8(b, o, default=0):
    return b[o] if o < len(b) else default


def _u32(b, o):
    if o + 4 > len(b):
        return None
    v = struct.unpack_from("<I", b, o)[0]
    return None if v == 0xFFFFFFFF else v


def _rgba(b, o):
    if o + 4 > len(b):
        return None, 0
    r, g, bl, a = b[o:o + 4]
    return f"#{r:02x}{g:02x}{bl:02x}", a


_GEOM_ROWS = {0x8a: "MoveTo", 0x8b: "LineTo", 0x8c: "ArcTo", 0x8d: "InfiniteLine", 0x8f: "Ellipse",
              0x90: "EllipticalArcTo", 0xc1: "PolylineTo", 0xc3: "NURBSTo", 0xa1: "SplineStart", 0xa2: "SplineKnot"}


class _Sheet:
    """Một sheet (shape, group, foreign, style, page sheet) với các ô đã giải mã."""

    def __init__(self, kind: int, sid: int, hdr: bytes):
        self.kind, self.id = kind, sid
        self.parent = self.master_page = self.master_shape = None
        self.style = {"line": None, "fill": None, "text": None}
        if kind in (0x47, 0x48, 0x4e):
            self.parent = _u32(hdr, 10)
            self.master_page = _u32(hdr, 18)
            self.master_shape = _u32(hdr, 26)
        if kind in (0x47, 0x48, 0x4e, 0x4a):
            # style dùng (shape) hoặc style cha (stylesheet)
            self.style = {"line": _u32(hdr, 34), "fill": _u32(hdr, 42), "text": _u32(hdr, 50)}
        self.xform = self.xform1d = self.txform = None
        self.line = self.fill = self.tblock = None
        self.geoms: list[dict] | None = None
        self.text: str | None = None
        self.chars: list[dict] = []
        self.paras: list[dict] = []
        self.children: list[int] = []  # id shape con (chunk 0x83 trong danh sách shape của group)
        self.page_size = None

    def add(self, c: _Chunk, geom_ctx: list):
        b, t = c.data, c.type
        if t == 0x9b:
            self.xform = {"PinX": _dbl(b, 0), "PinY": _dbl(b, 1), "Width": _dbl(b, 2), "Height": _dbl(b, 3),
                          "LocPinX": _dbl(b, 4), "LocPinY": _dbl(b, 5), "Angle": _dbl(b, 6),
                          "FlipX": _u8(b, 63) == 1, "FlipY": _u8(b, 64) == 1}
        elif t == 0x9d:
            self.xform1d = (_dbl(b, 0), _dbl(b, 1), _dbl(b, 2), _dbl(b, 3))
        elif t == 0x9c:
            self.txform = {"TxtPinX": _dbl(b, 0), "TxtPinY": _dbl(b, 1), "TxtWidth": _dbl(b, 2),
                           "TxtHeight": _dbl(b, 3), "TxtLocPinX": _dbl(b, 4), "TxtLocPinY": _dbl(b, 5),
                           "TxtAngle": _dbl(b, 6)}
        elif t == 0x85:
            col, alpha = _rgba(b, 10)
            self.line = {"weight": _dbl(b, 0), "color": col, "alpha": alpha, "pattern": _u8(b, 14, 1),
                         "rounding": struct.unpack_from("<d", b, 16)[0] if len(b) >= 24 else 0.0,
                         "begin_arrow": _u8(b, 25), "end_arrow": _u8(b, 26)}
        elif t == 0x86:
            fg, fa = _rgba(b, 1)
            bg, _ba = _rgba(b, 6)
            self.fill = {"fg": fg, "alpha": fa, "bg": bg, "pattern": _u8(b, 10, 1)}
        elif t == 0x87:
            self.tblock = {"valign": _u8(b, 36, 1)}
        elif t == 0x92:
            self.page_size = (_dbl(b, 0), _dbl(b, 1))
        elif t == 0x0e:
            self.text = b[8:].decode("utf-16-le", "replace") if len(b) > 8 else ""
        elif t == 0x94:
            col, _a = _rgba(b, 7)
            fs = struct.unpack_from("<d", b, 18)[0] if len(b) >= 26 else 0.1667
            self.chars.append({"count": _u32(b, 0) or 0, "font": struct.unpack_from("<H", b, 4)[0] if len(b) >= 6 else 0,
                               "color": col, "style": _u8(b, 11), "size": fs if 0 < fs < 10 else 0.1667})
        elif t == 0x95:
            self.paras.append({"count": _u32(b, 0) or 0, "align": _u8(b, 58, 1)})
        elif t == 0x83 and len(b) >= 4:
            self.children.append(struct.unpack_from("<I", b, 0)[0])
        elif t == 0x6c:
            if self.geoms is None:
                self.geoms = []
            sec = {"no_fill": False, "no_line": False, "no_show": False, "rows": []}
            self.geoms.append(sec)
            geom_ctx[:] = [sec]
        elif t == 0x89 and geom_ctx:
            f = _u8(b, 0)
            geom_ctx[0].update(no_fill=bool(f & 1), no_line=bool(f & 2), no_show=bool(f & 4))
        elif t in _GEOM_ROWS and geom_ctx:
            geom_ctx[0]["rows"].append((_GEOM_ROWS[t], [_dbl(b, i) for i in range(6)], c.id))


_SHEET_TYPES = (0x46, 0x47, 0x48, 0x4a, 0x4e)


def parse_sheets(d: bytes, start: int = 0) -> list[_Sheet]:
    """Gom chunk thành danh sách sheet theo cấp tương đối.

    Stream trang: sheet ở cấp 1, section cấp 2, row cấp 3. Stream shape của master lùi 1 cấp
    (sheet cấp 0) → lấy cấp của chunk sheet đầu tiên làm gốc.
    """
    out: list[_Sheet] = []
    cur = None
    base = None
    geom_ctx: list = []
    for c in iter_chunks(d, start):
        if c.type in _SHEET_TYPES and (base is None or c.level == base) and c.level <= 1:
            base = c.level
            cur = _Sheet(c.type, c.id, c.data)
            out.append(cur)
            geom_ctx = []
            continue
        if cur is None:
            continue
        if c.level <= base + 1 and c.type not in (0x6c, 0x89) and c.type not in _GEOM_ROWS:
            geom_ctx = []
        cur.add(c, geom_ctx)
    return out


def _shape_ids(sheets_stream: bytes) -> list[int]:
    """Danh sách id shape (chunk 0x83: id, loại) theo thứ tự trong page sheet của master."""
    out = []
    for c in iter_chunks(sheets_stream):
        if c.type == 0x83 and len(c.data) >= 8:
            out.append(struct.unpack_from("<I", c.data, 0)[0])
    return out


# ============================================================== tài liệu


class VsdDocument:
    def __init__(self, path: Path):
        data = Path(path).read_bytes()
        self.doc = _Doc(_Cfb(data).stream("VisioDocument"))
        top = self.doc.kids(self.doc.trailer)
        self.top = top
        self.names = self._names()
        self.fonts = self._fonts()
        self.styles = self._styles()
        self.masters = self._masters()
        self.pages = self._pages()

    # -- tiện ích
    def _first(self, t, lst=None):
        return next((p for p in (lst if lst is not None else self.top) if p.type == t and p.length), None)

    def _names(self) -> list[str]:
        p = self._first(0x32)
        out = []
        for k in (self.doc.kids(p) if p else []):
            d = self.doc.data(k) if k.length else b""
            s = d[8:] if len(d) >= 8 and struct.unpack_from("<I", d, 0)[0] == len(d) - 4 else d[4:]
            out.append(s.decode("utf-16-le", "replace").split("\x00")[0].strip())
        return out

    def _name_table(self, lst: list[_Ptr]) -> dict[int, tuple[str, str]]:
        """Bảng 0xc9: (chỉ số Name, chỉ số NameU, vị trí trong danh sách) → tên."""
        p = self._first(0xc9, lst)
        res: dict[int, tuple[str, str]] = {}
        if not p:
            return res
        d = self.doc.data(p)
        cnt = struct.unpack_from("<I", d, 4)[0] if len(d) >= 8 else 0
        for i in range(cnt):
            o = 8 + 13 * i
            if o + 12 > len(d):
                break
            n1, n2, idx = struct.unpack_from("<III", d, o)
            nm = self.names[n1] if n1 < len(self.names) else ""
            nu = self.names[n2] if n2 < len(self.names) else nm
            res[idx] = (nm, nu)
        return res

    def _fonts(self) -> dict[int, str]:
        for p in self.top:
            if (p.format >> 4) not in (4, 5) or not p.length:
                continue
            ks = self.doc.kids(p)
            if ks and all(k.type == 0xd7 for k in ks if k.length):
                out = {}
                for i, k in enumerate(ks):
                    if k.length:
                        d = self.doc.data(k)
                        out[i] = d[8:].decode("utf-16-le", "replace").split("\x00")[0].strip()
                return out
        return {}

    def _styles(self) -> dict[int, _Sheet]:
        p = self._first(0x1a)
        if not p:
            return {}
        return {s.id: s for s in parse_sheets(self.doc.data(p)) if s.kind == 0x4a}

    def _masters(self) -> dict[int, dict]:
        p = self._first(0x1d)
        if not p:
            return {}
        lst = self.doc.kids(p)
        names = self._name_table(lst)
        out = {}
        for idx, m in enumerate(lst):
            if m.type != 0x1e or not m.length:
                continue
            shapes: dict[int, _Sheet] = {}
            order: list[_Sheet] = []
            # header shape trong master không mang id (0xffffffff): id = vị trí stream trong danh sách
            # con trỏ của master (đối chiếu: danh sách 0x83 của group liệt kê đúng các vị trí này)
            for pos, k in enumerate(self.doc.kids(m)):
                if not k.length or (k.format >> 4) != 0xd or k.type not in (0x47, 0x48, 0x4e):
                    continue
                s = next((x for x in parse_sheets(self.doc.data(k)) if x.kind in (0x47, 0x48, 0x4e)), None)
                if s is None:
                    continue
                s.id = pos
                shapes[pos] = s
                order.append(s)
            nm = names.get(idx, ("", ""))
            top = next((s for s in order if not s.parent), order[0] if order else None)
            out[idx] = {"name": nm[1] or nm[0], "shapes": shapes, "top": top}
        return out

    def _pages(self) -> list[dict]:
        p = self._first(0x27)
        if not p:
            return []
        lst = self.doc.kids(p)
        names = self._name_table(lst)
        out = []
        for idx, k in enumerate(lst):
            if k.type != 0x15 or not k.length:
                continue
            sheets = parse_sheets(self.doc.data(k))
            nm = names.get(idx, ("", ""))
            out.append({"list_index": idx, "name": nm[0] or nm[1], "sheets": sheets})
        return out


# ============================================================== dựng raw


class _Shape:
    """Shape trên trang đã gộp kế thừa: instance → master shape → stylesheet."""

    def __init__(self, vd: VsdDocument, s: _Sheet):
        self.s = s
        self.m = None
        self.master_name = None
        if s.master_page is not None and s.master_page in vd.masters:
            md = vd.masters[s.master_page]
            self.master_name = md["name"] or None
            self.m = md["shapes"].get(s.master_shape) if s.master_shape is not None else md["top"]
            if self.m is None and not s.parent:  # chỉ shape cấp đầu mới lùi về shape chính của master
                self.m = md["top"]
        self.vd = vd

    def get(self, attr):
        v = getattr(self.s, attr)
        if v is None and self.m is not None:
            v = getattr(self.m, attr)
        return v

    def style_get(self, attr, which):
        v = self.get(attr)
        if v is not None:
            return v
        sid = self.s.style.get(which)
        if sid is None and self.m is not None:
            sid = self.m.style.get(which)
        for _ in range(10):
            st = self.vd.styles.get(sid) if sid is not None else None
            if st is None:
                return None
            v = getattr(st, attr)
            if v is not None:
                return v
            sid = st.style.get(which)
        return None

    def list_get(self, attr, which):
        v = getattr(self.s, attr)
        if not v and self.m is not None:
            v = getattr(self.m, attr)
        if not v:
            sid = self.s.style.get(which) if self.s.style.get(which) is not None else (self.m.style.get(which) if self.m else None)
            st = self.vd.styles.get(sid) if sid is not None else None
            v = getattr(st, attr) if st is not None else []
        return v or []

    def _master_scale(self):
        xf, mxf = self.s.xform, self.m.xform if self.m is not None else None
        sx = sy = 1.0
        if xf and mxf:
            sx = xf["Width"] / mxf["Width"] if abs(mxf["Width"]) > 1e-9 else 1.0
            sy = xf["Height"] / mxf["Height"] if abs(mxf["Height"]) > 1e-9 else 1.0
        return sx, sy

    def geoms(self):
        """Geometry cục bộ (inch, y lên).

        Instance chỉ lưu các hàng geometry đã đổi → ghép theo id hàng với section cùng vị trí của
        master; hàng lấy từ master được co giãn theo kích thước instance (công thức theo Width/Height).
        """
        own = self.s.geoms
        mst = self.m.geoms if self.m is not None else None
        if not mst or (own is not None and self.get("xform1d")):
            # connector 1D: đường đi lưu đủ ở instance; ghép hàng của master (co giãn theo khung) làm sai đường
            return (own or []), 1.0, 1.0
        sx, sy = self._master_scale()

        def scaled(rows):
            out = []
            for t, v, rid in rows:
                v = list(v)
                v[0], v[1] = v[0] * sx, v[1] * sy
                if t in ("EllipticalArcTo", "Ellipse"):
                    v[2], v[3] = v[2] * sx, v[3] * sy
                    if t == "Ellipse":
                        v[4], v[5] = v[4] * sx, v[5] * sy
                out.append((t, v, rid))
            return out
        if own is None:
            return [dict(sec, rows=scaled(sec["rows"])) for sec in mst], 1.0, 1.0
        merged = []
        for i in range(max(len(own), len(mst))):
            o = own[i] if i < len(own) else None
            m = mst[i] if i < len(mst) else None
            if m is None:
                merged.append(o)
                continue
            rows = {rid: (t, v, rid) for t, v, rid in scaled(m["rows"])}
            if o is not None:
                rows.update({rid: (t, v, rid) for t, v, rid in o["rows"]})
            base = o if o is not None else m
            merged.append(dict(base, rows=[rows[k] for k in sorted(rows)]))
        return merged, 1.0, 1.0

    def txform(self):
        """TextXForm; nếu kế thừa từ master thì co giãn theo kích thước instance (công thức theo Width/Height)."""
        if self.s.txform is not None:
            return self.s.txform
        if self.m is None or self.m.txform is None:
            return None
        sx, sy = self._master_scale()
        t = dict(self.m.txform)
        for k in ("TxtPinX", "TxtWidth", "TxtLocPinX"):
            t[k] *= sx
        for k in ("TxtPinY", "TxtHeight", "TxtLocPinY"):
            t[k] *= sy
        return t


def _xf_apply(xf: dict, p):
    x, y = p
    if xf["FlipX"]:
        x = xf["Width"] - x
    if xf["FlipY"]:
        y = xf["Height"] - y
    x -= xf["LocPinX"]
    y -= xf["LocPinY"]
    c, s = math.cos(xf["Angle"]), math.sin(xf["Angle"])
    return (xf["PinX"] + x * c - y * s, xf["PinY"] + x * s + y * c)


def _local_paths(sections, sx, sy):
    from .ingest import _arc3  # tránh import vòng khi nạp module
    out, has_ell = [], False
    for sec in sections:
        if sec["no_show"]:
            continue
        cur: list = []
        subs: list = []

        def flush():
            nonlocal cur
            if len(cur) > 1:
                subs.append(cur)
            cur = []
        for t, v, *_rid in sec["rows"]:
            X, Y = v[0] * sx, v[1] * sy
            if t == "MoveTo":
                flush()
                cur = [(X, Y)]
            elif t in ("LineTo", "PolylineTo", "NURBSTo", "SplineStart", "SplineKnot"):
                cur = cur or [(0.0, 0.0)]
                cur.append((X, Y))
            elif t == "ArcTo":
                p0 = cur[-1] if cur else (0.0, 0.0)
                cur = cur or [p0]
                A = v[2] * min(sx, sy)
                if abs(A) < 1e-9:
                    cur.append((X, Y))
                else:
                    dx, dy = X - p0[0], Y - p0[1]
                    L = math.hypot(dx, dy) or 1.0
                    cur += _arc3(p0, ((p0[0] + X) / 2 + A * dy / L, (p0[1] + Y) / 2 - A * dx / L), (X, Y))
            elif t == "EllipticalArcTo":
                p0 = cur[-1] if cur else (0.0, 0.0)
                cur = cur or [p0]
                A, B, C, D = v[2] * sx, v[3] * sy, v[4], v[5] or 1.0
                cosc, sinc = math.cos(-C), math.sin(-C)

                def fw(p):
                    x, y = p[0] * cosc - p[1] * sinc, p[0] * sinc + p[1] * cosc
                    return (x / D, y)

                def bw(p):
                    x, y = p[0] * D, p[1]
                    return (x * math.cos(C) - y * math.sin(C), x * math.sin(C) + y * math.cos(C))
                cur += [bw(p) for p in _arc3(fw(p0), fw((A, B)), fw((X, Y)))]
                cur[-1] = (X, Y)
            elif t == "Ellipse":
                flush()
                A, B, C2, D2 = v[2] * sx, v[3] * sy, v[4] * sx, v[5] * sy
                ax, ay, bx, by = A - X, B - Y, C2 - X, D2 - Y
                subs.append([(X + ax * math.cos(a) + bx * math.sin(a), Y + ay * math.cos(a) + by * math.sin(a))
                             for a in (2 * math.pi * i / 48 for i in range(49))])
                has_ell = True
        flush()
        for sp in subs:
            closed = len(sp) > 2 and math.hypot(sp[0][0] - sp[-1][0], sp[0][1] - sp[-1][1]) < 1e-6
            out.append({"points": sp, "closed": closed, "fill": (not sec["no_fill"]) and closed,
                        "stroke": not sec["no_line"]})
    return out, has_ell


def _split_text(elem: dict) -> list[dict]:
    """Tách text khỏi hình theo quy ước của backend fodg (LibreOffice xuất hình và khung chữ riêng):

    - shape có hình nhìn thấy + text → group {hình, text};
    - shape chỉ có text (không fill, không viền) → element ``text``;
    - group Visio có text → thêm element ``text`` làm con của group.
    """
    has_text = bool(elem["text"].strip())
    if not has_text:
        return [elem]
    tb = list(elem["text_bbox"] or elem["shape_box"])
    # khung chữ quá thấp (vd. tiêu đề group box cao 0.01 in) → nới theo cỡ chữ, giữ tâm
    min_h = (elem["font"] or {}).get("size", 12) * 1.25 * max(1, elem["text"].count("\n") + 1)
    if tb[3] < min_h:
        cy = tb[1] + tb[3] / 2
        tb[1], tb[3] = round(cy - min_h / 2, 3), round(min_h, 3)
    sb = elem["shape_box"]
    cx_t, cy_t = tb[0] + tb[2] / 2, tb[1] + tb[3] / 2
    centered = sb[2] > 0 and sb[3] > 0 and abs(cx_t - (sb[0] + sb[2] / 2)) <= sb[2] * 0.25 and \
        abs(cy_t - (sb[1] + sb[3] / 2)) <= sb[3] * 0.25
    text_el = dict(elem, id=elem["id"] + "t", kind="text", subpaths=[], fill=None, stroke=None, bbox=list(tb),
                   text_bbox=list(tb), one_d=False, arrow_start="none", arrow_end="none", parent=elem["id"])
    text_el.pop("begin", None)
    text_el.pop("end", None)
    if elem["kind"] == "group":
        g = dict(elem, text="", html="", text_bbox=None)
        return [g, text_el]
    visible = bool(elem["subpaths"]) and (elem["fill"] is not None or elem["stroke"] is not None)
    if not visible:
        text_el["id"], text_el["parent"] = elem["id"], elem["parent"]
        return [text_el]
    if not centered and not elem["one_d"]:
        # chữ đặt lệch khỏi hình (tiêu đề khung, nhãn bên cạnh) → text rời để giữ đúng vị trí
        text_el["parent"] = elem["parent"]
        geom = dict(elem, text="", html="", text_bbox=None)
        return [geom, text_el]
    group = dict(elem, kind="group", subpaths=[], fill=None, stroke=None, text="", html="", text_bbox=None,
                 one_d=False, arrow_start="none", arrow_end="none",
                 bbox=bbox_union_safe([elem["bbox"], tb]))
    group.pop("begin", None)
    group.pop("end", None)
    geom = dict(elem, id=elem["id"] + "g", parent=elem["id"], text="", html="", text_bbox=None)
    return [group, geom, text_el]


def _hoist_frame_titles(els: list[dict]) -> None:
    """Tiêu đề nằm lệch tâm trong một group dạng khung (lane, pool: có hình kín phủ gần hết group)
    → đưa text lên cấp trên cùng, để khi group được gộp thành một khối thì chữ không bị canh giữa khối."""
    by_id = {e["id"]: e for e in els}
    kids: dict = {}
    for e in els:
        kids.setdefault(e.get("parent"), []).append(e)

    def top(e):
        seen = 0
        while e.get("parent") in by_id and seen < 50:
            e = by_id[e["parent"]]
            seen += 1
        return e

    def desc(gid):
        out = []
        for k in kids.get(gid, []):
            out.append(k)
            out += desc(k["id"])
        return out

    def area(b):
        return max(b[2], 0) * max(b[3], 0)
    for t in els:
        if t["kind"] != "text" or not t.get("parent"):
            continue
        a = top(t)
        if a is t or a["kind"] != "group":
            continue
        ab, tb = a["bbox"], t["bbox"]
        if area(ab) <= 4 * area(tb):
            continue
        frame = any(any(sp["closed"] for sp in x["subpaths"]) and area(x["bbox"]) >= 0.8 * area(ab)
                    for x in desc(a["id"]))
        cx, cy = tb[0] + tb[2] / 2, tb[1] + tb[3] / 2
        centered = abs(cx - (ab[0] + ab[2] / 2)) <= ab[2] * 0.25 and abs(cy - (ab[1] + ab[3] / 2)) <= ab[3] * 0.25
        if frame and not centered:
            # tách cả dải tiêu đề (con trực tiếp của khung chứa text, vd. header lane) nếu nó nhỏ
            child = t
            while child.get("parent") in by_id and child["parent"] != a["id"]:
                child = by_id[child["parent"]]
            moved = child if child is not t and 4 * area(child["bbox"]) < area(ab) else t
            moved["parent"] = None


def bbox_union_safe(boxes):
    xs = [b[0] for b in boxes] + [b[0] + b[2] for b in boxes]
    ys = [b[1] for b in boxes] + [b[1] + b[3] for b in boxes]
    return [round(min(xs), 3), round(min(ys), 3), round(max(xs) - min(xs), 3), round(max(ys) - min(ys), 3)]


def read_vsd(path: Path, scale: float = 96.0) -> dict:
    """Đọc .vsd nhị phân → raw dict (tọa độ px, gốc trên-trái)."""
    from .ingest import _classify_kind, visio_arrow
    from .model import bbox_of_points
    vd = VsdDocument(path)
    pages = []
    for pidx, pg in enumerate(vd.pages, start=1):
        psheet = next((s for s in pg["sheets"] if s.kind == 0x46), None)
        pw_in, ph_in = (psheet.page_size if psheet is not None and psheet.page_size else (8.5, 11.0))
        page = {"index": pidx, "name": pg["name"] or f"Page-{pidx}", "background": False, "back_page": None,
                "visio_page_id": str(pg["list_index"]), "width": round(pw_in * scale, 2),
                "height": round(ph_in * scale, 2), "width_in": pw_in, "height_in": ph_in, "elements": []}
        shapes = [s for s in pg["sheets"] if s.kind in (0x47, 0x48, 0x4e)]
        by_id = {s.id: s for s in shapes}

        def to_px(p):
            return [round(p[0] * scale, 3), round((ph_in - p[1]) * scale, 3)]

        def chain_of(s: _Sheet, guard=0) -> list[dict]:
            if s.parent is None or s.parent not in by_id or guard > 20:
                return []
            par = _Shape(vd, by_id[s.parent])
            pxf = par.get("xform")
            return ([pxf] if pxf else []) + chain_of(by_id[s.parent], guard + 1)

        for z, s in enumerate(shapes, start=1):
            sh = _Shape(vd, s)
            xf = sh.get("xform") or {"PinX": 0, "PinY": 0, "Width": 0, "Height": 0, "LocPinX": 0, "LocPinY": 0,
                                     "Angle": 0, "FlipX": False, "FlipY": False}
            parent_chain = chain_of(s)
            chain = [xf] + parent_chain

            def tf(p, chain=chain):
                for x in chain:
                    p = _xf_apply(x, p)
                return p
            secs, gsx, gsy = sh.geoms()
            local, has_ell = _local_paths(secs, gsx, gsy)
            subpaths = [{"points": [to_px(tf(p)) for p in sp["points"]], "closed": sp["closed"],
                         "fill": sp["fill"], "stroke": sp["stroke"]} for sp in local]
            box = bbox_of_points([to_px(tf(p)) for p in ((0, 0), (xf["Width"], 0), (xf["Width"], xf["Height"]),
                                                          (0, xf["Height"]))])
            gb = bbox_of_points([p for sp in subpaths for p in sp["points"]]) if subpaths else box
            ang = sum(x["Angle"] for x in chain)
            # text
            txt = sh.get("text") or ""
            plain = "".join(ch for ch in txt.replace("\r", "\n").replace(" ", "\n").replace(" ", "\n")
                            if ch in "\n\t" or (ord(ch) >= 32 and ch != "￼")).strip("\n")
            chars = sh.list_get("chars", "text")
            paras = sh.list_get("paras", "text")
            c0 = chars[0] if chars else {"font": 0, "color": "#000000", "style": 0, "size": 0.1667}
            tb = sh.style_get("tblock", "text") or {"valign": 1}
            font = {"family": vd.fonts.get(c0["font"]) or "Arial", "size": round(c0["size"] * scale, 2),
                    "color": c0["color"] or "#000000", "bold": bool(c0["style"] & 1), "italic": bool(c0["style"] & 2),
                    "underline": bool(c0["style"] & 4),
                    "align": {0: "left", 1: "center", 2: "right"}.get(paras[0]["align"] if paras else 1, "center"),
                    "valign": {0: "top", 1: "middle", 2: "bottom"}.get(tb["valign"], "middle")}
            text_bbox, text_angle = None, 0.0
            if plain.strip():
                tx = sh.txform()
                if tx and tx["TxtWidth"] > 0 and tx["TxtHeight"] > 0:
                    c, sn = math.cos(tx["TxtAngle"]), math.sin(tx["TxtAngle"])
                    tc = [(tx["TxtPinX"] + (x - tx["TxtLocPinX"]) * c - (y - tx["TxtLocPinY"]) * sn,
                           tx["TxtPinY"] + (x - tx["TxtLocPinX"]) * sn + (y - tx["TxtLocPinY"]) * c)
                          for x, y in ((0, 0), (tx["TxtWidth"], 0), (tx["TxtWidth"], tx["TxtHeight"]),
                                       (0, tx["TxtHeight"]))]
                    text_bbox = bbox_of_points([to_px(tf(p)) for p in tc])
                    text_angle = math.degrees(ang + tx["TxtAngle"])
                else:
                    text_bbox, text_angle = box, math.degrees(ang)
            # style
            fl = sh.style_get("fill", "fill") or {"fg": "#ffffff", "alpha": 0, "pattern": 1}
            ln = sh.style_get("line", "line") or {"weight": 0.01, "color": "#000000", "alpha": 0, "pattern": 1,
                                                   "rounding": 0.0, "begin_arrow": 0, "end_arrow": 0}
            fill = fl["fg"] if fl["pattern"] != 0 and fl["alpha"] < 250 else None
            stroke = ln["color"] if ln["pattern"] != 0 and ln["alpha"] < 250 else None
            x1d = sh.get("xform1d")
            kind = "group" if s.kind == 0x47 else _classify_kind(subpaths, has_ell)
            pid = f"s{s.parent}" if s.parent is not None and s.parent in by_id else None
            elem = {
                "id": f"s{s.id}", "vsdx_id": str(s.id), "kind": kind,
                "type_attr": {0x47: "Group", 0x48: "Shape", 0x4e: "Foreign"}[s.kind],
                "master": sh.master_name, "name": None,
                "bbox": [round(v, 3) for v in (gb if kind != "group" else box)],
                "shape_box": [round(v, 3) for v in box], "subpaths": subpaths, "fill": fill, "stroke": stroke,
                "stroke_width": round(max(ln["weight"], 0.0) * scale, 3), "dashed": ln["pattern"] > 1,
                "arrow_start": visio_arrow(ln["begin_arrow"]) if x1d else "none",
                "arrow_end": visio_arrow(ln["end_arrow"]) if x1d else "none",
                "rounding": round(abs(ln["rounding"]) * scale, 3) if math.isfinite(ln["rounding"]) else 0.0,
                "text": plain, "html": "", "font": font,
                "text_bbox": [round(v, 3) for v in text_bbox] if text_bbox else None,
                "text_angle": round(text_angle, 3), "rotation": round(math.degrees(ang), 3),
                "flip_x": xf["FlipX"], "flip_y": xf["FlipY"], "z": z, "parent": pid, "one_d": bool(x1d),
                "hidden": False, "user": {}, "relations": {},
            }
            if s.kind == 0x4e:
                elem["foreign"] = True  # ảnh nhúng: chỉ giữ khung
            if x1d:
                b, e = (x1d[0], x1d[1]), (x1d[2], x1d[3])
                for x in parent_chain:
                    b, e = _xf_apply(x, b), _xf_apply(x, e)
                elem["begin"], elem["end"] = to_px(b), to_px(e)
            page["elements"] += _split_text(elem)
        _hoist_frame_titles(page["elements"])
        pages.append(page)
    return {"backend": "vsdbin", "scale": scale, "pages": pages}
