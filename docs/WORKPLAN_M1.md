# Workplan M1 — Phân công 2 developer song song

> **Goal M1 (định nghĩa xong là xong):** chạy được end-to-end cho 1 universe:
> audio local → transcript → ingest vào Qdrant (hybrid, idempotent) → story
> sinh bằng 2-pass brief (J1+J2 trước outline, J3 mỗi scene) → 15 câu golden
> set chạy được qua script eval và có kết quả baseline.
>
> Tham chiếu thiết kế: `KNOWLEDGE_BASE_DESIGN.md` (v4) + `FACT_LEDGER_DESIGN.md`.
> Scope M1 KHÔNG gồm: reranker (cờ tắt), fact ledger (no-op stand-in),
> LightRAG, reviewer pass, Postgres.

---

## 1. Nguyên tắc phân công

- **Chia theo package, không theo layer** — mỗi dev sở hữu hẳn một vùng file,
  không có file cả hai cùng sửa (trừ contract ngày 1–2).
- **Contract trước, code sau:** ngày 1–2 hai dev ngồi chốt chung
  `storyforge/kb/types.py` (pure pydantic models, không I/O). Sau đó file
  này ĐÓNG BĂNG — mọi thay đổi sau đó phải qua hội ý ngắn.
- **Integration point duy nhất:** `stages/knowledge.py` và `stages/story.py`
  của Dev 2 gọi vào `build_knowledge_store()` của Dev 1 — không dev nào viết
  xuyên sang phần kia.
- Mỗi PR phải xanh `make lint typecheck test` (CI rule trong CONVENTIONS.md).

### Sơ đồ phụ thuộc

```
            [ngày 1–2: CONTRACT CHUNG — 2 dev cùng làm]
                            │
        ┌───────────────────┴────────────────────┐
        ▼ DEV 1 — "KB Core"                      ▼ DEV 2 — "Writer Integration"
  kb/types.py (đóng băng)                  kb/types.py (chỉ đọc)
  kb/qdrant_store.py                       kb/compiler.py  (BriefCompiler)
  kb/memory_store.py (fake)                kb/brief.py     (render prompt sections)
  kb/alias.py (YAML + bootstrap)           stages/knowledge.py (refactor ingest)
  kb/embedder.py (BGE-M3/local+API)        stages/story.py (2-pass)
  kb/ingest.py (write path)                prompts/outline.txt, scene.txt
  docker/ (thêm service Qdrant)            tests/golden/kb_queries.yaml (15 câu)
  tests/knowledge/ (conformance)           tests/story/
        │                                        │
        └──────────────► integration ◄───────────┘
                    (ngày 8–10, cả hai)
```

---

## 2. DEV 1 — "KB Core" (backend store & ingest)

**Sở hữu file (được toàn quyền sửa):**
- `src/storyforge/kb/qdrant_store.py`, `memory_store.py`, `alias.py`,
  `embedder.py`, `ingest.py` (mới — tạo package `storyforge/kb/`)
- `src/storyforge/providers/knowledge.py` (chuyển thành re-export facade,
  xóa implementation Chroma cũ vào cuối M1 sau khi Dev 2 đổi xong call site)
- `src/storyforge/core/config.py` — phần `KnowledgeSettings` + `EmbeddingSettings`
- `docker/docker-compose.yml` (thêm service Qdrant + volume)
- `tests/knowledge/` toàn bộ
- `.env.example` (mục knowledge)

**Các việc theo thứ tự:**

| # | Việc | Chi tiết | Ngày |
|---|---|---|---|
| D1 | Qdrant collection bootstrap | Named vectors dense+sparse (BGE-M3), payload index TẠO TRƯỚC ingest (source_id, speaker, start_ts, end_ts, entities, topics, language) — mục 4.1 design v4 | 3–4 |
| D2 | Embedder | `EmbeddingProvider` protocol: `openai` (httpx, có sẵn) + `bge_m3_local` (sentence-transformers, lazy import). Một lần encode ra dense+sparse | 3–4 |
| D3 | Ingest write path | Đúng 7 bước mục 5 design: normalize nhẹ → alias pass (bản copy để index, raw giữ trong payload) → chunk speaker-turn 600/90 → entity rule-based (dictionary + pattern bà/ông/chú/cô + Tên) → embed → UPSERT theo chunk_id → merge alias (pending, không auto-merge). Idempotent: content_hash trùng = noop, khác = re-ingest (xóa points cũ của source) | 5–7 |
| D4 | Alias store + bootstrap | YAML `data/kb/<universe>/aliases.yaml`, schema `canonical, aliases[], type, status, added_by` (mục 5.3 design) + báo cáo "top 50 token chưa map" ra IngestReport | 5–6 |
| D5 | Search | Qdrant Query API prefetch dense+sparse → RRF, payload filter, group_by source (cap 2–3 hit/source), `query_entities()` từ payload + alias. `similar_sources()` bằng dense của source trung bình | 6–7 |
| D6 | Memory fake + conformance suite | `InMemoryKnowledgeStore` implement cùng protocol; `tests/knowledge/test_conformance.py` chạy ĐỐI VỚI CẢ HAI backend (parametrize) — đây là cổng merge cho cả Dev 2 | 7–8 |
| D7 | Qdrant health + degrade path | `health()` + exception chuẩn để Dev 2 bắt mà không biết Qdrant | 7 |

**Định nghĩa xong (DoD) Dev 1:**
- Ingest 1 transcript thật 2 lần liên tiếp → lần 2 `status=noop`, số point
  không đổi. Re-transcribe (hash khác) → points cũ bị xóa, chunk_id pattern
  giữ nguyên.
- Conformance suite xanh trên cả Qdrant (docker) lẫn memory fake.
- `storyforge kb-health` (hoặc tương đương) báo status universe.

---

## 3. DEV 2 — "Writer Integration" (compiler & stages)

**Sở hữu file:**
- `src/storyforge/kb/compiler.py`, `kb/brief.py` (mới)
- `src/storyforge/core/types.py` — `StoryConfig.universe` (BẮT BUỘC, validate
  lỗi rõ nếu thiếu), `season`, `facts_used` wiring, `CitedPassage` re-use
- `src/storyforge/stages/knowledge.py`, `stages/story.py` (refactor)
- `prompts/outline.txt`, `prompts/scene.txt` (mở rộng section có nhãn)
- `tests/golden/kb_queries.yaml` (15 câu, đúng bảng mục 7 design v4)
- `tests/story/`, `tests/test_knowledge.py` (di chuyển logic chunking cũ)
- `src/storyforge/cli.py` — lệnh `eval` chạy golden set + `ingest --drain`
- `config/story_config.example.yaml` (thêm universe/season)

**Các việc theo thứ tự:**

| # | Việc | Chi tiết | Ngày |
|---|---|---|---|
| W1 | Universe trong config | `StoryConfig.universe` bắt buộc (không default magic — mục 2.1 design), `season` optional. StoryConfig.example cập nhật | 3 |
| W2 | Refactor KnowledgeStage | `load transcript → store.ingest() → IngestReport + snapshot jsonl ra workspace`. KB fail khi loose → mark stage FAILED nhưng pipeline TIẾP TỤC (marker `pending_ingest`); strict → fail stage. Bỏ logic chunking cũ (chuyển vào Dev 1's write path) | 3–4 |
| W3 | BriefCompiler pass 1 | `build(config)`: J1 theme search (group_by_source) + J2 `query_entities` toàn cast + địa điểm từ premise. Miss → `unknown_entities`, KHÔNG bịa dossier | 5–6 |
| W4 | BriefCompiler pass 2 | `update(brief, beat)`: J3 query = beat.summary + image_hint + nhân vật; filter entities nếu resolve. Citation state episode-scoped (mục 4.1) + context budget truncate (mục 4.2: strict facts > palette > dossier samples > theme) | 6–7 |
| W5 | Prompt renderer + templates | Render section có nhãn [THEME]/[DOSSIERS]/[ESTABLISHED]/[UNKNOWN]/[SCENE PALETTE] đúng mẫu mục 4.3; degraded → in reason. Scene prompt KHÔNG nhận lại theme pack | 5–6 |
| W6 | StoryStage 2-pass | Outline dùng brief pass 1; mỗi scene gọi `update()`; strict mode bắt buộc `facts_used` map về chunk_id, loose optional + `invented` | 7–8 |
| W7 | Golden set + eval CLI | 15 câu đúng bảng mục 7 design (kể cả câu #8 anti-false-merge "ngoại"); lệnh `storyforge eval --universe X` in metrics: source-coverage@8, recall@50, entity hit-rate, nDCG@4 | 7–8 |
| W8 | Unit test compiler | Mock SearchHit/EntityFacts: budget truncate, citation dedup, UNKNOWN marking, degrade rendering — không cần backend thật (chạy với memory fake của Dev 1 sau integration) | 7–8 |

**Định nghĩa xong (DoD) Dev 2:**
- Story config thiếu universe → lỗi config rõ ràng, không default.
- Prompt outline/scene render đúng format, có thể đọc bằng mắt từ artifact
  (lưu prompt cuối vào `04_story/` để inspect).
- Loose + KB down → story vẫn sinh, brief `degraded=true` + reason.
- `storyforge eval` chạy 15 câu và in bảng metrics baseline.

---

## 4. Contract chung (ngày 1–2, hai dev cùng chốt)

Sản phẩm: PR nhỏ gộp `src/storyforge/kb/types.py` + `kb/__init__.py` chứa
TOÀN BỘ model của protocol (mục 9 design v4 đã có sẵn bản nháp — chỉ cần
rà soát, không viết mới):

- `SearchIntent`, `MetadataFilters`, `SearchQuery`, `SearchHit`
- `EntityFacts`, `EntityFactLine`, `CitedPassage`
- `IngestReport`, `KnowledgeStore` (Protocol), `build_knowledge_store()`
- `KnowledgeBrief` (Dev 2 tiêu thụ, nhưng định nghĩa chung một chỗ)

Quy tắc ngày 1–2:
- Chốt chính xác field nào `SearchHit` trả về (raw text, KHÔNG bản
  alias-normalized) — sai chỗ này là rewrite cả hai phía.
- Chốt exception contract: KB raise gì khi down (`KnowledgeBaseError` với
  `retryable`?) để compiler bắt đúng.
- Chốt hàm `build_knowledge_store(settings, universe_id)` — signature
  đóng băng từ ngày 3.
- Chia luôn test audio mẫu (2–3 file ngắn) + agrees golden set seed queries.

Sau ngày 2, `kb/types.py` chỉ đổi qua hội ý 15 phút + PR cả hai review.

---

## 5. Timeline 10 ngày

```
Ngày 1–2   ▓▓ contract chung (pair)          — cả 2 dev
Ngày 3–4   ░░ Dev1: D1+D2    │ Dev2: W1+W2
Ngày 5–6   ░░ Dev1: D3+D4    │ Dev2: W3+W5
Ngày 7–8   ░░ Dev1: D5+D6+D7 │ Dev2: W4+W6+W7+W8
Ngày 9     ▓▓ INTEGRATION: pipeline e2e trên Qdrant thật, fix bugs
Ngày 10    ▓▓ Chạy golden set → ghi baseline metrics → demo + retro
```

Merge points (pull nhỏ, đừng tích tụ):
- Cuối ngày 4: Dev 1 merge D1+D2 (collection + embedder, chưa ingest);
  Dev 2 merge W1+W2 (dùng memory fake tạm để không block).
- Cuối ngày 8: hai bên merge hết, Dev 2 chuyển call site từ fake sang
  `build_knowledge_store` thật.

## 6. Rủi ro & đối sách

| Rủi ro | Đối sách |
|---|---|
| Dev 2 bị block vì Dev 1 chưa xong store | Memory fake được viết NGAY trong contract spike (ngày 2) — Dev 2 code toàn bộ compiler/tests chống fake, chỉ đổi backend lúc integration |
| Sai lệch contract giữa hai phía | Conformance suite là cổng merge; `kb/types.py` đóng băng sau ngày 2 |
| BGE-M3 local cần GPU/VRAM quá nặng máy dev | Embedder có provider `openai` chạy được từ ngày 1; local chỉ là optimize sau |
| Qdrant sparse + RRF API sai version | Dev 1 spike Qdrant Query API vào ngày 3 (viết test nhỏ nhất có thể trước khi build D3) |
| Golden set "đo trước khi có corpus" | Chỉ cần 2–3 audio mẫu ingested là đủ chạy 15 câu; nếu corpus thiếu nguồn cho J1 câu #5 → thêm nguồn mẫu, không hạ ngưỡng |
| Cả hai cùng muốn sửa `core/config.py` | Chia section: Dev 1 sở hữu `KnowledgeSettings`/`EmbeddingSettings`; Dev 2 không đụng config ngoài `types.py` |

## 7. Demo ngày 10 (acceptance)

1. `docker compose up qdrant` → `storyforge kb-health --universe demo` xanh.
2. `storyforge run --project demo --local-file sample.mp3 ...` — ingest 2
   lần liên tiếp, lần 2 noop.
3. `04_story/story.json` có `facts_used` + prompt artifact đọc được các
   section [THEME]/[DOSSIERS]/[UNKNOWN].
4. Stop Qdrant → chạy lại (loose) → story vẫn sinh, brief degraded=true.
5. `storyforge eval --universe demo` → bảng 15 câu + metrics baseline
   lưu vào `tests/golden/baseline_M1.md`.
