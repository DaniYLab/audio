# Product & Technical Roadmap — hậu M1

> Vai trò soạn: Senior BA + Senior Architect. Trạng thái: Dev 1 (KB Core) và
> Dev 2 (Writer Integration) đang thực hiện M1 theo `WORKPLAN_M1.md`.
> Tài liệu này là kế hoạch tiếp theo + các track chạy song song NGAY BÂY GIỜ
> (không chờ M1 xong).

---

## 1. Bối cảnh kinh doanh & North Star

**Sản phẩm:** dây chuyền sản xuất video truyện dài tập từ corpus audio
(podcast/audiobook) — điểm khác biệt cạnh tranh là **universe memory**: truyện
dài nhất quán qua các tập nhờ KB + ledger, điều các tool tạo video one-shot
không có.

**North Star Metric (NSM):** số tập video/tuần được xuất bản **thỏa rubric
chất lượng ≥ 80/100 mà không cần sửa tay quá 30 phút/tập**.
(NSM đo cả 3 yếu tố cùng lúc: throughput, chất lượng, mức tự động.)

**Guardrail metric (bổ sung theo review):** NSM là operational metric —
đạt rubric nội bộ mà người xem drop ở 15 giây đầu vẫn là thất bại kinh
doanh. Guardrail đi kèm NSM, cùng nhau mới đủ:
- **Audience Retention Rate**: Average % Viewed ≥ 45% cho video 10 phút
  (đo từ 3 tập pilot trở đi; trước khi có kênh thì thay bằng proxy rubric
  "Hook Retention Power" — mục 4).
- Quy tắc can thiệp: rubric cao + retention thấp → vấn đề là nội dung/hook,
  không phải pipeline → ưu tiên Prompt Plan P4, không tối ưu kỹ thuật.

**KPI phụ theo giai đoạn:**

| KPI | M2 target | M3 target | Cách đo |
|---|---|---|---|
| Chi phí/video 10 phút (Standard tier) | ≤ $3 | ≤ $1.5 | stage metrics (đã có sẵn trong manifest) |
| Chi phí/video 10 phút (Premium tier) | baseline | ≤ $5 | như trên, tách theo tier |
| Tỷ lệ fact mâu thuẫn/tập | đo baseline | ≤ 1 | reviewer pass + human spot-check 10% |
| Golden set KB metrics | đạt ngưỡng mục 7 design v4 | giữ + không regression > 5% | `storyforge eval` |
| Story rubric score | baseline | ≥ 80 | prompt-eval harness (mục 4) |
| Giờ sửa tay/tập | baseline | ≤ 0.5h | tự log |
| Time-to-episode (từ audio → video) | ≤ 4h | ≤ 1h | manifest timestamps |
| Average % Viewed (từ pilot) | baseline | ≥ 45% | YouTube Analytics |

**Lưu ý tier chi phí (theo review):** $3/$1.5 áp dụng cho **Standard tier**
(Edge-TTS + Flux Schnell). Premium tier (ElevenLabs + reference image Pro)
khó về $1.5 — phân bổ tham khảo 08/2026 cho video 10 phút: TTS $0.30–0.50,
ảnh ~$0.10–0.90 (30 ảnh × $0.003–0.03), LLM $0.20–0.60, compute $0.10–0.20.
Do đó KPI Premium tách riêng, target M3 ≤ $5 — không cố bóp cả hai tier
vào một con số. Chi phí rủi ro thực sự không phải LLM (ngày càng rẻ) mà là
**image regeneration** khi ảnh lỗi hỏng tay/mặt — theo dõi riêng metric
`images_regenerated_per_video`.

**Phân tích 3 giả định rủi ro nhất (BA view) — cần thử nghiệm sớm:**

| Giả định | Cách kiểm chứng rẻ nhất | Nếu sai thì sao |
|---|---|---|
| Nội dung truyện từ corpus podcast đủ hay để giữ người xem | M2: xuất 3 tập thử lên kênh, đo retention 50% + **hook 15s** (mục 4) | Pivot: premise do người viết cung cấp, KB chỉ làm "màu" — giảm tham vọng grounding |
| Universe memory tạo độ dính (người xem quay lại tập sau) | So sánh retention tập 1 vs tập 3+ | Nếu không dính: bỏ đầu tư ledger/LightRAG, chuyển anthology one-shot |
| Chi phí ở quy mô 5 tập/tuần chấp nhận được | Cost model từ M1 baseline × 5, tách Standard/Premium | Đổi writer model rẻ hơn cho draft, giữ model tốt cho pass cuối; giới hạn Premium cho tập "đầu mùa" |

**Hai hệ quả bắt buộc từ giả định #2 (theo review):** nền tảng đề xuất video
độc lập (one-shot discovery) nên người xem mới có thể vào tập 5 trước —
story prompt cần **Context Standalone Guard**: mỗi tập tự đứng được, tham
chiếu quá khứ ở mức "recap nhẹ trong 1–2 câu", không bắt buộc xem tập trước.
Đây là ràng buộc prompt chứ không phải tính năng — vào Prompt Plan P4.

---

## 2. Personas & user stories (BA)

**P1 — Producer (người vận hành kênh, primary):** muốn đẩy audio vào, nhận
video ra, can thiệp được khi chất lượng thấp.
- US1: "Tôi muốn xem story.json + prompt artifact trước khi render video để
  sửa premise/character kịp" (đã có ở M1, cần UI đọc dễ hơn ở M3).
- US2: "Tôi muốn alias review 5 phút/tuần để nhân vật không bị tách/lộn."
- US3: "Tôi muốn biết tập này tốn bao nhiêu tiền và stage nào đắt nhất."

**P2 — Content owner (chủ podcast/audiobook):** muốn nội dung mình thành
video mà không mất bản quyền.
- US4: "Tôi muốn upload file thay vì YouTube link" (đã có) + ghi rõ license
  per source (M3: license field trên `kb_sources`).

**P3 — Team lead:** muốn số liệu quyết định đầu tư tiếp (đúng 3 giả định trên).

---

## 3. Kế hoạch milestone

### M2 — Chất lượng & Đánh giá (2 tuần, sau M1)

**Mục tiêu kinh doanh:** biết chính xác chất lượng truyện ở đâu, và nâng
lên bằng dữ liệu thay vì cảm giác.

| Feature | BA rationale | Arch note |
|---|---|---|
| **Hook 15s / Cold Open (Must, theo review)** | Yếu tố sống còn của retention 3 tập pilot — podcast lan man thì phải nhặt climax/hook đưa lên đầu | Ràng buộc trong outline prompt (Dramatic Re-structuring: không bám tuần tự podcast); prompt P4 mục 4 |
| Prompt-eval harness (mục 4) | Không tối ưu prompt không có rubric — tương tự golden set KB nhưng cho story | Chạy reviewer LLM chấm điểm, kết quả lưu `evals/` theo prompt version |
| **TextNormalizer cho TTS (Must, theo review)** | ASR bóc "10h đêm", "200k", từ mượn tiếng Anh → TTS đọc vấp/sai | Module thuần regex/rule TRƯỚC stage TTS, không dùng LLM; rule: số → chữ tiếng Việt, tắt từ viết tắt phổ biến, chuẩn hóa giờ/ngày |
| **Rule-based pacing/TTS lint (Must, theo review)** | Đo độ dài câu bằng regex rẻ hơn gọi LLM judge | Deterministic check chạy trước LLM rubric: câu > 25 từ, ký tự lạ, số chưa dịch — fail sớm ở stage story, không đợi render |
| Reranker bật + đo | Golden set baseline từ M1 nói lên/down | Cờ `use_reranker` đã có, chỉ cần đo + quyết định default |
| EpisodeSummary (1 LLM call/source) | J1 coverage tăng trực tiếp | Opt-in theo design 5 |
| Alias review CLI (`storyforge aliases --pending`) | US2 — việc human-in-the-loop rẻ nhất | Đọc/ghi aliases.yaml + in pending kèm 3 passage |
| Chi phí per-stage report (`storyforge cost --project`) | US3 + kiểm chứng giả định chi phí | Aggregate từ manifest metrics (đã ghi sẵn) + tách tier Standard/Premium |
| **A/B visual style (theo review)** | Người xem có thể chấp nhận watercolor nhưng không chấp nhận anime — cần đo trước khi chốt style mặc định | 3 style × cùng 1 truyện, đưa style vào rubric chiều 5 + survey nhỏ/pilot |
| 3 tập pilot xuất bản thật | Kiểm chứng giả định #1 | Cần kênh distribution riêng track |

**Định nghĩa xong M2:** có 3 tài liệu: `baseline_M2_eval.md` (rubric),
`baseline_M2_kb.md` (golden set với reranker on/off), `baseline_M2_cost.md`
(tách 2 tier).

### M3 — Production hardening + tự động serial (3 tuần)

**Mục tiêu kinh doanh:** từ "chạy được" → "xuất bản đều đặn 3–5 tập/tuần,
ít giám sát".

| Feature | BA rationale | Arch note |
|---|---|---|
| Reviewer pass + Fact ledger | Kiểm chứng giả định #2 (độ dính qua tính nhất quán) | Đúng `FACT_LEDGER_DESIGN.md` — gate: đã có ≥ 2 tập thật. **Phân biệt Plot Twist vs Hallucination (theo review):** nhân vật nói dối/tiết lộ là thiết kế kể chuyện hợp lệ — reviewer prompt phải xác nhận TÁC GIẢ có chủ đích (beat/outline khai báo twist) trước khi flag conflict; chỉ flag khi draft mâu thuẫn mà outline không chủ đích. Cụ thể hóa bằng cơ chế: beat có cờ `intent: twist` → reviewer treats mâu thuẫn trong phạm vi beat đó là "phát triển nhân vật", ghi ledger fact mới đè fact cũ qua supersede bình thường; không có cờ → conflict thật. |
| Batch runner (`storyforge run --queue`) | Producer không chạy từng project tay | Worker loop quanh Pipeline hiện có, KHÔNG Celery |
| **Disk lifecycle: `storyforge clean --project X --keep-final` (theo review)** | 1 run ~ 1GB intermediate; batch 50 tập = 50GB rác | Xóa/nén segments + logs + ảnh trung gian sau khi final.mp4 + story.json đã backup; giữ artifact tối thiểu để resume (manifest + transcripts + story) |
| **Hardware accel encode (NVENC/QSV, theo review)** | CPU x264 30 cảnh 1080p ~5–8 phút/video → nghẽn khi batch | Detect GPU, fallback libx264; cấu hình qua VideoSettings, giữ `preset` tương đương chất lượng |
| TTS chất lượng hơn (ElevenLabs provider hoàn thiện) | Giọng đọc là yếu tố giữ người xem lớn nhất với video truyện | Provider đã có skeleton; thêm voice per-character + caching |
| Character reference image (Flux Kontext / Nano Banana) | Nhất quán hình ảnh nhân vật qua tập | Mở rộng `ImageGenerator` protocol với `reference_image`; theo dõi `images_regenerated_per_video` |
| Music bed + SFX nhẹ | Giá trị sản xuất tăng, chi phí ~0 | **Chỉ dùng thư viện CC0/no- copyright check-in trong repo (theo review)** — nhạc trôi nổi sẽ dính Content ID, mất kiếm tiền/tắt tiếng |
| License field + nguồn an toàn | US4, giảm rủi ro pháp lý | Migration nhẹ trên kb_sources |
| Observability: log tập trung + alert khi stage fail 2 lần | Vận hành không người trực | structlog JSON đã có, chỉ cần sink |

### M4 — Tối ưu & mở rộng (theo dữ liệu, không cam kết scope)

- LightRAG **chỉ khi** golden set J2/quan hệ đa tập vẫn chưa đạt sau M2/M3
  (cổng đã chốt ở design).
- A/B hook tự động (2 mở đầu/tập) — khi có traffic đủ đo.
- Đa ngôn ngữ (en market) — embedding BGE-M3 đã hỗ trợ, chi phí nằm ở
  prompt rewrite + giọng đọc.
- Postgres migration (alias audit UI, embedding versioning) — khi entity
  count > ~500 hoặc cần multi-user.

**MoSCoW tổng hợp (đã điều chỉnh theo review):**
- **Must** (M2): prompt-eval harness, cost report (2 tier), alias CLI,
  reranker đo, **hook 15s/cold open, TextNormalizer, rule-based pacing/TTS
  lint**.
- **Should** (M2–M3): EpisodeSummary, reviewer + ledger (kèm phân biệt
  plot twist), batch runner, TTS, A/B visual style.
- **Could** (M3): reference image, music (CC0-only), license,
  **disk cleanup CLI, hardware accel encode**.
- **Won't** (giữ nguyên): LightRAG (đến khi có tín hiệu), Postgres sớm,
  ColBERT/quantization, web UI trước khi NSM chứng minh.

---

## 4. Prompt Plan — quản lý prompt như sản phẩm

Prompt là "business logic" của hệ thống này nhưng hiện nằm rải rác trong
`prompts/*.txt` không version, không đo. Kế hoạch:

**P1 — Prompt versioning (M2, 2 ngày):**
- Mỗi prompt file có frontmatter: `version, changelog, eval_ref`.
- Prompt render lưu vào artifact mỗi lần chạy (`04_story/prompts_used/`)
  — đã có sẵn chỗ ở design, Dev 2 làm ở M1.
- Rule: đổi prompt = PR kèm kết quả eval trước/sau (giống golden set KB).

**P2 — Rubric story (M2, 3 ngày) — 6 chiều, chấm 1–5 mỗi chiều:**
1. Grounding fidelity (strict: fact nào có citation)
2. Nhất quán nhân vật/bối cảnh (so với brief + ledger)
3. Nhịp kể (pacing theo beat)
4. Chất lượng TTS-ready (câu ngắn, không từ khó đọc) — **tiền lọc bằng
   deterministic lint (regex) trước khi LLM judge: câu > 25 từ, ký tự lạ,
   số chưa dịch thành chữ — rẽ hơn và fail sớm hơn LLM**
5. Hình ảnh sinh động (image_prompt cụ thể, nhất quán appearance)
6. **Hook Retention Power (theo review)** — 15–30 giây đầu có kéo người
   xem ở lại không (proxy retention trước khi có kênh thật)

Reviewer LLM chấm 3 episode mỗi prompt version; điểm lưu
`evals/story/<prompt_version>.json`. Người review 10% mẫu để calibrate
(LLM judge bị drift).

**P3 — Thử nghiệm có kiểm soát (nguyên tắc):** mỗi lần chỉ đổi 1 biến
(prompt/model/temperature), chạy cùng 3 seed premise, so rubric. Không đổi
nhiều thứ rồi tranh cãi tại sao tốt lên.

**P4 — Prompt backlog (ưu tiên theo dự kiến tác động, đã nâng hạng theo review):**
1. **Outline prompt: Cold Open Hook / Dramatic Re-structuring (nâng lên #1)**
   — nhặt climax/hook từ corpus đưa lên 15 giây đầu, không bám tuần tự
   podcast. Sống còn cho retention pilot.
2. **Context Standalone Guard (mới)** — mỗi tập tự đứng được: recap quá khứ
   tối đa 1–2 câu lồng trong kể, không tham chiếu bắt buộc tập trước.
3. Scene prompt: thêm ràng buộc độ dài theo target_minutes (hiện dễ lệch).
4. Outline prompt: beam "cliffhanger" cuối tập khi serial (tăng độ dính).
5. Narration prompt: split theo TTS breath group (câu ≤ 20 từ).
6. Image prompt: negative prompt cố định theo art_style (chống artifact).

---

## 5. Track của Architect + BA NGAY BÂY GIỜ (song song M1, không đụng dev)

| Việc | Ai | Vì sao không chờ |
|---|---|---|
| Chuẩn bị corpus mẫu: 2–3 giờ audio có license rõ (CC / tự sản xuất / có phép) | BA | M1 cần ngày 10; golden set M2 cần corpus lớn hơn |
| Viết rubric story + calibrate bằng tay trên 3 truyện mẫu | BA | P2 của prompt plan cần rubric trước khi M2 bắt đầu |
| Cost model (Google Sheet): áp giá vendor 08/2026 vào baseline M1 khi có | BA + Arch | Kiểm chứng giả định chi phí trước khi cam kết M3 scope |
| Chuẩn bị kênh pilot + quy trình xuất bản 3 tập | BA | Dẫn đường cho quyết định pivot/persevere cuối M2 |
| Review PR contract `kb/types.py` của 2 dev (ngày 2) | Arch | Chốt đóng băng đúng hạn |
| Spike Qdrant backup/restore + kế hoạch dữ liệu (naming, retention) | Arch | Vận hành M3 cần, không block ai |
| Định nghĩa quy trình release (tag, changelog, môi trường dev/prod) | Arch | M2 có 3 baseline cần tái lập được |

---

## 6. Rủi ro kế hoạch & cổng quyết định

| Rủi ro | Tín hiệu | Đối sách |
|---|---|---|
| M1 trễ quá ngày 10 | Ngày 6: conformance suite chưa xanh | Cắt D5 `similar_sources` (chưa ai dùng ở M2), không cắt fake/conformance |
| Chất lượng truyện thấp không phải do KB mà do prompt | Rubric thấp nhưng golden set KB đạt | Đầu tư Prompt Plan P4 trước khi đổ lỗi retrieval |
| Giả định #1 sai (nội dung không giữ người xem) | Retention pilot < 40% hoặc hook 15s yếu trên mọi version prompt | Quyết định pivot cuối M2 — scope M3 đổi sang anthology |
| 2 dev xong sớm | Ngày 8 xong hết | Pull trước: Dev 1 làm Qdrant backup spike; Dev 2 làm prompt versioning P1 hoặc TextNormalizer |
| Chi phí LLM vượt model | M2 cost report > $5/video Standard | Đổi writer draft sang model rẻ (Gemini Flash/DeepSeek) — đã thiết kế provider-agnostic |
| Tràn disk khi batch (theo review) | `df` < 20% trên volume workspace | Clean CLI là Must-could mở đầu M3; đến lúc đó giám sát thủ công qua cost report |
| Image regeneration leo thang (theo review) | `images_regenerated_per_video` > 5 | Chuyển sang reference image sớm hơn; check negative prompt P4#6 |

**Cổng quyết định chính (giữa M2 và M3):** persevere (đầu tư reviewer +
ledger + batch) hay pivot (anthology, giảm grounding) — dựa trên retention
pilot + rubric + cost. Không quyết định bằng cảm tính.

---

## 7. Tóm tắt 30 giây

M1 đang chạy (2 dev, 10 ngày). Song song ngay hôm nay: BA lo corpus có
license, rubric, kênh pilot, cost model; Architect lo review contract, spike
backup, quy trình release. M2 (2 tuần) = chất lượng & đo lường (prompt-eval
harness, reranker đo, cost report 2 tier, alias CLI, **hook 15s, TTS
normalizer + lint**) — kết thúc bằng 3 baseline và cổng quyết định
pivot/persevere. M3 (3 tuần) = production (reviewer + fact ledger có phân
biệt plot twist, batch, TTS tốt hơn, reference image, **disk cleanup,
NVENC, nhạc CC0-only**, observability). M4 = theo dữ liệu, LightRAG chỉ khi
golden set đòi. Prompt được quản lý như sản phẩm: version + rubric 6 chiều
( gồm Hook Retention Power) + đổi 1 biến/lần. NSM có guardrail: Average %
Viewed ≥ 45%.

> **Theo dõi review:** điều chỉnh từ `ROADMAP_REVIEW.md` đã absorb ở các
> mục 1 (guardrail, tier chi phí, standalone guard), 3 (M2/M3 table,
> MoSCoW), 4 (rubric 6 chiều, backlog P4), 6 (rủi ro disk + regen). Hai đề
> xuất được ghi nhận nhưng KHÔNG cho vào scope: recap video "Previously On"
> dạng clip riêng (đắt, để M4 nếu retention cho thấy cần) và A/B hook tự
> động đầy đủ (giữ ở M4 — pilot M2 chỉ chấm hook bằng rubric).
