# Workplan — Sprint Wiring & Fix P0 (5 ngày)

> **Nguồn:** `DEVELOPMENT_STATUS.md` (audit 09/2026) — 18/29 hạng mục xong
> nhưng nhiều module hoàn chỉnh chưa được nối vào pipeline chạy thật.
> **Mục tiêu sprint:** biến các tính năng "đã ship trên giấy" thành hoạt
> động end-to-end + chặn 2 bug P0 (cost luôn $0, alert không fire).
> **Cổng ra:** 1 bài test e2e chạy pipeline thật xanh (cost > 0, alert fire,
> music + recap xuất hiện trong video) — làm cổng chặn mọi feature sau.
>
> Kế thừa quy ước: P0→P1→P2→P3 phases; DEV1 = KB/backend/ops/video-assembly,
> DEV2 = writer/prompt/story/tts/imaging. Mọi thay đổi = CR.

---

## 1. Ownership file cho sprint (chống xung đột)

| File | Chủ | Ghi chú |
|---|---|---|
| `core/pipeline.py`, `core/cost.py`, `core/metrics.py` (mới) | DEV1 | |
| `core/config.py`, `core/types.py` | DEV1 | DEV2 đổi field → nhờ DEV1 (hoặc CR) |
| `kb/qdrant_store.py`, `kb/alias.py`, `kb/types.py`, `kb/episode_summary.py` | DEV1 | |
| `ledger/*`, `queue.py`, `publish/*` (trừ cuts), `analytics/*` | DEV1 | |
| `stages/video.py`, `stages/review.py`, `stages/knowledge.py` | DEV1 | Video nhận RecapSegment/music từ DEV2 |
| `providers/imaging.py`, `providers/tts.py`, `providers/llm.py`, `providers/stt.py` | DEV2 | |
| `stages/story.py`, `stages/tts.py`, `stages/imaging.py`, `stages/transcribe.py` | DEV2 | |
| `recap.py`, `m4tools.py`, `stylestat.py`, `eval_story.py`, `guard.py` | DEV2 | |
| `kb/compiler.py`, `kb/brief.py` | DEV2 | |
| `textnorm/`, `lint/`, `prompts/*`, `assets/*` (trừ music_cc0) | DEV2 | |
| `cli.py` | **DEV1 ngày 1–2 → DEV2 từ ngày 3** | Tuần tự: DEV1 sửa phần ops/FAILED trước, DEV2 wire story-side sau |

**assets/music_cc0/ + config/music_moods.yaml:** DEV1 tạo cấu trúc; BA cung
cấp 5 file nhạc CC0 + LICENSES.md (đầu ngày 2).

---

## 2. Task DEV1 (ngày 1–5, ~4.5 ngày)

### T1-DEV1 — Cost metrics recorder + prices.yaml (P0, 1.5d)

**Vấn đề (M2-8):** report luôn $0; stage metrics không được ghi;
`config/prices.yaml` không tồn tại.

**Việc:**
1. Tạo `core/metrics.py` — per-run recorder:
   ```python
   class MetricsRecorder:
       def __init__(self) -> None: ...
       def record(self, stage: str, **metrics: float | int | str) -> None: ...
       def snapshot(self, stage: str) -> dict[str, object]: ...
   # contextvar: current_run_recorder() -> MetricsRecorder | None
   ```
2. Tạo `config/prices.yaml` (2 tier, theo m2_design §5.4): giá per-model
   in/out, per-image theo provider/model, per-char TTS theo engine, giá
   fal/elevenlabs mặc định có chú thích "điều chỉnh theo hóa đơn thật".
3. `core/cost.py`: đọc prices.yaml (không hardcode), `cost` command thêm
   `--tier standard|premium`, output $ theo tier.
4. Hook recorder vào các điểm gọi API của **phía DEV1**: kb ingest
   (episode_summary LLM call), video render (không cost tiền nhưng ghi
   duration/render_seconds), publish (không cost — bỏ qua).

**AC:**
- AC1: `storyforge cost --project X --tier standard` in số $ > 0 sau khi
  chạy 1 pipeline có LLM call (dùng prices.yaml).
- AC2: prices.yaml có đủ model mặc định (writer/reviewer/embedding/image/tts).
- AC3: Unit test: recorder ghi → snapshot đúng; cost tính theo tier đúng.

### T2-DEV1 — Alert fire + FAILED status (P0, 1d)

**Vấn đề (M3-23):** `_execute_pipeline` không bao giờ mark FAILED →
`_check_alert` chỉ chạy khi success → alert không bao giờ fire.

**Việc:**
1. `cli.py` `_execute_pipeline`: bọc mỗi stage run trong try/except →
   `manifest.mark(stage, FAILED, error=...)` + `store.save_manifest()` +
   gọi `_check_alert` ở **finally** (cả success lẫn fail).
2. `_check_alert`: đếm fail liên tiếp theo (stage) từ manifest các run gần
   nhất → append `data/alerts.md` khi ≥ 2; thêm test chứng minh fire.
3. (Chạm cli.py — DEV1 làm NGÀY 1, trước DEV2 đụng cli.)

**AC:**
- AC1: Chạy pipeline với LLM key sai (stage fail) 2 lần → `data/alerts.md`
  có dòng alert stage đó.
- AC2: Unit test `_check_alert` với manifest giả 2 fail liên tiếp → fire;
  1 fail → không fire.

### T3-DEV1 — Music assets + CLI + wiring nhận (P0, 1d)

**Vấn đề (M3-22/A5):** filtergraph đúng nhưng pipeline không pass
`music_mood`; `assets/music_cc0/` không tồn tại.

**Việc:**
1. Tạo `assets/music_cc0/` + `LICENSES.md` (BA đưa 5 file ngày 2) +
   `config/music_moods.yaml` (mood → file/volume).
2. CLI `storyforge music --list` (đọc moods.yaml) + `music --add <file>
   --mood <mood> --license <url>` (copy + ffprobe duration + ghi license —
   thiếu license → reject).
3. `VideoStage._resolve_music`: đọc qua `config/music_moods.yaml` (không
   lookup thô); VideoStage đã nhận `music_mood` param — giữ, DEV2 sẽ pass.

**AC:**
- AC1: `music --add` reject file không license; `--list` in mood|file|license.
- AC2: `_resolve_music("calm")` trả Path khi file tồn tại; None khi thiếu.
- AC3: Unit test resolver.

### T4-DEV1 — EpisodeSummary dùng ở J1 search (P1, 0.5d)

**Vấn đề (M2-6):** `source_summary` được stamp payload nhưng không ai đọc.

**Việc:** `qdrant_store.search` (intent=theme) trả kèm `source_summary` của
từng source trong hits (thêm field `SearchHit.source_summary: str | None`
— kb/types.py, CR nhỏ); group header render để DEV2 (T7-DEV2) tiêu thụ.

**AC:** search theme trả hit có source_summary khi payload có; test.

### T5-DEV1 — Reviewer regenerate ≤ 2 vòng (P1, 1d)

**Vấn đề (M3-14):** CONFLICT strict hiện fail-fast, spec yêu cầu regenerate
scene ≤ 2 vòng với conflict report nhét prompt.

**Việc:** `stages/review.py`: khi verdict=CONFLICT và strict → gọi writer
LLM (LLMClient trực tiếp, không qua story stage) regenerate scene bị lỗi
với conflict report trong prompt → find_conflicts lại → vẫn conflict sau 2
vòng → fail rõ. Loose giữ nguyên (needs_review).

**AC:** Test: scene conflict → regenerate 1 lần → pass; conflict kéo dài 2
vòng → fail rõ ràng.

### T6-DEV1 — Alias CLI --rename (P1, 0.5d) + NVENC (stretch, 1.5d nếu còn)

- `--rename <old> --to <new>` trong aliases CLI (kb/alias.py + cli).
- NVENC (M3-18 MISSING): `providers/video_encoders.py` + probe cache +
  video.py dùng encoder config. **Stretch — chỉ làm khi xong hết P0/P1.**
  Nếu không kịp → ghi rõ còn thiếu, chuyển M4 backlog.

---

## 3. Task DEV2 (ngày 1–5, ~4.5 ngày)

### T1-DEV2 — Recap: build plan → RecapSegment thật (P0, 1.5d)

**Vấn đề (M4-A2):** recap.py + VideoStage prepend có, không ai gọi.

**Việc:**
1. `recap.py`: thêm hàm `execute_recap(settings, store, universe_ledger,
   episode_number) -> RecapSegment | None` — gọi `should_recap` → lấy facts
   (ledger `as_of_episode` nếu có) + summaries → viết script (builder hiện
   có) → TTS 1 clip → chọn 2–4 ảnh scene có sẵn → trả RecapSegment.
2. (Ngày 3, sau DEV1 xong cli) Wire vào `_execute_pipeline`: trước
   `VideoStage`, gọi execute_recap; pass `recap` vào VideoStage.
3. Test: episode 1 → None; episode 2+ → RecapSegment có audio + ảnh;
   recap off → None.

**AC:** Pipeline chạy tập 2 → video có recap clip prepend (kiểm tra bằng
duration video > tổng scenes hoặc ffprobe).

### T2-DEV2 — Wire music_mood + hook config vào pipeline (P0, 1d)

**Vấn đề (M4-A1/M3-22):** config không pipe; music không pass.

**Việc:**
1. StoryStage đọc `settings.story.hook` (a|b|auto) khi chọn hook scene 0:
   auto → điểm rubric cao hơn; a/b → chọn variant tương ứng (m4tools
   hook_ab đã viết artifacts — đọc choice.json).
2. `_execute_pipeline` (ngày 3+): đọc `story.config.music_mood` → pass vào
   `VideoStage(music_mood=...)`.

**AC:** config `hook: a` → scene 0 dùng hook_a; `music_mood: calm` + file
tồn tại → video có nhạc (ffprobe audio stream duration = narration).

### T3-DEV2 — Reference image fix (P0, 1d)

**Vấn đề (M3-19):** FalImageGenerator bỏ qua reference_image; thiếu CLI
character-ref.

**Việc:**
1. `providers/imaging.py` FalImageGenerator: khi có `reference_image` →
   dùng `fal_ref_model` + gửi image param (đúng API fal.ai kontext); không
   ref → model thường.
2. CLI `storyforge character-ref set "Bà Ngoại" --image path` (copy vào
   `data/kb/<universe>/characters/<slug>.png` — giữ layout codebase hiện
   có, CR ghi nhận lệch spec) + `character-ref generate --config ...`.
3. Test: mock httpx — request có fal_ref_model khi ref; không khi thiếu.

**AC:** Gen 1 scene có character + ref → request chứa ref model + image;
không ref → model mặc định.

### T4-DEV2 — Stylestat cross-episode + judge injection (P1, 1d)

**Vấn đề (M4-B1):** thiếu accumulate + judge không dùng style_stats.

**Việc:**
1. StoryStage cuối episode: ghi/accumulate style_stats vào
   `data/kb/<universe>/style_stats/<episode>.json` (giữ 10 tập gần nhất —
   prune cũ).
2. `eval_story.py`: judge nhận style_stats (như lint) — chiều tts_ready/
   visual tham chiếu.
3. Test: 2 episode → accumulate đúng; prune > 10.

**AC:** eval-story sau 2 tập có style_stats trong prompt judge; file
accumulate đúng 10 tập.

### T5-DEV2 — LLMClient ghi metrics (P0-phụ, 0.25d)

`providers/llm.py` LLMClient.chat: sau response, gọi
`current_run_recorder().record(stage?, llm_input_tokens, llm_output_tokens,
cost_usd theo prices.yaml)` — stage lấy từ contextvar do pipeline set.

**AC:** 1 pipeline chạy → metrics có tokens + cost > 0 cho stage story.

### T6-DEV2 — A3 thumbnail config override + tests (P1, 0.5d)

**Vấn đề (M4-A3):** `_pick_thumbnail_scene` luôn scene[0].

**Việc:** đọc `thumbnail_scene` config (types.py đã có field): `auto` → chọn
theo rubric visual (nếu có eval) hoặc scene[0]; số N → scene N. Test.

**AC:** config `thumbnail_scene: 3` → thumbnail từ scene 3; `auto` → mặc
định scene 0 khi chưa có eval.

### T7-DEV2 — Judge style_stats + J1 group header render (P1, 0.5d) — gộp T4

Nếu T4 xong sớm: compiler theme pack render group header kèm
`source_summary` (từ T4-DEV1).

---

## 4. Task chung — E2E gate (ngày 4–5, cả hai)

### T-E2E — Pipeline thật + cổng chặn (2d chung)

**Việc:**
1. `tests/e2e/test_pipeline_e2e.py` (marker `e2e`, dùng fake LLM + local
   audio 30s + Qdrant docker hoặc memory store):
   ingest 1 source → story (hook) → lint → review → tts (edge, nếu máy có
   mạng; không thì fake clip) → imaging (fake) → video (ffmpeg, 5s).
2. Assert cứng: manifest mọi stage DONE **hoặc FAILED rõ ràng**; cost report
   $ > 0; alerts.md fire khi fail 2 lần (test riêng); music file có → audio
   stream dài = narration; recap episode 2 → video dài hơn tổng scenes.
3. Chạy trên CI (GitHub Actions, 2 job: unit + e2e) — Makefile target
   `make e2e`.

**AC (cổng chặn M4+):** bài e2e xanh là điều kiện merge mọi feature mới.
Nếu flaky (TTS/ffmpeg môi trường) → đánh dấu skip rõ lý do, không xóa assert.

---

## 5. Timeline 5 ngày

```
Ngày 1   DEV1: T1 cost recorder + prices  │  DEV2: T1 recap execute (chưa wire cli)
                 T2 alert/FAILED (cli xong hôm nay)   │        T3 ref image fix
Ngày 2   DEV1: T3 music assets+CLI (BA giao nhạc)     │  DEV2: T2 hook pipe (story stage)
                 T4 episode summary search             │        T4 stylestat accumulate
Ngày 3   DEV1: T5 reviewer regenerate + T6 rename     │  DEV2: T2 wire cli (music+hook) + T1 wire cli (recap)
                                                       │        T5 LLMClient metrics + T6 thumbnail
Ngày 4   T-E2E: viết test + chạy pipeline thật, fix bugs (cả hai)
Ngày 5   T-E2E xanh → P3 mini-verification → retro + baseline sprint
```

**Ràng buộc cli.py:** DEV1 xong phần của mình trong cli.py trước cuối ngày
1; DEV2 chỉ đụng cli.py từ ngày 3. Nếu DEV1 trễ → DEV2 wire qua helper
module riêng (không sửa cli) và merge cuối ngày 3.

---

## 6. Phụ thuộc ngoài (BA/PM)

| Việc | Ai | Hạn |
|---|---|---|
| 5 file nhạc CC0 + LICENSES.md (nguồn, giấy phép, URL) | BA | Đầu ngày 2 |
| API key LLM + Qdrant docker chạy được cho e2e | PM/BA | Ngày 3 |
| Quyết định NVENC: làm trong sprint (stretch) hay chuyển M4 backlog | PM | Cuối ngày 3 |

---

## 7. Definition of Done sprint

1. 2 bug P0 (cost $0, alert không fire) đã fix + test chứng minh.
2. Music + recap + reference image hoạt động end-to-end (không chỉ có code).
3. `tests/e2e/test_pipeline_e2e.py` xanh trên máy dev — cổng chặn feature mới.
4. Baseline sprint: `docs/BASELINE_SPRINT_WIRING.md` (cost/video thực,
   thời gian render, các module đã wire) — do ARCH viết cuối ngày 5.
5. Retro: danh sách "module có code nhưng chưa wire" còn lại (nếu có) →
   vào backlog M4/M5 với lý do rõ.
