# M2 Design — P1 Specification (đã duyệt scope từ m2_requirements)

> Trưởng phase: ARCH. Trạng thái: **bản chốt để DEV implement** (P1 exit).
> Ticket mapping: M2-D1→D5 trong `WORKPLAN_WATERFALL.md`. Mọi thay đổi sau
> tài liệu này = CR.
>
> Quy ước chung: mọi module mới nằm trong `src/storyforge/`, lazy import
> dependency nặng, mypy strict, test plan đi kèm mỗi feature. Stage metrics
> chuẩn hóa ở mục 7 (cost report) là bắt buộc với MỌI feature gọi API trả
> phí trong M2.

---

## M1. TextNormalizer (M2-D1 — DEV2)

### 1.1. Vị trí & nguyên tắc

```
StoryStage ──story.json (narration_text GỐC, không đụng)
                    │
                    ▼
             TTSStage ──► TextNormalizer.normalize() ──► synth text
                    │                                     │
                    └── subtitles dùng text GỐC           └── NarrationClip.normalized_text
                                                          (lưu lại để trace + debug)
```

- **Không mutate story artifact.** Normalize chỉ xảy ra lúc synthesize;
  `NarrationClip` thêm field `normalized_text` để audit.
- Subtitle dùng text gốc (người đọc thấy "1995" là ổn; TTS mới cần chữ).
- Thuần rule + regex, **KHÔNG LLM**. Không có network call.

### 1.2. Cấu trúc file (mới)

```
src/storyforge/textnorm/
├── __init__.py          # export normalize_text, TextNormalizer
├── numbers.py           # num2words_vi(n: int|float) -> str  (tự viết, ~80 dòng)
├── rules.py             # RULES: list[Rule] — áp dụng theo thứ tự
└── normalizer.py        # TextNormalizer + NormalizedText model
config/textnorm_loanwords.yaml   # bảng từ mượn (dữ liệu, không phải code)
tests/test_textnorm.py
```

### 1.3. Interface & models

```python
class Rule(BaseModel):
    name: str                    # id ổn định, xuất hiện trong NormalizedText.rules_applied
    pattern: str                 # regex (compiled lúc module load)
    apply: Callable[[re.Match[str]], str]

class NormalizedText(BaseModel):
    original: str
    normalized: str
    rules_applied: list[str]

class TextNormalizer:
    def __init__(self, loanwords_path: Path | None = None) -> None: ...
    def normalize(self, text: str) -> NormalizedText: ...
```

### 1.4. Bảng rules (thứ tự áp dụng — cái trước thắng)

| # | name | pattern (rút gọn) | hành vi | ví dụ |
|---|---|---|---|---|
| 1 | `loanword` | tra bảng `config/textnorm_loanwords.yaml` | replace nguyên từ, case-insensitive | "OK" → "ô kê" |
| 2 | `time_h` | `(\d{1,2})h(\d{2})?\b` | "10h30" → "mười giờ ba mươi"; thiếu phút → "mười giờ" | 10h30, 8h |
| 3 | `time_colon` | `(\d{1,2}):(\d{2})\b` (giờ 0–23) | như trên | 21:05 |
| 4 | `percent` | `(\d+(?:[.,]\d+)?)\s*%` | số → chữ + "phần trăm" | 50% |
| 5 | `currency_k` | `(\d+(?:[.,]\d+)?)\s*(k\|K\|tr\|Tr\|triệu\|tỉ\|tỷ)\b` | "200k" → "hai trăm nghìn"; "1.5tr" → "một triệu rưỡi"; "3 tỷ" → "ba tỷ" (num2words_vi + hậu tố đúng đơn vị) | 200k, 1.5tr, 3 tỷ |
| 6 | `year` | 4 chữ số `\b(1[0-9]{3}|20[0-9]{2})\b` **CHỈ khi có ngữ cảnh thời gian** — xem 1.4.1 | đọc từng chữ số: "một chín chín năm" (quy ước nói năm phổ biến của tiếng Việt) | 1995, năm 2024 |
| 7 | `roman` | `\b(?=[IVXLCDM]+\b)(...)` chỉ khi sau "chương|phần|quyển|tập" | XIV → "mười bốn" | chương XIV |
| 8 | `decimal` | `\b\d+[.,]\d+\b` (không phải năm/thời gian) | "3,14" → "ba phẩy mười bốn" | 3,14 |
| 9 | `integer` | `\b\d+\b` (còn lại) | num2words_vi đầy đủ | 25 con → "hai mươi lăm con" |
| 10 | `whitespace` | `\s+` | collapse + strip | — |

`num2words_vi`: hỗ trợ 0–999 tỷ, âm, thập phân. Giới hạn: > 999 tỷ giữ
nguyên + thêm warning vào `rules_applied` dưới tên `integer:overflow`.

#### 1.4.1. Rule 6 (Year) — context-gating (bắt buộc, theo review)

Vấn đề: nếu Rule 6 chỉ match 4 chữ số, `"2000 con vịt"` sẽ bị đọc thành
"hai không không không" thay vì "hai nghìn con vịt" — Year nuốt số đếm.
Do đó Rule 6 **chỉ kích hoạt khi đủ ngữ cảnh thời gian**:

| Điều kiện kích hoạt | Pattern lookbehind/lookahead | Ví dụ |
|---|---|---|
| Có từ thời gian trước | `(?<=\b(năm|từ|vào|tháng|ngày|đầu|cuối|giữa)\s)` | "năm 1995", "từ 2020" |
| Có "thập niên" trước | `(?<=\bthập niên\s)` + 2 chữ số | "thập niên 80" |
| Đứng độc lập đầu câu | `^\s*(1[0-9]{3}|20[0-9]{2})\b` | "1995, bà mới về" |
| Theo sau dấu ngắt | `(?<=[.!?…]\s+)` | "… đi. 2020 mẹ mất." |

**Bất kỳ số 4 chữ số nào không khớp điều kiện trên → bị rơi xuống Rule 9
(Integer) và đọc bằng `num2words_vi` đầy đủ** ("hai nghìn con vịt").

Nếu cả Rule 6 và Rule 9 cùng match cùng vị trí (rare), Rule 6 thắng vì đứng
trước — nhưng lookbehind chặn đã đảm bảo 1000–2099 chỉ thành year khi thực
sự là năm. Quan hệ thứ tự: Rule 6 → Rule 9, không bao giờ ngược.

### 1.5. Loanword table — mặc định (DEV2 được bổ sung khi viết test thấy cần)

```yaml
# config/textnorm_loanwords.yaml
ok: "ô kê"
app: "áp"
video: "vi đi ô"
online: "on láyn"     # từ khó đọc: giữ gần gốc
email: "i méo"
# QUY TẮC: KHÔNG dịch nghĩa, chỉ phiên âm gần nhất để TTS đọc không vấp.
```

### 1.6. Config

```python
class TTSSettings(BaseModel):
    normalize_text: bool = True        # SF__TTS__NORMALIZE_TEXT
```

### 1.7. Test plan

- Unit: mỗi rule ≥ 3 case đúng + 2 case không match (vd "iPhone 15" —
  "15" thành "mười lăm" là ĐÚNG, "iPhone" không đụng).
- Bộ 50 câu tích hợp: `tests/fixtures/lint_cases_vi.yaml` (mục 8).
- AC: `normalize("Vào khoảng 10h30 tối năm 1995, bà bán được 200k.")` →
  chứa "mười giờ ba mươi", "một chín chín năm", "hai trăm nghìn", không còn
  chữ số nào.

---

## 2. Pacing / TTS Lint (M2-D2 — DEV2)

### 2.1. Vị trí

Chạy **trong StoryStage, sau khi sinh mỗi scene**, trước khi ghi
story.json. Lint chạy trên `normalized_text` (gọi TextNormalizer nội bộ —
dù normalize chỉ áp dụng lúc TTS, lint phải check bản TTS sẽ đọc).

### 2.2. Checks

| rule | điều kiện | severity | message |
|---|---|---|---|
| `sentence_length` | câu > 25 từ (tách theo `[.!?…]`) | **fail** | kèm câu gốc + gợi ý cắt |
| `sentence_length_warn` | 20–25 từ | warn | — |
| `digit_leftover` | còn `[0-9]` sau normalize | **fail** | "normalizer bỏ sót — thêm rule" |
| `weird_char` | emoji/ ký tự ngoài `[[:punct:]]\w\s` tiếng Việt | **fail** | liệt kê ký tự |
| `word_repeat` | một từ lặp ≥ 3 lần liên tiếp | warn | "rất rất rất" |
| `scene_length` | narration < 40 hoặc > 350 từ | warn | lệch target_minutes |

### 2.3. Interface

```python
# src/storyforge/lint/__init__.py
class LintIssue(BaseModel):
    scene_id: str
    rule: str
    severity: Literal["warn", "fail"]
    excerpt: str            # tối đa 120 ký tự quanh vị trí lỗi
    suggestion: str | None

class LintReport(BaseModel):
    issues: list[LintIssue]
    @property def passed(self) -> bool: ...  # không có severity=fail

def lint_scene(scene: StoryScene, normalizer: TextNormalizer) -> list[LintIssue]: ...
```

Xử lý: `fail` → StoryStage **regenerate scene đó 1 lần** với issue nhét vào
prompt ("câu sau quá dài, hãy tách: …"); vẫn fail lần 2 → ghi artifact,
đánh dấu `quality_flag`, KHÔNG block pipeline (loose) / block chỉ khi
grounding=strict.

Artifact: `04_story/lint_report.json` (LintReport của cả truyện) +
`04_story/prompts_used/retry_<scene>.txt` (lưu cả prompt retry và
`lint_issue_feedback` để audit/debug — theo review).

---

## 3. Prompt versioning + Prompt-eval harness (M2-D3 — DEV2)

### 3.1. Frontmatter format

```
---
version: 2
changelog: "thêm cold-open hook"
eval_ref: "evals/story/2026-09-15_v2.json"
---
Nội dung prompt... {placeholders} như cũ
```

`load_prompt()` (providers/llm.py) trả về `(meta, template)`; mọi thay đổi
`fill_prompt` giữ nguyên. Render artifact lưu
`04_story/prompts_used/<stage>_<version>_<scene>.txt`.

### 3.2. Lệnh & luồng

```bash
storyforge eval-story --project demo [--judge-model X]
```

1. Đọc `story.json` + `prompts_used/` + (nếu có) `review.json`.
2. Với mỗi scene: gọi judge model (default `SF__LLM__REVIEWER_MODEL`) bằng
   template mới `prompts/judge_story.txt`.
3. Ghi `evals/story/<date>_v<prompt_version>.json`.

### 3.3. Judge prompt — mẫu từ editor.md của ainovel-cli (port pattern)

Judge prompt (`prompts/judge_story.txt`) áp dụng các nguyên tắc từ editor 7-dimension:

**a) Score 0-100, verdict tự suy (không để LLM tự điền pass/fail)**:
- 0–39 → fail, 40–69 → warn, 70–100 → pass
- LLM chỉ trả `score`, hệ thống suy `verdict` từ ngưỡng
- Bắt buộc trích dẫn nguyên văn (`evidence`) làm bằng chứng cho mỗi chiều

**b) Chiều aesthetic (chiều 6 hook) bắt buộc trích nguyên văn**:
- Không chấp nhận kết luận chung chung kiểu "văn phong trôi chảy"
- Phải trích tối thiểu 1 câu nguyên văn thể hiện vấn đề

**c) StyleStats injection (see section 8)**:
- Khi có `episodic_memory.style_stats`, judge phải tham chiếu:
  "câu mở đầu quá dài (> 25 từ) — vi phạm ở chương/tập trước (style_stat: 3/5 scene)"
- Không dùng làm fail tuyệt đối, chỉ dùng làm warn pattern

### 3.4. JSON parsing — chống markdown codeblock (bắt buộc, theo review)

LLM hay bọc JSON trong ```json ... ``` dù prompt có cấm. Parser bắt buộc:

```python
def extract_json(text: str) -> dict[str, Any]:
    """Lấy substring từ '{' đầu tiên đến '}' cuối cùng, strip ```json."""
    text = re.sub(r"```(?:json)?", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise StoryGenerationError("judge response missing JSON", details={"response": text[:500]})
    return json.loads(text[start:end+1])
```

Dùng chung cho mọi response trả JSON (judge, episode_summary, review_
extract) — đặt trong `providers/llm.py` (private helper), không nhân bản
logic parse ở từng module.

### 3.5. Judge output schema (LLM phải trả JSON — parse giống `_parse_beats`)

```python
class DimensionScore(BaseModel):
    dimension: Literal["grounding", "consistency", "pacing",
                       "tts_ready", "visual", "hook"]
    score: float            # 1.0–5.0, bước 0.5
    evidence: str           # trích dẫn câu làm bằng chứng

class StoryEval(BaseModel):
    prompt_version: int
    project: str
    scene_id: str           # "episode" cho chấm tổng
    scores: list[DimensionScore]
    total: float            # trung bình 6 chiều × 20 → thang 100
    judge_model: str
    created_at: datetime
```

`tts_ready` dimension: judge NHẬN kèm kết quả lint (`lint_report.json`) —
điểm này phải nhất quán với lint (fail lint → không cho 5.0).

### 3.6. Judge prompt (`prompts/judge_story.txt`) — yêu cầu bắt buộc

- Chấm từng chiều độc lập, trích evidence cho mỗi điểm.
- Cấm điểm tròn suôn (khuyến khích dùng bước 0.5, bắt buộc evidence).
- Hook: chỉ chấm 40 từ đầu scene đầu.

### 3.5. Calibration (P3)

BA chấm tay 3 episode bằng cùng rubric → so hệ số tương quan với judge;
calibrate bằng cách sửa judge prompt (không sửa điểm sau факт).

---

## 4. Hook / Cold Open (M2-D4 — DEV2)

### 4.1. Thay đổi outline prompt (`prompts/outline.txt`, bump version)

Thêm quy tắc:
- Beat đầu tiên PHẢI là `hook_00`: khoảnh khắc căng nhất/lạ nhất từ
  KNOWLEDGE hoặc premise — **không nhất thiết theo thứ tự thời gian** của
  podcast (Dramatic Re-structuring).
- Narration của hook ≤ 80 từ (~25–30 giây nói).
- Từ beat 02 trở đi kể lại theo trục chính; có thể quay lại chi tiết hook
  beat sau (payoff).

### 4.2. Contract không đổi

`StoryBeat` schema giữ nguyên — hook chỉ là `beat_id="hook_00"` + ràng buộc
nội dung prompt. KB usage không đổi. Image của hook là scene bình thường.

### 4.3. Test plan

- Chạy 3 seed premise (đã có từ m2_requirements) × prompt v cũ/mới.
- AC: eval-story chiều `hook` trung bình tăng so baseline cũ; mỗi hook
  ≤ 80 từ (lint check thêm rule `scene_length` riêng cho hook).

---

## 5. Reranker đo + EpisodeSummary + Alias CLI + Cost report (M2-D5)

### 5.1. Reranker đo (DEV1)

- Bật cờ `SF__KNOWLEDGE__USE_RERANKER=true` (đã có trong SearchQuery —
  thêm env wire).
- `storyforge eval --universe X --reranker both`: chạy golden set 2 lần
  (on/off), ghi bảng so sánh vào `tests/golden/baseline_M2_kb.md`.
- **Quyết định default** (ARCH chốt ở P3): bật làm default nếu J2 hit-rate
  +≥ 3 điểm phần trăm VÀ p95 query < 2s; ngược lại giữ off.

### 5.2. EpisodeSummary (DEV1)

- Config: `SF__KNOWLEDGE__EPISODE_SUMMARY=true` (default M2: true).
- Tại ingest, sau khi chunk: 1 LLM call (writer model) với
  `prompts/episode_summary.txt` — extract **3–5 khoảnh khắc đáng kể**
  (người, chỗ, hành động, chi tiết giác quan), KHÔNG tóm tắt chủ đề chung.
- Lưu: payload field `source_summary` trên mọi chunk của source +
  `IngestReport.summary_generated: bool`.
- Dùng ở J1: theme search trả kèm summary của source (group header).
- Prompt output format: mỗi moment 1 dòng `MOMENT: <mô tả>` để parse.
- AC: golden J1 không tệ đi (summary không thay thứ hạng chunk, chỉ thêm
  context cho group).

### 5.3. Alias review CLI (DEV1)

```
storyforge aliases --universe X --pending
    # bảng: canonical | alias mới | 3 passage mẫu | mentions
storyforge aliases --universe X --confirm <canonical> --alias "<biến thể>"
storyforge aliases --universe X --reject <alias>          # blacklist
storyforge aliases --universe X --rename <old> --to "<new canonical>"
```

- Đọc/ghi `data/kb/<universe>/aliases.yaml`; mọi lệnh ghi append
  `audit.log` (actor=human).
- `--pending` lấy từ `entities_pending` của IngestReport gần nhất + quét
  payload entities chưa có canonical.

### 5.4. Cost report (DEV1)

**Chuẩn hóa stage metrics (bắt buộc mọi API call trả phí):**

| key | ý nghĩa |
|---|---|
| `cost_usd` | tổng tiền stage |
| `llm_input_tokens` / `llm_output_tokens` | cho call LLM |
| `api_calls` | số call |
| `images_generated` / `images_regenerated` | stage imaging |
| `tts_chars` | stage tts |

- Bảng giá ở `config/prices.yaml` (dữ liệu, không hardcode): per-model
  in/out giá, per-image theo model, per-char TTS theo engine.
- `storyforge cost --project X [--tier standard|premium]`: đọc manifest →
  tính theo prices.yaml → bảng console (stage | calls | tokens | $) + tổng.
- Tier = preset của (tts engine + image model) định nghĩa trong
  prices.yaml, không phải logic code.

---

## 6. A/B visual style tooling (DEV2)

```
storyforge ab-style --config config/story.yaml --scenes 1,4,9 \
    --styles watercolor,anime,cinematic
```

- Chạy imaging stage cho 3 scene đại diện (đầu/giữa/cuối) × 3 style
  (thay `art_style` trong StoryConfig copy — không đụng config gốc).
- Output: `06_images/ab/<style>_<scene>.png` + bảng tổng.
- BA duyệt bằng mắt ở P3; style thắng cập nhật
  `config/story_config.example.yaml`.

---

## 7. Lint dataset 50 câu (P1 deliverable cho M2-Q5 — ARCH + DEV2)

Format: `tests/fixtures/lint_cases_vi.yaml`

```yaml
- id: t01
  category: time
  input: "10h30 tối bà mới về"
  expected_contains: ["mười giờ ba mươi"]
- id: c01
  category: currency
  input: "bán được 200k"
  expected_contains: ["hai trăm nghìn"]
```

Phân bổ: time 8, currency 6, year 6, decimal 5, roman 3, percent 4,
loanword 6, long-sentence 6 (cho pacing lint — expect warn/fail), digit
khó 3, mixed 3. **Bộ đầy đủ 50 câu đính kèm trong PR của M2-W1** — ARCH
review chốt trước merge; danh sách trên là phân bổ bắt buộc.

---

## 8. StyleStats — deterministic style statistics (port từ ainovel-cli)

### 8.1. Mục đích

Tính thống kê phong cách bằng CODE (không LLM) để inject vào prompt của
writer + judge, giúp:
- Writer tự tránh lặp pattern (cấu trúc câu, mở đầu, kết thúc)
- Judge chấm chiều TTS-ready và visual có số liệu định lượng
- Phát hiện sớm lỗi phong cách xuyên suốt (cross-episode)

### 8.2. Tính năng (phiên bản đầu)

```python
# src/storyforge/stylestat/tracker.py
class StyleStatsTracker:
    def observe(self, scene: StoryScene) -> None: ...
    def compute(self) -> StyleStats: ...
    def inject(self, brief: KnowledgeBrief) -> None: ...
```

Các thống kê ban đầu (deterministic, regex/code thuần, không LLM):

| Stat | Phương pháp | Inject vào |
|---|---|---|
| `sentence_length_distribution` | đếm từ/câu (split `[.!?]`) | writer prompt + judge rubric |
| `scene_opener_pattern` | 5 từ đầu scene → pattern (4 loại: dialogue/narrative/description/action) | judge chiều hook |
| `ending_type` | 3 câu cuối → loại (cliffhanger/resolution/question) | writer prompt |
| `paragraph_length_avg` | tokens/paragraph | writer prompt (TTS-ready) |
| `repeated_phrases` | bigram tần suất cao > 3 lần/episode | judge chiều aesthetic |
| `title_prefix_consistency` | prefix pattern qua các scene (nếu có) | judge |

### 8.3. Tích hợp

- **StoryStage**: sau mỗi scene, `stylestat.observe(scene)`. Trước scene N,
  `stylestat.compute()` → inject vào `working_memory.style_stats` trong
  writer prompt.
- **Judge prompt**: khi có StyleStats → judge tham chiếu (mục 3.3c).
- **Lưu trữ**: per-episode trong `04_story/style_stats.json`; cross-episode
  accumulate trong `data/kb/<universe>/style_stats/` (append-only, tối đa
  10 episode gần nhất).
- **Zero LLM cost**: chỉ regex + counter.

### 8.4. Entry point

Mặc định tắt (bật qua `SF__STORY__STYLE_STATS=true`). Khi bật, chạy sau
mỗi scene generation trong StoryStage, trước khi ghi artifact.

## 9. Thứ tự implement khuyến nghị (P2)

1. DEV2: M2-W1 TextNormalizer + dataset (song song được ngay — module độc
   lập theo quy tắc gối đầu).
2. DEV1: M2-V4 cost report (song song được ngay) → V1 reranker → V3 alias
   CLI → V2 EpisodeSummary.
3. Sau P1 exit 100%: DEV2 W2 lint → W3 versioning → W4 eval harness →
   W5 hook → W6 ab-style.
