# M4 Design — P1 Specification (phần còn thiếu)

> Trưởng phase: ARCH. Trạng thái: **bản chốt để DEV implement các feature
> chưa có code** (A4/A5/A6/B5/B6). Các feature đã code (A1/A2/A3/B1/B2/B3/B4)
> chỉ liệt kê trạng thái + điểm cần hoàn thiện, không viết lại spec.
> Nguồn: `m4_requirements.md` (P0) + khảo sát codebase thực tế.
> Mọi thay đổi sau tài liệu này = CR.

---

## 0. Status matrix — feature M4 so với codebase hiện tại

| Feature | File code | Trạng thái | Còn thiếu / cần làm |
|---|---|---|---|
| A1 A/B Hook | `m4tools.py`, CLI `hook_ab_cmd` | **Đã code** | Khớp AC1–AC4? cần test; xác nhận `choice.json` + config `hook` |
| A2 Recap | `recap.py` | **Đã code** | Wiring vào VideoStage (prepend scene 0) + test |
| A3 Thumbnail | `m4tools.py`, CLI `thumbnail` | **Đã code** | Test + config `thumbnail_scene` |
| A4 YouTube Upload | — | **Chưa có** | Spec mục 1 |
| A5 Music Manager | `types.py` (field), `video.py` (mix) | **Một phần** | CLI `music` + kho CC0 + resolve — Spec mục 2 |
| A6 Multi-Worker | — | **Chưa có** | Spec mục 3 |
| B1 Stylestat | `stylestat.py` + `StorySettings` + story.py | **Đã code** | Cross-episode accumulate (Dev1 ops); test |
| B2 Editor Rubric | `eval_story.py` | **Đã code** | Verify AC2 (retry chấm lại) — nhỏ |
| B3 Scene Guard | `guard.py` | **Đã code** | Wiring StoryStage/ReviewStage + test |
| B4 Arbiter | `ledger/arbiter.py`, `LedgerSettings` | **Đã code** | Verify `conflict_verdicts.jsonl` + test |
| B5 Universe Bootstrap | kb/ (bootstrap report trong alias.py) | **Một phần** | CLI `universe bootstrap` + prompt tổng hợp — Spec mục 4 |
| B6 Dashboard read-only | — | **Chưa có** | Stretch — Spec mục 5 |

**Kết luận:** cần implement mới 4 feature (A4, A6, B5, B6) + hoàn thiện A5
(1 feature một nửa) + 4 điểm hoàn thiện nhỏ (A1/A2/A3/B2 test/wiring). Phần
"thiết kế lần lượt" dưới đây phủ kín phần này.

---

## 1. A4 — YouTube Upload (draft mode) — DEV1

### 1.1. Vị trí & nguyên tắc

```
VideoStage (final.mp4 + thumbnail)
        │
        ▼
storyforge publish --project X [--draft|--publish]
        │
        ├─ [metadata builder]  title/description/tags từ story config
        ├─ [OAuth2 client]     token từ data/secrets/youtube_token.json
        ├─ [resumable upload]  YouTube Data API v3
        └─ [publish receipt]   07_video/publish.json
```

- **"Draft mode" = `privacyStatus: private`** (YouTube không có khái niệm
  "draft" — private là gần nhất). `--publish` = `public`, bắt buộc cờ tường
  minh (AC4).
- Credentials qua env + token file, KHÔNG trong code/config commit (AC2).
- Upload là **resumable** (chunk 8MB) — fail giữa chừng tiếp tục được.

### 1.2. File & interface (mới)

```
src/storyforge/publish/
├── __init__.py
├── youtube.py        # OAuth + upload logic (httpx, không SDK nặng)
├── metadata.py       # title/description/tags builder (thuần, test dễ)
└── receipt.py        # PublishReceipt model + đọc/ghi 07_video/publish.json
config/publish_metadata.yaml   # template: "%{title} | %{episode}" ...
tests/test_publish_metadata.py
tests/test_youtube_upload.py   # mock httpx transport (không gọi API thật)
```

```python
# metadata.py
class PublishMetadata(BaseModel):
    title: str
    description: str
    tags: list[str]
    category_id: str = "22"          # Entertainment
    privacy: Literal["private", "unlisted", "public"] = "private"
    make_for_kids: bool = False

def build_metadata(config: StoryConfig, episode: int, total: int,
                   template: PublishTemplate) -> PublishMetadata: ...

# youtube.py
class YouTubeUploader:
    def __init__(self, token_path: Path, client_id: str, client_secret: SecretStr,
                 refresh_token: SecretStr, http: httpx.Client | None = None) -> None: ...
    def upload(self, video_path: Path, thumbnail_path: Path | None,
               meta: PublishMetadata) -> str:   # trả video_id

# receipt.py
class PublishReceipt(BaseModel):
    project: str
    video_id: str
    privacy: str
    uploaded_at: datetime
    file_hash: str          # sha256 final.mp4 (chống upload trùng)
    url: str
```

### 1.3. Config mới

```python
class PublishSettings(BaseModel):
    youtube_client_id: str = ""
    youtube_client_secret: SecretStr = SecretStr("")
    youtube_refresh_token: SecretStr = SecretStr("")   # pre-refreshed OAuth
    metadata_template: str = "%{title}"                # hay "%{title} — Tập %{episode}"
    upload_enabled: bool = False                       # SF__PUBLISH__UPLOAD_ENABLED
    token_path: Path = Path("data/secrets/youtube_token.json")
```

OAuth flow: lần đầu `storyforge publish --setup-oauth` mở URL, nhận
authorization code, lưu refresh token vào `data/secrets/` (gitignored).
Access token tự refresh (httpx) trước mỗi upload.

### 1.4. Idempotency (AC3)

- Đọc `07_video/publish.json`: nếu `file_hash` == sha256(final.mp4) và
  `privacy` đạt yêu cầu → no-op (in "đã upload").
- Upload fail → job `failed` + error rõ; retry thủ công (chạy lại lệnh —
  resumable session + receipt hash ngăn trùng).

### 1.5. Test plan

- Unit (metadata): title template thay thế, tags từ genre + characters,
  privacy default private, truncate title > 100 chars.
- Unit (upload, mock httpx): happy path trả video_id; 401 → refresh rồi
  retry 1; quota exceeded (403) → fail nhanh không retry; chunk resume.
- Integration (marker `integration`, cần token thật): upload private → xóa.

---

## 2. A5 — Music/SFX Library Manager — DEV1 (+BA kho nhạc)

### 2.1. Kho & license

```
assets/music_cc0/
├── calm.mp3 / tense.mp3 / nostalgic.mp3 / joyful.mp3 / dark.mp3   # BA thêm
└── LICENSES.md     # mỗi file: nguồn, giấy phép CC0, URL tải
```

`config/music_moods.yaml`:
```yaml
moods:
  calm: { file: assets/music_cc0/calm.mp3, volume: 0.15, note: "..." }
  ...
```

### 2.2. CLI (mới — AC4)

```
storyforge music --list              # in mood | file | license | duration
storyforge music --add <file> --mood <mood> --license <url>   # copy vào kho + ghi LICENSES
```

`--add` tự `ffprobe` duration, validate file tồn tại, ghi nguồn license
(bắt buộc — không file không license thì reject).

### 2.3. Wiring vào VideoStage

- `StoryConfig.music_mood: str | None` (đã có trong types).
- `MusicResolver(mood) -> Path | None`: lookup `config/music_moods.yaml` →
  file; không có mood/file → trả None (video không nhạc, không fail).
- Mix: tái dùng filtergraph M3 (aloop + afade + amix duration=first) —
  không thay đổi giọng. VideoStage nhận `music_path` qua constructor.

### 2.4. Test plan

- Unit: resolver (mood có/không/thiếu file), `--list` format.
- Integration: render 1 clip 5s có nhạc → duration = narration, volume 0.15
  (ffprobe loudness check nhẹ — không cần đo chính xác).

---

## 3. A6 — Multi-Worker (queue phân tán) — DEV1

### 3.1. Nguyên tắc (kế thừa batch runner M3)

Batch runner M3 đã có `data/queue/` + rename-lock trên 1 máy. A6 mở rộng
sang **nhiều worker/máy** trên shared volume (S3 để M5). Thay rename-lock
bằng **lease TTL + heartbeat** (AC1) — rename vẫn dùng để "lấy" job, nhưng
job bị mất lease khi worker chết sẽ được nhận lại.

### 3.2. Lease protocol

```
data/queue/
├── <job>.yaml            # job spec (+ owner, scheduled_at — AC3)
├── .leases/<job>.lease   # {"worker_id","pid","expires_at_epoch"}
├── .processing/<job>.yaml
├── .failed/<job>.yaml  + .error.txt
└── .done/<job>.yaml
```

- **Claim**: worker (a) rename job → `.processing/` (nguyên tử, cùng volume)
  → (b) ghi lease `.leases/<job>.lease` với `expires_at = now + 30s`.
  Nếu rename thất bại (đã có) → không claim.
- **Heartbeat**: mỗi 10s, worker cập nhật `expires_at` của lease job đang
  chạy (AC1).
- **Recovery**: worker khác (hoặc chính nó lúc restart) thấy
  `.processing/<job>` mà lease `expires_at` đã qua → coi như chết, claim
  lại (xóa lease cũ, rename giữ nguyên trong `.processing/` — không về
  queue để tránh chạy trùng lệnh; bản thân job idempotent ở stage level).
- **PID check** (kế thừa M3 stale-lock): lease còn hạn nhưng PID không tồn
  tại trên OS → xem như hết hạn ngay (Windows: `os.kill(pid,0)` không
  dùng được — thay bằng `psutil.pid_exists` hoặc token heartbeat file).

### 3.3. CLI (mới — AC2/AC4)

```
storyforge worker --worker-id w1 [--loop] [--interval 30]
storyforge queue status            # queued/processing(+lease còn hạn?)/done/failed × worker-id
storyforge queue cancel <job>      # chỉ job đang queued (không giết đang chạy)
```

### 3.4. Config mới

```python
class QueueSettings(BaseModel):
    queue_dir: Path = Path("data/queue")
    lease_ttl_seconds: int = 30
    heartbeat_interval_seconds: int = 10
    worker_id: str = ""       # bắt buộc với --worker-id, default hostname
    scheduled_grace_seconds: int = 0   # job chưa tới giờ → bỏ qua
```

### 3.5. Test plan

- Unit: claim/lease/renew; hết hạn lease → reclaim; PID chết → reclaim.
- Integration (2 worker giả trên cùng tmp queue): 5 job → không job nào
  chạy 2 lần (mỗi job ghi marker file); kill 1 worker giữa job → job được
  worker còn lại nhận lại sau TTL (dùng TTL nhỏ trong test: 2s).
- Cross-platform: `os.rename` nguyên tử trên Windows — verify không
  FileExistsError race (test song song 2 thread).

---

## 4. B5 — Universe Bootstrap từ corpus — DEV1 (+DEV2 prompt)

### 4.1. Luồng (AC1–AC4)

```
storyforge universe bootstrap --universe X [--output config/story_draft.yaml]
  1. Đọc KB: entity facts (type=person/place, mention_count ≥ 3)
  2. Đọc alias table + topics phân bố
  3. 1 LLM call (writer model) với prompt bootstrap.md
     → premise (1 đoạn), characters (CharacterSheet mặc định),
       world rules gợi ý, source_query
  4. Validate bằng StoryConfig schema → ghi
     data/kb/<universe>/bootstrap_draft.yaml
  5. Log: entities bị bỏ qua (type khác / mention thấp)
```

- Output là **draft YAML producer duyệt** (AC2) — KHÔNG tự chạy story.
- 1 LLM call tổng hợp (AC4) — không per-chunk.
- Nếu KB trống (chưa ingest) → lỗi rõ "universe chưa có entity nào".

### 4.2. File & prompt (mới)

```
src/storyforge/bootstrap/__init__.py
src/storyforge/bootstrap/service.py     # bootstrap(settings, universe) -> Path
prompts/bootstrap.md                    # {facts}, {aliases}, {topics}, {language}
tests/test_bootstrap.py                 # mock store + LLM stub
```

Interface:
```python
def bootstrap(settings: Settings, universe: str,
              output: Path | None = None) -> Path:
    """Return path to the written bootstrap_draft.yaml."""
```

Prompt output format (parse như `_parse_beats`):
```
PREMISE: <1 đoạn>
CHARACTER: <name> | <appearance> | <personality>
WORLD: <rule 1>
WORLD: <rule 2>
QUERY: <source_query gợi ý>
```

### 4.3. Test plan

- Unit: parse prompt output → StoryConfig draft hợp lệ (validate qua
  pydantic); entities filter (mention ≥ 3, type whitelist); log bỏ qua.
- Integration (KB giả 2 source): bootstrap tạo draft có characters trùng
  canonical trong alias table.

---

## 5. B6 — Producer Dashboard read-only (STRETCH) — DEV2

### 5.1. Phạm vi (AC1–AC3)

- Web **read-only** (FastAPI + static HTML/JS đơn giản — KHÔNG React).
- Trang: danh sách project + trạng thái manifest · cost theo stage ·
  xem story.json / prompts_used / lint_report / style_stats / hook_ab.
- Không auth M4 (dev only, bind 127.0.0.1).

### 5.2. File (mới)

```
src/storyforge/web/
├── __init__.py
├── app.py            # FastAPI app: GET /, /projects, /projects/{p}/{artifact}
├── static/index.html # trang đơn, fetch API, render bảng
└── tests/test_web.py # TestClient: 200 cho mọi route, POST → 405 (read-only)
```

CLI: `storyforge dashboard --port 8000` → `uvicorn` (dev server).

### 5.3. Điểm bắt buộc

- 100% read-only: không route POST/PUT; guard ở app-level (405).
- Đọc qua `ArtifactStore` (không bypass — không tự parse file trực tiếp
  ngoài các artifact đã có model).
- Chỉ render các artifact tồn tại; thiếu → "chưa có" (không 500).

### 5.4. Test plan

- TestClient: GET /projects → 200; GET /projects/x/manifest → 200 (hoặc
  404 khi project không tồn tại); POST bất kỳ → 405.
- B6 là stretch: chỉ implement khi 2 track còn dư ≥ 3 ngày sau ngày 16.

---

## 6. Điểm hoàn thiện nhỏ (feature đã code)

| Feature | Việc | Ai |
|---|---|---|
| A1 hook_ab | Test end-to-end (giả judge): 2 hook + choice.json + config `hook: a|b|auto` | DEV2 0.5d |
| A2 recap | Wiring VideoStage prepend + test (recap dài > 35s → lint lại) | DEV1 0.5d |
| A3 thumbnail | Test: scene best visual → overlay → 1280x720; config override | DEV2 0.5d |
| B1 stylestat | Cross-episode accumulate `data/kb/<universe>/style_stats/` (10 tập) | DEV1 1d |
| B2 eval_story | AC2 retry chấm lại khi hook evidence thiếu | DEV2 0.5d |
| B3 guard | Wiring StoryStage + ReviewStage (guard đã có, chưa nối) | DEV1+DEV2 0.5d |
| B4 arbiter | Verify `conflict_verdicts.jsonl` ghi đúng + test fallback | DEV1 0.5d |

---

## 7. Thứ tự implement khuyến nghị (P2)

1. **Ngày 1–2:** DEV1: A4 youtube (metadata trước, upload sau) + wiring B3/B4
   review stage; DEV2: hoàn thiện A1/A2/A3 test + B2.
2. **Ngày 3–5:** DEV1: A6 lease/claim + heartbeat; DEV2: A5 music wiring +
   CLI music + B1 cross-episode.
3. **Ngày 6–8:** DEV1: B5 bootstrap (service + prompt); DEV2: B6 dashboard
   (nếu còn dư — stretch).
4. **Ngày 9–12:** integration chéo (publish pipeline e2e: queue → story →
   tts/img/video → upload private), bug fix.
5. **Ngày 13–14:** P3 prep — 5 tập batch, baseline docs, demo.

**Bottleneck:** DEV1 nặng hơn (A4 + A6 + B5 + wiring ≈ 10–11d). Nếu cần,
chuyển B5 sang DEV2 (prompt + service, backend KB chỉ đọc) hoặc cắt B6.

---

## 8. Kế thừa từ codebase hiện có (tránh viết lại)

- `m4tools.py` đã có `hook_ab` + `thumbnail` — chỉ thêm CLI wiring + test.
- `recap.py` đã có `RecapPlan`/`RecapSegment`/`should_recap` — VideoStage
  chỉ cần consume `RecapSegment` (audio + image + subtitle) prepend trước
  scene 0.
- `stylestat.py`/`guard.py`/`ledger/arbiter.py`/`eval_story.py` đã implement
  spec M4 — phần "còn thiếu" chỉ là wiring + test + 2 field config nhỏ.
- Config pattern: thêm `PublishSettings`/`QueueSettings` theo đúng style
  `default_factory` + `SF__SECTION__KEY` như hiện có.
