# M3 Design — P1 Specification

> Trưởng phase: ARCH. Trạng thái: **bản chốt để DEV implement**.
> Ticket mapping: M3-D1→D6 trong `WORKPLAN_WATERFALL.md`.
> Điều kiện vào P0 M3 đã thỏa (x workplan mục 3.1); mọi thay đổi sau tài
> liệu này = CR qua ARCH.

---

## 0. CONTRACT FREEZE (theo review — chốt trước, mọi ticket dưới đây code theo)

Các model sau đóng băng từ ngày P1 exit. DEV1 (M3-V1/V2) và DEV2
(M3-W1/W2) code theo đúng contract. **Thêm/bớt field = CR**, không sửa trực
tiếp. Nguồn: `FACT_LEDGER_DESIGN.md` (schema đã duyệt) + bổ sung
`ConflictReport` và `Beat.intent` cho reviewer pass.

```python
# src/storyforge/kb/types.py  (append — không sửa model M1)

class FactOrigin(StrEnum):
    CITED = "cited"
    INFERRED = "inferred"
    INVENTED = "invented"

class FactKind(StrEnum):
    CHARACTER = "character"
    EVENT = "event"
    SETTING = "setting"
    RELATION = "relation"
    ITEM = "item"

class Fact(BaseModel):
    fact_id: str                      # "f0001" — duy nhất trong universe
    kind: FactKind
    subject: str                      # normalized qua alias table (chung với KB)
    statement: str                    # một câu khẳng định đơn
    origin: FactOrigin
    chunk_refs: list[str] = []        # bắt buộc non-empty khi origin=CITED
    episode_id: str                   # "ep_012" (đặt khi ship)
    scene_id: str | None = None
    superseded_by: str | None = None
    extracted_by: Literal["llm", "human", "facts_used"] = "llm"

class ConflictVerdict(StrEnum):
    NO_CONFLICT = "no_conflict"
    CONFLICT = "conflict"             # mâu thuẫn KHÔNG chủ đích → sửa draft
    TWIST_OK = "twist_ok"             # beat có intent=twist → supersede hợp lệ

class ConflictReport(BaseModel):
    candidate: Fact
    conflicts: list[Fact]             # facts hiện hành bị đụng độ (rỗng nếu none)
    verdict: ConflictVerdict
    reason: str

# core/types.py — sửa StoryBeat (thay field mới):
class StoryBeat(BaseModel):
    ...
    intent: Literal["normal", "twist"] = "normal"   # outline prompt đánh dấu
```

**Render contract prompt (DEV2 tuân thủ khi viết renderer):**

```
[ESTABLISHED — sự kiện đã thiết lập, KHÔNG mâu thuẫn]
- (ep_003) Bà Ngoại còn sống, ở cùng Lan          ← origin=cited|inferred
[INVENTED CÁC TẬP TRƯỚC — vẫn là canon của truyện]
- (ep_003) Lan là cháu ngoại của Bà Ngoại          ← origin=invented
```

---

## 1. FactLedger store (M3-V1 — DEV1)

### 1.1. File layout

```
src/storyforge/ledger/
├── __init__.py
├── models.py        # re-export Fact, ConflictReport từ kb/types.py
├── store.py         # YamlLedgerStore (duy nhất implementation M3)
└── loader.py        # load toàn bộ universe vào memory + episode ordering
data/ledgers/<universe>/
├── s0/ep_001.yaml   # facts per episode (schema FACT_LEDGER_DESIGN.md)
└── audit.log        # JSONL append-only
```

### 1.2. Interface (đúng FACT_LEDGER_DESIGN.md mục 3)

```python
class FactLedger(Protocol):
    def record_episode(self, episode_id: str, facts: list[Fact]) -> None: ...
    def query(self, subject: str | None = None, kind: FactKind | None = None,
              include_superseded: bool = False) -> list[Fact]: ...
    def find_conflicts(self, candidate: Fact) -> ConflictReport: ...
    def supersede(self, fact_id: str, replacement: Fact, *, actor: str) -> None: ...
    def get_audit_log(self, fact_id: str | None = None) -> list[AuditEntry]: ...

def build_ledger(universe_dir: Path) -> FactLedger: ...
```

### 1.3. find_conflicts — rule-based (M3, chưa LLM-assist)

Cho candidate, lấy `query(subject=candidate.subject, kind=candidate.kind)`:
1. Trùng statement (normalize lowercase/khoảng trắng) → NO_CONFLICT
   (duplicate, bỏ qua khi record).
2. Khác statement + cùng "slot ngữ nghĩa" (bảng slot theo kind: CHARACTER
   = {tên gọi, sống/chết, nghề, chỗ ở}; SETTING = {tên, vị trí, thời điểm};
   EVENT = {thời điểm, kết quả}; RELATION = {quan hệ A-B}; ITEM = {chủ sở
   hữu, trạng thái}) → CONFLICT tạm. Slot match bằng keyword pattern đơn
   (vd sống/chết: ["còn sống","qua đời","mất"]).
3. **Negation detection (bắt buộc, theo review):** trước khi so slot, quét
   phủ định `("không","chưa","chẳng","đã không","không còn","vẫn chưa")`
   trong khoảng 3 từ trước keyword → đảo cực (ALIVE ↔ DEAD). Nếu không làm:
   `"Bà Ngoại không còn sống"` chứa substring "còn sống" → bị đánh nhầm
   NO_CONFLICT. Kết quả: 2 fact cùng slot khác cực → CONFLICT.
4. Nếu pattern không rơi slot nào → NO_CONFLICT (fact bổ sung, không đụng).
5. Verdict cuối: caller (reviewer stage) override CONFLICT → TWIST_OK khi
   beat `intent=twist`.

#### 1.3.1. LLM Arbiter escalation (port từ ainovel-cli — không bắt buộc M3, opt-in)

Khi rule-based không quyết được (cùng slot, khác statement, negation không
rõ ràng, pattern không match slot nào nhưng từ khóa gợi ý mâu thuẫn):

1. Gọi writer model (model rẻ) 1 lần/episode với prompt:
   `prompts/arbiter_conflict.txt` — input: `[candidate fact] + [existing facts] + [beat context]`
   → output: `{verdict: "conflict"|"twist_ok"|"no_conflict", reason: str}`
2. Kết quả lưu vào `meta/conflict_verdicts.jsonl` (append-only, replayable):
   `{episode_id, candidate_fact, existing_facts, verdict, arbiter_prompt, created_at}`
3. Nếu arbitrator lỗi (exception/timeout) → fallback về rule-based verdict
   (conservative: CONFLICT).
4. **Điều kiện bật**: `SF__LEDGER__ARBITER_ENABLED=true` (mặc định false ở M3).

`meta/conflict_verdicts.jsonl` tương đương decisions.jsonl của ainovel-cli
— cho phép offline replay, regression test, và calibrate arbiter prompt.

`query()` mặc định loại superseded; ordering: episode mới trước.

### 1.4. Validation & failure

- Load: pydantic validate từng file; 1 file hỏng → fail episode đó (raise
  LedgerCorruptedError), KHÔNG fail cả universe loader (bỏ qua + warning).
- `record_episode`: reject fact_id trùng; atomic write (tmp + rename).

### 1.5. Test plan

- Unit: record→query→supersede→query; conflict từng kind; trùng statement;
  ordering episode (s2 ep1 > s1 ep10).
- Conformance: chạy lại bằng fixture 2 universe giả (min 3 episode).

---

## 2. Reviewer pass wiring (M3-V2 — DEV1) + prompt (M3-W2 — DEV2)

### 2.1. Stage mới: `stages/review.py` (giữa story và tts)

```
story.json ──► [extract facts] ──► [find_conflicts mỗi fact] ──► review.json
                    1 LLM call              rule-based (mục 1.3)
```

- **Extract** (`prompts/review_extract.txt`, DEV2): input = story scenes +
  `facts_used` của mỗi scene (seed gần đúng) → output fact lines
  `FACT: <kind> | <subject> | <statement> | <origin guess>`. Writer đánh
  dấu facts_used → các fact này `extracted_by="facts_used"`, tin hơn.
- **Verdict**: mỗi fact → ConflictReport. Nếu fact.relate tới scene có
  beat.intent="twist" và có conflict → TWIST_OK.
- **Hành xử theo verdict:**
  - NO_CONFLICT → ghi vào `facts_to_record` (record_episode SAU khi episode
    final — gọi từ P4 batch finish, không phải trong stage).
  - CONFLICT → strict: regenerate scene (≤ 2 vòng, conflict report nhét vào
    prompt); loose: cảnh báo + ghi `needs_review`, pipeline tiếp tục.
  - TWIST_OK → đánh dấu supersede khi record (cùng flow).
- Artifact: `04_story/review.json` = list[ConflictReport] + summary
  (n_conflict, n_twist, n_new_facts).
- Chi phí: 1 LLM call/episode (extract); find_conflicts thuần memory.

### 2.2. Test plan P3 (2 test bẫy — theo review)

1. **Trap test (recall):** sửa tay story.json chèn fact mâu thuẫn
   (Bà Ngoại "qua đời" vs ledger "còn sống") → reviewer PHẢI ra CONFLICT.
2. **Twist false-positive (precision):** same fact nhưng beat có
   `intent=twist` → verdict PHẢI là TWIST_OK, KHÔNG CONFLICT.
   Cả 2 pass mới chấp nhận reviewer.

### 2.3. Scene Artifact Guard — per-scene CheckpointDeltaGuard (port từ ainovel-cli)

ainovel-cli dùng CheckpointDeltaGuard: worker chỉ được `end_turn` khi có
artifact MỚI trên disk so với baseline — writer trả output trong chat là
không tính. Port cho StoryForge (StoryStage + ReviewStage):

- **Baseline**: lúc bắt đầu stage, đọc `scene` mới nhất đã có trong
  `04_story/` (nếu resume) → ghi baseline `{scene_id, digest}`.
- **Guard**: trước khi kết thúc mỗi scene, kiểm tra `04_story/story.json`
  đã có scene mới với `scene_id` tăng hay chưa. Nếu writer chỉ trả text
  trong chat mà không lưu qua store → reject + ném `GuardError` → StoryStage
  retry 1 lần với prompt feedback, rồi fail.
- **Digest**: sha256 của `narration_text + image_prompt` — phát hiện trùng
  lặp (writer ghi cùng nội dung 2 lần).
- **Liên kết reviewer**: reviewer cũng được guard — `save_review` phải tạo
  artifact `review.json` mới, không chỉ trả verdict trong chat.

Giống như âm thanh `check_consistency` trước `commit_chapter` trong
ainovel-cli, guard đặt tại biên stage: đảm bảo **mọi fact có nguồn trên
disk**, không bao giờ chỉ tồn tại trong context LLM.

---

## 3. BriefCompiler hòa nhập ledger (M3-W1 — DEV2)

- `build()` / `update()`: thêm nguồn dữ liệu ledger
  (`query(subject=...)` cho từng character + địa điểm trong beat).
- Render [ESTABLISHED] / [INVENTED CÁC TẬP TRƯỚC] đúng contract mục 0.
- Budget: [ESTABLISHED] ưu tiên cao hơn [THEME], ngang [DOSSIERS] facts;
  [INVENTED] capped 6 dòng.
- Ledger trống (chưa có tập nào) → section bỏ qua, không render header rỗng.
- Gate: compiler nhận ledger qua constructor (inject từ stage); KHÔNG tự
  đọc file (giữ test được bằng fake).

---

## 4. Batch runner (M3-V3 — DEV1)

### 4.1. Queue structure (không queue server)

```
data/queue/
├── demo_ep013.yaml     # job spec
├── demo_ep014.yaml
├── .processing/demo_ep013.yaml   # đang chạy (rename)
├── .failed/demo_ep013.yaml       # dead letter + .error.txt kèm
└── .done/demo_ep013.yaml
```

Job spec = đúng tham số `storyforge run` (project, source_config path,
universe, urls/local_files, tier).

### 4.2. Worker

```
storyforge worker [--loop] [--interval 30]
```

- Mỗi lần tick: rename job trẻ nhất sang `.processing/` (rename = lock,
  nguyên tử trên cùng volume) → chạy pipeline → `.done/` hoặc `.failed/`
  (kèm .error.txt từ StageFailedError).
- Retry: stage-level đã có `core/retry` cho transient; job fail = KHÔNG
  tự retry (người xem error.txt rồi quyết) — M3 giữ chủ đích thận trọng.
- `--loop`: ngủ interval giữa job; SIGTERM dừng sau job hiện tại.
- Sau job `.done/`: gọi disk cleanup (mục 5) policy mặc định.
- Concurrency: 1 worker/máy (GPU bound); chống 2 worker bằng probe
  `.processing/` non-empty + PID file.
- **Stale lock recovery (bắt buộc, theo review):** worker crash (OOM/
  SIGKILL/đứt nguồn) để file `.processing/` mãi — khi worker khởi động
  lại, thấy `.processing/` non-empty sẽ bị deadlock. Cơ chế: file lock
  chứa `pid: <PID>`. Lúc khởi động, worker kiểm tra mỗi file
  `.processing/`:
  1. Nếu PID không tồn tại trên OS (`os.kill(pid, 0)` raise) → lock stale
     → chuyển job sang `.failed/` + log `"worker_crashed_stale_lock"`.
  2. Nếu PID còn sống nhưng file tuổi đời > 2h (stale) → cũng chuyển sang
     `.failed/` (worker treo mà không crash).
  3. Nếu PID = chính worker này (resume nhanh) → giữ, tiếp tục.

### 4.3. Test plan

Unit: state transitions rename; integration: 3 job tuần tự qua pipeline
giả (stage no-op), 1 job fail → dead letter đúng chỗ.

---

## 5. Disk lifecycle (M3-V4 — DEV1)

### 5.1. Policy (mặc định)

| Thành phần | Hành động |
|---|---|
| `01_download/` | GIỮ nếu source=local_file (không lấy lại được); xóa được nếu youtube (re-download được) |
| `02_transcripts/`, `04_story/`, `07_video/final.mp4`, `manifest.json` | GIỮ |
| `05_tts/`, `06_images/` (trừ canonical character refs — mục 7) | XÓA sau khi final render xong + `--keep-final` |
| `logs/seg_*.mp4`, `logs/concat.txt`, ffmpeg logs | XÓA (giữ `subtitles.srt`) |

### 5.2. Lệnh

```
storyforge clean --project X [--keep-final] [--dry-run] [--older-than 7d]
```

`--dry-run` in danh sách + tổng MB sẽ giải phóng. Báo cáo freed bytes vào
log. Batch runner gọi `clean(keep_final=True)` sau mỗi job thành công.

---

## 6. Hardware accel encode (M3-V5 — DEV1)

### 6.1. Config + detection

```python
class VideoSettings(BaseModel):
    encoder: Literal["auto", "libx264", "h264_nvenc", "h264_qsv"] = "auto"
```

- `auto`: probe `ffmpeg -hide_banner -encoders` một lần (cache kết quả vào
  `.video_encoder_cache.json` trong workspace root); ưu tiên nvenc > qsv >
  libx264.
- Map tham số: libx264 dùng `-crf N -preset P`; nvenc dùng `-cq N -preset
  p5`; qsv dùng `-global_quality N`. Bảng chuyển đổi đặt trong
  `providers/video_encoders.py` (dữ liệu, không rải IF).

### 6.2. Test plan

- Benchmark P3: render 1 video 10 phút × 3 encoder (có GPU), ghi bảng thời
  gian + kích thước file vào `baseline_M3` notes. AC: nvenc (nếu có) ≤ 1/3
  thời gian libx264, chất lượng nhìn bằng mắt tương đương (BA duyệt 1 frame giữa).

---

## 7. Reference image (M3-W4 — DEV2 protocol, DEV1 không đụng)

### 7.1. Canonical character refs

```
data/universe/<universe>/characters/<slug>.png    # slug = slugify(tên)
```

- Chọn: `storyforge character-ref set "Bà Ngoại" --image path` (người chọn
  1 lần) HOẶC tự lấy từ ảnh scene đầu tiên sinh ra (`--from-scene`) — luôn
  qua lệnh tường minh, không tự gán ngầm.
- Workflow tạo ref: `storyforge character-ref generate --config story.yaml
  --character "Bà Ngoại"` — 1 call ảnh "character sheet" (front, neutral
  background, đúng appearance) để người duyệt.

### 7.2. Protocol extension

```python
class ImageGenerator(Protocol):
    def generate_from_prompt(self, prompt: str, out_path: str,
                             reference_image: Path | None = None) -> Illustration: ...
```

- ImagingStage resolve ref theo character xuất hiện trong beat (đa nhân
  vật: ref của nhân vật chính — character đầu trong beat.characters).
- Provider fal: model id cấu hình (`SF__IMAGING__FAL_REF_MODEL`,
  default `fal-ai/flux-pro/kontext`); không có ref → model thường.
- Metric: `images_generated`, `images_regenerated` (regen = vẽ lại scene do
  người/eval yêu cầu — đếm qua flag `--force-scene`).

---

## 8. TTS premium + license (M3-D6 — DEV2)

### 8.1. Voice resolution

```
scene có đúng 1 character → CharacterSheet.tts_voice
        else → config default (SF__TTS__ELEVENLABS_VOICE_ID)
        engine=edge → SF__TTS__EDGE_VOICE
```

### 8.2. Audio cache

- Key: `sha256(engine + model + voice + normalized_text)` → file
  `data/cache/tts/<hash>.mp3`.
- Hit → không call API, NarrationClip.char_count vẫn tính, `api_calls`
  metric = 0 (cache hit track thêm `tts_cache_hits`).
- Xóa cache bằng lệnh `storyforge clean --tts-cache` (không tự expire M3).

### 8.3. License field (DEV1 — đụng kb payload)

- `kb_sources.license: Literal["cc0","cc_by","owned","permission","unknown"] = "unknown"`.
- Ingest nhận `--license` (CLI + job spec); default unknown.
- Query filter: `SF__KNOWLEDGE__ALLOWED_LICENSES` (default: tất cả; đặt
  chặt nếu xuất bản thương mại). Unknown luôn phát warning trong IngestReport.

---

## 9. Music bed + Observability (M3-W5/W6 — DEV2)

### 9.1. Music bed

- Kho CC0 cố định trong repo: `assets/music_cc0/<mood>.mp3` (BA chuẩn bị
  file + bằng chứng license ở P0; DEV2 chỉ wiring).
- StoryConfig thêm `music_mood: str | None` → VideoStage mix nhạc.
- **Filter complex chuẩn (theo review):** giọng TTS thường là Mono,
  24/44.1kHz; nhạc CC0 thường Stereo 48kHz — dùng `amix` trần sẽ méo/
  lệch pitch. Áp filtergraph:
  ```
  [1:a]aloop=loop=-1:size=2e+09,afade=t=in:st=0:d=2,volume=0.15[bgm];
  [0:a][bgm]amix=inputs=2:duration=first:dropout_transition=2[aout]
  ```
  `aloop` lặp nhạc dài bao nhiêu cũng được; `duration=first` cắt theo
  giọng đọc (narration = input 0); `dropout_transition` fade mượt khi
  nhạc kết thúc.
- Không có mood / không có file → bỏ qua im lặng (không fail).

### 9.2. Observability

- structlog JSON đã có → thêm sink file `data/logs/runs/<YYYY-MM-DD>.jsonl`
  (configure ở cli, size-rotate 10 file × 10MB).
- Alert: sau mỗi pipeline run, nếu cùng stage fail ≥ 2 lần liên tiếp (đọc
  manifest các run gần nhất trong workspace) → append dòng vào
  `data/alerts.md` (`[date] project X stage Y fail ×N`). Đủ cho M3; webhook
  là M4 nếu có nhu cầu thật.

---

## 10. Thứ tự implement khuyến nghị (P2 — 14 ngày)

1. **Ngày 1–2:** contract freeze phần 0 merge trước tiên (DEV1+DEV2 cùng
   review 30 phút). Sau đó: DEV1 V1 ledger store; DEV2 W3 TTS premium/cache.
2. **Ngày 3–6:** DEV1 V2 reviewer wiring; DEV2 W1 compiler + W2 reviewer
   prompt (test bẫy 2 chiều chạy liên tục trên branch).
3. **Ngày 7–9:** DEV1 V3 batch runner; DEV2 W4 reference image.
4. **Ngày 10–12:** DEV1 V4 clean + V5 nvenc; DEV2 W5 music + W6 observ.
5. **Ngày 13–14:** V6 license + V7/W7 docs/ops tests; integration cuối.
