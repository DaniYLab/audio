# Báo Cáo Thẩm Định & Đánh Giá: Roadmap v2 (Hậu Bổ Sung Feedback)

> **Tài liệu được thẩm định:** `docs/ROADMAP.md` (v2 - đã cập nhật theo feedback)  
> **Người thực hiện:** Senior AI Architect & Product Strategist  
> **Kết quả thẩm định:** **ĐẠT TIÊU CHUẨN SẢN PHẨM (PRODUCTION-READY SPEC)**

---

## 1. Tổng quan Đánh giá (Executive Assessment)

Bản cập nhật mới của `ROADMAP.md` đã tiếp thu và cụ thể hóa **100% các phản biện trọng yếu** từ đợt review trước:
1. **Bổ sung Guardrail Metric (Audience Retention Rate $\ge$ 45%):** Khắc phục triệt để lỗ hổng "đạt rubric nội bộ nhưng thất bại ngoài thị trường".
2. **Tách biệt 2 Tier Chi phí (Standard $\le$ $1.5 vs Premium $\le$ $5):** Phù hợp thực tế thị trường GenAI 08/2026, theo dõi thêm chỉ số `images_regenerated_per_video`.
3. **Nâng Hook 15s (Cold Open / Dramatic Restructuring) lên P4 #1:** Giải quyết trực diện điểm yếu lan man của podcast gốc.
4. **Bổ sung TextNormalizer & Rule-based Linting vào M2 (Must-have):** Giảm thiểu chi phí gọi LLM và tránh lỗi phát âm TTS.
5. **Cơ chế Phân biệt Plot Twist vs Hallucination trong Fact Ledger:** Bổ sung cờ `intent: twist` ở cấp độ Beat, giúp Reviewer không phạt nhầm các tình huống nhân vật nói dối/tiết lộ có chủ đích.
6. **Bổ sung Quản lý Rác Đĩa (Disk Lifecycle CLI) & Hardware Acceleration (NVENC) vào M3:** Chống nghẽn hạ tầng khi scale batch.
7. **Ràng buộc Nhạc Nền CC0-only:** Triệt tiêu rủi ro Content ID trên YouTube.

---

## 2. Điểm Sáng Nổi Bật Của Bản Update v2

* **Tư duy Kiến trúc Prompt Chặt chẽ (Context Standalone Guard):** Rất thực tế với thuật toán phân phối nội dung của YouTube/TikTok. Mỗi tập vừa là một phần của series, vừa phải đứng độc lập để người xem mới không bị ngợp.
* **Cân bằng giữa Tự động hóa bằng Code vs LLM:** Đẩy các bài toán linting (câu > 25 từ, ký tự lạ, số chưa dịch) về Regex/Code thay vì phụ thuộc 100% vào LLM Judge giúp tiết kiệm đáng kể chi phí eval ở M2.
* **Phân định Scope Thực Tế:** Quyết định không làm video recap "Previously On" dạng clip riêng và lùi A/B Hook tự động về M4 là quyết định cắt tỉa scope chuẩn xác, tránh phân tán lực lượng trong giai đoạn pilot.

---

## 3. Ba Lưu Ý Triển Khai Cho Đội Ngũ Kỹ Thuật (Engineering Nuances)

Dù tài liệu Roadmap đã hoàn thiện về mặt chiến lược và nghiệp vụ, khi bắt tay vào triển khai M2 cần lưu ý **3 chi tiết kỹ thuật nhỏ**:

1. **Bộ từ điển TextNormalizer tiếng Việt:**
   * Cần có rule xử lý: Ký hiệu tiền tệ (`200k` $\to$ `hai trăm nghìn`, `$5` $\to$ `năm đô la`), Giờ giấc (`10h30` $\to$ `mười giờ ba mươi phút`), Ngày tháng (`20/11` $\to$ `ngày hai mươi tháng mười một`).
   * Nên dùng thư viện thuần Python như `num2words` (hỗ trợ tiếng Việt tốt) kết hợp custom regex dict.

2. **Cơ chế Metadata trong Manifest để tính Cost:**
   * M1 cần đảm bảo các stage context khi `mark_done` đều đẩy đúng: `char_count` (TTS), `image_count` + `regen_count` (Imaging), `input_tokens` / `output_tokens` (LLM), `render_seconds` (FFmpeg) vào manifest để M2 dựng lệnh `storyforge cost` không bị thiếu trường dữ liệu.

3. **Thư viện Nhạc Nền CC0 (Music Bed):**
   * Chuẩn bị sẵn 5-10 track nhạc nền mood đa dạng (hoài niệm, hồi hộp, vui tươi, trầm lắng) dạng MP3/WAV đặt trong thư mục `assets/audio/music/` có đính kèm file `LICENSE.txt`.

---

## 4. Kết luận & Phê duyệt

Tài liệu `ROADMAP.md` hiện tại đã đạt độ hoàn thiện cao nhất: **Chặt chẽ về nghiệp vụ, tường minh về kiến trúc, thực tế về chi phí và an toàn về mặt pháp lý**. 

Team có thể yên tâm sử dụng tài liệu này làm kim chỉ nam chính thức cho toàn bộ giai đoạn phát triển hậu M1 (M2 $\to$ M4).