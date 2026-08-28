# Knowledge Base Subsystem — Thiết kế kiến trúc (v4)

> Vai trò trong pipeline: `transcribe → [KB] → story`. KB là **bộ nhớ dài hạn
> của một vũ trụ kể chuyện** (universe memory), không phải cache của một lần
> chạy project. Tập 12 phải nhớ bà ngoại xuất hiện ở tập 3.
>
> v4 = v3 + xử lý review `KNOWLEDGE_BASE_REVIEW.md`: protocol đầy đủ,
> Retrieval Compiler với prompt mẫu, golden set 15 câu cụ thể, ingest
> versioning (season vs universe), và Fact Ledger tách thành
> **`FACT_LEDGER_DESIGN.md`** (chi tiết ở đó, tóm tắt ở mục 8).
> Nghiên cứu nền 08/2026 đã verify qua trang chính thức (Qdrant, LightRAG,
> BGE-M3, reranker).

---

## 1. Bài toán thật — writer cần nguyên liệu, không cần "câu trả lời đúng"

KB phục vụ 3 job khác nhau của writer, không phải 3 query giống nhau:

| Job | Khi nào | Thất bại trông như thế nào |
|---|---|---|
| **J1 — Premise pack** | Trước outline | 8 chunk cùng một video, toàn đoạn lạc đề → outline lặp / lệch premise |
| **J2 — Entity dossier** | Trước outline + trước mỗi scene có nhân vật/địa điểm | Dense miss "bà Ngoại / ba Ngoai / ngoại" → nhân vật mới vô tình, hoặc gộp 2 người khác nhau |
| **J3 — Scene palette** | Mỗi beat, trước khi viết narration | Chunk quá to / sai thời điểm → cảnh 4 viết bằng chi tiết cảnh 1 |

Code hiện tại chỉ làm một `search(source_query or premise)` nhét vào cả
outline lẫn mọi scene — đó là J1 cắt cụt; J2/J3 không tồn tại. Graph không
cứu được nếu writer vẫn gọi KB một lần.

**Đặc thù khiến dense-only gãy:** ASR tiếng Việt thiếu dấu câu, tên không
dấu, biệt danh ("ba", "ngoại", "ông Tư") — proper noun là xương sống truyện
kể, đúng điểm yếu của dense embedding. Ghi incremental (podcast mới mỗi
tuần, cấm full reindex). Offline batch (recall > latency).

**Nguyên tắc đóng đinh:**
1. Chunk store là nguồn sự thật; mọi graph/summary rebuild được.
2. Ingest idempotent theo content_hash.
3. Không tối ưu retrieval khi chưa có golden set.
4. Story không chết vì KB down nếu grounding=loose.
5. KB = thế giới nguồn (podcast). Fact ledger = thế giới truyện đã viết.
   Hai thứ này không bao giờ trộn vào một collection.

---

## 2. Quyết định kiến trúc

### 2.1. Universe — quyết định số 1

```
workspace/<project>/03_knowledge/     # snapshot ingest của run này (debug, provenance)
data/kb/<universe_id>/                # bộ nhớ dài hạn của series
```

- `StoryConfig` thêm `universe: str` — **bắt buộc, không có default magic**.
  Nếu bỏ trống và default = project, serial storytelling chết im lặng: tập
  12 không thấy tập 3, không có lỗi nào báo. Đây là failure mode tệ nhất có
  thể của tính năng cốt lõi → validate lỗi rõ ràng ngay từ config.
- **Store instance scoped theo universe**: `build_knowledge_store(settings,
  universe_id)` trả về store buộc vào một universe — filter universe là bất
  biến cấu tạo, không phải quy ước query. Quên filter là không thể xảy ra.
  (universe_id cũng chính là ranh giới tenancy nếu sau này multi-user.)
- Registry `data/kb/universes.yaml`: id, ngôn ngữ, ghi chú, alias table —
  nơi neo cho governance và bootstrap alias.

### 2.2. Phase 1: Qdrant-only, không Postgres

30k–150k chunks chưa cần hai datastore. Qdrant làm cả Layer 0 lẫn Layer 1:

- Point id = chunk_id ổn định `{source_id}:{seq:04d}`.
- Payload = text + metadata + entities[] + topics[] + content_hash.
- Collection phụ (hoặc payload index) cho source-level idempotency.
- Entity/alias: YAML khi ≤ ~200 entities, SQLite một file khi chật. Chưa Postgres.
- Nâng Postgres khi có nhu cầu thật: alias audit UI, embedding versioning,
  query "entity → mọi source". Interface không đổi.

### 2.3. LightRAG: cửa mở, không phải nền

Graph có giá khi writer hỏi quan hệ ("Lan và bà ngoại đã gặp nhau lần nào?"),
không khi hỏi "chi tiết chợ sáng". 500 giờ đầu, hybrid + alias thắng
LLM-extract-mỗi-chunk. Cổng Phase 2 (mục 12): serial phức tạp + golden set
J2 không đạt sau 2 vòng tinh chỉnh. Khi bật: chạy song song, A/B, chunk store
vẫn của mình.

---

## 3. Mô hình dữ liệu — 4 granule

Chunk 600 token là đơn vị index, không phải đơn vị tiêu thụ:

```
Source (1 video/file)
  ├─ EpisodeSummary      # 3–5 "khoảnh khắc đáng kể" (person, place, action,
  │                       # sensory detail) — 1 LLM call rẻ/source, opt-in
  ├─ Chunk (600 tok)     # retrieval unit
  │    └─ Passage (2–4 câu)  # thứ thực sự nhét vào prompt scene
  └─ EntityMention
```

- **J1** retrieve sources đa dạng → đọc summary + 1–2 chunk đại diện.
- **J3** retrieve chunk → cắt passage quanh câu hit (sentence window).
- **J2** không retrieve "gần nghĩa" — `query_entities()` → dossier:

```yaml
canonical: Bà Ngoại
aliases: [bà ngoại, ngoại, ba Ngoai, bà]
type: person
mentions: 47
sources: [ep_03, ep_07, ep_12]
facts:                       # rút từ chunk, có source_id
  - "gánh hàng rong"  {source: ep_03, chunk: ep_03:0007}
  - "chợ Đồng Xuân"   {source: ep_07, chunk: ep_07:0012}
conflicts:                   # KHÔNG auto-resolve — đưa ra cho writer
  - "tên chợ: Đồng Xuân (ep_07) vs chợ Mới (ep_12)"
sample_passages: [...]
```

Dossier này là "story bible tự động". Vector search không thay được.

**Payload tối thiểu trên chunk** (metadata hiện tại quá mỏng):
`universe_id, source_id, seq, start_ts, end_ts (thiếu end là lỗ J3), speaker,
language, entities[], topics[], content_hash, transcript_quality`
(ASR tệ → filter lúc query, không xóa chunk).

---

## 4. Writer API — KnowledgeBrief compiler

`StoryStage` không biết Qdrant tồn tại. Nó nói
`brief = compiler.build(config, outline=None)` rồi
`brief = compiler.update(brief, beat)` mỗi scene.

```
StoryConfig ──▶ [Brief Compiler] ──▶ KnowledgeBrief ──▶ prompt
                    │
                    ├─ Pass 1 (trước outline):
                    │    J1 theme search  (group_by source, cap 2–3 hit/source)
                    │    J2 query_entities cho TOÀN BỘ cast + địa điểm trong premise
                    │    miss → log "unknown entity", KHÔNG bịa dossier
                    │
                    └─ Pass 2 (trước mỗi scene):
                         J3 query = beat.summary + image_hint + tên nhân vật
                         filter entities nếu resolve được
                         → palette ≤ 4 passage + facts + timestamps
```

**KnowledgeBrief:**

```python
class CitedPassage(BaseModel):
    text: str
    chunk_id: str          # citation về nguồn
    source_id: str
    start_ts: float | None

class KnowledgeBrief(BaseModel):
    theme: list[CitedPassage]          # J1 — "color", optional
    dossiers: list[EntityFacts]        # J2 — nhân vật đã tồn tại
    palette: list[CitedPassage]        # J3 — per-scene, empty ở bước outline
    unknown_entities: list[str]        # writer biết mình đang bịa
    degraded: bool
    reason: str | None
```

Prompt nhận brief có cấu trúc, không nhận `list[str]`:

```
[FACTS — must cite if grounding=strict]
- (ep_07 12:40, SPEAKER_00) "chợ họp từ tờ mờ đất..."
[COLOR — atmosphere, optional]
- ...
[CONFLICT]
- tên chợ: Đồng Xuân (ep_07) vs chợ Mới (ep_12) — writer chọn hoặc né
[UNKNOWN]
- không có dossier cho "Lan" trong KB
```

**Grounding có răng:**

| | loose | strict |
|---|---|---|
| KB down | viết tiếp, warning | fail stage |
| entity miss | được bịa, gắn `invented: true` vào ledger | cấm nhân vật/sự kiện mới |
| facts_used | optional | bắt buộc map về chunk_id |

`facts_used` trên StoryScene (hiện luôn `[]`) phải được compiler điền —
không có nó, reviewer/fact ledger sau này không có việc mà làm.

### 4.1. Chống lặp trích dẫn trong một tập

Compiler giữ **episode-scoped citation state**: `cited_chunk_ids` qua các
scene. Scene N: (a) passage đã cited ở scene 1..N-1 bị loại khỏi palette
(đánh dấu "already used — vary it" nếu thật sự relevant); (b) dedup toàn
brief theo chunk_id, ưu tiên palette > theme. Không có cơ chế này, một
passage đẹp sẽ bị nhai lại ở 5/10 cảnh — lỗi chất lượng truyện điển hình.

### 4.2. Context budget là ràng buộc nhất hạng

Brief có token budget, compiler truncate theo thứ tự ưu tiên:
**strict facts (J2) > scene palette (J3) > dossier sample passages >
theme color (J1)**. Khi context LLM đầy: cắt theme pack trước — outline đã
"nhớ" theme rồi. Định lượng khởi điểm: color ≤ 300 token, palette ≤ 4
passage ~ 400 token, dossier facts không cắt ở strict.

### 4.3. Prompt mẫu — outline (pass 1) và scene (pass 2)

Compiler render brief thành các section có nhãn, nhét vào prompt qua
placeholder. Outline prompt (`prompts/outline.txt` mở rộng) nhận:

```
[THEME — màu sắc cho premise, optional, dùng làm cảm hứng]
- (ep_03) "...chợ quê với những gánh hàng rong từ tờ mờ đất..."
- (ep_07) "...tiếng rao cá buổi sớm vang qua con ngõ nhỏ..."

[DOSSIERS — nhân vật có thật trong corpus, giữ nhất quán nếu dùng]
- Bà Ngoại (person, 47 mentions, ep_03/ep_07/ep_12)
    facts: gánh hàng rong · chợ Đồng Xuân · hay kể chuyện buổi chiều
- CONFLICT: tên chợ — "Đồng Xuân" (ep_07) vs "chợ Mới" (ep_12): chọn hoặc né

[ESTABLISHED — sự kiện đã thiết lập ở các tập trước, KHÔNG mâu thuẫn]
- (ep_003) Bà Ngoại còn sống, ở cùng Lan
[INVENTED CÁC TẬP TRƯỚC — vẫn là canon của truyện]
- (ep_003) Lan là cháu ngoại của Bà Ngoại

[UNKNOWN — không có dữ liệu, được phép sáng tạo]
- "Hà Giang" không có trong corpus
```

Scene prompt (`prompts/scene.txt` mở rộng) chỉ nhận phần gọn của brief —
palette của cảnh đó + established facts của nhân vật trong cảnh, KHÔNG nhận
lại toàn bộ theme pack (outline đã "nhớ" theme):

```
[SCENE PALETTE — chi tiết thật để viết cảnh, cite nếu grounding strict]
- (ep_07 12:40, SPEAKER_00) "chợ họp từ tờ mờ đất, khi sương chưa tan
  thì các gánh cá đã dọc theo bờ nước..."
[ESTABLISHED — nhân vật trong cảnh này]
- (ep_005) Lan sợ nước to
```

Compiler đảm bảo: cùng passage không xuất hiện ở 2 prompt khác nhau trong
một tập (citation state, mục 4.1), và tổng token của các section không vượt
budget (mục 4.2). Degraded → renderer in reason vào prompt
(`[KB DEGRADED: qdrant unavailable — viết từ premise]`) thay vì bỏ trống âm
thầm — writer LLM biết trạng thái grounding của mình.

---

## 5. Ingest — rẻ, idempotent, tách khỏi critical path

```
Transcript
  1. Normalize nhẹ (whitespace/punctuation) — không "sửa" nội dung
  2. Alias pass: replace biến thể đã biết trong BẢN SAO để index,
     giữ raw trong payload (citation phải đúng lời gốc)
  3. Chunk: speaker-turn, ~600 tok, overlap 90, gắn start+end
  4. Entity/topic: mặc định dictionary + rule (viết hoa, pattern
     bà/ông/chú/cô + Tên); opt-in 1 LLM call/source
  5. Embed BGE-M3 dense+sparse một lần encode
  6. UPSERT Qdrant theo chunk_id; source row theo content_hash:
       hash trùng → no-op; hash khác (re-transcribe) → xóa point cũ, ghi version mới
  7. Merge alias table — entity mới = pending_review, KHÔNG auto-merge
```

**Job mechanics, không cần queue infra:** KnowledgeStage trong pipeline
chính ingest inline với timeout ngắn; fail → đánh dấu `pending_ingest`
trong workspace, transcript vẫn DONE, video vẫn chạy nếu story không cần
KB lần này. Lệnh `storyforge ingest --universe X --drain` quét mọi
workspace có marker và bù ingest. Idempotency (chunk_id + content_hash)
làm retry trở nên trivial — marker file + sweep là đủ, không Celery.

**Ba ý dễ sai:**
- Không nhúng LLM vào write path mặc định — khẩu ngữ làm NER LLM không ổn
  định; ingest phải chạy đêm không người.
- Alias là **dữ liệu sản phẩm**, không phải phụ kiện: bảng viết tay 50–100
  dòng ("ba Nam" ≡ "ông Nam" ≡ "ba") tuần đầu kéo J2 hơn mọi reranker.
- EpisodeSummary phải extract "khoảnh khắc đáng kể" (người, chỗ, hành động,
  chi tiết giác quan), KHÔNG phải abstract topic summary — "host kể về
  tuổi thơ" vô dụng cho J1; "cảnh chợ sáng với gánh cá của bà Sáu" là vàng.

### 5.1. Idempotency & versioning — ma trận đầy đủ

| Tình huống | Hành động |
|---|---|
| Same universe + same content_hash | No-op (source đã ingest) |
| Same universe + hash khác (re-transcribe model mới) | Xóa points cũ của source, ghi version mới; chunk_id giữ nguyên pattern `{source_id}:{seq}` — citation trong fact ledger không gãy |
| Same content, universe khác (nội dung dùng lại cho series mới) | Ingest bình thường vào universe kia — points độc lập theo collection-per-universe |
| **Season mới** | **Cùng universe** (bà Ngoại season 1 vẫn phải được nhớ ở season 2). Season chỉ là metadata episode |
| Universe mới | Chỉ khi reset canon (reboot, spin-off timeline riêng). Registry `universes.yaml` ghi rõ lý do tạo |

Nguyên tắc: **universe = ranh giới canon, season = nhóm tập trong canon,
episode = đơn vị ghi ledger**. Không bao giờ dùng universe mới để "gói gọn"
một season — đó là cách giết universe memory một cách vô tình.

### 5.2. Chroma → Qdrant: đường di chuyển

`KnowledgeStore` protocol là hợp đồng; backend chỉ là implementation.
Thứ tự di chuyển: (1) implement `QdrantKnowledgeStore` cạnh Chroma hiện có,
(2) protocol conformance suite chạy gegen cả hai, (3) production config
chuyển `SF__KNOWLEDGE__STORE=qdrant`, (4) script backfill đọc
`03_knowledge/chunks.jsonl` mọi workspace → ingest vào universe qua đúng
write path (không copy thẳng vào Qdrant — phải qua normalize/entity pass),
(5) xóa Chroma backend sau 1 release không ai đổi lại. Không dual-write
song song dài hạn — một cửa write, một lần di chuyển.

### 5.3. Alias cold-start bootstrap

Tuần 0 bảng alias rỗng, J2 sẽ yếu. Ba nguồn bootstrap ngay:
1. Harvest `CharacterSheet.name` từ mọi story config trong universe.
2. Ingest sinh báo cáo "top 50 token viết hoa/bare theo tần suất chưa map"
   → 15 phút review cuối tuần gieo bảng đầu tiên.
3. Punctuation-restore của ASR (đã có ở WhisperX) giúp quy tắc NER chạy ổn.

Schema alias với provenance:
`canonical, aliases[], type, status: confirmed|pending, added_by: human|llm`.

---

## 6. Retrieval Phase 1

| Thành phần | Phase 1 | Để sau |
|---|---|---|
| Qdrant named vectors dense+sparse BGE-M3, RRF | mặc định | |
| Payload filter: universe, language, source, entities | mặc định | |
| group_by source_id (J1) | mặc định | |
| Alias → entity filter / sparse boost (J2) | mặc định, YAML/SQLite | |
| BGE-reranker-v2-m3 | **cờ tắt**, bật khi golden set có | |
| Postgres | không | khi cần audit/versioning |
| LightRAG | không | cổng mục 12 |
| ColBERT / quantization / Milvus | không | |

Query planning rất mỏng — `SearchQuery.intent` do StoryStage set (writer
biết mình đang làm job nào, search engine không đoán):

- intent=theme → group_by source, candidate_k lớn
- intent=scene → top_k nhỏ (4 passage), ưu tiên chunk ngắn + timestamp
- query chứa alias đã biết → must/should filter entities

**Degrade matrix:**

| Tình huống | loose | strict |
|---|---|---|
| Qdrant down lúc query | `KnowledgeBrief.empty(reason=...)`, story chạy | fail rõ |
| J1 zero hit | viết từ premise | fail "không có ground" |
| Ingest fail | transcript DONE, KB FAILED/retry, video chạy | như nhau |

---

## 7. Golden set — theo job, có ngưỡng định lượng

30–50 cặp, chia đều 3 job. Cách lấy mẫu rẻ nhất: 2 tuần đầu, người viết
story highlight "đoạn này lẽ ra phải lên" khi outline sai.

| Job | Bộ đo | Ngưỡng gate ban đầu |
|---|---|---|
| J1 | source-coverage@8 (số source khác nhau) + recall@50 | ≥ 4 source / ≥ 0.70 |
| J2 | entity hit-rate (canonical đúng) + **false merge rate** | ≥ 0.90 / **≤ 0.02** (gate cứng — gộp 2 người nguy hiểm hơn miss) |
| J3 | nDCG@4 trên **passage** (không phải chunk 600 tok) + timestamp-band accuracy | ≥ 0.60 / ≥ 0.80 |

Không merge thay đổi chunk size / reranker / graph nếu không chạy lại bộ này.

**Bộ khởi đầu 15 câu (5/job) — mẫu cụ thể:**

| # | Job | Query | Expected |
|---|---|---|---|
| 1 | J1 | "tuổi thơ ở nông thôn miền Bắc" | ≥ 4 source khác nhau trong top-8 |
| 2 | J1 | "ký ức về bà ngoại" | ≥ 4 source, không source nào > 3 hit |
| 3 | J1 | "ngày xưa đi chợ sớm" | ≥ 4 source |
| 4 | J1 | "mưa và những buổi chiều quê" | ≥ 4 source |
| 5 | J1 | "tin nhắn/điện thoại về nhà" | ≥ 4 source (nếu corpus thiếu → thêm nguồn trước khi đo) |
| 6 | J2 | "bà Ngoại" | canonical đúng + ≥ 1 passage đúng người |
| 7 | J2 | "ba Ngoai" (không dấu) | resolve về cùng canonical #6 |
| 8 | J2 | "ngoại" (biệt danh trần) | resolve về #6, KHÔNG merge sang người khác cùng alias |
| 9 | J2 | "ông Tư" | canonical đúng + ≥ 1 passage |
| 10 | J2 | "chợ Đồng Xuân" | type=place + ≥ 1 passage đúng chỗ |
| 11 | J3 | "cảnh chợ buổi sáng sớm" + hint "chợ quê" | chunk có start_ts trong band mô tả chợ sáng |
| 12 | J3 | "gánh hàng rong đi qua ngõ" | passage nhắc gánh hàng, đúng nguồn |
| 13 | J3 | "trời mưa, trẻ con chạy về" | passage đúng band cảnh mưa |
| 14 | J3 | "bữa cơm tối gia đình" | passage đúng band bữa ăn |
| 15 | J3 | "tiếng rao cá buổi sớm" | passage đúng, đúng thời điểm trong source |

Ghi chú: #8 là câu anti-false-merge chủ đích — nếu hệ thống gộp "ngoại" của
hai gia đình khác nhau thành một entity, câu này phải fail. Giữ nguyên bộ
này trong repo (`tests/golden/kb_queries.yaml`) — mọi PR chạm retrieval
phải chạy và dán kết quả.

**Mới — test compiler bằng unit test:** golden set đo retrieval, nhưng brief
compiler là logic thuần (budget truncate, citation dedup, UNKNOWN marking,
degrade) → unit test với mocked SearchHit/EntityFacts. Rẻ và giá trị cao.

---

## 8. Fact ledger — thế giới truyện đã viết (tách khỏi KB)

**Chi tiết đầy đủ ở `FACT_LEDGER_DESIGN.md`** — schema, interface, reviewer
pass, temporality/retcon. Tóm tắt những gì liên quan trực tiếp đến KB:

- KB không bao giờ lưu sự kiện truyện tự bịa. Ledger là YAML per-episode
  (`data/ledgers/<universe>/s<season>/<episode>.yaml`), human-editable,
  load vào memory khi build brief.
- Fact có `origin: cited | inferred | invented` (provenance rời rạc, không
  confidence float) + `chunk_refs` khi cited. Trạng thái chỉ có
  `established / superseded` — "disputed" là kết quả của `find_conflicts()`
  lúc review, không phải trạng thái lưu.
- Brief compiler là điểm hòa nhập duy nhất của hai thế giới: prompt nhận
  [ESTABLISHED] (facts từ ledger) tách bạch với [DOSSIERS] (từ KB) —
  xem prompt mẫu mục 4.3.
- **Season ≠ universe mới**: season dùng cùng universe (bà Ngoại season 1
  vẫn được nhớ ở season 2); universe mới chỉ khi reset canon.
- Subject của fact normalize qua **cùng alias table** với KB — một bảng
  chuẩn hóa tên cho cả hai thế giới.
- Gate triển khai: chỉ build khi tồn tại tập 2; trước đó `FactLedger` là
  no-op in-memory trả `[]` — interface đứng sẵn.

---

## 9. Interface — protocol đầy đủ, khóa trước backend

```python
# --- query & hits -----------------------------------------------------------

class SearchIntent(StrEnum):
    THEME = "theme"      # J1 — trước outline, cần độ phủ nguồn
    ENTITY = "entity"    # J2 — quanh một thực thể
    SCENE = "scene"      # J3 — per-beat, cần passage nhỏ đúng thời điểm

class MetadataFilters(BaseModel):
    language: str | None = None
    source_ids: list[str] | None = None
    topics: list[str] | None = None
    time_range: tuple[float, float] | None = None   # (start_ts, end_ts)
    min_transcript_quality: float | None = None

class SearchQuery(BaseModel):
    text: str
    intent: SearchIntent = SearchIntent.THEME
    top_k: int = 8
    candidate_k: int = 50          # đầu vào reranker
    filters: MetadataFilters | None = None
    group_by_source: bool = False  # J1 bật; cap 2–3 hit/source
    use_reranker: bool = False

class SearchHit(BaseModel):
    chunk_id: str
    text: str                      # raw (không phải bản alias-normalized)
    score: float
    source_id: str
    start_ts: float
    end_ts: float
    speaker: str | None
    entities: list[str]
    topics: list[str]

# --- entity dossier (J2) ----------------------------------------------------

class EntityFacts(BaseModel):
    canonical: str
    aliases: list[str]
    type: str                      # person | place | event | org | other
    mention_count: int
    sources: list[str]
    facts: list[EntityFactLine]    # dòng rút từ chunk, kèm chunk_id
    conflicts: list[str]           # mâu thuẫn giữa các nguồn — KHÔNG auto-resolve
    sample_passages: list[CitedPassage]

# --- ingest -----------------------------------------------------------------

class IngestReport(BaseModel):
    source_id: str
    universe_id: str
    content_hash: str
    status: Literal["ingested", "noop", "reingested"]  # hash trùng = noop
    chunks_written: int
    entities_new: int              # pending_review — KHÔNG auto-merge
    entities_pending: int

# --- protocol ---------------------------------------------------------------

class KnowledgeStore(Protocol):
    def ingest(self, transcript: Transcript) -> IngestReport: ...
    def search(self, query: SearchQuery) -> list[SearchHit]: ...
    def query_entities(self, name: str) -> EntityFacts | None: ...
    def similar_sources(self, source_id: str) -> list[SourceRef]: ...
    def health(self) -> bool: ...

def build_knowledge_store(settings, universe_id: str) -> KnowledgeStore: ...

# --- writer-facing facade (không phải store protocol) ----------------------

class KnowledgeBrief(BaseModel):
    theme: list[CitedPassage]          # J1
    dossiers: list[EntityFacts]        # J2
    palette: list[CitedPassage]        # J3 — per-scene
    unknown_entities: list[str]
    degraded: bool
    reason: str | None

class BriefCompiler:
    def build(self, config: StoryConfig) -> KnowledgeBrief: ...
    def update(self, brief: KnowledgeBrief, beat: StoryBeat) -> KnowledgeBrief: ...
```

Ghi chú thiết kế:
- `ingest(Transcript)` chứ không `index(chunks)`: normalize + entity +
  chunking nằm gọn trong KB. KnowledgeStage chỉ: load transcript → ingest →
  ghi IngestReport + jsonl snapshot ra workspace.
- Store bound theo universe_id (mục 2.1) — filter là bất biến cấu tạo,
  `SearchQuery` không cần (và không được có) universe_id.
- `SearchHit.text` trả raw — citation đúng lời gốc; bản alias-normalized
  chỉ dùng nội bộ cho matching.
- `BriefCompiler` là facade riêng, không nằm trong store protocol —
  StoryStage không biết Qdrant tồn tại.
- `KnowledgeSettings.store` nới Literal thêm `"qdrant"`; Chroma giữ tạm làm
  backend dev đến khi v1 ship rồi **xóa**, không maintain mãi.

**Phản biện về Chroma làm backend dev/test:** thay vì giữ Chroma như
backend thật thứ hai (sẽ thối rữa vì không ai chạy production trên nó), dùng
**một real backend (Qdrant, docker-compose đã có) + một in-memory fake**
cho unit test. Protocol conformance suite chạy gegen cả hai. Một backend
thật + một test double > hai backend thật.

---

## 10. Lộ trình

- **A — Chốt hợp đồng (1–2 ngày):** universe bắt buộc, SearchQuery + intent,
  `ingest(Transcript)`, KnowledgeBrief, end_ts trên chunk, golden set
  skeleton 15 câu (5/job) từ audio mẫu. Song song: tách KB stage khỏi
  "fail cả pipeline" khi loose.
- **B — Qdrant-only hybrid + alias YAML:** idempotent ingest, group-by
  source, entity filter, 2-pass brief + degrade + citation state.
  Reranker tắt.
- **C — Đo:** golden set đầy đủ. J2 kém → bỏ công vào alias/NER, không vào
  graph. J1 coverage kém → sửa group-by/chunk, không tăng top_k mù.
- **D — Opt-in:** reranker cờ, EpisodeSummary 1 call/source, SQLite entity
  nếu YAML chật, alias review định kỳ.
- **E — Ledger:** khi có tập 2.
- **F — Cổng LightRAG:** chỉ khi C không đạt J2/quan hệ đa tập.

---

## 11. TL;DR

| Câu hỏi | Chốt |
|---|---|
| KB là gì? | Universe memory, không phải cache của một project run |
| Universe | Bắt buộc trong config, store bound theo universe — không default magic |
| Season vs universe? | Season = cùng universe (metadata episode). Universe mới chỉ khi reset canon |
| Writer cần gì? | Theme pack + entity dossier + scene palette qua brief compiler, không phải list[str] |
| Chống lặp | Citation state episode-scoped + dedup brief theo chunk_id |
| Context đầy | Cắt theo ưu tiên: strict facts > palette > dossier samples > theme |
| Prompt nhận gì? | Section có nhãn ([THEME]/[DOSSIERS]/[ESTABLISHED]/[UNKNOWN]) — xem mục 4.3 |
| Store Phase 1? | Qdrant-only (chunk + hybrid + payload). Postgres/LightRAG sau |
| Embedding? | BGE-M3 dense+sparse. OpenAI small chỉ để dev |
| Entity? | Alias table (dữ liệu sản phẩm) + payload filter, NER rule-based; LLM opt-in |
| Graph? | Không, cho đến khi golden set J2/quan hệ thất bại |
| Fact ledger? | Tách khỏi KB — chi tiết ở FACT_LEDGER_DESIGN.md; gate khi có tập 2 |
| Ingest versioning? | content_hash: trùng=no-op, khác=re-ingest; chunk_id ổn định giữ citation |
| Golden set? | 15 câu khởi đầu (5/job) trong repo; mọi PR chạm retrieval phải chạy |
| Fail? | loose degrade; strict fail; ingest tách khỏi video |
| Việc đầu tiên? | Protocol + universe + 2-pass brief + 15 câu golden — trước khi docker Qdrant cho "đủ stack" |

Điểm then chốt: công cụ đã đúng. Việc còn lại là cho writer một **API trí
nhớ** (dossier/palette/citation) và một **universe** để trí nhớ sống qua các
tập. Làm Qdrant trước mà writer vẫn `search(premise)` một lần thì KB mới cũng
chỉ là RAG chatbot gắn nhầm vào máy viết truyện.
