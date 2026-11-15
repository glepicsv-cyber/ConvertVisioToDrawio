# Ký hiệu Activity Diagram — cách nhận diện và quyết định

Bảng dưới dùng khi quyết định `type` cho node trong `overrides.yaml`. Cột "Trong
file Visio thường gặp" mô tả cả bộ UML lẫn bộ Flowchart cơ bản (nhiều sơ đồ của
dự án vẽ AD bằng shape Flowchart + Cross-Functional Flowchart).

| type | Ký hiệu UML | Trong file Visio thường gặp | Dấu hiệu quyết định |
|---|---|---|---|
| `initial` | Chấm tròn tô đặc | "Initial node"; hoặc Start/End (viên thuốc) ghi "Begin/Start/Bắt đầu" | Điểm bắt đầu, chỉ có cạnh ra |
| `activity_final` | Tròn có vòng ngoài, lõi đặc | "Final node"; hoặc Start/End ghi "End/Kết thúc" | Kết thúc toàn hoạt động, chỉ có cạnh vào |
| `flow_final` | Tròn có dấu X | "Flow final node" | Kết thúc một luồng, không kết thúc cả hoạt động |
| `action` | Chữ nhật bo góc có tên động từ | "Action"; Flowchart "Process" (thường góc vuông) | Câu mô tả hành động ("Gửi lệnh…", "Xử lý…"), có cạnh vào và ra |
| `decision` | Hình thoi, 1 vào nhiều ra có guard | "Decision" | Có câu hỏi ("…thành công?") hoặc các nhánh có nhãn `[…]` |
| `merge` | Hình thoi, nhiều vào 1 ra | "Merge"/"Decision" không chữ | Không có chữ, ≥2 cạnh vào, 1 cạnh ra |
| `fork_join` | Thanh đặc dài mảnh (ngang/dọc) | "Fork node"/"Join node"; group thanh đen | 1 vào nhiều ra (fork) hoặc nhiều vào 1 ra (join) |
| `object` | Chữ nhật góc vuông, tên danh từ | "Object node", "Data", "Document" | Tên là dữ liệu/tài liệu ("Hóa đơn", "Biên nhận"), nối bằng object flow |
| `note` | Hình gập góc, nối bằng nét đứt | "Note", "Comment", "Side line callout" | Đoạn chú thích, không nằm trong luồng điều khiển |
| `send_signal` | Ngũ giác lồi (mũi tên) | "Send signal" | Gửi tín hiệu/sự kiện ra ngoài |
| `receive_signal` | Ngũ giác lõm (khía) | "Accept event", "Receive signal" | Chờ/nhận tín hiệu/sự kiện |
| `text` | Chữ tự do | Text box | Tiêu đề, chú thích rời không thuộc node |
| `decor` | — | Khung trang trí, dải lề pool, footer trang nền | Không mang ngữ nghĩa AD, chỉ giữ hình |
| `unknown` | — | Bất kỳ hình nào khác | Không đủ căn cứ; tool vẽ đúng hình gốc |

## Gợi ý quyết định

- **Hộp chữ nhật góc vuông có chữ**: luật mặc định của dự án coi là `action` (bước xử
  lý vẽ bằng shape Process). Nếu nội dung là dữ liệu/tài liệu (danh từ: "Hóa đơn",
  "Biên nhận") nối bằng object flow thì override thành `object`. Nếu một bộ file dùng hộp
  vuông cho Object node ở nhiều chỗ, đề xuất chạy bộ đó với rules riêng
  (`rect_square_with_text: {type: object, confidence: 0.6}`) thay vì override từng node.
- **Khung chữ nhật nét đứt bao một nhóm node**: thường là vùng (interruptible
  region/partition) hoặc chỉ để nhóm trực quan → `decor` (giữ hình), không phải action.
- **Viên thuốc "Begin"/"End"**: `initial` / `activity_final` (tool giữ nguyên hình viên
  thuốc khi vẽ; ngữ nghĩa ghi trong thuộc tính `ad_type`).
- **Hình "Off-page reference"** (ngũ giác chỉ xuống, ghi tên trang/luồng khác):
  coi là `send_signal` nếu là điểm đi ra luồng khác, `receive_signal` nếu là điểm vào;
  không chắc thì `accept` để giữ `unknown`.
- **Thanh fork/join có mũi tên nhỏ ở đầu thanh**: đó là chi tiết trang trí của master
  Visio; nếu bị tách thành node riêng thì `merge` vào thanh.
- **Lane**: node thuộc lane chứa phần lớn diện tích của nó. Ghi chú (`note`) đè ranh
  giới thường vẫn thuộc lane chứa đối tượng mà nó chú thích.
- **Guard `[…]`**: thuộc cạnh đi ra từ decision gần nhãn nhất, theo hướng nhãn được
  đặt (thường ngay sau hình thoi trên nhánh đó).
- **Hướng cạnh**: theo đầu mũi tên trên ảnh gốc; cạnh không có mũi tên nối với note
  là `annotation`.
