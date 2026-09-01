# M6 Design — Advanced AI (6 tuần)

> Trưởng phase: ARCH. Trạng thái: P1 — bản chốt để DEV implement.
> Tiền đề: M5 đã ship (platform, multi-tenant, web editor). Team có thêm
> DEV4 (AI/research). M6 nâng chất lượng visual + continuity, tận dụng
> pattern ainovel-cli (arbiter, ctxpack, stylestat đã có từ M2-M4).
> Nguồn: `WORKPLAN_LONGTERM.md` Phần IV + `DEEPDIVE_AINOVEL.md`.
> Mọi thay đổi sau tài liệu này = CR.

---

## 1. Mục tiêu & phạm vi

**Mục tiêu kinh doanh:** video kể chuyện động (animated scenes) thay vì
ảnh tĩnh Ken Burns; continuity bi-temporal (truy vấn "facts đúng tại episode
N"); LightRAG hoặc giữ Qdrant — quyết định bằng dữ liệu.

**Nguyên tắc:** mọi nâng cấp AI phải có **fallback deterministic** — animated
fail → Ken Burns; LightRAG fail → Qdrant-only; LoRA fail → reference-image.
Không bao giờ để một model mới làm sập pipeline.

---

## 2. Features & spec

### 2.1. M6-V1/W1 — Image-to-Video (animated scenes) (DEV2 2.5d + DEV1 2d cost)

**Mục đích:** chuyển từng scene image thành clip động ngắn (motion) trước
khi ghép video.

**Protocol:**
```python
# src/storyforge/providers/animation.py
class AnimationProvider(Protocol):
    def animate(self, image_path: str, duration_seconds: float,
                motion: MotionSpec, out_path: str) -> AnimatedClip: ...

class MotionSpec(BaseModel):
    kind: Literal["kenburns", "slow_push", "pan", "subtle_zoom", "particles"]
    intensity: float = 0.3      # 0.0 = static, 1.0 = full

class AnimatedClip(BaseModel):
    clip_path: Path
    duration_seconds: float
    provider: str               # "fal_kling" | "veo" | "kenburns_fallback"
    cost_usd: float
```

**Providers:**
- `KenBurnsFallback` — hiện có (zoompan FFmpeg), cost 0, luôn available.
- `FalKlingProvider` / `VeoProvider` — API video gen (httpx), lazy import,
  cost model trong `config/prices.yaml`.

**Config:**
```python
class AnimationSettings(BaseModel):
    provider: Literal["auto", "kenburns", "fal_kling", "veo"] = "auto"
    # auto: thử API, fail → kenburns
    motion_default: Literal["kenburns", "slow_push", "pan", "subtle_zoom"] = "slow_push"
    min_duration_seconds: float = 3.0     # scene ngắn hơn → luôn kenburns
    budget_per_video_usd: float = 2.0     # cap chi phí animated/video
```
Env: `SF__ANIMATION__*` + API keys (`SF__ANIMATION__FAL_KEY` reuse imaging).

**Wiring:** ImagingStage sau khi sinh image → AnimationStage (mới) animate
từng scene → VideoStage nhận clip_path thay image_path. Fallback: nếu
provider fail hoặc vượt budget → `kenburns` cho scene đó (log metric).

**Metric:** `scenes_animated`, `scenes_kenburns`, `animation_cost_usd` vào
stage metrics (m2 §5.4 chuẩn).

**Test plan:** unit (MotionSpec, budget cap, fallback quyết định); integration
(mock API fail → kenburns; duration < min → kenburns).

### 2.2. M6-V3/A1 — Character LoRA pipeline (DEV1 3.5d + DEV4 3d)

**Mục đích:** train LoRA riêng cho từng nhân vật chính → hình ảnh nhất
quán vượt reference-image.

**Pipeline (DEV1, automation):**
```
collect 20-50 ảnh ref/character (từ data/universe/<u>/characters/ + scene images)
  → lọc chất lượng (DEV4: metric blur/duplicate)
  → kohya_ss train (lệnh chuẩn hóa, GPU)
  → validate (1 ảnh gen test, đo similarity với ref)
  → registry data/universe/<u>/loras/<slug>.safetensors + manifest.json
```

**Registry (DEV1):**
```python
class LoRAManifest(BaseModel):
    slug: str
    character: str
    source_images: int
    trained_at: datetime
    model: str = "sd_xl"              # base model
    metrics: dict[str, float]         # validation similarity, cost_usd
    active: bool
```

**Inference (DEV2):** `ImagingStage` khi có LoRA active cho character →
thêm `--lora` arg cho provider (fal/replicate SDXL + LoRA adapter).

**Config:**
```python
class LoRASettings(BaseModel):
    enabled: bool = False
    registry_dir: Path = Path("data/universe") / "<universe>" / "loras"
    min_similarity: float = 0.8       # gate để active LoRA
    training_base_model: str = "stabilityai/stable-diffusion-xl-base-1.0"
```
Env: `SF__LORA__*`.

**Test plan:** registry CRUD; pipeline dry-run (không train thật trong unit);
validate gate (similarity < min → không active); DEV4 có eval set ảnh
consistency (reference → LoRA output, CLIP similarity + human).

### 2.3. M6-V1 — LightRAG integration (DEV1 4d + DEV4 2.5d) — theo gate

**Gate mở khi:** golden set J2 (entity hit-rate < 0.90) hoặc query quan hệ
đa tập fail sau 2 vòng tinh chỉnh ở M2–M4.

**Thiết kế nếu gate mở:**
- `KnowledgeStore` interface giữ nguyên (M1 protocol) — LightRAG là một
  implementation mới `LightRAGStore`, KHÔNG thay thế Qdrant.
- `SF__KNOWLEDGE__STORE=lightrag` hoặc hybrid (`qdrant+lightrag`): search
  chạy cả hai → RRF hợp nhất kết quả.
- Chunk store vẫn là sự thật: LightRAG index build từ chunks (không phải
  nguồn độc lập) — re-index khi chunk thay đổi.
- LightRAG entity extraction dùng model cấu hình riêng (`extract_model`,
  mặc định rẻ như reviewer).

**A/B (DEV4):** golden set chạy Qdrant-only vs LightRAG vs hybrid → quyết
định default. Chỉ chuyển default khi J2 +≥ 3 điểm VÀ chi phí indexing/episode
chấp nhận (ghi vào baseline).

**Config:**
```python
class KnowledgeSettings(BaseModel):
    store: Literal["qdrant", "lightrag", "qdrant+lightrag"] = "qdrant"
    lightrag_extract_model: str | None = None
```

**Test plan:** conformance suite (M1) chạy trên LightRAGStore; A/B golden;
cost measurement (LLM extract per source).

### 2.4. M6-V2 — Continuity bi-temporal (DEV1 2.5d)

**Mục đích:** truy vấn "facts đúng tại episode N" — phục vụ recap, reviewer,
writer khi xử lý timeline phức tạp.

**Thiết kế:** `FactLedger.query()` thêm tham số `as_of_episode: str | None`:

```python
def query(self, subject=None, kind=None, *, as_of_episode: str | None = None,
          include_superseded: bool = False) -> list[Fact]:
    """as_of: chỉ trả facts hiệu lực tại episode đó (đã loại supersede
    SAU episode đó). None = hiện tại."""
```

- Ordering đã có (s<N>/ep<M> → số thứ tự đơn điệu). `as_of` = filter
  `established_episode <= target` AND (`superseded_by` null OR
  `superseding_episode > target`).
- Index in-memory: `subject -> (episode_order, fact)` để query O(k).

**Wiring:**
- Recap M4: dùng `as_of_episode=episode_N-1` cho facts "đúng tới tập trước".
- Reviewer M3: khi fact cũ bị conflict ở tập N — biết nó được thiết lập ở
  tập M nào (đã ship chưa) → quyết định supersede hợp lệ.
- Writer serial: brief [ESTABLISHED] dùng `as_of` — không lộ fact chưa xảy
  ra ở timeline truyện.

**Test plan:** fixture 3 episode + supersede → query as_of đúng; recap
dùng as_of; reviewer dùng as_of cho conflict.

### 2.5. M6-W3 — ctxpack port (DEV2 2d)

**Mục đích:** context compression cho brief compiler khi serial > 20 tập.

**Thiết kế** (port StoreSummaryCompact từ ainovel-cli, đơn giản hóa):
- `BriefCompiler` nhận `ContextCompactor` (new strategy):
  ```
  nếu estimated tokens(brief) > budget:
      giữ [strict facts, palette scene hiện tại]
      thay dossier sample_passages + theme bằng:
        - episode summaries gần nhất (10)
        - store_summary_text: {characters: tóm tắt, arcs: 3 dòng mỗi arc, recent_events: 5}
  ```
- `store_summary_text` build từ ledger + summaries hiện có (đã có từ M3/M4)
  — không LLM call mới.
- `CommitOnProject`: summary chỉ ghi khi thay thế thật (tránh cache break).

**Config:**
```python
class StorySettings(BaseModel):
    compact_when_over_tokens: int = 0        # 0 = tắt
    compact_keep_recent_episodes: int = 10
```
Env: `SF__STORY__COMPACT_WHEN_OVER_TOKENS`.

**Test plan:** brief > budget → summary thay thế đúng thứ tự ưu tiên; dưới
budget → không đụng; serial 25 tập giả → tokens ổn định.

### 2.6. M6-W2 — Music mood auto (DEV2 1.5d)

**Mục đích:** tự chọn mood nhạc theo cảm xúc scene (thay config tay).

- `MusicMoodClassifier`: LLM 1 call per episode (cheap) — input: beat
  summaries + emotion_target → output mood từ whitelist (config
  `music_moods.yaml` keys).
- Fallback: mood rỗng → `calm` (nếu có) → không nhạc.
- CLI giữ `music --mood` override (M4 A5) — auto chỉ là default.

**Test plan:** classifier parse đúng whitelist; fail → calm fallback;
override thắng auto.

---

## 3. Effort & phân bổ

| Vai | Feature | Effort |
|---|---|---|
| **DEV1** | V1 LightRAG 4d (nếu gate) · V2 bi-temporal 2.5d · V3 LoRA automation 3.5d · V4 cost 2d | **12d** |
| **DEV2** | W1 animate 2.5d · W2 music mood 1.5d · W3 ctxpack 2d · W4 LoRA inference 2d · W5 docs/eval 2d | **10d** |
| **DEV4** | A1 LoRA train/eval 3d · A2 animate quality eval 2.5d · A3 LightRAG A/B 2.5d | **8d** |
| BA | User testing visual, gate LightRAG decision | 4d |
| ARCH | P1 spec + review + A/B design | 6d |

Timeline: P0 2.5d → P1 4d → P2 22d → P3 4d → P4 1d ≈ **6 tuần**.
LightRAG chỉ được lên P2 nếu gate mở — nếu không, DEV1 giảm 4d (thêm vào
polish animate/LoRA hoặc chuyển sang M7 sớm).

## 4. MoSCoW & cắt plan

| Ưu tiên | Feature | Lý do |
|---|---|---|
| **Must** | W1 animate (có fallback), V2 bi-temporal, W3 ctxpack, V4 cost | Chất lượng lõi + bền vững |
| **Should** | V3 LoRA, W2 music mood | Visual/audio nâng cao |
| **Could (gate)** | V1 LightRAG | Chỉ khi golden set đòi |
| **Stretch** | LoRA cho > 2 nhân vật/tập | Chi phí GPU |

**Thứ tự cắt:** LightRAG (nếu gate không mở) → LoRA multi-char → music auto
(giữ manual) → ctxpack (giữ nếu serial < 20 tập). Không cắt: animate
fallback, bi-temporal, cost tracking.

## 5. Thứ tự implement

1. **Ngày 1–4:** DEV2 W1 animate protocol + kenburns fallback; DEV1 V2
   bi-temporal; DEV4 A2 eval set motion.
2. **Ngày 5–10:** DEV2 W3 ctxpack; DEV1 V3 LoRA automation; DEV4 A1 train 1
   nhân vật mẫu.
3. **Ngày 11–16:** DEV2 W2 music + W4 LoRA inference; DEV1 V1 LightRAG (nếu
   gate) hoặc polish; DEV4 A3 LightRAG A/B (nếu gate).
4. **Ngày 17–22:** integration animated episode hoàn chỉnh; cost baseline;
   user testing 2 tập.
5. **Ngày 23–24:** P3 golden regression + demo; P4 baseline.

## 6. Rủi ro M6

| Rủi ro | Đối sách |
|---|---|
| Video gen API đắt/không ổn định | Budget cap/video + fallback kenburns mọi scene fail; cost metric trước khi cam kết |
| LoRA training tốn GPU thời gian | 1 nhân vật mẫu trước; parallel training theo queue; min_similarity gate |
| LightRAG extraction tệ tiếng Việt | A/B 2 tuần; nếu không +3 điểm → giữ Qdrant-only vĩnh viễn (ghi quyết định) |
| ctxpack phá prompt cache | Chỉ compact khi over-budget; summary dựa artifact có sẵn (không LLM mới) |
| Bi-temporal làm chậm query | Index in-memory O(k); test 3 episode fixture trước khi scale |
