"""Phần dùng chung của ConvertVisioToDrawio: đường dẫn, cấu hình, ghép file, popup LibreOffice."""
from __future__ import annotations

import json
import os
import sys
import webbrowser
from pathlib import Path

APP = Path(__file__).resolve().parent          # ...\ConvertVisioToDrawio\app
ROOT = APP.parent                               # ...\ConvertVisioToDrawio
for p in (str(APP / "lib"), str(APP)):
    if p not in sys.path:
        sys.path.insert(0, p)

from vsd2drawio.ingest import SourceSet, find_drawio, find_soffice  # noqa: E402

SETTINGS = ROOT / "settings.json"
LO_URL = "https://www.libreoffice.org/download/download-libreoffice/"
DRAWIO_URL = "https://github.com/jgraph/drawio-desktop/releases"
VISIO_EXT = {".vsd", ".vsdx"}


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(data: dict) -> None:
    try:
        SETTINGS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def apply_settings(st: dict) -> None:
    """Đường dẫn LibreOffice / draw.io người dùng tự chọn → biến môi trường mà lõi vsd2drawio đọc."""
    if st.get("soffice") and Path(st["soffice"]).exists():
        os.environ["VSD2DRAWIO_SOFFICE"] = st["soffice"]
    if st.get("drawio") and Path(st["drawio"]).exists():
        os.environ["VSD2DRAWIO_DRAWIO"] = st["drawio"]


def make_sources(paths) -> list[SourceSet]:
    """Ghép .vsd với .vsdx cùng tên trong cùng thư mục (tự lấy .vsdx nằm cạnh nếu có)."""
    groups: dict[tuple[str, str], dict] = {}
    for p in map(Path, paths):
        if p.suffix.lower() not in VISIO_EXT or p.name.startswith(("~$", ".~lock")):
            continue
        g = groups.setdefault((str(p.parent.resolve()).lower(), p.stem.lower()), {})
        g[p.suffix.lower()] = p.resolve()
    out = []
    for g in groups.values():
        vsd, vsdx, note = g.get(".vsd"), g.get(".vsdx"), ""
        if vsd and vsdx:
            note = "ghép theo tên"
        elif vsd:
            side = vsd.with_suffix(".vsdx")
            if side.exists():
                vsdx, note = side, "tự ghép .vsdx cùng tên"
            else:
                note = "chỉ có .vsd"
        else:
            note = "chỉ có .vsdx"
        out.append(SourceSet((vsd or vsdx).stem, "", vsdx=vsdx, vsd=vsd, note=note))
    return sorted(out, key=lambda s: s.name.lower())


def needs_libreoffice(sources) -> list:
    """File chỉ có .vsd (không có .vsdx) cần LibreOffice để đọc chuẩn."""
    return [s for s in sources if s.vsd and not s.vsdx]


LO_MESSAGE = (
    "Có {n} file .vsd không kèm .vsdx:\n{names}\n\n"
    "Để chuyển .vsd chính xác, máy cần cài LibreOffice (miễn phí). Máy này CHƯA có LibreOffice.\n\n"
    "• Tải LibreOffice: mở trang tải về, cài xong rồi bấm Convert lại.\n"
    "• Chọn soffice.exe…: nếu đã cài LibreOffice ở thư mục khác (bản portable…).\n"
    "• Bỏ qua: chuyển bằng bộ đọc .vsd dự phòng (thử nghiệm, chỉ Visio 2003–2010). "
    "CÓ THỂ LỖI hoặc thiếu chi tiết; không có ảnh gốc để so.\n\n"
    "Cách khác: mở file bằng Visio, Save As .vsdx, rồi chuyển file .vsdx."
)


def libreoffice_dialog(parent, missing) -> str:
    """Popup yêu cầu cài LibreOffice. Trả về 'install' | 'path' | 'skip' | 'cancel'."""
    import tkinter as tk
    from tkinter import filedialog, ttk

    names = "\n".join(f"  - {s.vsd.name}" for s in missing[:8]) + ("\n  …" if len(missing) > 8 else "")
    res = {"v": "cancel"}
    own_root = parent is None
    if own_root:
        parent = tk.Tk()
        parent.withdraw()
    win = tk.Toplevel(parent)
    win.title("Cần cài LibreOffice để chuyển file .vsd")
    win.resizable(False, False)
    win.transient(parent)
    frm = ttk.Frame(win, padding=16)
    frm.pack(fill="both", expand=True)
    ttk.Label(frm, text="⚠  Cần LibreOffice", font=("Segoe UI", 12, "bold")).pack(anchor="w")
    ttk.Label(frm, text=LO_MESSAGE.format(n=len(missing), names=names), wraplength=520, justify="left").pack(anchor="w", pady=(8, 12))
    btns = ttk.Frame(frm)
    btns.pack(fill="x")

    def done(v):
        res["v"] = v
        win.destroy()

    def choose():
        f = filedialog.askopenfilename(parent=win, title="Chọn soffice.exe của LibreOffice",
                                       filetypes=[("soffice.exe", "soffice.exe"), ("Chương trình", "*.exe")])
        if f:
            st = load_settings()
            st["soffice"] = f
            save_settings(st)
            apply_settings(st)
            done("path")

    def install():
        webbrowser.open(LO_URL)
        done("install")
    ttk.Button(btns, text="Tải LibreOffice", command=install).pack(side="left")
    ttk.Button(btns, text="Chọn soffice.exe…", command=choose).pack(side="left", padx=6)
    ttk.Button(btns, text="Bỏ qua (có thể lỗi)", command=lambda: done("skip")).pack(side="left")
    ttk.Button(btns, text="Hủy", command=lambda: done("cancel")).pack(side="right")
    win.protocol("WM_DELETE_WINDOW", lambda: done("cancel"))
    win.update_idletasks()
    x = parent.winfo_rootx() + max(0, (parent.winfo_width() - win.winfo_width()) // 2) if not own_root else \
        (win.winfo_screenwidth() - win.winfo_width()) // 2
    y = parent.winfo_rooty() + 60 if not own_root else (win.winfo_screenheight() - win.winfo_height()) // 3
    win.geometry(f"+{max(0, x)}+{max(0, y)}")
    win.grab_set()
    win.focus_force()
    parent.wait_window(win)
    if own_root:
        parent.destroy()
    return res["v"]
