# M7 Design — Distribution & Monetization (6 tuần)

> Trưởng phase: ARCH. Trạng thái: P1 — bản chốt để DEV implement.
> Tiền đề: M6 đã ship (animated scenes, LoRA, bi-temporal, ctxpack). M7 đưa
> sản phẩm ra nhiều kênh + thu tiền. Team 4 DEV (1+2+3+4) đầy đủ.
> Nguồn: `WORKPLAN_LONGTERM.md` Phần V.
> Mọi thay đổi sau tài liệu này = CR.

---

## 1. Mục tiêu & phạm vi

**Mục tiêu kinh doanh:** MRR > 0 với ≥ 3 paying users; 5 kênh hoạt động
(2 YouTube + 2 TikTok + 1 Shorts); analytics loop ra ít nhất 1 khuyến nghị
được áp dụng.

**Nguyên tắc:** billing là infrastructure an toàn (không phá pipeline);
multi-channel reuse cùng video nguồn (vertical cut là transform không phải
render lại); analytics là vòng phản hồi khép kín → prompt iteration.

---

## 2. Features & spec

### 2.1. M7-V1/W1 — Stripe billing + subscription state machine (DEV1 3d + DEV3 2d)

**Mục đích:** Free/Pro/Studio tiers + usage-based phụ phí (image/video).

**Schema (Postgres M5 mở rộng):**
```sql
subscriptions (id, user_id FK, stripe_customer_id, stripe_sub_id,
               tier TEXT, status TEXT, current_period_end, created_at)
usage_billing (id, user_id, universe_id, month TEXT, cost_usd)
```

**Stripe integration:**
- Webhook: `checkout.session.completed`, `customer.subscription.updated`,
  `customer.subscription.deleted`, `invoice.payment_failed`.
- State machine (đơn giản, an toàn): `active` ⇄ `past_due` ⇄ `canceled`;
  `canceled` sau grace 3 ngày → role downgrade (Pro→Free).

```python
# src/storyforge/billing/
class BillingService:
    def create_checkout(self, user_id: int, tier: Tier, success_url: str) -> str: ...
    def handle_webhook(self, event: dict) -> None: ...
    def current_tier(self, user_id: int) -> Tier: ...
    def enforce(self, tier: Tier, feature: BillingFeature) -> None:
        """Raise PlanLimitError nếu vượt quota."""
```

**Plan limits (v1):**

| | Free | Pro ($19/mo) | Studio ($49/mo) |
|---|---|---|---|
| Video/tuần | 2 | 10 | 30 |
| Universe | 1 | 3 | 10 |
| Animated scene | ❌ | ✅ | ✅ |
| Multi-channel publish | ❌ | 1 kênh | 5 kênh |

**Quota enforcement:** ở API + worker claim (job không vượt quota mới chạy);
`PlanLimitError` → 402 + thông báo upgrade. Không chặn publish đã enqueue —
chỉ chặn enqueue mới.

**Config:**
```python
class BillingSettings(BaseModel):
    stripe_secret_key: SecretStr = SecretStr("")
    stripe_webhook_secret: SecretStr = SecretStr("")
    grace_days: int = 3
```
Env: `SF__BILLING__*`.

**Test plan:** webhook signature verify; state machine transitions (mỗi
event); enforce matrix 3×5; checkout URL tạo đúng; webhook fail → retry
(webhook_deliveries M5).

### 2.2. M7-V2/W4 — Multi-channel publish + vertical cuts (DEV1 2.5d + DEV2 3d)

**Mục đích:** cùng video gốc → publish nhiều kênh (YouTube + TikTok +
Shorts) + vertical cut 9:16.

**Vertical cut (FFmpeg, deterministic):**
```
nền 1920x1080 → crop trung tâm theo hook-first:
  - đầu video (hook 15-30s) giữ nguyên băng rộng, phần sau crop 9:16
  - hoặc: toàn bộ crop 1080x1920 với Ken Burns center (config per kênh)
caption overlay + progress bar (ass template)
```

```python
# src/storyforge/publish/cuts.py
class VerticalCutConfig(BaseModel):
    width: int = 1080
    height: int = 1920
    hook_full_frame: bool = True      # giữ hook full width
    captions: bool = True
    progress_bar: bool = True
    out_fps: int = 30

def build_vertical_cut(video_path: Path, config: VerticalCutConfig,
                       out_path: Path, ffmpeg: str = "ffmpeg") -> None: ...
```

**Channel registry:**
```python
class ChannelSpec(BaseModel):
    platform: Literal["youtube", "tiktok", "shorts"]
    credentials_ref: str        # key vào secret vault
    vertical: bool
    default_privacy: str
```

`storyforge channels add <platform> --ref ...` / `--list` / `--remove`.
Job spec mở rộng: `channels: [youtube, tiktok]` → pipeline publish gọi
từng adapter (A4 YouTube reuse + TikTok adapter mới).

**TikTok adapter:** TikTok Content Posting API (sandbox v1 — khó, hậu kỳ);
v1 M7 chỉ **YouTube + Shorts (YouTube Shorts = vertical video trên YouTube)**
— TikTok ghi rõ là "best-effort, có thể trễ do API".

**Test plan:** vertical cut unit (crop đúng tỷ lệ, hook full-frame flag);
channel registry CRUD; publish e2e giả adapter (mock upload).

### 2.3. M7-V3 — Analytics ingestion (DEV1 2.5d)

**Mục đích:** kéo metrics từ YouTube Data API (views, retention, avg % viewed)
→ warehouse cho dashboard + agentic loop.

```python
# src/storyforge/analytics/
class AnalyticsIngestor:
    def pull_video_stats(self, video_id: str) -> VideoStats: ...
    def pull_retention(self, video_id: str) -> RetentionCurve: ...
    def ingest_all(self, universe_id: str) -> int: ...
```

```sql
video_stats (video_id PK, project, channel, views, watch_time_min,
             avg_view_pct, published_at, pulled_at)
retention_curve (video_id, segment_idx, view_pct)   -- 0..1 theo giây
```

- Cron/manual: `storyforge analytics pull --universe X` (cron entry point —
  không daemon).
- Retention theo giây → map sang scene (dùng `NarrationClip.duration` để
  cắt) → `scene_retention` view.

**Config:** `SF__YOUTUBE__ANALYTICS_KEY` (reuse OAuth M4) +
`SF__ANALYTICS__RETENTION_CACHE_TTL_HOURS`.

**Test plan:** ingest 1 video giả (mock API) → rows đúng; retention → scene
map đúng (dùng clip durations); kéo trùng → upsert.

### 2.4. M7-W3/D4 — Agentic analysis loop (DEV2 3d + DEV4 4d)

**Mục đích:** retention thấp → đề xuất cải thiện hook/prompt → con người
duyệt → tái xuất bản.

```
scene_retention (thấp hơn ngưỡng)
  → [analyst LLM] 1 call: input {story config, scene, retention curve, rubric}
    → output đề xuất {dimension, change (prompt variant | hook rewrite | pacing), reason, expected_impact}
  → lưu proposals/ (artifacts, chưa áp dụng)
  → UI: producer duyệt/từ chối (M5 web editor + F3)
  → áp dụng = tạo prompt variant + re-run story stage cho scene đó
```

```python
# src/storyforge/analytics/agentic.py
class RetentionProposal(BaseModel):
    project: str
    scene_id: str
    dimension: Literal["hook", "pacing", "grounding", "visual", "tts"]
    change_kind: Literal["prompt_variant", "hook_rewrite", "pacing_cut"]
    change: str
    reason: str
    expected_impact: str
```

- Đề xuất KHÔNG tự áp dụng — luôn qua approve (an toàn sáng tạo).
- Eval (DEV4): lấy N scene retention thấp → sinh variant → chạy rubric
  judge → so với baseline → chỉ áp dụng khi cải thiện rubric.

**Config:** `SF__ANALYTICS__AGENTIC_ENABLED=false` (default off, bật sau
khi có data) + `SF__ANALYTICS__LOW_RETENTION_THRESHOLD=0.4`.

**Test plan:** proposal schema; approve flow (UI/CLI); eval loop trên
fixture retention thấp (mock judge).

### 2.5. M7-W1/W2 — Stripe checkout UI + channel settings + analytics dashboard (DEV3 6d)

**DEV3 F1:** billing pages (pricing, portal) — Stripe Checkout redirect +
Customer Portal embed. 2d.
**DEV3 F2:** channel settings UI (add/remove channel, per-channel config,
test connection). 2d.
**DEV3 F3:** analytics dashboard (views, avg_view_pct theo video + scene,
retention curve chart, proposals list + approve buttons). 2d.

Stack: React (M5) + recharts (charts) — không thêm framework mới.

---

## 3. Effort & phân bổ

| Vai | Feature | Effort |
|---|---|---|
| **DEV1** | V1 billing 3d · V2 vertical cut 2.5d · V3 analytics ingest 2.5d · V4 ops/security 2d | **10d** |
| **DEV2** | W1 checkout/portal 2d · W2 channel metadata/caption 2d · W3 agentic loop 3d · W4 multi-channel queue 3d | **10d** |
| **DEV3** | F1 billing 2d · F2 channels 2d · F3 analytics dashboard 2d | **6d** |
| **DEV4** | A1 retention→prompt experiments 4d | **4d** |
| BA | Pricing, channel setup, user acceptance | 5d |
| ARCH | P1 spec + security review (billing/webhooks) | 5d |

Timeline: P0 3d → P1 4d → P2 22d → P3 4d → P4 1d ≈ **6 tuần**.

## 4. MoSCoW & cắt plan

| Ưu tiên | Feature | Lý do |
|---|---|---|
| **Must** | V1 billing, V2 vertical cut, W4 multi-channel queue, F1 billing pages | Monetize + distribute core |
| **Should** | V3 analytics ingest, F3 analytics dashboard | Vòng phản hồi khép kín |
| **Could** | W3 agentic loop, W1 checkout/portal, F2 channel settings | Nâng cao |
| **Stretch** | TikTok adapter thật | API phụ thuộc bên ngoài, best-effort |

**Thứ tự cắt:** TikTok → agentic loop (giữ manual analytics) → F2 → W1
(dùng Stripe hosted pages thay vì embed). Không cắt: billing state machine,
vertical cut, multi-channel queue, analytics ingest tối thiểu.

## 5. Thứ tự implement

1. **Ngày 1–4:** DEV1 V1 billing (schema + webhook + state machine); DEV2 W4
   channel registry + multi-channel queue skeleton; DEV3 F1 pricing pages.
2. **Ngày 5–10:** DEV1 V2 vertical cut; DEV2 W2 metadata/caption template;
   DEV3 F2 channel settings.
3. **Ngày 11–16:** DEV1 V3 analytics ingest; DEV2 W3 agentic loop + W1
   checkout; DEV3 F3 analytics dashboard.
4. **Ngày 17–22:** integration — publish 1 video → analytics → 1 proposal
   → approve → re-render; security review billing.
5. **Ngày 23–26:** P3 — 3 paying users test, 5 channels, demo.

## 6. Rủi ro M7

| Rủi ro | Đối sách |
|---|---|
| Stripe webhook/refund phức tạp hút thời gian | Test mode e2e trước; payment_failed → email + downgrade grace (không tự hủy) |
| TikTok API không ổn định / sandbox | Ghi rõ best-effort; Shorts (YouTube vertical) là kênh thay thế |
| Analytics data ít (chưa đủ view) | Threshold cấu hình thấp lúc đầu; agentic off cho tới khi có data |
| Vertical cut làm xấu hook | hook_full_frame=true mặc định (giữ hook full width) |
| Billing bug → mất tiền người dùng | Stripe test mode mọi thứ; manual reconcile 1 tuần đầu; double-entry log |
