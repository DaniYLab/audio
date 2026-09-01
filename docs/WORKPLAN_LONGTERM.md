# Workplan Dài Hạn — StoryForge (M2 → M7)

> **Kế hoạch chiến lược 6–9 tháng** (Q3/2026 → Q2/2027), cấu trúc waterfall
> đồng bộ `WORKPLAN_WATERFALL.md`. M2/M3 đã có design chi tiết ở
> `m2_design.md` / `m3_design.md` (đã duyệt); tài liệu này mở rộng M4→M7 với
> cùng kỷ luật phase P0→P4 và nâng M4/M5/M6/M7 lên mức ticket cụ thể.
> Nguồn tham chiếu: `ROADMAP.md`, `KNOWLEDGE_BASE_DESIGN.md` (v4),
> `FACT_LEDGER_DESIGN.md`, `DEEPDIVE_AINOVEL.md` (pattern import từ ainovel-cli).
>
> **Quy ước kế thừa:** P0=Requirements (BA) → P1=Design (ARCH) →
> P2=Implementation (DEV) → P3=Verification (BA+ARCH) → P4=Release & Gate.
> DEV1 = KB/backend/ops, DEV2 = writer/prompt/pipeline-UI, DEV3 = web frontend
> (mới từ M5), DEV4 = AI/research (mới từ M6). CR discipline như Workplan chính.

---

## Phần I — Bản đồ milestone & mục tiêu kinh doanh

| Milestone | Thời gian | Mục tiêu kinh doanh (BA) | Cổng ra (Gate) |
|---|---|---|---|
| **M2** — Chất lượng & Đánh giá | 2 tuần | Biết chính xác chất lượng story ở đâu; 3 baseline docs | Rubric ≥ baseline; quyết định reranker; **Gate: persevere/pivot** |
| **M3** — Production hardening | 3 tuần | 3–5 tập/tuần tự động, ít giám sát; reviewer + fact ledger | SLO pilot đạt; trap tests xanh; ledger gate (≥ 2 tập) |
| **M4** — Serial engine & Producer ops | 4 tuần | XUẤT BẢN serial tự động (queue + upload + recap + thumbnail); nền tảng can thiệp thủ công tối thiểu | 1 tập/tuần hoàn toàn tự động, retention ≥ 45% |
| **M5** — Platform & multi-tenant | 4–5 tuần | Nhiều producer, nhiều universe, API + web dashboard | 2 producer thử nghiệm; API v1 public |
| **M6** — Advanced AI | 6 tuần | Hình ảnh động (image-to-video), character LoRA, KG đầy đủ, continuity bi-temporal | Video động ≥ 1 cảnh/tập; J2 golden đạt |
| **M7** — Distribution & monetization | 6 tuần | SaaS billing, multi-channel (Shorts/TikTok), analytics loop | 5 kênh hoạt động; MRR > 0 |

**Nguyên tắc dây chuyền:** mỗi milestone kết thúc bằng baseline docs +
gate quyết định (persevere / pivot / scale) — không milestone nào tự mở rộng
scope trước khi gate đạt.

---

## Phần II — M4: Serial Engine & Producer Ops (4 tuần)

**Mục tiêu:** từ "chạy được" → "xuất bản đều đặn không cần người canh".
Kế thừa thành quả M3 (batch runner, reviewer/ledger, observability).

### P0 — Requirements (2d, BA)

| ID | Việc | Ai | Effort |
|---|---|---|---|
| M4-R1 | Đóng băng scope M4 từ ROADMAP M3-còn-dư + backlog M4 (A/B hook, recap, thumbnail, upload, dashboard read-only, multi-worker, music manager, character sheet auto) | BA | 0.5d |
| M4-R2 | User story cho **Producer Dashboard (read-only)**: xem jobs, cost, story editor đọc, approve hook | BA | 0.5d |
| M4-R3 | User story **Auto-publish pipeline**: từ queue → upload YouTube (draft) → thumbnail → recap → notify | BA | 0.5d |
| M4-R4 | Điều kiện gate M3 (persevere) → chốt M4 scope; nếu pivot → viết lại | BA+all | 0.5d |

### P1 — Design (3d, ARCH)

| ID | Việc | Effort |
|---|---|---|
| M4-D1 | **A/B Hook**: 2 hook/1 truyện → rubric chiều hook + (nếu có traffic) retention → chọn 1; artifact `04_story/hook_ab/` | 0.5d |
| M4-D2 | **Previously-On recap**: LLM 1 call từ ledger (facts gần nhất) + summaries → 20–30s intro clip (phụ đề + ảnh montage) | 0.5d |
| M4-D3 | **Auto thumbnail**: chọn scene image giàu visual (điểm aesthetic cao từ rubric) + text overlay title → 1280x720 PNG | 0.25d |
| M4-D4 | **YouTube upload (draft mode)**: API v3, draft-only ban đầu, mapping title/description/tags từ story config | 0.5d |
| M4-D5 | **Producer Dashboard read-only**: web (FastAPI + React sau M5) đọc manifest/cost/story artifacts — KHÔNG ghi | 0.5d |
| M4-D6 | **Multi-worker**: queue trên shared volume (hoặc S3 nếu cloud), lock via lease TTL 30s + heartbeat; KHÔNG Celery | 0.25d |
| M4-D7 | **Character sheet auto**: từ EntityFacts + alias → CharacterSheet mặc định (tên/appearance/personality) để producer chỉnh | 0.5d |
| M4-D8 | **Universe bootstrap từ corpus** (port ý tưởng reverse-foundation của ainovel-cli): từ KB đã ingest → premise/characters/world seed → StoryConfig draft | 0.5d |

### P2 — Implementation (14d)

**DEV1 (14d):**

| ID | Feature | Effort |
|---|---|---|
| M4-V1 | Recap generator (ledger + summaries → intro clip via existing TTS/imaging/video stages) | 3d |
| M4-V2 | YouTube upload draft mode + credentials vault (env/secret) | 2d |
| M4-V3 | Multi-worker: lease/heartbeat trên shared volume, worker ID, dead-letter | 2.5d |
| M4-V4 | Character sheet auto + universe bootstrap API (KB → StoryConfig seed) | 2.5d |
| M4-V5 | Cost/usage export API (cho dashboard) + metrics bổ sung | 1.5d |
| M4-V6 | Music/SFX library manager (CC0, mood tags, per-episode mood resolution) | 1.5d |
| M4-V7 | Integration + ops tests (publish pipeline e2e với fake upload) | 1d |

**DEV2 (14d):**

| ID | Feature | Effort |
|---|---|---|
| M4-W1 | A/B hook: sinh 2 hook, rubric chọn, artifact lưu | 2d |
| M4-W2 | Thumbnail generator (scene selection + text overlay) | 2d |
| M4-W3 | Producer dashboard read-only (FastAPI + basic HTML/JS, KHÔNG React chưa cần) | 3.5d |
| M4-W4 | Recap prompt (ledger→script 20–30s, giọng đọc) | 1.5d |
| M4-W5 | Hook/recap/thumbnail tích hợp vào pipeline CLI (`storyforge publish --draft`) | 2.5d |
| M4-W6 | Prompt pack mở rộng: M4 hooks, recap, thumbnail text | 1.5d |
| M4-W7 | Docs vận hành producer (runbook publish 1 tập tự động) | 1d |

### P3 — Verification (3d, BA+ARCH)

- E2E: 1 tuần chạy 5 tập tự động (queue → publish draft) — đo SLO, disk, alert
- A/B hook: 5 tập thử, chọn 1 hook/tập bằng rubric
- Thumbnail/recap: BA duyệt bằng mắt 5 tập
- Dashboard: BA chấm UX read-only (navigation, load time, đủ thông tin quyết định)
- **Gate M4**: 1 tập/tuần hoàn toàn tự động (không sửa tay) + retention ≥ 45% (nếu kênh có số liệu) hoặc rubric hook ≥ 4.0

### P4 — Release (1d)

Tag `v0.4.0`; baseline vận hành M4 (cost thực, SLO thực); retro → input M5.

---

## Phần III — M5: Platform & Multi-tenant (4–5 tuần)

**Mục tiêu:** từ 1 producer → nhiều producer/team; API + web làm cổng vận hành.

### P0 — Requirements (2d, BA)

| ID | Việc | Ai |
|---|---|---|
| M5-R1 | User stories: đăng ký/đăng nhập, quản lý universe per account, RBAC (owner/editor/viewer) | BA |
| M5-R2 | User stories: web story editor (chỉnh premise/characters/scenes, approve hook, alias review UI) | BA |
| M5-R3 | Scope: REST API v1 (jobs, universes, artifacts read, cost, alerts) + webhooks (run_end, fail) | BA+ARCH |
| M5-R4 | Gate M4 đạt → mở M5; nếu chưa → M5 trì hoãn 2 tuần | BA |

### P1 — Design (4d, ARCH)

| ID | Việc | Effort |
|---|---|---|
| M5-D1 | **Multi-tenant data model**: `universe` per account (chuyển từ local `data/kb/<universe>` sang registry có owner), RBAC matrix | 1d |
| M5-D2 | **Postgres migration** (bước đầu: users, workspaces, aliases, usage, decisions) — Qdrant vẫn là store vector; chunk store giữ Qdrant payload + Postgres ref | 1d |
| M5-D3 | **Embedding versioning**: model_version field + re-embed pipeline (có gate, không downtime) | 0.5d |
| M5-D4 | **REST API v1 spec**: OpenAPI, auth (JWT), rate limit, idempotency keys | 1d |
| M5-D5 | **Web editor spec**: read/write story artifacts, optimistic lock (digest), approval flow hook | 0.5d |

### P2 — Implementation (20d)

**DEV1 (10d):** M5-V1 Postgres migration + entity layer (2.5d) · M5-V2 tenant registry + RBAC enforcement (2.5d) · M5-V3 embedding versioning (1.5d) · M5-V4 usage/decisions export + webhook dispatcher (2d) · M5-V5 ops tests (1.5d)

**DEV2 (10d):** M5-W1 REST API v1 (FastAPI, JWT, OpenAPI) (3d) · M5-W2 webhook sink + alert webhook (1d) · M5-W3 web editor read/write + optimistic lock (3.5d) · M5-W4 alias review UI (1.5d) · M5-W5 docs API + runbook (1d)

**DEV3 (mới, 10d, frontend):** M5-F1 React dashboard (jobs/cost/artifacts browse) (3.5d) · M5-F2 Story editor UI (2.5d) · M5-F3 Alias review + hook approval screens (2d) · M5-F4 Auth flow + role switching (2d)

### P3 — Verification (3d, BA+ARCH)

- 2 producer thử nghiệm thật (user testing), ghi feedback
- API conformance (OpenAPI → test suite), RBAC matrix test
- Embedding re-embed: golden set regression (không regression > 5%)
- **Gate M5**: 2 producer hoạt động ≥ 2 tuần, API v1 public, web editor dùng được cho sửa story

### P4 — Release (1d)

Tag `v0.5.0`; baseline M5 (cost/producer, thời gian can thiệp thủ công); retro.

---

## Phần IV — M6: Advanced AI (6 tuần)

**Mục tiêu:** nâng chất lượng visual + continuity lên mức "video kể chuyện
động", tận dụng pattern ainovel-cli (arbiter, ctxpack, KG).

### P0 — Requirements (2.5d, BA)

| ID | Việc | Ai |
|---|---|---|
| M6-R1 | Chốt scope: image-to-video (animated scenes), character LoRA, LightRAG gate, continuity bi-temporal, ctxpack port, music mood auto | BA |
| M6-R2 | BA đo demand: retention theo cảnh (YouTube chapter analytics) → quyết định đầu tư animated vs Ken Burns | BA |
| M6-R3 | Gate LightRAG: chạy golden set J2 → nếu fail sau 2 vòng → đưa LightRAG vào scope M6 | ARCH |

### P1 — Design (5d, ARCH)

| ID | Việc | Effort |
|---|---|---|
| M6-D1 | **Image-to-video provider** (Kling/Veo/Runway class): protocol `animate(scene, duration, motion)`, cost model, fallback Ken Burns | 1d |
| M6-D2 | **Character LoRA pipeline**: thu thập 20–50 ảnh ref/character → kohya_ss train → registry (SDXL) | 1.5d |
| M6-D3 | **LightRAG integration** (nếu gate mở): parallel với Qdrant, A/B golden, chunk store vẫn là sự thật | 1d |
| M6-D4 | **Continuity bi-temporal**: fact ledger query "facts đúng tại episode N" (supersede timeline), phục vụ recap + reviewer | 1d |
| M6-D5 | **ctxpack port**: StoreSummaryCompact strategy cho brief compiler khi serial > 20 tập; budget/digest semantics | 0.5d |

### P2 — Implementation (30d)

**DEV1 (12d):** M6-V1 LightRAG backend (nếu gate) (4d) · M6-V2 continuity bi-temporal (ledger query as-of) (2.5d) · M6-V3 LoRA registry + training pipeline automation (3.5d) · M6-V4 cost/usage cho video gen + LoRA (2d)

**DEV2 (10d):** M6-W1 animate protocol + fallback (2.5d) · M6-W2 music mood auto (classifier nhẹ hoặc LLM 1 call) (1.5d) · M6-W3 ctxpack port (2d) · M6-W4 LoRA model selection + inference wiring (2d) · M6-W5 docs + eval sets cho motion (2d)

**DEV4 (mới, 8d, AI/research):** M6-A1 LoRA training experiments (3d) · M6-A2 image-to-video quality eval (motion consistency, artifact) (2.5d) · M6-A3 LightRAG A/B golden (nếu gate) (2.5d)

### P3 — Verification (4d)

- Golden set toàn bộ regression (KB + rubric + motion)
- 1 tập demo animated: BA/Producer duyệt visual + cost/video
- LightRAG A/B: nếu không cải thiện J2 ≥ 3 điểm → giữ Qdrant-only
- **Gate M6**: animated ≥ 1 cảnh/tập mặc định (hoặc rõ ràng cost-benefit chưa đủ → giữ Ken Burns, ghi quyết định); J2 đạt ngưỡng

### P4 — Release (1d)

Tag `v0.6.0`; baseline M6 (visual quality, cost/video animated vs still).

---

## Phần V — M7: Distribution & Monetization (6 tuần)

**Mục tiêu:** doanh thu + multi-channel.

### P0 — Requirements (3d, BA)

| ID | Việc | Ai |
|---|---|---|
| M7-R1 | Pricing tiers (Free/Pro/Studio) + Stripe billing, usage-based phụ phí image/video | BA |
| M7-R2 | Multi-channel: YouTube Shorts/TikTok vertical cuts (9:16), chapterization, hook-first cuts | BA |
| M7-R3 | Analytics loop: retention per scene → đề xuất cải thiện prompt/hook (agentic analysis) | BA+ARCH |

### P1 — Design (4d, ARCH)

M7-D1 Stripe webhook + subscription state machine (1d) · M7-D2 Vertical cut pipeline (FFmpeg crop 9:16 + hook-first + caption overlay) (1d) · M7-D3 Analytics ingestion (YouTube API → warehouse) (1d) · M7-D4 Agentic analysis loop (retention → prompt variant suggestion, human-approve) (1d)

### P2 — Implementation (30d)

**DEV1 (10d):** billing webhook + plan enforcement (3d) · vertical cut FFmpeg (2.5d) · analytics ingestion (2.5d) · ops/security tests (2d)

**DEV2 (10d):** Stripe checkout UI integration (2d) · Shorts/TikTok metadata + caption template (2d) · agentic analysis loop (prompt + approve flow) (3d) · multi-channel publish queue (3d)

**DEV3 (6d):** billing pages (pricing/portal) (2d) · channel settings UI (2d) · analytics dashboard (retention per scene, hook perf) (2d)

**DEV4 (4d):** retention→prompt experiments design + eval set (4d)

### P3 — Verification (4d)

- Payment flow end-to-end (Stripe test mode), refund/cancel path
- 5 kênh thử nghiệm (2 YouTube + 2 TikTok + 1 Shorts) hoạt động 2 tuần
- Analytics: đối chiếu số liệu YouTube API với internal metrics
- **Gate M7**: MRR > 0 với ít nhất 3 paying users; 5 kênh active; analytics loop có 1 khuyến nghị được áp dụng

### P4 — Release (1d)

Tag `v0.7.0`; vòng phản hồi sản phẩm khép kín (xuất bản → analytics → cải thiện prompt → tái xuất bản).

---

## Phần VI — Phụ thuộc & Critical Path

```
M2 (quality base) ──► M3 (production) ──► M4 (serial ops) ──► M5 (platform)
                                              │                    │
                                              └──► M6 (AI) ────────┘
                                                        │
                                                        ▼
                                                   M7 (monetize)
```

- **Critical path chính:** M2 → M3 → M4 → M5 → M6 → M7 (mỗi milestone bắt buộc đủ gate)
- **Track song song (không critical):** track BA (corpus, rubric, kênh pilot, cost model), track ARCH (backup, release process, spike NVENC từ M3)
- **Phụ thuộc cross:** M4 recap cần ledger (M3); M5 web editor cần dashboard (M4); M6 LightRAG gate cần golden set M2; M7 analytics cần kênh pilot (M2/M4)

**Resource ramp (mức tối thiểu để giữ timeline):**

| Giai đoạn | Team | Tổng effort/tháng |
|---|---|---|
| M2–M3 | 2 DEV + BA + ARCH | ~9 người-ngày/tuần |
| M4 | 2 DEV + BA + ARCH | ~10 |
| M5 | 3 DEV (thêm DEV3 frontend) + BA + ARCH | ~14 |
| M6 | 4 DEV (thêm DEV4 AI) + BA + ARCH | ~18 |
| M7 | 4 DEV + BA + ARCH + (ops part-time) | ~18 |

---

## Phần VII — Ngân sách ước tính (envelope)

| Giai đoạn | LLM/API | Compute/Infra | Tổng/tháng |
|---|---|---|---|
| M2–M3 | $300–800 | $50–150 | $350–950 |
| M4 | $500–1,500 | $150–400 | $650–1,900 |
| M5 | $800–2,000 | $300–800 (Postgres + web) | $1,100–2,800 |
| M6 | $1,500–4,000 (video gen + LoRA GPU) | $500–1,500 | $2,000–5,500 |
| M7 | $2,000–5,000 (multi-channel + analytics) | $1,000–3,000 | $3,000–8,000 |

Chi phí/video 10 phút (Standard) mục tiêu giảm từ $1.5 (M3) → $1.0 (M4,
cache TTS + batch) → $0.8 (M5, model tối ưu). Premium tier ≤ $5 giữ nguyên.

---

## Phần VIII — Risk Register (bổ sung so với Workplan chính)

| Rủi ro | Tín hiệu | Đối sách | Milestone |
|---|---|---|---|
| Producer không dùng dashboard (adoption thấp) | < 2 lần login/tuần | Interview P1 producer sau M4 pilot; giảm tính năng, tăng value (approve 1-clic) | M4→M5 |
| Video gen (M6) chi phí cao hơn giá trị | cost/video > $15 hoặc retention không tăng | Giữ Ken Burns mặc định; animated chỉ cho cảnh "hot" (rubric visual ≥ 4.5) | M6 |
| Multi-tenant làm phức tạp KB (universe per user) | Query chậm, alias bẩn | Tenant isolation test ngay P1 M5; migrate từng universe | M5 |
| Stripe/analytics chiếm 2 tuần không xong | API thay đổi | Cut scope M7: analytics loop giữ ở P0 tối thiểu (retention per scene only) | M7 |
| LightRAG tốn indexing nhưng không cải thiện | A/B sau 2 tuần không đạt | Chốt giữ Qdrant-only vĩnh viễn (ghi quyết định vào architecture doc) | M6 |
| Recruiter chậm (DEV3/DEV4) | Trễ ramp | M4 tách dashboard minimal (DEV2 làm được), DEV3 chỉ cần khi M5 bắt đầu | M4→M5 |

---

## Phần IX — Backlog dài hạn (chưa lên lịch, theo dõi)

| Feature | Trigger kích hoạt | Ghi chú |
|---|---|---|
| Web UI admin (users, billing, alerts) | MRR > $5k | Thay thế CLI admin |
| Mobile/PWA producer app | Producer chạm tay > 50% | PWA trước, native sau |
| Community template marketplace | > 10 producer | Style/genre/voice packs, revenue share |
| ColBERT multi-vector | Chunk > 2M hoặc J2 vẫn yếu sau LightRAG | Storage nổ — cân nhắc kỹ |
| Graphiti/Zep temporal KG | Query "facts tại episode N" trở nên phức tạp | Thay thế bi-temporal custom |
| Full video gen (Sora-class) | Giá < $0.1/giây | Thay thế image-to-video |
| Đa ngôn ngữ mở rộng (zh/en) | Kênh vi đạt NSM + demand | Prompt pack + voices + eval set mới |

---

## Phần X — Tóm tắt 30 giây

M2 (2 tuần, đang design chi tiết) → M3 (3 tuần, production) → M4 (4 tuần,
serial auto-publish + dashboard) → M5 (4–5 tuần, multi-tenant + web editor)
→ M6 (6 tuần, video động + LoRA + KG, theo gate) → M7 (6 tuần, monetize +
multi-channel). Tổng ~6 tháng (25–27 tuần) nếu gate đạt và ramp đúng lịch.
Mỗi milestone kết thúc bằng baseline + gate quyết định; pattern ainovel-cli
(arbiter, ctxpack, import pipeline, stylestat) được hấp thụ dần vào M4–M6.
Backlog dài hạn có trigger kích hoạt rõ ràng, không lên lịch trước.
