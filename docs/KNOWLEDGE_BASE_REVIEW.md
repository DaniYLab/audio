# Knowledge Base Subsystem — Review & Feedback (v3)

**Ngày:** 26/08/2026  
**File:** `docs/KNOWLEDGE_BASE_DESIGN.md` (đã update từ v2 sang v3)  
**Mục tiêu:** Đánh giá feedback sau khi user update design. Tập trung vào những gì còn hở, cần chỉnh, và cách tiếp tục brainstorm.

---

## 1. Đánh giá tổng thể

Update v3 đã **tốt hơn nhiều** so với v2. Các điểm nổi bật:

- **Universe memory** được nhấn mạnh rõ ràng — đây là quyết định kiến trúc quan trọng nhất, không còn tranh cãi.
- **J1/J2/J3** được liệt kê rõ, kèm lý do tại sao code cũ chỉ làm J1.
- **Fact ledger tách biệt** với KB — đúng, vì ledger là thế giới truyện đã viết, KB là thế giới nguồn (podcast).
- **Nguyên tắc đóng đinh** (5 điểm) được rút gọn và mạnh mẽ.
- **Phase 1 Qdrant-only** vẫn giữ, không vội LightRAG.

**Điểm còn hở lớn nhất:**  
v3 chưa giải quyết **Fact Ledger** rõ ràng (chỉ đề cập “tách biệt” mà không nói schema, interface, hay cách integrate vào reviewer). Đây là phần quan trọng nhất cho serial storytelling, vì reviewer cần kiểm tra “sự kiện đã thiết lập ở tập trước có bị mâu thuẫn với tập này không”.

---

## 2. Những điểm cần chỉnh / bổ sung (P0 / P1)

### 2.1. Fact Ledger — chưa có schema & interface

Hiện tại chỉ nói “Fact ledger = thế giới truyện đã viết” và “không trộn với KB”. Nhưng reviewer cần:

- Schema: `FactLedger` (episode_id, fact_id, canonical_fact, cited_from_kb_chunk, confidence, status: {verified, disputed, pending}).
- Interface: `add_fact(fact, source_id)` + `query_conflicting_facts()` + `get_audit_log()`.
- Integrate vào reviewer pass (mới) — reviewer kiểm tra draft có fact nào bị mâu thuẫn không.

**Đề xuất thêm section (sau mục 2.1):**

```
## 2.4 Fact Ledger (phần quan trọng cho serial)

FactLedger không phải “sự kiện đã xảy ra” mà là “sự kiện đã thiết lập trong truyện này”.

Schema:
- fact_id: UUID
- episode_id
- canonical: “Bà Ngoại từng kể về chợ quê”
- cited_from: list[chunk_id]
- confidence: float
- status: StrEnum("verified | disputed | pending")

Reviewer pass sẽ:
- query conflicting facts
- flag draft nếu có mâu thuẫn
- auto-generate fact ledger cho reviewer

Không integrate vào KB (để không làm KB dirty).
```

### 2.2. Retrieval flow (2-pass) chưa có prompt example

v3 nói “2-pass: J1+J2 trước outline, J3 per-scene” nhưng chưa có:

- Prompt system/user cho J1/J2.
- Prompt system/user cho J3 scene palette.
- Cách compiler build `KnowledgeBrief` (có degraded flag?).

**Đề xuất:** Thêm subsection “Retrieval Compiler” với 2 prompt template mẫu (outline + scene).

### 2.3. Ingest idempotency chưa rõ ràng

v3 nói “Idempotent theo content_hash” nhưng chưa nói:

- universe_id thay đổi thì sao (new season = new universe hay same universe)?
- re-transcribe model mới thì tạo version hay override?

**Đề xuất bổ sung:**

```
### 2.3 Ingest Idempotency & Versioning

- Same universe + same content_hash → no-op
- New season = new universe_id (không override)
- Re-transcribe = new version (xóa old points, ghi new)
- Quality_score filter trong query
```

### 2.4. Golden set & Metric chưa cụ thể

v3 đề cập “golden set” nhưng chưa có:

- 15 câu golden set (5 J1, 5 J2, 5 J3).
- Metric cụ thể (source-coverage@8, entity hit-rate, nDCG@4).

**Đề xuất:** Thêm bảng golden set mẫu.

### 2.5. Interface protocol chưa khớp v3

v3 nói “Gợi ý protocol mới” nhưng chưa đưa full protocol.

**Đề xuất:** Thêm full `KnowledgeStore` protocol + `KnowledgeBrief`.

---

## 3. Điểm còn hở nhỏ (P2)

- Chưa nói **alias review workflow** (người duyệt alias 5 phút/tháng).
- Chưa nói **episode summary RAPTOR** (1 tầng, phục vụ J1 tốt).
- Chưa nói **degrade khi Qdrant down** chi tiết (loose vs strict).
- Chưa nói **how to switch from Chroma to Qdrant** (interface không đổi).

---

## 4. Kết luận & khuyến nghị tiếp theo

v3 đã **từ design v2 trở nên rất rõ ràng** về universe và J1/J2/J3. Phần còn hở lớn nhất là **Fact Ledger** (phần quan trọng cho serial storytelling) và **Retrieval Compiler** (cách compiler build brief).

**Thứ tự ưu tiên:**
1. Fact Ledger (schema + interface + reviewer integration).
2. Retrieval Compiler (2 prompt example + KnowledgeBrief).
3. Golden set cụ thể (15 câu + metric).
4. Ingest versioning rõ ràng.

Nếu bạn muốn, mình có thể tiếp tục brainstorm **Fact Ledger** chi tiết (schema, interface, reviewer pass) và tạo file `docs/FACT_LEDGER_DESIGN.md` riêng. Hoặc chỉnh v3 thêm.

Bạn muốn mình làm gì tiếp theo?