# Fact Ledger — Thiết kế chi tiết (v1)

> Phụ lục của `KNOWLEDGE_BASE_DESIGN.md` (v4, mục 8). Trả lời điểm hở lớn
> nhất của review v3: schema, interface, và cách integrate vào reviewer pass.
>
> Định vị: **FactLedger = "sự kiện đã thiết lập trong truyện"** — thế giới
> thứ hai, song song với KB (thế giới nguồn/podcast). Không bao giờ trộn
> hai store này: reviewer phải phân biệt được "có thật trong corpus" vs
> "đã bịa ở tập trước".

---

## 1. Ba phản biện với schema đề xuất trong review

Trước khi vào thiết kế, chốt lại 3 chỗ tôi không theo đề xuất của review
(kèm lý do):

### 1.1. Bỏ `confidence: float` — dùng provenance thay vì pseudo-precision

Một con số "0.87 tự tin" do LLM tự gán cho fact nó vừa trích xuất không có
nghĩa thống kê gì; nó là noise trông giống metric. Thứ review thực sự muốn
biết ở đây là **"fact này đến từ đâu"** — và điều đó mô hình hóa chính xác
hơn bằng provenance rời rạc:

- `origin: cited` — truy vết được về chunk KB (`chunk_refs` có giá trị),
  tức fact được grounding.
- `origin: inferred` — LLM diễn giải từ cited facts.
- `origin: invented` — writer bịa (được phép ở loose mode).

Ba trạng thái rời rạc + source ref >> một float không ai biết cách
calibrate. Nếu sau này cần độ tin cậy số, thêm `extraction_note` tự do.

### 1.2. Bỏ `status: disputed` — conflict là thứ được TÍNH TOÁN, không được lưu

Fact trong truyện không "disputed" — nó **đã được thiết lập** tại một tập
nào đó (viết ra là canonical). Mâu thuẫn là quan hệ *giữa hai facts* phát
hiện lúc kiểm tra draft mới, nên nó là kết quả của `find_conflicts()` ở
query time, không phải trạng thái stored. Lưu "disputed" vào schema tạo
race: hai reviewer cùng đọc, ai thắng?

Thay vào đó, vòng đời fact có 2 trạng thái thật: `established` (mặc định
khi episode ship) và `superseded` (bị thay thế có chủ đích — xem retcon ở
mục 4).

### 1.3. Season KHÔNG phải universe mới

Review đề xuất "new season = new universe_id". Ngược lại: **season mới dùng
cùng universe** — bà Ngoại của season 1 phải còn được nhớ ở season 2, đó
chính là lý do existence của KB. Universe mới chỉ khi **reset canon**:
reboot, spin-off dòng thời gian riêng, hoặc đổi hoàn toàn dàn nhân vật.

Phân biệt ba khái niệm này vào config một cách tường minh:

| Khái niệm | Ý nghĩa | Hành động với KB/ledger |
|---|---|---|
| `episode` | 1 video truyện | append ledger, ingest nguồn nếu có |
| `season` | nhóm tập cùng canon | cùng universe, chỉ là metadata episode |
| `universe` | 1 vũ trụ canon | KB + ledger scoped theo nó |

`StoryConfig` thêm `season: str | None`; ledger episode path thành
`data/ledgers/<universe>/s<season>/<episode>.yaml` (season=None → `s0`).

---

## 2. Schema

### 2.1. Fact model

```python
class FactOrigin(StrEnum):
    CITED = "cited"        # ground về chunk KB
    INFERRED = "inferred"  # diễn giải từ cited facts
    INVENTED = "invented"  # writer bịa (loose mode)

class FactKind(StrEnum):
    CHARACTER = "character"   # thuộc tính nhân vật ("Bà Ngoại gánh hàng rong")
    EVENT = "event"           # sự kiện ("Lan mất chiếc lá trong mưa")
    SETTING = "setting"       # bối cảnh ("chợ quê họp từ tờ mờ đất")
    RELATION = "relation"     # quan hệ ("Lan là cháu bà Ngoại")
    ITEM = "item"             # đạo cụ liên hồi ("chiếc lá đỏ")

class Fact(BaseModel):
    fact_id: str             # "f0001" — id ổn định, không UUID (đọc được)
    kind: FactKind
    subject: str             # entity chính — KHÔNG bắt buộc tồn tại trong KB
                             # (nhân vật truyện có thể hoàn toàn bịa)
    statement: str           # một câu khẳng định đơn, thì quá khứ
    origin: FactOrigin
    chunk_refs: list[str] = []        # chỉ khi origin=cited
    episode_id: str                   # tập thiết lập fact ("ep_012")
    scene_id: str | None = None       # cảnh thiết lập
    superseded_by: str | None = None  # fact_id thay thế (retcon) — xem mục 4
    extracted_by: Literal["llm", "human", "facts_used"] = "llm"
```

Ràng buộc validation:
- `origin == CITED` ⟺ `chunk_refs` non-empty.
- `subject` normalize qua **cùng alias table** của KB (mục 5) — một bảng
  chuẩn hóa tên dùng cho cả hai thế giới, tránh "Bà Ngoại" (KB) và
  "bà ngoại" (ledger) thành hai subject.

### 2.2. Layout — YAML per episode, append-only

```
data/ledgers/<universe>/
├── s0/
│   ├── ep_001.yaml        # facts được thiết lập ở tập 1
│   ├── ep_002.yaml
│   └── ...
└── audit.log              # append-only JSONL: mọi thay đổi fact
```

Lý do YAML-per-episode thay vì một DB:

- **Human-editable + git-friendly**: sửa tay một fact sai là workflow bình
  thường của serial (đây là dữ liệu sáng tạo, không phải dữ liệu hệ thống).
- **Episode là đơn vị naturally immutable**: tập đã ship không viết lại;
  fact mới thuộc tập mới. Sửa fact cũ = retcon có audit (mục 4).
- **Quy mô nhỏ**: vài trăm facts/universe — load toàn bộ vào memory mỗi
  lần build brief, query = lọc Python thuần. Không cần index engine.

`audit.log` (JSONL, append-only): `{ts, actor: llm|reviewer|human, action:
add|supersede|edit, fact_id, before?, after?}`. Đây chính là `get_audit_log()`
mà review yêu cầu — không cần bảng riêng.

### 2.3. Ví dụ

```yaml
# data/ledgers/storyvu/s0/ep_012.yaml
episode: ep_012
facts:
  - fact_id: f0031
    kind: relation
    subject: "Lan"
    statement: "Lan là cháu ngoại của Bà Ngoại"
    origin: invented
    episode_id: ep_012
    scene_id: beat_01_scene
    extracted_by: facts_used
  - fact_id: f0032
    kind: setting
    subject: "Chợ Đồng Xuân"
    statement: "Chợ quê họp từ tờ mờ đất, khi sương chưa tan"
    origin: cited
    chunk_refs: ["ep07:0012"]
    episode_id: ep_012
    scene_id: beat_02_scene
    extracted_by: llm
```

---

## 3. Interface

```python
class FactLedger(Protocol):
    def record_episode(self, episode_id: str, facts: list[Fact]) -> None:
        """Append facts của một episode. Reject fact_id trùng."""

    def query(
        self,
        subject: str | None = None,
        kind: FactKind | None = None,
        episode_from: str | None = None,   # lọc theo thời gian truyện
    ) -> list[Fact]:
        """Trả facts còn hiệu lực (đã loại superseded), mới nhất trước."""

    def find_conflicts(self, candidate: Fact) -> list[Fact]:
        """Fact nào ĐANG hiệu lực mâu thuẫn với candidate? Rule-based
        trước (cùng subject+kind, statement khác biệt về khóa: sống/chết,
        vị trí, quan hệ), LLM-assist sau."""

    def supersede(self, fact_id: str, replacement: Fact, *, actor: str) -> None:
        """Retcon có kiểm soát — ghi audit, không xóa."""

    def get_audit_log(self, fact_id: str | None = None) -> list[AuditEntry]: ...
```

Lưu ý interface:
- **`record_episode` chỉ nhận sau khi episode FINAL** (không phải per
  scene draft) — tránh ledger ôm theo rác của các vòng iterate bị vứt.
- `find_conflicts` nhận một fact candidate — đây là API mà reviewer pass
  gọi cho từng fact trích xuất từ draft.

---

## 4. Temporality & retcon

Truyện dài sẽ cần đổi fact đã thiết lập (nhân vật chết, lộ rõ thân thế).
Không xóa — **supersede**:

```
f0007 "Bà Ngoại còn sống"  (ep_003)
   └─ superseded_by → f0041 "Bà Ngoại đã qua đời mùa đông năm ấy"  (ep_015)
```

- `query()` mặc định chỉ trả facts không bị supersede (hiệu lực hiện tại).
  Tham số `include_superseded=True` cho reviewer muốn truy vết lịch sử.
- **Số tập (episode ordering)**: so sánh `episode_id` theo thứ tự phát
  hành, không theo ngày — season 2 tập 1 > season 1 tập 10. Chuẩn hóa
  `s{n}/ep{m}` thành số thứ tự đơn điệu khi load.
- Retcon ngoài ý định (writer quên tập cũ) KHÔNG được im lặng supersede —
  đó chính là conflict mà reviewer phải flag (mục 6).

---

## 5. Dùng chung alias table với KB

`subject` của fact và entity của KB qua cùng một bảng chuẩn hóa
(`data/kb/<universe>/aliases.yaml`):
- Thuận: dossier KB (nguồn) và fact ledger (truyện) join được theo tên —
  compiler gộp hai thế giới cho writer bằng một key.
- Nghịch: một entity chỉ tồn tại trong truyện (Lan bịa hoàn toàn) không có
  entry KB — `query_entities` trả None, ledger vẫn có facts. Đây là trạng
  thái hợp lệ, không phải lỗi.
- Alias mới do ledger sinh ra (nhân vật truyện) đi vào cùng bảng với
  `status: pending`, provenance `ledger` — review alias một chỗ cho cả hai
  thế giới.

---

## 6. Reviewer pass — integration chi tiết

Đây là phần review yêu cầu rõ nhất. Ledger không phải module đứng riêng —
nó cắm vào story generation ở **3 điểm**:

```
StoryStage (tập N)
│
├─ [A] TRƯỚC OUTLINE — brief compiler pass 1
│      query(subject=mỗi CharacterSheet.name)
│      → dossier KB + ESTABLISHED facts (ledger)
│      → prompt outline nhận section [ESTABLISHED — must not contradict]
│
├─ [B] TRƯỚC MỖI SCENE — brief compiler pass 2
│      query(subject=nhân vật trong beat)
│      → scene prompt nhận [ESTABLISHED] gọn (chỉ facts của người trong cảnh)
│
└─ [C] SAU DRAFT HOÀN CHỈNH — reviewer pass (stage mới giữa story và tts)
       1. Trích xuất facts từ draft: 1 LLM call / episode
          (seed bằng facts_used của các scene — các fact này gần như
          chính xác sẵn vì writer đã đánh dấu khi viết)
       2. Cho mỗi fact extracted: find_conflicts(fact)
       3. Kết quả:
            - không conflict → record_episode() khi episode ship
            - conflict → 3 lựa chọn cho writer loop:
              (a) sửa draft (default, ≤ 2 vòng regenerate)
              (b) supersede có chủ đích (retcon thật sự — cần actor đánh dấu)
              (c) human override (reviewer người duyệt)
```

Prompt renderer của brief thêm đúng một section:

```
[ESTABLISHED — sự kiện đã thiết lập ở các tập trước, KHÔNG được mâu thuẫn]
- (ep_003) Bà Ngoại còn sống, ở cùng Lan
- (ep_007) Lan nhận được chiếc lá đỏ từ bà
[INVENTED CÁC TẬP TRƯỚC — vẫn là canon]
- (ep_003) Lan là cháu ngoại của Bà Ngoại
```

Tách [ESTABLISHED] (cited/inferred) khỏi [INVENTED] để reviewer strict-mode
phân biệt độ tin cậy khi kiểm tra — đúng tinh thần "hai thế giới không trộn",
ngay cả trong prompt.

**Chi phí**: trích xuất 1 call/episode (không per-scene, không per-fact);
`find_conflicts` rule-based trước (cùng subject+kind, so khớp câu) — chỉ
gọi LLM assist khi rule đánh dấu "khả nghi". Ledger vài trăm facts load
vào memory, so khớp là mili-giây.

---

## 7. Failure modes

| Mode | Đối sách |
|---|---|
| Trích xuất fact sai (LLM hiểu nhầm draft) | `extracted_by` đánh dấu; human sửa YAML trực tiếp + audit |
| Conflict rule báo ồn (false positive) | Threshold + LLM-assist xác nhận; reviewer config `strictness: lenient|strict` |
| Draft tốt nhưng bị vứt vì 2 vòng regenerate không xong | Conflict report ghi vào workspace (`04_story/conflicts.json`) — human quyết, không tự supersede |
| Ledger file hỏng format | Pydantic validate lúc load; fail episode, không fail universe |
| Ingest KB fail sau khi episode dùng KB | facts cited giữ chunk_refs — chunk_id ổn định theo source, không gãy khi re-ingest (content_hash đổi mới đổi points) |

---

## 8. Gate triển khai & roadmap

- **Gate**: chỉ build khi tồn tại tập 2 thực sự (có `episode ≥ 2` trong
  cùng universe). Trước đó `FactLedger` là no-op in-memory trả [] —
  interface đứng sẵn, không tốn công.
- **Bước 1**: schema + YAML store + `query(subject)` + render [ESTABLISHED]
  vào brief (điểm A, B) — writer "nhớ" ngay từ tập 2.
- **Bước 2**: reviewer pass (điểm C) + `find_conflicts` rule-based.
- **Bước 3**: LLM-assist conflicts, supersede workflow, audit viewer
  (CLI bảng, chưa cần web UI).
- **Không làm**: ledger là graph DB, auto-merge facts tương tự, confidence
  float, sync hai chiều tự động KB↔ledger.

---

## 9. TL;DR

| Câu hỏi | Chốt |
|---|---|
| Ledger là gì? | Sự kiện đã THIẾT LẬP trong truyện, per-universe, per-episode |
| Confidence? | Không — origin (cited/inferred/invented) + chunk_refs |
| Status? | established / superseded; "disputed" là kết quả find_conflicts, không lưu |
| Season? | Cùng universe. Universe mới = reset canon, không phải season |
| Storage? | YAML per episode + audit.log append-only, load vào memory |
| Ai ghi? | Reviewer pass trích xuất từ episode FINAL; human sửa được |
| Conflict? | find_conflicts(candidate) → sửa draft / retcon có audit / human override |
| Khi nào làm? | Gate: tồn tại tập 2. Trước đó no-op stand-in |
