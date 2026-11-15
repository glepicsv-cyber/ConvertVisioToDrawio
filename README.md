# ConvertVisioToDrawio

Chuyển sơ đồ Visio (`.vsd`, `.vsdx`), nhất là Activity Diagram có swimlane, sang draw.io
(`.drawio`). Giữ nguyên layout, swimlane, connector và chữ. Chạy hoàn toàn trên máy, không cần
AI, không gửi file ra ngoài.

Dùng **toàn bộ bộ xử lý `vsd2drawio`**: đọc file, suy luận pool/lane và ký hiệu Activity Diagram
theo `app\vsd2drawio\rules.yaml`, áp `overrides.yaml`, sinh `.drawio`, rồi kiểm tra tự động.

## 1. Cài đặt (một lần)

| Phần mềm | Bắt buộc? | Dùng để |
|---|---|---|
| **Python 3.10 trở lên** | Bắt buộc | Chạy tool. Tải ở https://www.python.org/downloads/windows/, khi cài tích **Add python.exe to PATH** (cài cho riêng user, không cần quyền admin). |
| **LibreOffice** | Bắt buộc với file `.vsd` | Đọc `.vsd` chuẩn và render ảnh gốc để so. Tải ở https://www.libreoffice.org/download/download-libreoffice/ |
| **draw.io Desktop** | Không bắt buộc | Render ảnh `.drawio` để so với ảnh gốc. Tải ở https://github.com/jgraph/drawio-desktop/releases |

- Thư viện PyYAML và Pillow đã kèm sẵn trong `app\lib` (bản cho Python 3.12, 64-bit). Python bản
  khác vẫn chạy được phần chuyển đổi; nếu báo thiếu `yaml` thì chạy `python -m pip install pyyaml pillow`.
- Giải nén zip ra một thư mục bất kỳ (đường dẫn nên ngắn, ví dụ `D:\Tools\ConvertVisioToDrawio`).

## 2. Cách dùng

### Giao diện (khuyên dùng)

1. Bấm đúp **`ConvertVisioToDrawio.bat`**.
2. **Chọn file…** (một hoặc nhiều `.vsd`/`.vsdx`) hoặc **Chọn thư mục…** (lấy mọi file Visio
   trong thư mục, kể cả thư mục con).
3. Chọn **Thư mục kết quả** (mặc định `drawio_files\`).
4. Bấm **Convert**. Bảng hiện trạng thái từng file: PASS / REVIEW / FAIL, số node/edge/lane, số issue.
5. Mở file `.drawio` bằng draw.io Desktop hoặc https://app.diagrams.net (File → Open).

### Chạy hàng loạt

1. Chép file Visio vào thư mục **`input\`** (có thể chia thư mục con theo project).
2. Bấm đúp **`ConvertVisioToDrawio-HangLoat.bat`**.
3. Kết quả nằm ở `drawio_files\` (cùng cấu trúc thư mục con), chi tiết từng file ở `work\`,
   bảng tổng hợp ở `work\summary.csv`.
4. Muốn chỉ xuất file PASS (bỏ file REVIEW): mở cmd tại thư mục tool và chạy
   `ConvertVisioToDrawio-HangLoat.bat --strict`.

### File `.vsd` và LibreOffice

- Có cả `X.vsd` và `X.vsdx` cùng tên trong một thư mục: tool tự ghép, đọc cấu trúc từ `.vsdx`.
- File **chỉ có `.vsd`** mà máy **chưa cài LibreOffice**: tool hiện **popup yêu cầu cài LibreOffice**:
  - **Tải LibreOffice**: mở trang tải về. Cài xong bấm Convert lại (không cần tắt tool).
  - **Chọn soffice.exe…**: nếu LibreOffice nằm ở thư mục khác (bản portable…). Đường dẫn được
    nhớ trong `settings.json`.
  - **Bỏ qua (có thể lỗi)**: chuyển bằng bộ đọc `.vsd` dự phòng (thử nghiệm, chỉ Visio 2003–2010).
    Có thể lỗi hoặc thiếu chi tiết, và không có ảnh gốc để so.
  - **Hủy**: không chuyển.
- Cách chắc chắn nhất khi không cài được LibreOffice: mở file bằng Visio, **Save As `.vsdx`**, rồi
  chuyển file `.vsdx`.

## 3. Trạng thái kết quả

| Trạng thái | Nghĩa | Có xuất `.drawio`? |
|---|---|---|
| **PASS** | Không lỗi cấu trúc, không còn chỗ chưa chắc | Có |
| **REVIEW** | Không lỗi, nhưng còn issue (chỗ tool chưa chắc) | Có khi bật "Xuất cả file REVIEW" (mặc định bật); bản hàng loạt xuất trừ khi chạy `--strict` |
| **FAIL** | Lỗi cấu trúc (thiếu node/edge/lane/chữ, lệch vị trí…) hoặc lỗi đọc file | Không (bản cũ cùng tên trong thư mục kết quả bị gỡ) |

Kiểm tra tự động gồm:
- đối chiếu số node, edge, lane;
- không thiếu đoạn chữ nào;
- vị trí lệch không quá 2px;
- node nằm đúng lane cha;
- edge đủ hai đầu.

Có LibreOffice và draw.io Desktop thì có thêm điểm so ảnh với bản gốc.

## 4. Xem chi tiết và sửa issue (không cần AI)

Chọn một dòng rồi bấm **Mở chi tiết**. Thư mục `work\<tên file>\` gồm:

| File | Nội dung |
|---|---|
| `report.json` | Trạng thái, lỗi, issue còn mở, điểm so ảnh, vùng lệch nhất |
| `issues.json` | Danh sách issue: id (`I001`…), trang, loại, đối tượng (`n…` node, `c…` connector, `L…` lane), ứng viên, bằng chứng |
| `model.json` | Mô hình tool dựng: pool, lane, node, edge (kèm `confidence`, `evidence`) |
| `overrides.yaml` | **Nơi sửa** quyết định của tool |
| `compare\` | `pageN_drawio.png` (ảnh kết quả); có LibreOffice thì thêm `pageN_orig.png`, `pageN_side.png`, `pageN_diff.png` |

Để sửa: bấm **Sửa overrides.yaml**, thêm dòng theo mẫu dưới, lưu lại, rồi bấm **Chuyển lại file đang chọn**.

```yaml
nodes:
  - {page: "Trang 1", id: n21, type: action, reason: "hộp Process là bước xử lý"}
  - {page: 1, id: n45, lane: L8, reason: "nằm trong cột Hệ thống"}
  - {page: 1, id: n46, text: "Từ chối đơn", reason: "sửa chữ"}
edges:
  - {page: 1, id: c42, source: n39, target: n113, reason: "nối đúng hình"}
  - {page: 1, id: c73, reverse: true, reason: "đảo hướng mũi tên"}
  - {page: 1, id: c128, label_from: c127, reason: "[No] thuộc nhánh c128"}
  - {page: 1, id: c9, kind: annotation, reason: "đường nối ghi chú"}
merge:
  - {page: 1, ids: [n68, n70], type: fork_join, reason: "hai mảnh của cùng thanh fork"}
accept:
  - {id: I003, reason: "giữ như bản gốc"}
```

- `page` là tên trang hoặc số thứ tự (từ 1).
- Loại node hợp lệ: initial, activity_final, flow_final, action, decision, merge, fork_join,
  object, note, send_signal, receive_signal, text, decor, unknown.
- Cách nhận diện ký hiệu và hướng xử lý từng loại issue: xem `docs\ad-notation.md`.
- Không ghi tọa độ vào overrides, vì tọa độ luôn do tool tính. Không sửa tay file `.drawio`.

## 5. Luật chuyển đổi (rules.yaml)

- Toàn bộ ngưỡng và luật nằm ở `app\vsd2drawio\rules.yaml`: bắt mút connector, nhãn, lane, so
  ảnh, phân loại hình học, bảng ánh xạ master Visio sang loại AD (`lane_masters`, `pool_masters`,
  `connector_masters`, `callout_masters`, `ignore_masters`…).
- Muốn chỉnh cho một bộ file: chép `rules.yaml` ra file riêng, giữ lại phần cần đổi, rồi chọn file
  đó ở ô **Rules tùy chỉnh**. File riêng được gộp đè lên bộ luật mặc định.
- Ví dụ: bộ file vẽ Object node bằng hộp vuông thì dùng
  `classify: {rect_square_with_text: {type: object, confidence: 0.6}}`.

## 6. Thư mục

| Thư mục / file | Vai trò |
|---|---|
| `ConvertVisioToDrawio.bat` | Mở giao diện |
| `ConvertVisioToDrawio-HangLoat.bat` | Chuyển mọi file trong `input\` |
| `input\` | File Visio cho chế độ hàng loạt (tool chỉ đọc) |
| `drawio_files\` | Kết quả `.drawio` |
| `work\` | Dữ liệu trung gian, report, issue, overrides (tự tạo) |
| `app\vsd2drawio\` | Bộ xử lý và `rules.yaml` |
| `app\lib\` | Thư viện kèm (PyYAML, Pillow, kèm giấy phép) |
| `docs\ad-notation.md` | Ký hiệu Activity Diagram và cách quyết định issue |
| `settings.json` | Lựa chọn của giao diện (tự tạo) |

## 7. Giới hạn

- `.vsd` không có LibreOffice chỉ đọc được Visio 2003–2010, ảnh nhúng chỉ còn khung, field (ngày,
  số trang…) chỉ còn dấu phân cách.
- Chữ có thể xuống dòng hơi khác Visio do khác phông.
- Cung tròn/đường cong được xấp xỉ bằng đường gấp khúc.
- Trang nền (Background) của Visio xuất thành một trang riêng.
- Tool không bao giờ ghi vào thư mục chứa file gốc; LibreOffice chỉ chạy trên bản chép trong `work\`.

Nguồn: bộ công cụ `vsd2drawio` (Tools\VS2Drawio), phiên bản đóng gói không cần AI.
