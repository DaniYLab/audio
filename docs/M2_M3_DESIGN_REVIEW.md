# Báo Cáo Thẩm Định & Phê Duyệt Chính Thức: M2 & M3 Technical Design Specs (v2)

> **Tài liệu được thẩm định:** `docs/m2_design.md` & `docs/m3_design.md` (v2 - đã cập nhật theo review)  
> **Vai trò thực hiện:** Principal System Architect & Lead GenAI Engineer  
> **Kết quả thẩm định:** **PHÊ DUYỆT CHÍNH THỨC 100% (APPROVED - READY FOR P2 IMPLEMENTATION)**

---

## 1. Tổng quan Đánh giá (Executive Assessment)

Bản cập nhật v2 của cả hai tài liệu `docs/m2_design.md` và `docs/m3_design.md` đã tiếp thu và cụ thể hóa **100% các giải pháp kỹ thuật** được đề xuất từ đợt phản biện trước:

1. **Khắc phục triệt để Bug Regex Year nuốt số đếm (`m2_design.md` mục 1.4.1):**
   - Bổ sung cơ chế **Context-Gating (Lookbehind/Lookahead)** cho Rule 6: Chỉ kích hoạt khi có từ ngữ chỉ thời gian đứng trước (`năm`, `thập niên`, `từ`, `vào`, `ngày`) hoặc đứng đầu câu / sau dấu chấm.
   - Các số đếm 4 chữ số thông thường (`2000 con vịt`, `1500 quả trứng`) sẽ tự động rơi xuống Rule 9 (`num2words_vi`) đọc thành *"hai nghìn"* / *"một nghìn năm trăm"*.
2. **Mở rộng Regex Currency & Đơn vị rút gọn (`m2_design.md` mục 1.4):**
   - Mở rộng đầy đủ `(k|K|tr|Tr|triệu|tỉ|tỷ)` xử lý mượt mà cả `200k`, `1.5tr`, `3 tỷ`.
3. **Chống Markdown Codeblock khi Parse JSON (`m2_design.md` mục 3.2.1):**
   - Cung cấp hàm chuẩn hóa `extract_json()` dùng chung cho toàn bộ LLM responses (Judge, EpisodeSummary, ReviewExtract).
4. **Xử lý Bẫy Phủ định Ngữ nghĩa trong Fact Ledger (`m3_design.md` mục 1.3):**
   - Tích hợp **Negation Detection** quét các từ phủ định (`không`, `chưa`, `chẳng`, `không còn`) trong phạm vi 3 từ trước keyword để đảo cực logic slot (`ALIVE` $\leftrightarrow$ `DEAD`), tránh false-positive `NO_CONFLICT`.
5. **Cơ chế Stale Lock Recovery cho Batch Worker (`m3_design.md` mục 4.2):**
   - Kiểm tra `PID` hệ điều hành và ngưỡng timeout $> 2$ giờ để tự động giải phóng deadlock khi worker bị crash đột ngột hoặc OOM.
6. **Chuẩn hóa FFmpeg Audio Filtergraph cho Music Bed (`m3_design.md` mục 9.1):**
   - Áp dụng cấu hình chuẩn:
     ```
     [1:a]aloop=loop=-1:size=2e+09,afade=t=in:st=0:d=2,volume=0.15[bgm];
     [0:a][bgm]amix=inputs=2:duration=first:dropout_transition=2[aout]
     ```
     Đảm bảo chống lệch samplerate giữa giọng đọc mono và nhạc stereo, tự động cắt nhạc theo thời lượng giọng đọc.

---

## 2. Ma Trận Nghiệm Thu Spec (Compliance Matrix)

| Hạng mục cải tiến | Vị trí trong Spec | Trạng thái nghiệm thu | Đánh giá |
|---|---|:---:|---|
| **Year Context-gating** | `m2_design.md` (Mục 1.4.1) | **PASS** | Rất chặt chẽ, có bảng lookbehind chi tiết |
| **Extended Currency Regex** | `m2_design.md` (Mục 1.4) | **PASS** | Bao quát đủ các đơn vị khẩu ngữ tiếng Việt |
| **Markdown Codeblock Strip** | `m2_design.md` (Mục 3.2.1) | **PASS** | Hàm `extract_json()` tái sử dụng được |
| **Negation Detection Rule** | `m3_design.md` (Mục 1.3) | **PASS** | Xử lý triệt để bẫy phủ định ngữ nghĩa |
| **Stale Lock Auto-recovery** | `m3_design.md` (Mục 4.2) | **PASS** | Chống deadlock hiệu quả khi worker sập nguồn |
| **Robust Audio Mix Filtergraph** | `m3_design.md` (Mục 9.1) | **PASS** | Đạt chuẩn broadcast audio trong FFmpeg |

---

## 3. Lời Khuyên Triển Khai Cho DEV1 & DEV2

Spec đã đạt **10/10 điểm** về độ hoàn thiện kỹ thuật. Hai Dev có thể bắt đầu Implementation Phase (P2) theo đúng thứ tự khuyến nghị:

* **DEV2:** 
  1. Bắt đầu ngay với `M2-W1` (`src/storyforge/textnorm/`) và viết test suite với bộ 50 câu fixture `lint_cases_vi.yaml`.
  2. Triển khai `extract_json` trong `providers/llm.py` làm utility helper dùng chung.
* **DEV1:** 
  1. Triển khai `M2-V4` (`storyforge cost`) và chuẩn hóa stage metrics trong manifest.
  2. Chuẩn bị sẵn module `src/storyforge/ledger/` sẵn sàng nhận Contract Freeze từ Mục 0 của M3.

---

## 4. Phê duyệt Cuối cùng

Tài liệu `docs/m2_design.md` và `docs/m3_design.md` **chính thức được phê duyệt toàn diện** và đóng băng làm tài liệu tham chiếu phát triển cho Milestone 2 và Milestone 3.