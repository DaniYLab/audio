# Workplan Waterfall — Phân bổ việc M2 / M3 / M4

> Mô hình: **Waterfall** — mỗi milestone là một dự án mini đi qua 5 phase
> tuần tự, có sign-off cuối mỗi phase trước khi sang phase sau. Không quay
> lại phase trước khi đã duyệt (thay đổi yêu cầu sau Design = raise CR,
> đánh giá tác động, không lén sửa).
>
> Phạm vi: M2, M3 (chi tiết full). M4 chỉ ở mức khung — scope M4 phụ thuộc
> cổng quyết định cuối M2/M3, waterfall không cho phép "kế hoạch ước lệ"
> chi tiết cho thứ chưa chốt yêu cầu.
> M1 đã có kế hoạch riêng: `WORKPLAN_M1.md` (đang thực hiện).
>
> Team: **BA** (business analyst), **ARCH** (senior architect),
> **DEV1** (KB Core), **DEV2** (Writer Integration).
> Quy ước mức effort: 1d = 1 ngày công 1 người.

---

## Phần I — Quy ước chung waterfall

### 1.1. Các phase chuẩn (áp cho mọi milestone)

| Phase | Mục đích | Trưởng phase | Exit criteria |
|---|---|---|---|
| **P0 — Requirements** | Đóng băng scope theo ROADMAP; viết user stories + acceptance criteria | BA | Tài liệu requirement được ARCH + 2 DEV duyệt; không còn câu hỏi "cái này có làm không" |
| **P1 — Design** | Spec kỹ thuật từng feature: interface, schema, prompt, test plan | ARCH | Spec có tên file/thay đổi cụ thể; DEV estimate xong; test plan viết trước code |
| **P2 — Implementation** | Code theo spec, PR nhỏ theo feature | DEV1 + DEV2 | Mọi feature trong scope merge, `make lint typecheck test` xanh |
| **P3 — Verification** | QA theo acceptance criteria + baseline measurement + demo | BA + ARCH | Báo cáo verification + demo duyệt; bug P0/P1 = 0 |
| **P4 — Release & Baseline** | Tag release, ghi baseline, retro, chuẩn bị P0 milestone sau | ARCH | Baseline docs committed; retro có action items |

### 1.2. Quy tắc phân bổ (không đổi giữa các milestone)

- **Ownership file như M1**: DEV1 = KB/backend (`kb/`, `providers/`, docker,
  config knowledge); DEV2 = writer/frontend-of-pipeline (`stages/`,
  `prompts/`, `kb/compiler|brief`, CLI, eval tooling). File chung chỉ đổi
  trong P0/P1 khi spec đã duyệt.
- **BA không code; ARCH không code feature** (trừ spec mẫu/spike).
- Mỗi feature = 1 ticket có: responsible, spec reference, acceptance
  criteria, estimate, dependency.
- **CR (change request)**: yêu cầu đổi sau khi phase exit → ARCH đánh giá
  tác động (ngày, rủi ro), BA duyệt giá trị, rồi mới nhận. Ghi vào
  `docs/changelog` của milestone.

### 1.3. Sản phẩm cố định mỗi milestone

1. `docs/m<X>_requirements.md` (P0)
2. `docs/m<X>_design.md` (P1)
3. Code + tests (P2)
4. `docs/m<X>_verification.md` + baseline docs (P3)
5. Tag `v0.<X>.0` + retro notes (P4)

---

## Phần II — MILESTONE 2: Chất lượng & Đánh giá (2 tuần)

### 2.0. Tổng quan phân bổ

| Vai trò | Tổng effort | Ghi chú |
|---|---|---|
| BA | 4d | P0 + P3 chủ lực |
| ARCH | 5d | P1 + review PR + P3 đo |
| DEV1 | 8d | Reranker đo, EpisodeSummary, alias CLI, cost report backend |
| DEV2 | 8d | Prompt-eval harness, TextNormalizer, pacing lint, hook prompt, A/B style tooling |

Timeline: P0 (1.5d) → P1 (2d) → P2 (7d) → P3 (2d) → P4 (0.5d). P2 chồng
lệch cuối P1 **chỉ với module độc lập** (theo review — giữ kỷ luật
waterfall): trong thời gian gối đầu, DEV1 chỉ được code `M2-V4` (cost
report, không phụ thuộc spec mới), DEV2 chỉ được code `M2-W1`
(TextNormalizer). Các module phụ thuộc prompt (`M2-W4`, `M2-W5`) bắt buộc
đợi P1 sign-off 100%.

### 2.1. P0 — Requirements (1.5 ngày, BA trưởng)

**Việc:**

| ID | Việc | Ai | Effort |
|---|---|---|---|
| M2-R1 | Đóng băng scope M2 từ ROADMAP MoSCoW Must/Should; mỗi feature viết user story + acceptance criteria (dùng rubric mục 4 ROADMAP làm ngôn ngữ chung) | BA | 0.5d |
| M2-R2 | Hoàn thiện **rubric story 6 chiều** bản chấm được (thang 1–5, ví dụ minh họa mỗi mức, form calibrate) — đã ủ từ track song song M1 | BA | 0.5d |
| M2-R3 | Xác định 3 seed premise + 3 style ảnh (watercolor/anime/cinematic) cho A/B; chốt tiêu chí "style thắng" | BA | 0.25d |
| M2-R4 | Duyệt requirement cùng ARCH + 2 DEV; ước lệ effort mỗi feature | BA + all | 0.25d |

**Deliverable:** `docs/m2_requirements.md` (bảng feature × acceptance
criteria × estimate).

### 2.2. P1 — Design (2 ngày, ARCH trưởng)

| ID | Việc | Ai | Effort |
|---|---|---|---|
| M2-D1 | Spec **TextNormalizer**: vị trí chèn (giữa story stage và TTS stage), rule table (số, giờ, ngày, viết tắt vi, từ mượn), không-LLM, config on/off | ARCH | 0.5d |
| M2-D2 | Spec **pacing/TTS lint**: deterministic check chạy trong story stage; ngưỡng fail (warn vs fail) | ARCH | 0.25d |
| M2-D3 | Spec **prompt-eval harness**: `evals/` layout, cách gọi reviewer LLM, prompt chấm điểm, lưu kết quả theo prompt_version; frontmatter prompt versioning | ARCH | 0.5d |
| M2-D4 | Spec **Hook/Cold Open**: thay đổi outline prompt + kiểm soát ảnh hưởng lên KB usage (hook từ beat đầu) | ARCH | 0.25d |
| M2-D5 | Spec **reranker đo + EpisodeSummary + alias CLI + cost report**: chủ yếu là khớp với design KB hiện có, chỉ bổ sung chi tiết measurement | ARCH | 0.5d |

**Deliverable:** `docs/m2_design.md` (mỗi feature: file đụng, interface,
test plan, estimate xác nhận).

### 2.3. P2 — Implementation (7 ngày)

**DEV1 (8d):**

| ID | Feature | Chi tiết | Effort | Phụ thuộc |
|---|---|---|---|---|
| M2-V1 | Reranker integration + đo | Bật cờ `use_reranker`, chạy golden set on/off, xuất bảng so sánh | 1.5d | M1 xong |
| M2-V2 | EpisodeSummary | 1 LLM call/source lúc ingest, prompt "khoảnh khắc đáng kể", lưu payload | 2d | M2-D5 |
| M2-V3 | Alias review CLI | `storyforge aliases --pending`: list pending + 3 passage/context, confirm/merge/reject, ghi audit | 2d | M2-D5 |
| M2-V4 | Cost report | `storyforge cost --project`: aggregate manifest metrics, tách Standard/Premium, group by stage | 1.5d | — |
| M2-V5 | Conformance suite mở rộng | Thêm test cho reranker flag + EpisodeSummary payload | 1d | M2-V1/V2 |

**DEV2 (8d):**

| ID | Feature | Chi tiết | Effort | Phụ thuộc |
|---|---|---|---|---|
| M2-W1 | TextNormalizer | Module + rule table + test (case "10h đêm", "200k", "1995") | 2d | M2-D1 |
| M2-W2 | Pacing/TTS lint | Deterministic check trong story stage + test | 1d | M2-D2 |
| M2-W3 | Prompt versioning + render artifact | Frontmatter version, prompt render lưu `04_story/prompts_used/` | 1d | M2-D3 |
| M2-W4 | Prompt-eval harness | `storyforge eval-story`: gọi reviewer LLM chấm rubric 6 chiều, lưu `evals/story/<version>.json` | 2.5d | M2-W3 |
| M2-W5 | Hook/Cold Open prompt | Rewrite outline prompt + rule chọn hook từ corpus; chạy thử 3 seed premise | 1d | M2-D4 |
| M2-W6 | A/B visual style tooling | Chạy 1 truyện × 3 style (config art_style), xuất bộ ảnh so sánh cho BA duyệt | 0.5d | — |

### 2.4. P3 — Verification (2 ngày, BA + ARCH)

| ID | Việc | Ai |
|---|---|---|
| M2-Q1 | Chạy acceptance criteria từng feature theo m2_requirements; ghi pass/fail | BA (chạy) + DEV (sửa) |
| M2-Q2 | **3 baseline docs**: `baseline_M2_eval.md` (rubric qua 3 seed), `baseline_M2_kb.md` (reranker on/off), `baseline_M2_cost.md` (2 tier) | ARCH |
| M2-Q3 | Demo 3 tập pilot nội bộ + chọn style ảnh thắng (BA duyệt bằng mắt + survey nhỏ) | BA |
| M2-Q4 | Bug bash: BA + ARCH chạy pipeline end-to-end 3 lần tìm lỗi | all |
| M2-Q5 | **Lint test dataset (theo review):** verify TextNormalizer + pacing lint trên bộ 50 câu tiếng Việt phức tạp (số la mã, số thập phân, giờ lẻ "10h30", "200k", "1995", từ mượn tiếng Anh) — bộ dữ liệu viết trong P1 như một phần spec, chạy ở P3 như acceptance | ARCH (P1) + 2 DEV (P3) |

### 2.5. P4 — Release & Baseline (0.5 ngày)

- Tag `v0.2.0`; baseline docs commit; retro 1h (action items vào M3 P0).
- **Cổng quyết định persevere/pivot** diễn ra ở ĐÂY (retro M2) — dùng
  retention pilot (nếu kênh đã có số liệu) + rubric + cost.
- **Fallback cổng M2 (theo review):** thuật toán YouTube cần 3–7 ngày sau
  xuất bản mới có đủ số liệu retention. Nếu chưa đủ view/Analytics sau 3
  ngày, dùng **Rubric chiều 6 (Hook Retention Power) ≥ 4.0/5.0 trên cả 3
  seed premise** làm điều kiện thông qua tạm thời để mở P0 M3 — team không
  ngồi chờ dữ liệu. Khi số liệu thật về sau, đối chiếu lại ở retro M3:
  nếu rubric hook ≥ 4.0 mà retention thật < 40% → nâng chuẩn calibrate
  rubric (đây chính là dữ liệu calibrate judge LLM).

---

## Phần III — MILESTONE 3: Production Hardening (3 tuần)

### 3.0. Tổng quan phân bổ

| Vai trò | Effort |
|---|---|
| BA | 5d (P0/P3) |
| ARCH | 8d (P1, reviewer/ledger spec chủ lực, review PR) |
| DEV1 | 13d |
| DEV2 | 13d |

Timeline: P0 (2d) → P1 (3d) → P2 (14d) → P3 (3d) → P4 (1d).

### 3.1. P0 — Requirements (2d, BA)

| ID | Việc | Ai | Effort |
|---|---|---|---|
| M3-R1 | Đóng băng scope từ ROADMAP M3 + kết quả cổng M2 (nếu pivot → scope đổi, viết lại ở đây) | BA | 0.5d |
| M3-R2 | User story + AC cho reviewer pass / fact ledger (ngôn ngữ: "tập N không mâu thuẫn tập 1..N-1 unless twist flag") | BA | 0.5d |
| M3-R3 | Yêu cầu vận hành: SLO thử nghiệm (5 tập/tuần, alert stage fail ×2), disk policy (giữ gì sau clean), music CC0 list | BA + ARCH | 0.5d |
| M3-R4 | Gate check: đã có ≥ 2 tập thật trong universe (điều kiện ledger) — nếu chưa, đẩy ledger xuống M4 và ghi rõ | BA | 0.5d |

### 3.2. P1 — Design (3d, ARCH)

| ID | Việc | Effort |
|---|---|---|
| M3-D1 | Spec reviewer pass + plot-twist flag (`intent: twist` trên beat) + hội nhập `FACT_LEDGER_DESIGN.md`; test plan conflict/twist/unknown. **Contract freeze (theo review):** models `Fact`, `ConflictReport`, `Beat.intent` chốt trong spec này thành text đính kèm — DEV1 (M3-V1/V2) và DEV2 (M3-W1/W2) code theo đúng contract đã đóng băng; thêm/bớt field = CR qua ARCH | 1d |
| M3-D2 | Spec batch runner: worker loop, trạng thái queue, retry policy tái dùng `core/retry`, không Celery | 0.5d |
| M3-D3 | Spec disk lifecycle: giữ gì (manifest, transcripts, story, final) / xóa gì (segments, logs render, ảnh trung gian tùy flag) | 0.25d |
| M3-D4 | Spec NVENC/QSV: detect + fallback libx264, VideoSettings mới | 0.25d |
| M3-D5 | Spec reference image: protocol `reference_image` param, chọn ảnh canonical cho character (nơi lưu, ai chọn), metric regen | 0.5d |
| M3-D6 | Spec TTS premium (voice per-character, caching) + license field | 0.5d |

### 3.3. P2 — Implementation (14d)

**DEV1 (13d):**

| ID | Feature | Effort |
|---|---|---|
| M3-V1 | FactLedger store (YAML per-episode + audit log + query/find_conflicts rule-based) | 3d |
| M3-V2 | Reviewer pass wiring (stage mới: trích fact từ draft, gọi find_conflicts, conflict report artifact) | 2.5d |
| M3-V3 | Batch runner (`storyforge run --queue`) | 2.5d |
| M3-V4 | Disk cleanup CLI + áp dụng policy sau mỗi run batch | 1.5d |
| M3-V5 | NVENC/QSV encode + config + benchmark trước/sau | 1.5d |
| M3-V6 | License field + migration kb_sources | 1d |
| M3-V7 | Conformance/ops tests mới | 1.5d |

**DEV2 (13d):**

| ID | Feature | Effort |
|---|---|---|
| M3-W1 | BriefCompiler hòa nhập ledger: [ESTABLISHED]/[INVENTED] render, pass 1+2 | 2d |
| M3-W2 | Reviewer prompt (phân biệt twist vs hallucination) + prompt version cho reviewer | 2d |
| M3-W3 | TTS premium: ElevenLabs voice per-character + cache audio theo hash text+voice | 2.5d |
| M3-W4 | Reference image wiring: character canonical image, gửi kèm mỗi generate, track regen metric | 2.5d |
| M3-W5 | Music bed: kho CC0 trong repo + FFmpeg mix theo mood tag | 1.5d |
| M3-W6 | Observability sink + alert rule (stage fail ×2) | 1.5d |
| M3-W7 | Docs vận hành runbook (xuất bản 1 tập, xử lý alert) | 1d |

### 3.4. P3 — Verification (3d)

| Việc | Ai |
|---|---|
| Chạy batch thật 5 tập/tuần trong 1 tuần quan sát — đo SLO, disk, alert | BA + ARCH |
| Kiểm tra fact mâu thuẫn: cài 1 "bẫy" (fact sai cố ý ở tập test) xem reviewer có bắt không | ARCH |
| **Twist false-positive test (theo review):** cài 1 fact sai CÓ cờ `intent: twist` ở beat — reviewer KHÔNG được flag conflict (chỉ ghi supersede bình thường). Cùng pass 2 test này mới chấp nhận reviewer | ARCH |
| Spot-check 10% facts bằng tay | BA |
| Verification report + demo + bug bash | all |

### 3.5. P4 — Release (1d)

Tag `v0.3.0`; baseline vận hành (SLO thực tế, cost thực tế, rubric sau
reviewer); retro → input cho M4 P0.

---

## Phần IV — MILESTONE 4: Khung chỉ (không kế hoạch chi tiết)

Waterfall không cho phép lập kế hoạch chi tiết cho yêu cầu chưa chốt.
M4 chỉ có khung + điều kiện vào P0:

| Ứng viên scope M4 | Điều kiện kích hoạt (đo được) |
|---|---|
| LightRAG layer | Golden set J2 hoặc truy vấn quan hệ đa tập fail sau 2 vòng tinh chỉnh (M2/M3) |
| A/B hook tự động | Traffic pilot đủ ý nghĩa thống kê (≥ 500 view/tập thử) |
| Recap clip "Previously On" | Retention tập 3+ thấp hơn tập 1 > 15% (người xem cần ngữ cảnh) |
| Đa ngôn ngữ | Kênh vi đạt NSM + có demand en (comment/analytics) |
| Postgres migration | Entity > ~500 hoặc cần multi-user |

P0 của M4 chỉ mở khi có tối thiểu 2 điều kiện trên chạm — ngược lại team
chuyển sang chế độ **maintenance + prompt iteration** (chạy P1–P3 mini
theo quý cho prompt/rubric refresh).

---

## Phần V — Ma trận trách nhiệm tổng (RACI rút gọn)

| Hoạt động | BA | ARCH | DEV1 | DEV2 |
|---|---|---|---|---|
| Requirement (P0) | **R** | C | C | C |
| Design spec (P1) | C | **R** | C | C |
| Implement feature | I | C | **R** (KB/ops) | **R** (writer/prompt) |
| Code review | — | **A** | R | R |
| QA acceptance (P3) | **R** | C | I | I |
| Baseline docs (P3) | C | **R** | I | I |
| Release tag (P4) | I | **R** | C | C |
| Cổng quyết định (M2/M3 cuối) | **R** | C | I | I |

R = làm, A = chấp thuận, C = tham vấn, I = được thông báo.

---

## Phần VI — Theo dõi & biến thể kế hoạch

- **Tracking**: mỗi phase có checklist trong `docs/m<X>_requirements.md`
  (cột status). Exit phase = duyệt trong meeting 30 phút, ghi ngày.
- **Trễ**: feature trễ > 2d → ARCH quyết định cắt scope (đưa xuống milestone
  sau, theo MoSCoW) hoặc trễ milestone — không nén QA (P3) vì baseline
  là sản phẩm chính của M2/M3.
- **Phụ thuộc chéo milestone**: mọi thứ DEV cần từ track song song (corpus,
  rubric, kênh pilot, cost model) phải DONE trước P1 của M2 — BA theo dõi,
  đây là điều kiện vào P0 M2.
- **Điều chỉnh từ review (đã absorb):** (1) gối đầu P1/P2 chỉ cho module
  độc lập; (2) contract freeze `Fact`/`ConflictReport`/`Beat.intent` ở
  M3-D1; (3) fallback cổng M2 bằng rubric hook ≥ 4.0 khi YouTube Analytics
  chưa đủ số liệu + đối chiếu lại ở retro M3; (4) twist false-positive
  test ở P3 M3; (5) lint dataset 50 câu (viết ở P1, chạy ở P3 M2).
