# Deep Research & Brainstorm — Hệ thống sản xuất video truyện tự động

> Pipeline: Tải audio YouTube → Speech-to-Text → Phân tích & cập nhật Knowledge Base → Viết truyện (theo config + KB) → Text-to-Speech → Image Generation → Ghép video hoàn chỉnh
>
> Nghiên cứu cập nhật: 08/2026

---

## 1. Kiến trúc tổng thể đề xuất

```
┌─────────────┐   ┌──────────────┐   ┌───────────────┐   ┌──────────────────┐
│  YouTube     │   │  Transcribe  │   │  Phân tích    │   │  KNOWLEDGE BASE   │
│  Download ───┼──▶│  (WhisperX ──┼──▶│  trích xuất  ──┼──▶│  LightRAG        │
│  (yt-dlp)    │   │  / API)      │   │  thực thể     │   │  (KG + vector)   │
└─────────────┘   └──────────────┘   └───────────────┘   └────────┬─────────┘
                                                                  │ retrieve
┌─────────────┐   ┌──────────────┐   ┌───────────────┐            ▼
│  VIDEO       │   │  TTS +       │   │  Story Writer │   ┌──────────────────┐
│  Assembly ◀──┼───│  Image Gen   │◀──│  (LLM: outline┼───│  Story Config    │
│  (FFmpeg)    │   │              │   │  → scene)     │   │  (bible/style)   │
└─────────────┘   └──────────────┘   └───────────────┘   └──────────────────┘
```

Nguyên tắc cốt lõi: **mỗi stage là một module độc lập** giao tiếp qua file/queue (folder pipeline hoặc job queue), để có thể thay provider từng stage mà không ảnh hưởng phần còn lại.

---

## 2. Kết quả nghiên cứu từng thành phần

### 2.1. Download audio từ YouTube

| Tool | Trạng thái 2026 | Kết luận |
|---|---|---|
| **yt-dlp** | Hoạt động tích cực (release 2026.08.19), ~100k sao | ✅ **Chọn** — không có thay thế thực sự |
| pytube | Bỏ hoang từ 08/2024, 667 issue mở | ❌ Tránh |
| youtube-dl | Không release từ 12/2021 | ❌ Tránh |

```bash
yt-dlp -f bestaudio -x --audio-format m4a --audio-quality 0 -o "%(id)s.%(ext)s" URL
```

⚠️ **Rủi ro pháp lý**: YouTube ToS cấm download nội dung không được ủy quyền và cấm truy cập tự động. YouTube Data API **không** phải giải pháp thay thế (chỉ có metadata + caption của chính bạn). Việc dùng yt-dlp là vấn đề vi phạm hợp đồng ToS chứ không tự động là xâm phạm bản quyền, nhưng nội dung tải về vẫn chịu luật bản quyền. Khuyến nghị:
- Chỉ dùng nội dung có license CC / của chính mình / có giấy phép cho POC nội bộ.
- Dự phòng pipeline "manual upload audio file" để không phụ thuộc YouTube.

### 2.2. Speech-to-Text

**Phương án local (khuyến nghị nếu có GPU < 8GB VRAM):**

| Tool | Điểm nổi bật |
|---|---|
| **WhisperX** ⭐ | faster-whisper backend + word-level timestamp (forced alignment có sẵn **model alignment tiếng Việt** `nguyenvulebinh/wav2vec2-base-vi-vlsp2020`) + speaker diarization (pyannote). Giải pháp tốt nhất cho YouTube dài. |
| faster-whisper | Nhanh hơn openai/whisper 4x, int8 chạy được trên CPU, VAD + word timestamp built-in |
| whisper.cpp | Tốt nhất cho CPU/Apple Silicon, quantized |
| distil-whisper | ❌ Chỉ tiếng Anh — bỏ qua cho tiếng Việt |

Model: `large-v3` (chất lượng cao nhất) hoặc `large-v3-turbo` (nhanh ~8x, chính xác gần bằng).

**Phương án cloud (không cần GPU):**

| Provider | Giá/giờ | Tiếng Việt |
|---|---|---|
| AssemblyAI Universal-3.5 Pro | $0.21/giờ | Tier 1 (top 18 ngôn ngữ) ⭐ |
| Deepgram Nova-3 | $0.26–0.31/giờ | Có, diarization miễn phí |
| OpenAI gpt-4o-mini-transcribe | $0.18/giờ | Có (giới hạn 25MB/file) |
| Google STT V2 batch | $0.18/giờ | Có |

**Khuyến nghị**: WhisperX + large-v3 local cho POC; benchmark so với AssemblyAI trên mẫu audio thật trước khi commit. Chi phí 100 giờ: ~$0 local, ~$15–31 cloud.

### 2.3. Knowledge Base

| Thành phần | Khuyến nghị | Lý do |
|---|---|---|
| **Framework** | **LightRAG** (`lightrag-hku`, MIT, 39k sao) | Xây **knowledge graph thực thể/quan hệ** (nhân vật, địa điểm, sự kiện — đúng cấu trúc cần cho truyện) + vector retrieval, hỗ trợ **incremental insert** (thêm audio mới liên tục), rẻ hơn GraphRAG nhiều lần. Query mode `mix`. |
| Vector DB | **Chroma** (POC) → **Qdrant** (production) | Chroma embedded, zero-ops; Qdrant hỗ trợ hybrid dense+sparse |
| Embedding | **BGE-M3** (MIT, tự host ~2GB VRAM) | SOTA đa ngôn ngữ trên MIRACL (có tiếng Việt), một model cho cả dense + sparse + multi-vector, chunk 8192 token |
| Fallback đơn giản | LlamaIndex + Chroma + BGE-M3 + metadata filter | Nếu LightRAG quá nặng cho MVP |

**Xử lý transcript trước khi index:**
1. Khôi phục dấu câu (WhisperX đã có; nếu raw ASR thì chạy punctuation restoration).
2. Chunk theo ranh giới speaker-turn / chủ đề, 400–800 token, overlap 15%.
3. Gắn **metadata** vào mọi chunk: speaker, timestamp, nguồn video, topic tags — metadata filtering thường có giá trị hơn thuật toán chunking thông minh.
4. (Tùy chọn) Tóm tắt phân cấp kiểu RAPTOR: vừa tra cứu chi tiết vừa tra cứu chủ đề.

### 2.4. Viết truyện bằng LLM

**Kỹ thuật long-form nhất quán (best practice 2026):**
1. **Outline-first**: config truyện → outline từng hồi → từng cảnh → mỗi call LLM viết 1 cảnh.
2. **Story bible / character bible** (structured config): nhân vật (tên, tính cách, cách nói, quan hệ), bối cảnh, timeline, style guide (POV, giọng văn, tone). Inject vào mọi call.
3. **Rolling summary**: sau mỗi cảnh, cập nhật bản tóm tắt diễn biến để chương sau vẫn nhất quán.
4. **RAG-grounded drafting**: trước khi viết cảnh N, retrieve từ KB các chi tiết liên quan.
5. **Reviewer model rẻ hơn** (Haiku/GPT-5-mini/DeepSeek Flash) kiểm tra draft nhất quán với bible + KB facts.
6. **Prompt caching** (Claude $0.20/MTok cached read, Gemini rẻ hơn nữa) giảm chi phí 5–10x vì bible được gửi lại mỗi call.

**Chọn model viết:**

| Model | Giá (in/out /1M) | Điểm mạnh |
|---|---|---|
| **Claude Sonnet 5** | $2 / $10 | Văn xuôi xuất sắc, prompt caching |
| **GLM-4.6** | rẻ (~$0.6/$2.2) | Tune cho writing/role-play, output 128K token |
| Gemini Flash | $0.30 / $2.50 | Context 1M, rẻ nhất cho draft |
| DeepSeek V4 | $1.32 / $3.96 | Context 1M, output 384K |

Chi phí ấu bản truyện 50k từ: ~$1–3/câu chuyện.

### 2.5. Text-to-Speech (đọc truyện)

**Chất lượng tiếng Việt (xếp hạng):** ElevenLabs v3/Multilingual v2 > Azure ≈ Fish Speech > OpenAI > Google. (Kokoro: không có tiếng Việt.)

| Service | Giá | Ghi chú |
|---|---|---|
| **ElevenLabs** ⭐ | $5/30k → $22/121k chars; cloning từ gói Starter | Chất lượng kể chuyện tốt nhất; tiếng Việt "dùng được", chưa phải tier đầu |
| **Edge-TTS** | **Miễn phí** (unofficial MS endpoint) | Giọng vi-VN neural (NamMinh, HoaiMy); rủi ro break vì không SLA |
| Azure TTS | Free 0.5M chars/tháng, ~$16/1M sau đó | vi-VN neural ổn định; custom voice cần duyệt |
| Fish Speech | Tự host free; tier-3 tiếng Việt | Zero-shot cloning từ 10–30s |
| F5-TTS/XTTS | Free, self-host | Tiếng Việt chỉ có community fine-tune; weights CC-BY-NC ⚠️ **cấm thương mại** |

**Khuyến nghị**: POC dùng **Edge-TTS (free)** hoặc ElevenLabs free tier; production tiếng Việt chất lượng cao → ElevenLabs Creator ($22/tháng ~ 12 video 10 phút) hoặc Azure.

### 2.6. Image Generation (minh họa nhất quán nhân vật)

**Kỹ thuật nhất quán nhân vật** (yếu tố quyết định, theo độ tin cậy):
1. **LoRA fine-tune** trên 20–50 ảnh nhân vật — đáng tin nhất, train 1 lần (SDXL ecosystem: kohya_ss).
2. **Reference-image APIs** — dễ nhất, hiện rất tốt: **Nano Banana / Nano Banana Pro** (~$0.03/ảnh, dẫn đầu multi-reference consistency), **Flux Kontext** (~$0.04, edit ảnh giữ nguyên nhân vật), gpt-image-1 (chấp nhận ảnh tham chiếu).
3. IP-Adapter (chỉ SD/SDXL) — không cần train.
4. Prompt-only character sheet — yếu nhất.

**API giá rẻ**: fal.ai / Replicate — flux-schnell $0.003/ảnh, flux-dev $0.025, Nano Banana $0.03.

**Khuyến nghị**: Nano Banana (fal.ai) hoặc Flux Kontext với 1 ảnh character reference cố định; scale lớn → local SDXL + LoRA.

### 2.7. Ghép video

| Tool | Đánh giá |
|---|---|
| **FFmpeg** ⭐ | Miễn phí, nhanh nhất cho batch: `concat` ghép cảnh, `zoompan` (Ken Burns), `xfade` (transition), `subtitles` burn SRT/ASS |
| MoviePy | Lớp Python dễ code timeline logic; render chậm, API v2 hay đổi |
| Remotion | React template, phụ đề động đẹp; dư cho nhu cầu này |
| Shotstack / Creatomate | $0.20–0.30/phút; chỉ cần khi không muốn maintain FFmpeg |

**Mẫu sync (chuẩn cho mọi tool):** chia truyện thành cảnh → TTS **từng cảnh riêng lẻ** → đọc duration từng file audio (ffprobe) → thời gian hiển thị ảnh = duration cảnh → crossfade → sinh SRT từ timing → burn phụ đề + Ken Burns.

---

## 3. Hai cấu hình stack đề xuất

### Stack A — MVP / POC (chi phí ~$0, chạy local)
- yt-dlp + **WhisperX** (GPU hoặc CPU int8)
- LightRAG (hoặc Chroma đơn giản) + BGE-M3 local
- GLM-4.6 / Gemini Flash API (rẻ) hoặc model local
- **Edge-TTS** (free, giọng Việt)
- flux-schnell trên Replicate ($0.003/ảnh) hoặc SDXL local
- FFmpeg
- Orchestration: Python scripts theo pipeline folder (`01_audio/ → 02_transcripts/ → 03_kb/ → 04_stories/ → 05_assets/ → 06_videos/`)

### Stack B — Production (chất lượng cao, trả phí)
- yt-dlp (cân nhắc pháp lý!) + AssemblyAI Universal-3.5
- LightRAG + Qdrant + BGE-M3 API
- Claude Sonnet 5 (writer) + Haiku (reviewer), prompt caching
- ElevenLabs v3 (voice cloning giọng kể riêng)
- Nano Banana Pro (nhất quán nhân vật)
- FFmpeg cluster / Shotstack

**Chi phí ước tính mỗi video 10 phút (Stack B):** TTS ~$1.15 + ~25 ảnh × $0.03 = $0.75 + LLM ~$1–3 + STT (nếu dùng) ≈ **$3–5/video**. Stack A ≈ $0.

---

## 4. Brainstorm — tính năng & ý tưởng mở rộng

**Config truyện nên có (story_config.yaml):**
- `genre`, `length`, `tone`, `pov`, `target_audience`
- `characters[]`: tên, mô tả ngoại hình (dùng cố định cho image gen), tính cách, giọng nói TTS riêng
- `style`: style ảnh (nét vẽ, palette, aspect ratio), giọng đọc, nhạc nền
- `knowledge grounding`: mức độ bám KB (strict = chỉ dùng sự kiện có trong KB / loose = lấy cảm hứng)
- `video`: tốc độ Ken Burns, kiểu transition, có phụ đề không

**Ý tưởng thêm giá trị:**
- **Fact ledger**: reviewer ghi lại mọi "sự kiện đã thiết lập" trong truyện để tránh mâu thuẫn nội bộ giữa các tập.
- **Serial storytelling**: KB tích lũy qua từng tập video → nhân vật ngày càng "sâu"; đây là lợi thế độc đáo của kiến trúc này so với tool tạo video one-shot.
- **A/B tự động**: sinh 2 hook mở đầu khác nhau cho cùng truyện.
- **Deduplication khi ingest**: hash video ID để không transcription lại, phát hiện trùng nội dung.
- **Music bed + SFX**: thêm ảnh nền âm thanh theo mood từng cảnh (kho SFX free + FFmpeg mix).
- **Lipsync/avatar (giai đoạn sau)**: nếu muốn dạng talking-head.

**Rủi ro & mitigation:**
| Rủi ro | Mitigation |
|---|---|
| YouTube ToS / block IP | Chế độ upload file thủ công; dùng nội dung CC-licensed / của mình |
| yt-dlp hỏng khi YouTube đổi | Pin version, cron update, job queue retry |
| Giọng đọc Việt kém tự nhiên | Cho user chọn giữa Edge-TTS (free) ↔ ElevenLabs (chất lượng) |
| Nhân vật lỗi consistency giữa ảnh | Chốt 1 ảnh character reference, luôn gửi kèm; nâng lên LoRA nếu chưa đủ |
| Truyện bị lặp ý qua các tập | Rolling summary + fact ledger + reviewer pass |
| Chi phí LLM tăng | Prompt caching, batch API (-50%), model rẻ cho draft + model tốt cho final pass |

---

## 5. Lộ trình đề xuất

1. **Tuần 1–2**: Pipeline cứng (hardcoded) chạy end-to-end 1 video 3 phút: yt-dlp → WhisperX → (KB bỏ qua) → prompt viết truyện trực tiếp → Edge-TTS → flux-schnell → FFmpeg. Mục tiêu: chứng minh luồng hoạt động.
2. **Tuần 3–4**: Thêm LightRAG KB + story config YAML + reviewer pass; TTS theo cảnh + sync phụ đề.
3. **Tuần 5–6**: Character reference image + kiểm tra độ nhất quán nhân vật; queue hóa (job per stage); dashboard theo dõi chi phí.
4. **Sau đó**: batch production, A/B hook, music, tối ưu chi phí, cân nhắc pháp lý nếu công khai thương mại.

---

## 6. Ghi chú nguồn

Thông tin giá/model đã verify qua trang chính thức của các nhà cung cấp (08/2026). Một số mục chưa verify hoàn toàn: giá Qwen/GLM per-token, context window Gemini 3.x, trạng thái voice vi-VN trên Azure — cần kiểm tra console trước khi commit ngân sách.
