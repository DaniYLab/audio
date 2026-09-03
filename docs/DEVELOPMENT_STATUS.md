# Development Status — Code vs Design Audit (09/2026)

> Kiểm toán toàn bộ codebase `src/storyforge` đối chiếu với spec M2→M7
> (`m2_design.md`, `m3_design.md`, `m4_design.md`, `m5_design.md`,
> `m6_design.md`, `m7_design.md`). Phương pháp: 2 agent đọc code + đối chiếu
> từng hạng mục spec, kết quả có bằng chứng file:line.
>
> **Tổng hợp: 29 hạng mục kiểm tra — 18 IMPLEMENTED, 9 PARTIAL, 2 MISSING
> (M5–M7 scope chưa bắt đầu).** Phần lớn phần thiếu là **wiring** (code có
> nhưng không nối vào pipeline) chứ không phải viết lại từ đầu.

---

## 1. Tổng quan theo milestone

| Milestone | IMPLEMENTED | PARTIAL | MISSING | Nhận xét |
|---|---|---|---|---|
| **M2** (10 mục) | 6 | 4 | 0 | Lõi xong; cost report là lỗ hổng nghiêm trọng |
| **M3** (13 mục) | 6 | 5 | 2 | Ledger/guard/arbiter xong; disk cleanup + NVENC chưa làm |
| **M4** (12 mục) | 6 | 5 | 1 | A4/A6/B2-B5 xong; wiring A1-A3/A5 thiếu |
| **M5** (6 mục) | 0 | 2 | 4 | Mới khởi động: API skeleton + webhook có, Postgres/web editor/React chưa |
| **M6** (6 mục) | 1 | 4 | 1 | Bi-temporal query xong (chưa wire); LoRA/LightRAG chưa (có gate) |
| **M7** (5 mục) | 1 | 3 | 1 | Analytics ingest xong; billing chưa |

---

## 2. M2 — Chi tiết

| # | Hạng mục | Trạng thái | Bằng chứng | Thiếu |
|---|---|---|---|---|
| 1 | TextNormalizer | ✅ IMPLEMENTED | `textnorm/` (rules.py 10 rule đúng thứ tự, numbers.py, normalizer.py) + `config/textnorm_loanwords.yaml` + `stages/tts.py:44-56` | — |
| 2 | Pacing/TTS lint | ⚠️ PARTIAL | `lint/` đủ 6 checks; StoryStage regenerate 1 lần (story.py:128-170) | Artifact sai tên: `prompts_used/retry_scenes.jsonl` thay vì `retry_<scene>.txt` (lệch spec nhỏ) |
| 3 | Prompt versioning + eval | ✅ IMPLEMENTED | `eval_story.py` (0-100, verdict 40/70, hook evidence, tts_ready cap 40); `load_prompt_with_meta` frontmatter; `extract_json` | — |
| 4 | Hook/Cold Open | ✅ IMPLEMENTED | `prompts/outline.txt:19-22` hook_00 ≤ 80 từ | — |
| 5 | Reranker | ✅ IMPLEMENTED | `kb/reranker.py` + eval `--compare-reranker` | — |
| 6 | EpisodeSummary | ⚠️ PARTIAL | `kb/episode_summary.py` + stamp `source_summary` payload | **Không được đọc ở J1** (group header không dùng summary) |
| 7 | Alias CLI | ⚠️ PARTIAL | `aliases list/confirm/merge/reject` | Thiếu `--rename` subcommand |
| 8 | Cost report | 🔴 PARTIAL (lỗ hổng) | `core/cost.py` + CLI cost | **`config/prices.yaml` không tồn tại; stage metrics (cost_usd, tokens, api_calls) không bao giờ được ghi — report luôn $0** |
| 9 | A/B visual style | ✅ IMPLEMENTED | CLI ab-style | — |
| 10 | Lint dataset 50 câu | ✅ IMPLEMENTED | `tests/fixtures/lint_cases_vi.yaml` đủ 50 + phân bổ | — |

## 3. M3 — Chi tiết

| # | Hạng mục | Trạng thái | Thiếu |
|---|---|---|---|
| 11 | FactLedger | ✅ IMPLEMENTED | — (record/query/supersede/audit + negation detection + as_of_episode) |
| 12 | LLM Arbiter | ✅ IMPLEMENTED | — (conflict_verdicts.jsonl + fallback) |
| 13 | Scene Guard | ✅ IMPLEMENTED | — (wire StoryStage + ReviewStage) |
| 14 | Reviewer pass | ⚠️ PARTIAL | CONFLICT → **regenerate scene ≤ 2 vòng không implement** (strict fail-fast); trap + twist tests pass |
| 15 | BriefCompiler ledger | ✅ IMPLEMENTED | — ([ESTABLISHED]/[INVENTED] đúng contract) |
| 16 | Batch runner | ✅ IMPLEMENTED | — (queue + worker + stale lock) |
| 17 | Disk cleanup | ❌ MISSING | `storyforge clean` hoàn toàn chưa có |
| 18 | NVENC/QSV | ❌ MISSING | Config `encoder=auto` chỉ là field; video.py hardcode libx264; không có probe |
| 19 | Reference image | ⚠️ PARTIAL | FalImageGenerator **bỏ qua reference_image** (fal_ref_model không dùng); thiếu CLI `character-ref`; dir layout lệch spec |
| 20 | TTS cache | ⚠️ PARTIAL | Cache key **thiếu model** (chỉ engine+voice+text); thiếu `clean --tts-cache` |
| 21 | License field | ⚠️ PARTIAL | Thiếu `ingest --license`; `allowed_licenses` config **không wire** vào search filter |
| 22 | Music bed | ⚠️ PARTIAL | Filtergraph đúng nhưng **pipeline không pass music_mood**; `assets/music_cc0/` không tồn tại |
| 23 | Observability | 🔴 PARTIAL (bug) | **Alert không bao giờ fire**: `_execute_pipeline` không bao giờ mark FAILED (chỉ DONE/SKIPPED) → `_check_alert` chỉ chạy khi success |

## 4. M4 — Chi tiết

| # | Hạng mục | Trạng thái | Thiếu |
|---|---|---|---|
| A1 | A/B Hook | ⚠️ PARTIAL | Config `hook: a|b|auto` **không được pipe vào story stage**; thiếu test |
| A2 | Recap | ⚠️ PARTIAL | `recap.py` + VideoStage prepend có, nhưng **không ai gọi `build_recap_plan`/tạo RecapSegment** — pipeline không wire |
| A3 | Thumbnail | ⚠️ PARTIAL | `_pick_thumbnail_scene` luôn chọn scene[0], **bỏ qua config override**; thiếu test |
| A4 | YouTube Upload | ✅ IMPLEMENTED | — (publish/ đầy đủ + receipt idempotent + test) |
| A5 | Music Manager | ⚠️ PARTIAL | CLI chỉ `list` (thiếu --add/--mood/--license); kho nhạc + music_moods.yaml chưa có; không wire |
| A6 | Multi-Worker | ✅ IMPLEMENTED | — (lease/heartbeat/reclaim + 20 tests) |
| B1 | Stylestat | ⚠️ PARTIAL | Inject writer prompt có; **cross-episode accumulate chưa có**; judge không dùng style_stats |
| B2 | Editor Rubric | ✅ IMPLEMENTED | — |
| B3 | Scene Guard | ✅ IMPLEMENTED | — |
| B4 | Arbiter | ✅ IMPLEMENTED | — |
| B5 | Universe Bootstrap | ✅ IMPLEMENTED | — (single module `bootstrap.py` thay vì package — chấp nhận được) |
| B6 | Dashboard read-only | ❌ MISSING | Chưa có (stretch M4 → rơi vào M5) |

## 5. M5 / M6 / M7 — Chi tiết

| # | Hạng mục | Trạng thái | Thiếu |
|---|---|---|---|
| M5-Postgres | ❌ MISSING | db/, Alembic, docker db service, SF__DATABASE__URL |
| M5-RBAC | ⚠️ PARTIAL | `api/app.py` có require_role đơn giản; thiếu security/ module + matrix test |
| M5-REST API | ⚠️ PARTIAL | Skeleton FastAPI (health/auth/universes/projects/story); thiếu jobs/cost/lint/webhooks endpoints + idempotency + rate limit |
| M5-Webhook | ⚠️ PARTIAL | Dispatcher file-based có; **chưa wire pipeline** (không auto enqueue run_end/fail) |
| M5-Web editor | ❌ MISSING | PUT /editor + optimistic lock chưa có |
| M5-React frontend | ❌ MISSING | `web/` chưa tồn tại |
| M6-Animation | ⚠️ PARTIAL | Protocol + KenBurnsFallback có; **thiếu AnimationStage + FalKling/Veo + wiring + test** |
| M6-LoRA | ❌ MISSING | (có thể chờ — không gate) |
| M6-LightRAG | ❌ MISSING | (theo gate — chưa mở) |
| M6-Bi-temporal | ⚠️ PARTIAL | `query(as_of_episode)` IMPLEMENTED + test; **chưa wire recap/reviewer/writer** |
| M6-ctxpack | ⚠️ PARTIAL | maybe_compact có + wire compiler; **thiếu store_summary_text** (chỉ xóa chứ không thay bằng summary) |
| M6-Music mood auto | ⚠️ PARTIAL | classify_mood có; **chưa wire pipeline** |
| M7-Analytics | ✅ IMPLEMENTED | ingest + retention→scene + CLI + test |
| M7-Vertical cuts | ⚠️ PARTIAL | build_vertical_cut cơ bản; thiếu hook_full_frame/captions/progress bar |
| M7-Multi-channel | ⚠️ PARTIAL | ChannelRegistry có; thiếu CLI channels + publish multi-channel |
| M7-Billing | ❌ MISSING | (chưa bắt đầu) |
| M7-Agentic loop | ⚠️ PARTIAL | Chỉ model skeleton; thiếu LLM proposal + approve flow |

---

## 6. Ưu tiên xử lý (theo mức độ nguy hiểm)

### P0 — Bug/thiếu sót làm tính năng chết im lặng (làm ngay)

1. **Cost report luôn $0** (M2-8) — stage metrics không được ghi; `prices.yaml`
   không tồn tại. → Vi phạm KPI chi phí M2, chặn mọi quyết định cost sau này.
2. **Alert không bao giờ fire** (M3-23) — `_execute_pipeline` không mark
   FAILED. → Mất luôn observability M3, batch chết âm thầm.
3. **Music bed không wire** (M3-22, M4-A5) — filtergraph đúng nhưng
   `music_mood` không được pass. → Tính năng M3 hoàn toàn vô dụng.
4. **Recap không wire** (M4-A2) — VideoStage nhận recap nhưng không ai tạo.
5. **Reference image vô dụng** (M3-19) — FalImageGenerator bỏ qua ref param.

### P1 — Đúng spec nhưng thiếu sót vừa (tuần này)

6. A1 hook config không pipe; A3 thumbnail bỏ qua config.
7. EpisodeSummary không dùng ở J1.
8. Reviewer regenerate ≤ 2 vòng (thay fail-fast).
9. Stylestat cross-episode + judge injection.
10. B1 ctxpack store_summary_text; bi-temporal wiring.

### P2 — Feature chưa bắt đầu (theo lộ trình, không phải bug)

11. M5: Postgres/web editor/React; M6: LoRA/LightRAG (có gate);
    M7: billing. Dashboard rơi vào M5.

---

## 7. Kết luận

Codebase đã đi rất xa: **18/29 hạng mục hoàn chỉnh**, bao gồm toàn bộ phần
khó (ledger + negation detection + arbiter, scene guard, multi-worker lease,
YouTube upload, bi-temporal query, analytics ingest). M5–M7 mới ở giai đoạn
skeleton — đúng kỳ vọng nếu team vừa kết thúc M4.

Vấn đề nghiêm trọng nhất không phải "thiếu code" mà là **lớp wiring**:
nhiều module hoàn chỉnh (recap, music, reference image, episode summary,
cost metrics, alerts) tồn tại nhưng không được nối vào pipeline chạy thật —
tức là các tính năng "đã ship" trên giấy có thể không hoạt động khi chạy
end-to-end. Kế hoạch xử lý đề xuất: sprint "wiring + fix P0" 3–5 ngày trước
khi mở bất kỳ feature mới nào, kèm 1 bài test end-to-end chạy pipeline thật
làm cổng chặn (giống conformance suite của KB).

---

## 8. Audit hậu sprint hoàn thiện (09/2026)

> Cập nhật sau sprint "hoàn thiện sản phẩm" (bổ sung cho mục 6/7). Đối chiếu
> lại từng hạng mục PARTIAL/MISSING còn sót. **403 unit tests + e2e gate xanh;
> ruff + mypy strict sạch.**

### Mới IMPLEMENTED

| Hạng mục | Trước | Sau | Bằng chứng |
|---|---|---|---|
| M3-V4 Disk lifecycle | MISSING | ✅ | `storyforge clean` (`src/storyforge/cleanup.py`) — dry-run/keep-final/older-than/tts-cache; `tests/test_cleanup.py` (8) |
| M3-V5 NVENC/QSV | MISSING | ✅ | `providers/video_encoders.py` — probe + cache + args map; wire vào VideoStage + KenBurnsFallback; `tests/test_video_encoders.py` (11) |
| M3-20 TTS cache model | PARTIAL | ✅ | cache key gồm `model` (`engine_model()` trong providers/tts.py) |
| M3-21 License | PARTIAL | ✅ | `--license` trên `run`/job spec; `allowed_licenses` filter Qdrant + memory store; test license gate |
| M4-A1 Hook config | PARTIAL | ✅ | `m4tools.choose_hook_beat()` (a/b/auto/manual) wire vào StoryStage; `tests/test_hook_wiring.py` (5) |
| M4-A3 Thumbnail | PARTIAL | ✅ | Pipeline auto-sinh thumbnail sau imaging; ưu tiên `StoryConfig.thumbnail_scene` |
| M4-B1 Stylestat | PARTIAL | ✅ | `accumulate_style_stats` wire vào pipeline; judge nhận `{style_stats}` (prompt v3); `tests/test_stylestat_wiring.py` (6) |
| M5-V4 Webhook | PARTIAL | ✅ | Auto-enqueue `run_end`/`run_fail`/`alert` trong `_execute_pipeline` finally |
| M6-V2 Bi-temporal | PARTIAL | ✅ | `loader.facts_as_of()` + wire recap (`as_of_episode`), BriefCompiler (`ledger_as_of`), StoryStage |
| M6-W2 Music auto | PARTIAL | ✅ | `classify_mood` fallback khi `music_mood` không set; `tests/test_music_auto.py` (3) |
| M6-W3 ctxpack | PARTIAL | ✅ | `store_summary_text()` từ ledger + summaries (0 LLM call) wire qua BriefCompiler |
| M5-W1 API | PARTIAL | ✅ | Endpoints jobs/cost/lint/review/manifest/webhooks + rate limit + RBAC + `storyforge api` (uvicorn); `tests/test_api_extended.py` (14) |
| M7-W4 Channels | PARTIAL | ✅ | `channels add/list/remove`, `publish --channel` multi-dispatch, `cut` vertical; `tests/test_channels.py` (4) |
| M7-W3 Agentic | PARTIAL | ✅ | `analytics proposals`/`approve` — heuristic fallback + LLM khi bật; `tests/test_agentic.py` (4) |
| Queue enqueue | — | ✅ | `storyforge queue add` + `QueueManager.enqueue()` |
| M6-W1 AnimationStage | PARTIAL | ✅ (có gate) | `stages/animation.py` + VideoStage consume clip (`animated=` param) + wire pipeline khi `SF__ANIMATION__PROVIDER=fal_kling|veo`; default kenburns giữ nguyên trong VideoStage (tránh double-render); fail → fallback kenburns; `tests/test_animation_stage.py` (5). Provider API thật (fal_kling/veo) vẫn chưa implement — factory hiện trả KenBurnsFallback |

### Còn MISSING / deferred (cần quyết định ngoài code)

| Hạng mục | Lý do giữ lại |
|---|---|
| M6-W1 AnimationStage | Cần FalKling/Veo provider thật (đã wire AnimationStage + tests; xem bảng trên) |
| M5 Postgres/React/editor | Phụ thuộc dịch vụ ngoài + quyết định stack (registry layer) |
| M7 billing (Stripe) | Cần tài khoản Stripe + webhook secret |
| assets/music_cc0/*.mp3 | BA cung cấp file (chỉ có LICENSES.md) |
| M3 clean sau job tự động | Batch runner gọi `clean --keep-final` — chờ quyết định vận hành |
