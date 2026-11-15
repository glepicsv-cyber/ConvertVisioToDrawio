"""Chạy hàng loạt: chuyển mọi file .vsd/.vsdx trong thư mục input\\ sang drawio_files\\.

Dùng lại CLI vsd2drawio (cùng rules, overrides, kiểm tra). Trước khi chạy, nếu có file .vsd
không kèm .vsdx mà máy chưa có LibreOffice thì hiện popup yêu cầu cài.
Tham số thêm được chuyển thẳng cho vsd2drawio (vd. --rules my_rules.yaml, --no-render).
"""
from __future__ import annotations

import sys

import cvd_common as C

from vsd2drawio.cli import main as cli_main
from vsd2drawio.ingest import discover


def run(argv: list[str]) -> int:
    C.apply_settings(C.load_settings())
    in_dir = C.ROOT / "input"
    in_dir.mkdir(exist_ok=True)
    sources = discover(in_dir)
    if not sources:
        print(f"Không có file .vsd/.vsdx trong {in_dir}. Chép file Visio vào đó rồi chạy lại.")
        return 0
    missing = C.needs_libreoffice(sources)
    if missing and not C.find_soffice():
        ans = C.libreoffice_dialog(None, missing)
        if ans == "install":
            print("Đã mở trang tải LibreOffice. Cài xong hãy chạy lại.")
            return 2
        if ans == "cancel":
            print("Đã hủy.")
            return 2
        if ans == "skip":
            print("! Bỏ qua LibreOffice: file .vsd chuyển bằng bộ đọc dự phòng (thử nghiệm), có thể lỗi.")
    args = ["run", "--in", str(in_dir), "--out", str(C.ROOT / "drawio_files"), "--work", str(C.ROOT / "work")]
    if "--strict" in argv:
        argv = [a for a in argv if a != "--strict"]
    elif "--accept-review" not in argv:
        args.append("--accept-review")
    return cli_main(args + argv)


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
