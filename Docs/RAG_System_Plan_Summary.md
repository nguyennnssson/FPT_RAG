# Hệ thống RAG FPT — Tóm tắt Kế hoạch

*Hệ thống Retrieval-Augmented Generation (RAG) song ngữ (Tiếng Việt / Tiếng
Anh): trả lời câu hỏi dựa trên tài liệu nội bộ của công ty, và mọi câu trả lời
đều được truy xuất từ — và trích dẫn — nguồn thật, thay vì để mô hình tự bịa ra.*

---

## Cấu trúc tổng thể

Một hệ thống RAG gồm hai nửa. Một nửa chạy **trước** (offline) để biến tài liệu
thành cơ sở tri thức tìm kiếm được; nửa còn lại chạy **theo từng câu hỏi** để tìm
bằng chứng và viết câu trả lời có căn cứ.

```
  TÀI LIỆU ──▶  [ Xây dựng cơ sở tri thức ]  ──▶  CHỈ MỤC (INDEX)
                                                     │
  CÂU HỎI  ──▶  [ Tìm bằng chứng ]  ──▶  [ Viết câu trả lời có trích dẫn ]  ──▶  TRẢ LỜI
```

---

## Các bước tổng quát

### A. Ingestion — Xây dựng cơ sở tri thức (chạy trước)
1. **Nạp tài liệu** (.txt/.pdf/.docx/.html/.csv). *Không thể trả lời điều gì nếu tài liệu chưa từng được nạp.*
2. **Cắt thành các đoạn (chunk)** vài trăm từ, theo ranh giới tự nhiên. *Mô hình có giới hạn đầu vào, và đoạn nhỏ thì truy xuất chính xác hơn.*
3. **Bổ sung ngữ cảnh cho mỗi đoạn** (tiêu đề/mục). *Một dòng trơ như "giới hạn là 30 ngày" sẽ vô nghĩa nếu không biết nó thuộc chính sách nào.*
4. **Gắn nhãn ngôn ngữ** (VI/EN) và **kiểm tra an toàn** — phát hiện chỉ dẫn độc hại ẩn và che dữ liệu cá nhân (PII) — *trước khi* lưu trữ.
5. **Mã hóa hai cách**: vector *ngữ nghĩa* (embeddings) và chỉ mục *từ khóa* (BM25). *Tìm theo nghĩa và tìm theo từ khóa bổ trợ cho nhau.*
6. **Lưu trữ** bền vững để tồn tại qua khởi động lại, có phiên bản và xóa an toàn.

### B. Retrieval — Tìm đúng bằng chứng (theo câu hỏi)
7. **Hiểu câu hỏi**: chuyển câu hỏi nối tiếp thành câu độc lập, nhận diện ngôn ngữ.
8. **Tìm cả hai cách** (nghĩa + từ khóa) rồi **hợp nhất** hai danh sách xếp hạng.
9. **Rerank** (xếp hạng lại) các ứng viên hàng đầu bằng mô hình chính xác để tìm nội dung thực sự đúng chủ đề.
10. **Kiểm tra quyền truy cập xuyên suốt** — người dùng chỉ thấy nội dung được phép (xem *Nguyên tắc bảo vệ*).

### C. Generation — Viết câu trả lời đáng tin
11. **Lắp ghép bằng chứng**: loại trùng, sắp thứ tự, vừa với giới hạn token.
12. **Sinh câu trả lời có căn cứ và trích dẫn** đúng ngôn ngữ người dùng — hoặc **từ chối trả lời** ("không đủ thông tin") thay vì đoán bừa.
13. **Kiểm chứng mọi trích dẫn** so với nguồn thật trước khi trả về.

### D. Serving & Trải nghiệm
14. **API** — dịch vụ web nhỏ để ứng dụng gọi (`/query`, `/ingest`, `/health`, và luồng trả lời theo từng token).
15. **Frontend** — màn hình chat với trích dẫn bấm được và trang quản trị.

### E. Nhanh & Có trách nhiệm
16. **Cache** cho câu hỏi lặp, **nhật ký kiểm toán (audit log)** mọi request, **giám sát (monitoring)** độ trễ/tỉ lệ lỗi, và vòng **phản hồi (feedback)** thích/không thích.

### F. Chứng minh hoạt động đúng
17. **Bộ câu hỏi chuẩn (golden set)** có đáp án đã biết, **cổng đánh giá (evaluation gate)** chặn mọi thay đổi làm truy xuất kém đi, và **kiểm thử bảo mật** cố tình phá hệ thống bằng dữ liệu xấu.

### G. Triển khai
18. **Đóng gói** để khởi động bằng một lệnh (containers), sau **proxy biên (edge proxy)** lo TLS, giới hạn tần suất, đăng nhập — triển khai lên môi trường công ty (**VDI**).

---

## Nguyên tắc bảo vệ xuyên suốt mọi bước

- **Bảo mật trước hết (fail-closed).** Ba lớp kiểm tra quyền — theo tổ chức (tenant), theo mức độ nhạy cảm, và theo danh sách truy cập của từng người. Khi nghi ngờ, hệ thống **giữ lại thay vì rò rỉ**.
- **Có căn cứ hoặc im lặng.** Mô hình chỉ được dùng bằng chứng đã truy xuất; nếu bằng chứng yếu, nó từ chối thay vì bịa.
- **Song ngữ xuyên suốt.** Tiếng Việt và Tiếng Anh được xử lý từ đầu đến cuối, từ gắn nhãn tài liệu đến ngôn ngữ câu trả lời.
- **Đo lường, không phỏng đoán.** Chất lượng truy xuất được chấm điểm trên golden set, nên cải thiện (và hồi quy) đều chứng minh được, không cảm tính.

---

## Phân chia công việc

Cả hai cùng làm **Backend**; **Quang Anh** làm thêm **Frontend**.

### 👤 Son — Nạp dữ liệu · Hạ tầng · Kiểm thử
- **A. Ingestion (đường ghi):** nạp / cắt chunk / embed / lưu trữ + an toàn lúc nạp (quét injection, che PII).
- **E. Vận hành:** cache, audit log, monitoring, feedback.
- **F. Đánh giá:** golden set, evaluation gate, security pass — và **nạp mock data trên VDI để kiểm thử tài liệu thật**.
- **G. Triển khai:** Docker/nginx, đưa lên VDI, cấu hình model thật (BGE-M3 + gpt-4o-mini qua `aiportalapi`), thiết lập lại baseline đánh giá bằng model thật.

### 👤 Quang Anh — Truy vấn · API · Frontend
- **B. Retrieval (đường đọc):** truy xuất nghĩa + từ khóa, hợp nhất (RRF), rerank.
- **C. Generation:** lắp ghép ngữ cảnh, sinh câu trả lời có trích dẫn, kiểm chứng trích dẫn / từ chối khi thiếu chứng cứ.
- **D. API:** `/query`, `/ingest`, `/health`, luồng streaming.
- **Frontend:** màn hình chat, trích dẫn bấm được, trang quản trị (upload tài liệu, xem tình trạng hệ thống), kết nối tất cả với API.

### 🤝 Cùng làm (cả hai)
- **Config & schemas** — hợp đồng dữ liệu chung mà mọi module dùng.
- **Bảo mật quyền truy cập** — 3 lớp kiểm tra trải trên cả hai đường (Son: lúc nạp; Quang Anh: lúc truy vấn).
- **Cột mốc M2** — ghép đường ghi của Son với đường đọc của Quang Anh: *một tài liệu vào → một câu trả lời có trích dẫn ra*.

---

## Cột mốc (Milestones)

| Mốc | Ý nghĩa |
|---|---|
| **M1** | Mỗi thành phần được xây dựng và kiểm thử đơn vị |
| **M2** | Một tài liệu thật vào → một câu trả lời có trích dẫn ra |
| **M3** | Ứng dụng chat chạy được trên trình duyệt (cần frontend) |
| **M4** | Câu hỏi lặp trả lời nhanh; mọi request đều được ghi log |
| **M5** | Bộ test đạt ngưỡng chất lượng; vượt qua kiểm thử bảo mật |
| **M6** | Khởi động bằng một lệnh; demo cuối cùng |
