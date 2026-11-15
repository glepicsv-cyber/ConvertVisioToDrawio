"""Giao diện dòng lệnh vsd2drawio.

    vsd2drawio run                  # xử lý mọi file trong original_files/
    vsd2drawio convert <file>       # một file (.vsd hoặc .vsdx)
    vsd2drawio status               # bảng: file, trạng thái, điểm, số issue
    vsd2drawio ingest|reconstruct|map|verify <file>   # chạy riêng từng bước
"""
from __future__ import annotations

import argparse
import csv
import shutil
import sys
import time
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path

from . import __version__
from .ingest import SourceSet, auto_backend, discover, ingest, source_for_file
from .mapper import write_drawio
from .model import load_json, load_model, save_json
from .overrides import apply_overrides, ensure_template, load_overrides, open_issues
from .reconstruct import load_rules, model_to_dict, reconstruct
from .verify import verify


@dataclass
class Config:
    in_dir: Path
    out_dir: Path
    work_dir: Path
    rules_path: Path | None
    scale: float
    backend: str
    render: bool
    force: bool
    accept_review: bool

    @property
    def rules(self) -> dict:
        return load_rules(self.rules_path)


def _safe_dirs(cfg: Config) -> None:
    """original_files/ là CHỈ ĐỌC: không cho work/out nằm bên trong nó."""
    src = cfg.in_dir.resolve()
    for d in (cfg.out_dir, cfg.work_dir):
        r = d.resolve()
        if r == src or src in r.parents:
            raise SystemExit(f"Thư mục ghi '{d}' không được nằm trong thư mục gốc chỉ-đọc '{cfg.in_dir}'")


def work_dir_for(cfg: Config, src: SourceSet) -> Path:
    return cfg.work_dir / src.rel_dir / src.name if src.rel_dir else cfg.work_dir / src.name


def out_path_for(cfg: Config, src: SourceSet) -> Path:
    return (cfg.out_dir / src.rel_dir if src.rel_dir else cfg.out_dir) / f"{src.name}.drawio"


# ------------------------------------------------------------- các bước


def step_ingest(cfg: Config, src: SourceSet, log=print) -> dict:
    wd = work_dir_for(cfg, src)
    log(f"  ingest  ({auto_backend(src) if cfg.backend == 'auto' else cfg.backend})")
    return ingest(src, wd, scale=cfg.scale, backend=cfg.backend, render_png=cfg.render, log=log)


def step_reconstruct(cfg: Config, src: SourceSet, log=print):
    wd = work_dir_for(cfg, src)
    raw = load_json(wd / "raw.json")
    model, issues, logs = reconstruct(raw, cfg.rules)
    save_json(wd / "model.json", model_to_dict(model))
    save_json(wd / "issues.json", [asdict(i) for i in issues])
    (wd / "reconstruct.log").write_text("\n".join(logs), encoding="utf-8")
    log(f"  reconstruct: {sum(len(p.nodes) for p in model.pages)} node, {sum(len(p.edges) for p in model.pages)} edge, "
        f"{sum(len(p.lanes) for p in model.pages)} lane, {len(issues)} issue")
    return model, issues


def step_overrides(cfg: Config, src: SourceSet, log=print):
    wd = work_dir_for(cfg, src)
    model = load_model(wd / "model.json")
    raw = load_json(wd / "raw.json")
    issues = load_json(wd / "issues.json")
    ov_path = wd / "overrides.yaml"
    ensure_template(ov_path)
    res = apply_overrides(model, load_overrides(ov_path), raw)
    save_json(wd / "model.final.json", model_to_dict(model))
    remaining = open_issues(issues, res)
    if res.count or res.errors:
        log(f"  overrides: áp {res.count} mục, {len(res.errors)} lỗi; còn {len(remaining)}/{len(issues)} issue")
    for e in res.errors:
        log(f"    ! {e}")
    return model, res, remaining


def step_map(cfg: Config, src: SourceSet, log=print) -> Path:
    wd = work_dir_for(cfg, src)
    final = wd / "model.final.json"
    model = load_model(final if final.exists() else wd / "model.json")
    out = write_drawio(model, wd / f"{src.name}.drawio")
    log(f"  map → {out}")
    return out


def step_verify(cfg: Config, src: SourceSet, remaining=None, ov_res=None, log=print) -> dict:
    wd = work_dir_for(cfg, src)
    final = wd / "model.final.json"
    model = load_model(final if final.exists() else wd / "model.json")
    raw = load_json(wd / "raw.json")
    if remaining is None:
        _, ov_res, remaining = step_overrides(cfg, src, log=lambda *_: None)
    extra = {"overrides_applied": ov_res.applied if ov_res else [], "overrides_errors": ov_res.errors if ov_res else [],
             "issues_total": len(load_json(wd / "issues.json")), "ai_decisions": ov_res.count if ov_res else 0}
    rep = verify(wd, wd / f"{src.name}.drawio", model, raw, remaining, cfg.rules, render=cfg.render, extra=extra, log=log)
    if ov_res and ov_res.errors:
        rep["errors"] += [f"overrides: {e}" for e in ov_res.errors]
        rep["status"] = "FAIL"
        save_json(wd / "report.json", rep)
    log(f"  verify: {rep['status']}  điểm={rep['score']}  lỗi={len(rep['errors'])}  issue mở={len(rep['open_issues'])}")
    for e in rep["errors"][:8]:
        log(f"    ✗ {e}")
    return rep


def deliver(cfg: Config, src: SourceSet, rep: dict, log=print) -> bool:
    wd = work_dir_for(cfg, src)
    draft = wd / f"{src.name}.drawio"
    out = out_path_for(cfg, src)
    ok = rep["status"] == "PASS" or (rep["status"] == "REVIEW" and cfg.accept_review)
    if ok and draft.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(draft, out)
        log(f"  → {out}")
        return True
    if rep["status"] == "FAIL" and out.exists():
        out.unlink()  # file FAIL không bao giờ nằm trong drawio_files/
        log(f"  ! đã gỡ bản cũ {out} vì trạng thái FAIL")
    return False


def needs_run(cfg: Config, src: SourceSet) -> bool:
    if cfg.force:
        return True
    out = out_path_for(cfg, src)
    if not out.exists():
        return True
    newest = max(p.stat().st_mtime for p in src.sources)
    ov = work_dir_for(cfg, src) / "overrides.yaml"
    if ov.exists():
        newest = max(newest, ov.stat().st_mtime)
    return out.stat().st_mtime <= newest


def convert_one(cfg: Config, src: SourceSet, log=print) -> dict:
    wd = work_dir_for(cfg, src)
    wd.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    try:
        step_ingest(cfg, src, log)
        step_reconstruct(cfg, src, log)
        _, ov_res, remaining = step_overrides(cfg, src, log)
        step_map(cfg, src, log)
        rep = step_verify(cfg, src, remaining, ov_res, log)
    except Exception as ex:
        rep = {"file": src.name, "source": {"vsd": str(src.vsd) if src.vsd else None,
                                            "vsdx": str(src.vsdx) if src.vsdx else None, "pairing": src.note},
               "status": "FAIL", "score": None, "errors": [f"{type(ex).__name__}: {ex}"],
               "traceback": traceback.format_exc(), "open_issues": [], "pages": [], "totals": {}}
        save_json(wd / "report.json", rep)
        log(f"  ✗ FAIL: {ex}")
    rep["delivered"] = deliver(cfg, src, rep, log)
    rep["seconds"] = round(time.time() - t0, 1)
    save_json(wd / "report.json", rep)
    return rep


# ------------------------------------------------------------- bảng trạng thái


def collect_status(cfg: Config) -> list[dict]:
    rows = []
    for src in discover(cfg.in_dir):
        wd = work_dir_for(cfg, src)
        rp = wd / "report.json"
        rep = load_json(rp) if rp.exists() else None
        out = out_path_for(cfg, src)
        rows.append({
            "file": src.key, "sources": " + ".join(p.name for p in src.sources), "backend": (rep or {}).get("backend") or "",
            "status": (rep or {}).get("status", "CHƯA CHẠY"), "score": (rep or {}).get("score"),
            "nodes": (rep or {}).get("totals", {}).get("nodes", ""), "edges": (rep or {}).get("totals", {}).get("edges", ""),
            "lanes": (rep or {}).get("totals", {}).get("lanes", ""),
            "issues_open": len((rep or {}).get("open_issues", [])) if rep else "",
            "ai_decisions": (rep or {}).get("ai_decisions", ""), "errors": len((rep or {}).get("errors", [])) if rep else "",
            "delivered": "có" if out.exists() else "không", "note": src.note,
        })
    return rows


def print_table(rows: list[dict]) -> None:
    cols = [("file", "File"), ("backend", "Nguồn"), ("status", "Trạng thái"), ("score", "Điểm"), ("nodes", "Node"),
            ("edges", "Edge"), ("lanes", "Lane"), ("issues_open", "Issue"), ("errors", "Lỗi"),
            ("delivered", "drawio_files")]
    data = [[("" if r[k] is None else (f"{r[k]:.3f}" if isinstance(r[k], float) else str(r[k]))) for k, _ in cols] for r in rows]
    widths = [max(len(h), *(len(d[i]) for d in data)) if data else len(h) for i, (_, h) in enumerate(cols)]
    print("  ".join(h.ljust(w) for (_, h), w in zip(cols, widths)))
    print("  ".join("-" * w for w in widths))
    for d in data:
        print("  ".join(x.ljust(w) for x, w in zip(d, widths)))


def write_summary(cfg: Config, rows: list[dict]) -> Path:
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    p = cfg.work_dir / "summary.csv"
    with p.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["file"])
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return p


# ------------------------------------------------------------- main


def _cfg(a) -> Config:
    return Config(in_dir=Path(a.in_dir), out_dir=Path(a.out_dir), work_dir=Path(a.work_dir),
                  rules_path=Path(a.rules) if a.rules else None, scale=a.scale, backend=a.backend,
                  render=not a.no_render, force=getattr(a, "force", False), accept_review=getattr(a, "accept_review", False))


def _resolve_src(cfg: Config, file: str) -> SourceSet:
    p = Path(file)
    if not p.exists():
        cand = cfg.in_dir / file
        if cand.exists():
            p = cand
        else:
            matches = [s for s in discover(cfg.in_dir) if s.key == file or s.name == file
                       or any(x.name == file for x in s.sources)]
            if matches:
                return matches[0]
            raise SystemExit(f"Không tìm thấy file '{file}'")
    return source_for_file(p, cfg.in_dir)


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="vsd2drawio", description="Chuyển Activity Diagram Visio (.vsd/.vsdx) sang draw.io")
    ap.add_argument("--version", action="version", version=__version__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--in", dest="in_dir", default="original_files", help="thư mục file gốc (chỉ đọc)")
    common.add_argument("--out", dest="out_dir", default="drawio_files", help="thư mục .drawio hoàn chỉnh")
    common.add_argument("--work", dest="work_dir", default="work", help="thư mục trung gian")
    common.add_argument("--rules", default=None, help="rules.yaml tùy chỉnh (gộp đè lên mặc định)")
    common.add_argument("--scale", type=float, default=96.0, help="px trên mỗi inch (mặc định 96)")
    common.add_argument("--backend", choices=["auto", "vsdx", "fodg", "vsdbin"], default="auto",
                        help="auto: dùng .vsdx nếu có; .vsd qua LibreOffice (fodg) nếu có, "
                             "không thì đọc nhị phân trực tiếp (vsdbin, thử nghiệm)")
    common.add_argument("--no-render", action="store_true", help="bỏ qua render/so ảnh")
    deliver_opts = argparse.ArgumentParser(add_help=False)
    deliver_opts.add_argument("--force", action="store_true", help="chạy lại cả file đã có .drawio mới hơn")
    deliver_opts.add_argument("--accept-review", action="store_true", help="chép cả file REVIEW sang drawio_files/")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", parents=[common, deliver_opts], help="xử lý mọi file trong thư mục gốc")
    c = sub.add_parser("convert", parents=[common, deliver_opts], help="xử lý một file")
    c.add_argument("file")
    sub.add_parser("status", parents=[common], help="bảng trạng thái")
    for name in ("ingest", "reconstruct", "map", "verify"):
        s = sub.add_parser(name, parents=[common, deliver_opts], help=f"chỉ chạy bước {name}")
        s.add_argument("file")
    a = ap.parse_args(argv)
    cfg = _cfg(a)
    _safe_dirs(cfg)

    if a.cmd == "status":
        rows = collect_status(cfg)
        print_table(rows)
        return 0
    if a.cmd == "run":
        sources = discover(cfg.in_dir)
        if not sources:
            print(f"Không có file .vsd/.vsdx trong {cfg.in_dir}")
            return 0
        for src in sources:
            if not needs_run(cfg, src):
                print(f"= {src.key}: bỏ qua (đã có .drawio mới hơn file gốc; dùng --force để chạy lại)")
                continue
            print(f"▶ {src.key}  [{' + '.join(p.name for p in src.sources)}] ({src.note})")
            convert_one(cfg, src)
        rows = collect_status(cfg)
        p = write_summary(cfg, rows)
        print()
        print_table(rows)
        print(f"\nĐã ghi {p}")
        return 1 if any(r["status"] == "FAIL" for r in rows) else 0
    src = _resolve_src(cfg, a.file)
    print(f"▶ {src.key}  [{' + '.join(p.name for p in src.sources)}] ({src.note})")
    if a.cmd == "convert":
        rep = convert_one(cfg, src)
        write_summary(cfg, collect_status(cfg))
        return 1 if rep["status"] == "FAIL" else 0
    if a.cmd == "ingest":
        step_ingest(cfg, src)
    elif a.cmd == "reconstruct":
        step_reconstruct(cfg, src)
    elif a.cmd == "map":
        step_overrides(cfg, src)
        step_map(cfg, src)
    elif a.cmd == "verify":
        rep = step_verify(cfg, src)
        rep["delivered"] = deliver(cfg, src, rep)
        save_json(work_dir_for(cfg, src) / "report.json", rep)
        return 1 if rep["status"] == "FAIL" else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
