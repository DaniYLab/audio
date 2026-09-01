# M4 Requirements — P0 (đóng băng scope)

> **Trạng thái:** P0 exit — bản chốt cho P1 Design.
> **Giả định gate:** M2 persevere + M3 hoàn thành (reviewer + fact ledger
> rule-based, batch runner, NVENC, reference image, music bed, license,
> observability) — scope M4 kế thừa các thành quả này.
> **Thay đổi so với plan cũ:** M4 còn nhận thêm 4 pattern post-M2/M3 từ
> deepdive ainovel-cli (stylestat, editor rubric, scene guard, LLM arbiter)
> — M2/M3 đã ship với scope cũ nên các pattern này chưa được implement.
>
> Kế thừa quy ước: ticket M4-*, DEV1 = KB/backend/ops, DEV2 = writer/prompt/
> pipeline-UI. Mọi thay đổi sau tài liệu này = CR.

---

## 1. Phạm vi tổng quan

**Mục tiêu M4:** từ "chạy được tự động" → "xuất bản serial không cần người
canh" + hoàn thiện nền tảng chất lượng mà M2/M3 chưa có.

Hai track song song:

| Track | Chủ đề | Owner chính | Số feature |
|---|---|---|---|
| **A — Serial Ops** | Giá trị producer thấy ngay (auto-publish) | DEV1 | 6 |
| **B — Quality & Pattern Port** | Chất lượng + pattern từ deepdive | DEV2 | 6 (2 stretch) |

Tổng: **12 feature** (10 trong scope + 2 stretch).

---

## 2. Track A — Serial Ops (6 feature)

### A1 — A/B Hook

**User story:** *Là producer, tôi muốn hệ thống sinh 2 phiên bản hook (15–30
giây đầu) cho mỗi tập để tôi chọn/duyệt cái mạnh hơn, nhằm tăng retention
mà không phải tự viết.*

**Lý do BA:** hook là yếu tố sống còn retention; sinh + chọn bằng rubric
rẻ hơn chờ dữ liệu thật ở mỗi tập.

**Acceptance criteria:**
- AC1: Với mỗi outline, sinh **2 hook khác nhau** (2 beat đầu khác nhau),
  mỗi hook ≤ 80 từ, đều bám premise/theme pack.
- AC2: Chấm cả 2 bằng rubric chiều `hook` → ghi `04_story/hook_ab/`
  (`hook_a.md`, `hook_b.md`, `eval_a.json`, `eval_b.json`).
- AC3: Mặc định chọn hook điểm cao hơn; producer ghi đè được qua config
  `hook: a|b|manual` (mặc định `auto`).
- AC4: Hook chọn được dùng cho scene 0 của truyện (không phải sinh lại).

**Effort:** DEV2 2d. **Phụ thuộc:** eval harness M2, prompt versioning M2.

### A2 — Previously-On Recap

**User story:** *Là producer, tôi muốn mỗi tập (từ tập 2 trở đi) có 1 recap
20–30 giây tóm tắt diễn biến quan trọng trước đó để người xem mới không bị
lạc.*

**Lý do BA:** giải quyết vấn đề one-shot discovery (người xem vào tập 5);
tận dụng ledger + summaries đã có.

**Acceptance criteria:**
- AC1: Recap sinh từ **fact ledger** (facts quan trọng các tập gần nhất) +
  arc/volume summaries — không phải từ toàn bộ story.
- AC2: Recap = script 20–30s (~60–90 từ) đọc bởi cùng giọng TTS.
- AC3: Recap video = montage 2–4 ảnh scene đã có + phụ đề, dài đúng clip
  TTS, chèn trước scene 0 (chỉ khi `serial_recap: true`, default true từ
  tập 2).
- AC4: Không recap ở tập 1; config `recap: off` tắt được.

**Effort:** DEV1 3d (video mount + timing) + DEV2 1.5d (prompt script).
**Phụ thuộc:** ledger M3, TTS/imaging/video stages.

### A3 — Auto Thumbnail

**User story:** *Là producer, tôi muốn hệ thống tự chọn 1 ảnh đẹp nhất +
ghi tiêu đề để dùng làm thumbnail YouTube, không cần phần mềm ảnh.*

**Lý do BA:** thumbnail quyết định CTR; tự động hóa rẻ và nhất quán.

**Acceptance criteria:**
- AC1: Chọn scene image có điểm `visual` rubric cao nhất (hoặc mặc định
  scene đầu).
- AC2: Overlay tiêu đề truyện + số tập (font sẵn, không cần font mới) →
  `06_images/thumbnail.png` 1280×720.
- AC3: Producer có thể chỉ định scene thay thế qua config
  `thumbnail_scene: N|auto`.
- AC4: Không block pipeline khi thumbnail fail (best-effort).

**Effort:** DEV2 2d. **Phụ thuộc:** rubric M2, imaging stage.

### A4 — YouTube Upload (draft mode)

**User story:** *Là producer, tôi muốn pipeline upload video lên YouTube ở
chế độ draft (kèm title/description/tags) để tôi duyệt rồi mới publish.*

**Lý do BA:** đóng vòng xuất bản tự động an toàn — draft tránh lỗi công
khai; cần chỗ "người quyết cuối".

**Acceptance criteria:**
- AC1: Lệnh `storyforge publish --project X --draft` upload lên YouTube
  với title từ config, description + tags từ genre/characters.
- AC2: Credentials lưu qua env/secret vault (`SF__YOUTUBE__*`), KHÔNG trong
  code/config commit.
- AC3: Upload fail → job `failed` với error rõ; retry thủ công được.
- AC4: `--publish` (công khai) chỉ chạy khi có cờ tường minh — mặc định
  luôn draft.
- AC5: Bỏ qua nếu `upload: off`.

**Effort:** DEV1 2d. **Phụ thuộc:** batch runner M3, disk cleanup M3.

### A5 — Music/SFX Library Manager

**User story:** *Là producer, tôi muốn chọn nhạc nền theo mood cho từng tập
từ kho CC0 đã kiểm định, để video không bị Content ID.*

**Lý do BA:** nhạc trôi nổi = rủi ro tắt tiếng/mất kiếm tiền; kho CC0 check-in
+ mood tag là cách duy nhất an toàn.

**Acceptance criteria:**
- AC1: Kho CC0 cố định `assets/music_cc0/<mood>.mp3` (BA bổ sung file + ghi
  nguồn license trong `assets/music_cc0/LICENSES.md`).
- AC2: `music_mood` trong story config → resolve file → mix theo
  filtergraph M3 (aloop + amix duration=first) — không thay đổi giọng.
- AC3: Mood không có file → bỏ qua im lặng (không fail).
- AC4: `storyforge music --list` in các mood có sẵn + license.

**Effort:** DEV1 1.5d + BA 0.5d (kho nhạc). **Phụ thuộc:** music bed M3.

### A6 — Multi-Worker (queue phân tán)

**User story:** *Là producer, tôi muốn chạy nhiều job đồng thời trên nhiều
máy (hoặc container) để xuất bản 5 tập/tuần không nghẽn.*

**Lý do BA:** batch runner M3 là single-machine; scale cần queue chia sẻ.

**Acceptance criteria:**
- AC1: Queue dùng chung trên shared volume (hoặc S3) với **lease TTL 30s +
  heartbeat** — worker crash → job được nhận lại sau TTL, không deadlock
  (kế thừa stale-lock recovery M3).
- AC2: Worker có `--worker-id`; 2 worker không bao giờ chạy cùng job (kiểm
  tra lease trước khi chạy, nguyên tử).
- AC3: Job spec mở rộng: thêm `owner`, `scheduled_at` (delay).
- AC4: `storyforge queue status` in các job (queued/processing/done/failed)
  kèm worker-id.
- AC5: Không dùng Celery/Redis — giữ filesystem/S3.

**Effort:** DEV1 2.5d. **Phụ thuộc:** batch runner M3 (lock semantics).

---

## 3. Track B — Quality & Pattern Port (6 feature)

### B1 — Stylestat (deterministic style stats)

**User story:** *Là producer, tôi muốn truyện không bị lặp pattern (câu dài
liên tục, mở đầu giống nhau) mà không tốn thêm LLM.*

**Lý do:** port từ ainovel-cli — thống kê bằng code, inject vào writer +
judge prompt.

**Acceptance criteria:**
- AC1: `StyleStatsTracker` tính: sentence length distribution, scene opener
  type (dialogue/narrative/description/action), ending type, repeated
  phrases (>3 lần/tập), paragraph length avg — thuần regex/counter, 0 LLM.
- AC2: Inject vào writer prompt `working_memory.style_stats` trước scene N;
  vào judge rubric chiều tts_ready/visual.
- AC3: Lưu `04_story/style_stats.json` per-episode; cross-episode accumulate
  `data/kb/<universe>/style_stats/` (10 tập gần nhất).
- AC4: Bật/tắt qua `SF__STORY__STYLE_STATS` (default true).

**Effort:** DEV2 2d. **Phụ thuộc:** prompt versioning M2.

### B2 — Editor Rubric Refinement (score→verdict, evidence bắt buộc)

**User story:** *Là producer, tôi muốn điểm chất lượng đáng tin — LLM chấm
điểm, hệ thống suy pass/fail, và mọi chấm điểm đều có bằng chứng trích
nguyên văn.*

**Lý do:** port từ editor.md của ainovel-cli — giảm judge drift, dễ
calibrate.

**Acceptance criteria:**
- AC1: Judge chỉ trả `score` 0–100/chiều + `evidence` (trích nguyên văn) —
  `verdict` (fail/warn/pass) suy từ ngưỡng 40/70, KHÔNG để LLM tự điền.
- AC2: Chiều hook (aesthetic) bắt buộc ≥ 1 câu trích; kết luận chung chung
  → judge prompt yêu cầu chấm lại (1 retry).
- AC3: `tts_ready` tham chiếu lint_report (fail lint → score ≤ 40).
- AC4: Schema `StoryEval` đổi `score` sang thang 0–100; backward-compat
  đọc được bản cũ (1–5) khi render.

**Effort:** DEV2 1.5d. **Phụ thuộc:** eval harness M2, lint M2.

### B3 — Scene Artifact Guard (CheckpointDeltaGuard)

**User story:** *Là kỹ sư, tôi muốn đảm bảo mọi scene được sinh ra đều nằm
trên disk — không có "fact chỉ tồn tại trong chat LLM".*

**Lý do:** port từ ainovel-cli — nguồn sự thật duy nhất là artifact; guard
chặn writer "trả output mà không lưu".

**Acceptance criteria:**
- AC1: StoryStage ghi baseline `{scene_id, digest}` đầu stage; mỗi scene
  xong phải có artifact mới trong `04_story/` (digest khác baseline).
- AC2: Writer chỉ trả text trong chat mà không lưu → `GuardError` → retry 1
  lần với feedback, rồi fail stage rõ ràng.
- AC3: Digest = sha256(`narration_text + image_prompt`) — trùng lặp 2 lần
  → reject.
- AC4: Áp dụng cho cả reviewer: `save_review` phải tạo `review.json` mới.

**Effort:** DEV1 1d (reviewer) + DEV2 0.5d (story). **Phụ thuộc:** stage
artifacts M1, review stage M3.

### B4 — LLM Arbiter Escalation (conflict khi rule không quyết được)

**User story:** *Là producer, tôi muốn fact conflict được xử lý đúng cả khi
quy tắc không đủ — nhưng mọi quyết định đều có thể replay để kiểm tra.*

**Lý do:** port từ ainovel-cli Arbiter — rule-based làm nền, LLM chỉ được
hỏi khi cần, kết quả lưu `meta/conflict_verdicts.jsonl` (replayable).

**Acceptance criteria:**
- AC1: Khi `find_conflicts` rule-based không quyết được (cùng slot, khác
  statement, negation không rõ) → gọi 1 LLM call/episode (writer model)
  với prompt `arbiter_conflict.txt` → `{verdict, reason}`.
- AC2: Kết quả lưu append-only `meta/conflict_verdicts.jsonl` (episode,
  candidate, existing facts, verdict, created_at) — đọc lại được để replay.
- AC3: LLM fail/timeout → fallback rule-based (conservative: CONFLICT).
- AC4: Bật qua `SF__LEDGER__ARBITER_ENABLED` (default false ở M4; bật sau
  khi có baseline).

**Effort:** DEV1 1.5d. **Phụ thuộc:** find_conflicts M3, ledger audit.

### B5 — Universe Bootstrap từ corpus

**User story:** *Là producer, tôi muốn tạo StoryConfig (premise, characters,
world) từ chính KB đã ingest — không phải viết config tay từ đầu.*

**Lý do:** port ý tưởng reverse-foundation của ainovel-cli import pipeline;
biến corpus → universe một bước.

**Acceptance criteria:**
- AC1: `storyforge universe bootstrap --universe X` đọc entity facts + alias
  + topics từ KB → đề xuất: premise (1 đoạn), characters (CharacterSheet
  mặc định từ EntityFacts person), world rules gợi ý, source_query.
- AC2: Output là `StoryConfig` draft YAML — producer duyệt/sửa rồi mới dùng
  (KHÔNG tự động chạy story).
- AC3: Ghi `data/kb/<universe>/bootstrap_draft.yaml` + log các thực thể bỏ
  qua (type != person/place, mention thấp).
- AC4: 1 LLM call tổng hợp (không per-chunk).

**Effort:** DEV1 2.5d. **Phụ thuộc:** KB entity index M1, alias M1.

### B6 — Producer Dashboard read-only (STRETCH)

**User story:** *Là producer, tôi muốn xem jobs, chi phí, story artifact qua
web thay vì CLI, để giám sát mà không cần terminal.*

**Lý do:** bước đệm M5 (platform); làm read-only trước, ghi sau.

**Acceptance criteria (nếu làm):**
- AC1: Web (FastAPI + HTML/JS đơn giản, chưa React) đọc manifest/cost/story
  artifacts — 100% read-only, không ghi.
- AC2: Trang: danh sách project/jobs + trạng thái + chi phí theo stage +
  xem story.json/prompts_used/lint_report.
- AC3: Không yêu cầu auth M4 (local/dev only) — auth là M5.

**Effort:** DEV2 3.5d. **Stretch:** chỉ làm khi 2 track còn dư thời gian
sau ngày 16.

---

## 4. MoSCoW & Kế hoạch cắt (nếu quá tải)

| Ưu tiên | Feature | Lý do |
|---|---|---|
| **Must** | A1 A/B hook, A2 recap, A4 upload, A6 multi-worker, B1 stylestat, B3 scene guard | Cốt lõi auto-publish + chất lượng |
| **Should** | A3 thumbnail, A5 music manager, B2 editor rubric, B4 arbiter | Giá trị cao, phụ thuộc ít |
| **Could** | B5 universe bootstrap | Nice-to-have, không chặn M5 |
| **Stretch** | B6 dashboard read-only | Làm sau cùng; cắt đầu tiên nếu trễ |

**Thứ tự cắt khi trễ (từ bỏ trước):** B6 → B5 → B4 → A5 → A3. Không bao
giờ cắt: A2 (recap), A4 (upload), B1 (stylestat — rẻ + giá trị), B3 (guard).

---

## 5. Effort tổng & phân bổ

| Track | DEV1 | DEV2 | BA | ARCH |
|---|---|---|---|---|
| Track A (6) | 2d A4 + 3d A2 + 2.5d A6 + 1.5d A5 = **9d** | 2d A1 + 2d A3 = **4d** | 1d (nhạc CC0 + review) | 2d (P1 spec) |
| Track B (6) | 1d B3 + 1.5d B4 + 2.5d B5 = **5d** | 2d B1 + 1.5d B2 + 3.5d B6(stretch) = **7d** | 0.5d | 1.5d |
| **Tổng** | **14d** | **11d** (+3.5d stretch) | 1.5d | 3.5d |

Timeline: P0 (2d) → P1 (2.5d) → P2 (14d) → P3 (3d) → P4 (1d) ≈ **4 tuần**.
DEV1 là bottleneck (14d) — nếu cần, chuyển B5 sang DEV2 hoặc cắt B5.

---

## 6. Phụ thuộc giữa các feature

```
A1 A/B hook ────────► A2 recap (cần eval rubric)
A4 upload ◄───────── A6 multi-worker (upload là 1 job type)
B1 stylestat ───────► B2 editor rubric (judge dùng style_stats)
B3 scene guard ◄──── B4 arbiter (cùng vùng story/review stage)
B5 universe bootstrap ── độc lập, chỉ đọc KB
```

**Cross-milestone:** M4 không có feature nào chặn M5 (M5 cần M4 dashboard
là stretch — đã ghi rõ; nếu cắt B6, M5 phải tự build dashboard).

---

## 7. Rủi ro M4 (P0-level)

| Rủi ro | Tín hiệu | Đối sách |
|---|---|---|
| DEV1 overload (14d / 14 ngày P2) | Ngày 7 chưa xong A2 | Chuyển B5→DEV2; cắt A5 nếu cần |
| YouTube API quota/thay đổi | Upload fail 401/quota | Draft-mode giảm rủi ro; retry thủ công; document quota |
| Recap dài > 30s (LLM viết dài) | Clip recap > 35s | Lint recap riêng (≤ 90 từ) + retry 1 lần |
| Dashboard (B6) hút thời gian | Không kịp track B | Cắt B6 khỏi M4, ghi rõ là M5 đầu tiên |
| Editor rubric đổi schema phá eval cũ | Eval history lỗi | Backward-compat đọc 1–5 (AC4 B2) |

---

## 8. Định nghĩa xong M4 (DoD)

1. `storyforge publish --project X --draft` upload 1 tập thật lên YouTube
   (draft) với recap + thumbnail + hook đã chọn.
2. 5 tập chạy batch tự động tuần cuối (multi-worker ≥ 2 worker), SLO giữ
   M3, không tràn disk.
3. Stylestat + scene guard + arbiter (nếu bật) chạy trên 5 tập, không lỗi
   guard, style_stats xuất hiện trong prompts_used.
4. Baseline M4: `baseline_M4_ops.md` (SLO thực, cost/video, giờ can thiệp)
   + `baseline_M4_quality.md` (rubric 6 chiều × 5 tập, stylestat).
5. Demo P4: 5 bước acceptance như trên + quyết định B6 (dashboard) có vào
   M5 hay không.
