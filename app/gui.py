"""ConvertVisioToDrawio — chuyển sơ đồ Visio (.vsd/.vsdx) sang draw.io, không cần AI.

Dùng nguyên bộ xử lý vsd2drawio (đọc file → suy luận pool/lane/ký hiệu AD theo rules.yaml →
overrides.yaml → sinh .drawio → kiểm tra). Chạy: ConvertVisioToDrawio.bat
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import traceback
import webbrowser
from pathlib import Path

import cvd_common as C  # thiết lập sys.path (app, app/lib) trước khi import vsd2drawio

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from vsd2drawio import __version__
from vsd2drawio.cli import Config, convert_one, work_dir_for
from vsd2drawio.overrides import ensure_template

STATUS_VI = {"PASS": "PASS", "REVIEW": "REVIEW", "FAIL": "FAIL"}


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.st = C.load_settings()
        C.apply_settings(self.st)
        self.sources = []          # list[SourceSet]
        self.reports = {}          # name -> report
        self.q = queue.Queue()
        self.busy = False
        self.skip_lo = False       # người dùng đã chọn "Bỏ qua" trong phiên này
        root.title(f"Convert Visio → draw.io  (vsd2drawio {__version__})")
        root.geometry("1100x680")
        root.minsize(760, 480)
        self.out_var = tk.StringVar(value=self.st.get("out_dir") or str(C.ROOT / "drawio_files"))
        self.review_var = tk.BooleanVar(value=self.st.get("accept_review", True))
        self.rules_var = tk.StringVar(value=self.st.get("rules") or "")
        self._build()
        self._refresh_tools()
        root.after(150, self._pump)

    # ------------------------------------------------------------ giao diện
    def _build(self):
        pad = {"padx": 8, "pady": 4}
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        ttk.Button(top, text="Chọn file…", command=self.pick_files).pack(side="left")
        ttk.Button(top, text="Chọn thư mục…", command=self.pick_folder).pack(side="left", padx=4)
        ttk.Button(top, text="Xóa danh sách", command=self.clear).pack(side="left")
        ttk.Button(top, text="Hướng dẫn", command=lambda: self._open(C.ROOT / "README.md")).pack(side="right")
        ttk.Button(top, text="Đường dẫn LibreOffice…", command=self.pick_soffice).pack(side="right", padx=4)
        self.tools_lbl = ttk.Label(top, text="")
        self.tools_lbl.pack(side="right", padx=10)

        cols = ("src", "status", "nodes", "edges", "lanes", "issues", "out")
        self.tree = ttk.Treeview(self.root, columns=cols, show="tree headings", height=12, selectmode="extended")
        self.tree.heading("#0", text="File")
        self.tree.column("#0", width=260)
        for c, t, w in (("src", "Nguồn", 150), ("status", "Trạng thái", 80), ("nodes", "Node", 60), ("edges", "Edge", 60),
                        ("lanes", "Lane", 50), ("issues", "Issue mở", 70), ("out", "Kết quả", 200)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="center" if c not in ("src", "out") else "w")
        self.tree.tag_configure("PASS", foreground="#1a7f37")
        self.tree.tag_configure("REVIEW", foreground="#9a6700")
        self.tree.tag_configure("FAIL", foreground="#cf222e")
        self.tree.pack(fill="both", expand=True, **pad)
        self.tree.bind("<Double-1>", lambda e: self.open_work())

        row = ttk.Frame(self.root)
        row.pack(fill="x", **pad)
        ttk.Button(row, text="Mở chi tiết (report, issue)", command=self.open_work).pack(side="left")
        ttk.Button(row, text="Sửa overrides.yaml", command=self.edit_overrides).pack(side="left", padx=4)
        ttk.Button(row, text="Chuyển lại file đang chọn", command=self.reconvert).pack(side="left")

        opt = ttk.LabelFrame(self.root, text="Tùy chọn")
        opt.pack(fill="x", **pad)
        r1 = ttk.Frame(opt)
        r1.pack(fill="x", padx=6, pady=3)
        ttk.Label(r1, text="Thư mục kết quả:").pack(side="left")
        ttk.Entry(r1, textvariable=self.out_var).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(r1, text="Đổi…", command=self.pick_out).pack(side="left")
        ttk.Button(r1, text="Mở", command=lambda: self._open(Path(self.out_var.get()))).pack(side="left", padx=4)
        r2 = ttk.Frame(opt)
        r2.pack(fill="x", padx=6, pady=3)
        ttk.Checkbutton(r2, text="Xuất cả file REVIEW (còn chỗ tool chưa chắc, xem 'Mở chi tiết')",
                        variable=self.review_var).pack(side="left")
        r3 = ttk.Frame(opt)
        r3.pack(fill="x", padx=6, pady=3)
        ttk.Label(r3, text="Rules tùy chỉnh (để trống = mặc định app\\vsd2drawio\\rules.yaml):").pack(side="left")
        ttk.Entry(r3, textvariable=self.rules_var, width=40).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(r3, text="Chọn…", command=self.pick_rules).pack(side="left")

        bot = ttk.Frame(self.root)
        bot.pack(fill="x", **pad)
        self.btn = ttk.Button(bot, text="Convert", command=self.convert_all)
        self.btn.pack(side="left")
        self.prog = ttk.Progressbar(bot, mode="determinate", length=260)
        self.prog.pack(side="left", padx=10)
        self.stat_lbl = ttk.Label(bot, text="Chọn file .vsd / .vsdx để bắt đầu.")
        self.stat_lbl.pack(side="left")

        logf = ttk.LabelFrame(self.root, text="Nhật ký")
        logf.pack(fill="both", expand=False, **pad)
        self.log = tk.Text(logf, height=9, wrap="word", font=("Consolas", 9), state="disabled")
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True)

    def _refresh_tools(self):
        lo = "có" if C.find_soffice() else "chưa cài"
        dr = "có" if C.find_drawio() else "chưa cài"
        self.tools_lbl.configure(text=f"LibreOffice: {lo}   ·   draw.io Desktop: {dr}")

    def _save(self):
        self.st.update(out_dir=self.out_var.get(), accept_review=bool(self.review_var.get()), rules=self.rules_var.get())
        C.save_settings(self.st)

    def write(self, msg: str):
        self.q.put(("log", msg))

    def _log_now(self, msg: str):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    @staticmethod
    def _open(p: Path):
        try:
            p = Path(p)
            if not p.exists() and p.suffix == "":
                p.mkdir(parents=True, exist_ok=True)
            os.startfile(str(p))
        except Exception as ex:
            messagebox.showerror("Không mở được", str(ex))

    # ------------------------------------------------------------ danh sách file
    def _add(self, paths):
        new = C.make_sources([*[s.vsd for s in self.sources if s.vsd], *[s.vsdx for s in self.sources if s.vsdx], *paths])
        before = {s.name for s in self.sources}
        self.sources = new
        self._render()
        added = [s.name for s in new if s.name not in before]
        if added:
            self._log_now(f"Đã thêm {len(added)} sơ đồ: " + ", ".join(added))
        self._check_lo(on_add=True)

    def _render(self):
        self.tree.delete(*self.tree.get_children())
        for s in self.sources:
            src = " + ".join(p.suffix for p in s.sources) + (f" ({s.note})" if s.note else "")
            rep = self.reports.get(s.name)
            if rep:
                t = rep.get("totals", {})
                out = "Đã xuất" if rep.get("delivered") else ("Không xuất (FAIL)" if rep["status"] == "FAIL" else "Chưa xuất (REVIEW)")
                vals = (src, rep["status"], t.get("nodes", ""), t.get("edges", ""), t.get("lanes", ""),
                        len(rep.get("open_issues", [])), out)
                self.tree.insert("", "end", iid=s.name, text=s.name, values=vals, tags=(rep["status"],))
            else:
                self.tree.insert("", "end", iid=s.name, text=s.name, values=(src, "Chờ", "", "", "", "", ""))
        self.stat_lbl.configure(text=f"{len(self.sources)} sơ đồ trong danh sách.")

    def pick_files(self):
        fs = filedialog.askopenfilenames(title="Chọn file Visio", initialdir=self.st.get("last_dir") or str(C.ROOT / "input"),
                                         filetypes=[("Visio", "*.vsd *.vsdx"), ("Tất cả", "*.*")])
        if fs:
            self.st["last_dir"] = str(Path(fs[0]).parent)
            self._save()
            self._add(fs)

    def pick_folder(self):
        d = filedialog.askdirectory(title="Chọn thư mục chứa file Visio", initialdir=self.st.get("last_dir") or str(C.ROOT / "input"))
        if d:
            fs = [p for p in Path(d).rglob("*") if p.is_file() and p.suffix.lower() in C.VISIO_EXT]
            if not fs:
                messagebox.showinfo("Không có file", "Thư mục không có file .vsd / .vsdx.")
                return
            self.st["last_dir"] = d
            self._save()
            self._add(fs)

    def clear(self):
        if self.busy:
            return
        self.sources, self.reports = [], {}
        self._render()

    def pick_out(self):
        d = filedialog.askdirectory(title="Chọn thư mục lưu file .drawio", initialdir=self.out_var.get())
        if d:
            self.out_var.set(d)
            self._save()

    def pick_rules(self):
        f = filedialog.askopenfilename(title="Chọn rules.yaml tùy chỉnh", filetypes=[("YAML", "*.yaml *.yml")])
        self.rules_var.set(f or "")
        self._save()

    def pick_soffice(self):
        f = filedialog.askopenfilename(title="Chọn soffice.exe của LibreOffice", filetypes=[("soffice.exe", "soffice.exe"), ("Chương trình", "*.exe")])
        if f:
            self.st["soffice"] = f
            C.save_settings(self.st)
            C.apply_settings(self.st)
            self._refresh_tools()

    def _selected(self):
        ids = self.tree.selection()
        return [s for s in self.sources if s.name in ids]

    # ------------------------------------------------------------ LibreOffice
    def _check_lo(self, on_add=False) -> bool:
        """True = được phép chạy tiếp."""
        missing = C.needs_libreoffice(self.sources)
        if not missing or C.find_soffice() or self.skip_lo:
            return True
        ans = C.libreoffice_dialog(self.root, missing)
        self._refresh_tools()
        if ans == "path" and C.find_soffice():
            self._log_now(f"Dùng LibreOffice: {C.find_soffice()}")
            return True
        if ans == "skip":
            self.skip_lo = True
            self._log_now("Bỏ qua LibreOffice: file .vsd sẽ chuyển bằng bộ đọc dự phòng (thử nghiệm), có thể lỗi.")
            return True
        if ans == "install":
            self._log_now("Đã mở trang tải LibreOffice. Cài xong hãy bấm Convert lại (không cần khởi động lại tool).")
        return False if not on_add else True

    # ------------------------------------------------------------ chuyển đổi
    def convert_all(self):
        self._run(self.sources)

    def reconvert(self):
        sel = self._selected()
        if not sel:
            messagebox.showinfo("Chưa chọn", "Chọn một hoặc nhiều dòng trong bảng.")
            return
        self._run(sel)

    def _run(self, items):
        if self.busy or not items:
            return
        if not self._check_lo():
            return
        rules = self.rules_var.get().strip()
        if rules and not Path(rules).exists():
            messagebox.showerror("Rules", f"Không thấy file rules: {rules}")
            return
        self._save()
        out = Path(self.out_var.get())
        try:
            out.mkdir(parents=True, exist_ok=True)
        except Exception as ex:
            messagebox.showerror("Thư mục kết quả", f"Không tạo được thư mục: {ex}")
            return
        cfg = Config(in_dir=C.ROOT / "input", out_dir=out, work_dir=C.ROOT / "work",
                     rules_path=Path(rules) if rules else None, scale=96.0, backend="auto",
                     render=True, force=True, accept_review=bool(self.review_var.get()))
        self.busy = True
        self.btn.configure(state="disabled")
        self.prog.configure(maximum=len(items), value=0)
        threading.Thread(target=self._worker, args=(cfg, list(items)), daemon=True).start()

    def _worker(self, cfg, items):
        for i, s in enumerate(items, start=1):
            self.q.put(("status", f"Đang chuyển {i}/{len(items)}: {s.name}…"))
            self.write(f"▶ {s.name}  [{' + '.join(p.name for p in s.sources)}]")
            try:
                rep = convert_one(cfg, s, log=self.write)
            except Exception as ex:  # convert_one đã tự bắt lỗi; đây là lớp bảo vệ cuối
                rep = {"status": "FAIL", "errors": [str(ex)], "open_issues": [], "totals": {}, "delivered": False}
                self.write(traceback.format_exc())
            self.q.put(("done", s.name, rep, i))
        self.q.put(("finish", len(items)))

    def _pump(self):
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == "log":
                    self._log_now(m[1])
                elif m[0] == "status":
                    self.stat_lbl.configure(text=m[1])
                elif m[0] == "done":
                    _, name, rep, i = m
                    self.reports[name] = rep
                    self.prog.configure(value=i)
                    self._render()
                elif m[0] == "finish":
                    self.busy = False
                    self.btn.configure(state="normal")
                    reps = list(self.reports.values())
                    n = {k: sum(1 for r in reps if r["status"] == k) for k in ("PASS", "REVIEW", "FAIL")}
                    msg = f"Xong {m[1]} sơ đồ: {n['PASS']} PASS, {n['REVIEW']} REVIEW, {n['FAIL']} FAIL."
                    self.stat_lbl.configure(text=msg)
                    self._log_now(msg + f"  Kết quả ở: {self.out_var.get()}")
        except queue.Empty:
            pass
        self.root.after(150, self._pump)

    # ------------------------------------------------------------ chi tiết / overrides
    def _cfg_dirs(self):
        return Config(in_dir=C.ROOT / "input", out_dir=Path(self.out_var.get()), work_dir=C.ROOT / "work",
                      rules_path=None, scale=96.0, backend="auto", render=True, force=True, accept_review=True)

    def open_work(self):
        sel = self._selected()
        if not sel:
            messagebox.showinfo("Chưa chọn", "Chọn một dòng trong bảng.")
            return
        wd = work_dir_for(self._cfg_dirs(), sel[0])
        if not wd.exists():
            messagebox.showinfo("Chưa có", "File này chưa được chuyển.")
            return
        self._open(wd)

    def edit_overrides(self):
        sel = self._selected()
        if not sel:
            messagebox.showinfo("Chưa chọn", "Chọn một dòng trong bảng.")
            return
        wd = work_dir_for(self._cfg_dirs(), sel[0])
        wd.mkdir(parents=True, exist_ok=True)
        ov = wd / "overrides.yaml"
        ensure_template(ov)
        try:
            subprocess.Popen(["notepad.exe", str(ov)])
        except Exception:
            self._open(ov)
        self._log_now(f"Mở {ov}. Xem issues.json cùng thư mục để biết id cần sửa; lưu xong bấm 'Chuyển lại file đang chọn'.")


def main():
    if "--smoke" in sys.argv:          # kiểm tra nhanh: dựng cửa sổ rồi đóng
        root = tk.Tk()
        App(root)
        root.after(1200, root.destroy)
        root.mainloop()
        print("smoke ok")
        return
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 1.25)
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
