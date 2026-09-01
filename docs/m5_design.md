# M5 Design — Platform & Multi-tenant (4–5 tuần)

> Trưởng phase: ARCH. Trạng thái: P1 — bản chốt để DEV implement.
> Tiền đề: M4 đã ship (serial ops + publish + quality patterns). M5 mở rộng
> từ 1 producer → nhiều producer/team; thêm DEV3 (frontend) vào team.
> Nguồn: `WORKPLAN_LONGTERM.md` Phần III + `m4_design.md` (kế thừa dashboard
> read-only B6 nếu M4 đã làm — nếu chưa, M5 phải build cả dashboard).
> Mọi thay đổi sau tài liệu này = CR.

---

## 1. Mục tiêu & phạm vi

**Mục tiêu kinh doanh:** 2 producer thử nghiệm hoạt động thật ≥ 2 tuần;
API v1 public; web editor dùng được cho sửa story.

**Nguyên tắc dữ liệu:** Qdrant vẫn là vector store; Postgres là **registry +
metadata** (users, universes, aliases, usage, decisions, webhook delivery).
Chunk store giữ trong Qdrant payload — KHÔNG migrate chunk text sang
Postgres ở M5 (chỉ thêm ref + model_version).

**Bổ sung so với plan cũ:** M5 nhận luôn B6 dashboard từ M4 nếu M4 cắt.

---

## 2. Features & spec

### 2.1. M5-V1 — Postgres migration (DEV1, 2.5d)

**Mục đích:** registry layer thay cho file-only (`data/kb/<universe>`,
`data/ledgers/<universe>`).

**Files:**
```
src/storyforge/db/
├── __init__.py
├── engine.py        # SQLAlchemy async engine + session factory (lazy init)
├── models.py        # ORM: User, Universe, Alias, UsageRow, DecisionRow, WebhookDelivery
├── migrations/      # Alembic: 001_initial.py
└── repository.py    # thin repo layer (no ORM leak vào domain)
```

**Schema (v1 tối thiểu):**

```sql
users (id, email UNIQUE, password_hash, role, created_at)
universes (id, owner_id FK users, name, language, created_at,
           kb_config JSONB, ledger_path TEXT)
aliases (universe_id FK, canonical, alias, type, status, added_by, UNIQUE(universe_id, alias))
usage_rows (id, universe_id FK, project, stage, cost_usd, tokens_in, tokens_out,
            api_calls, created_at)
decision_rows (id, universe_id FK, episode_id, verdict, payload JSONB, created_at)
webhook_deliveries (id, universe_id, event, payload JSONB, status, attempts, next_retry_at)
```

**Quy tắc:**
- Migration: Alembic; `storyforge db migrate` + `db seed` (tạo admin).
- Dev chạy Postgres qua docker-compose (service mới `db:`).
- Không migrate chunk text/vector — chỉ registry/metadata/audit.
- Universe registry thay thế `data/kb/universes.yaml`; aliases.yaml giữ cho
  đọc nhanh nhưng ghi qua Postgres (v1: ghi cả hai, đọc từ Postgres).

**Config:**
```python
class DatabaseSettings(BaseModel):
    url: str = "postgresql+asyncpg://storyforge:storyforge@localhost:5432/storyforge"
    echo: bool = False
```
Env: `SF__DATABASE__URL`.

**Test plan:** repo unit (CRUD + unique constraint); migration từ 0;
universe registry roundtrip; không có Postgres → command fail rõ ràng
(không crash ngầm).

### 2.2. M5-V2 — Tenant registry + RBAC (DEV1, 2.5d)

**Mục đích:** universe per account; owner/editor/viewer.

```python
# src/storyforge/security/
class Role(StrEnum): OWNER, EDITOR, VIEWER

class AuthContext(BaseModel):
    user_id: int
    role: Role

def require_role(auth: AuthContext, universe_id: str, min_role: Role) -> None:
    """Raise ForbiddenError nếu user không có quyền trên universe."""
```

**RBAC matrix:**

| Hành động | Owner | Editor | Viewer |
|---|---|---|---|
| Chạy pipeline / publish | ✅ | ✅ | ❌ |
| Sửa story config | ✅ | ✅ | ❌ |
| Alias confirm/merge | ✅ | ✅ | ❌ |
| Xem artifacts/cost | ✅ | ✅ | ✅ |
| Mời user / đổi role | ✅ | ❌ | ❌ |

**Cưỡng chế:** ở API layer (dependency inject) + ở CLI (flag `--as-user` cho
dev); store KHÔNG biết RBAC (giữ domain sạch).

**Test plan:** matrix test đầy đủ 3×5; ForbiddenError → 403; CLI `--as-user`
không có quyền → exit code 1.

### 2.3. M5-V3 — Embedding versioning (DEV1, 1.5d)

**Mục đích:** đổi model embedding không mất dữ liệu + golden set không
regression > 5%.

**Thiết kế:**
- `EmbeddingSettings.model_version: str = "bge-m3-v1"` (mới) — ghi vào
  payload mọi chunk khi ingest.
- Collection Qdrant đặt tên versioned: `<prefix>_<universe>_<model_version>`.
- Lệnh `storyforge kb reembed --universe X [--model <name>]`:
  1. Đọc chunks từ collection cũ (chunk store giữ text)
  2. Embed bằng model mới
  3. Ghi collection mới → switch alias trong registry → xóa cũ (giữ 7 ngày)
- Golden set chạy trước/sau: regression > 5% → abort (giữ collection cũ).

**Test plan:** reembed trên universe giả (2 chunks) → collection mới đúng
model_version; switch; rollback; golden regression gate.

### 2.4. M5-V4 — Usage/decisions export + webhook dispatcher (DEV1, 2d)

**Mục đích:** usage_rows + decision_rows từ manifest; webhook gửi run_end/fail.

```python
# src/storyforge/notify/webhook.py
class WebhookDispatcher:
    def __init__(self, repo, http: httpx.Client | None = None) -> None: ...
    def enqueue(self, universe_id: str, event: str, payload: dict) -> None: ...
    def flush(self, max_attempts: int = 5) -> list[WebhookDelivery]: ...
```

- Backoff: 1m, 5m, 15m, 1h, 6h (attempts lưu trong DB).
- `storyforge webhooks add --universe X --url ...` / `--list` / `--remove`.
- Export: `storyforge usage export --universe X --from <date>` → CSV/JSONL
  (phục vụ cost report M2 mở rộng + billing M7).

**Test plan:** enqueue→flush với mock httpx (200/500/timeout); retry lịch
backoff; delivery status cập nhật; export CSV đúng header.

### 2.5. M5-W1 — REST API v1 (DEV2, 3d)

**Files:**
```
src/storyforge/api/
├── __init__.py
├── app.py            # FastAPI app factory (lifespan: init db + qdrant client)
├── deps.py           # auth dependency, universe resolution
├── routers/
│   ├── projects.py   # GET /projects, GET /projects/{p}/{artifact}
│   ├── universes.py  # CRUD universe, invite/role
│   ├── jobs.py       # POST /jobs (run pipeline), GET /jobs/{id}
│   ├── webhooks.py
│   └── auth.py       # POST /auth/login, /auth/refresh (JWT)
└── openapi_spec.py   # generate openapi.json (commit vào repo)
```

**Endpoints v1 (minimal, đủ cho dashboard + editor + producer):**

| Method | Path | Auth | Mô tả |
|---|---|---|---|
| POST | /auth/login | public | JWT access+refresh |
| GET | /universes | any | danh sách universe của user |
| POST | /universes | owner | tạo universe |
| GET | /projects | any | jobs + manifest của universe |
| POST | /jobs | editor+ | enqueue job (universe, config ref) |
| GET | /jobs/{id} | any | status + error |
| GET | /projects/{p}/story | any | story.json |
| GET | /projects/{p}/prompts | any | prompts_used list |
| GET | /projects/{p}/cost | any | cost per stage |
| GET | /projects/{p}/lint | any | lint_report |
| POST | /webhooks | owner | add webhook |

- Idempotency: POST /jobs nhận header `Idempotency-Key` → trả job cũ nếu key
  trùng (bảng `job_idempotency`).
- Rate limit: per-user token bucket (in-memory, v1).

**Config:**
```python
class APISettings(BaseModel):
    jwt_secret: SecretStr = SecretStr("")
    jwt_ttl_minutes: int = 60
    rate_limit_per_minute: int = 120
```
Env: `SF__API__JWT_SECRET` (bắt buộc khi chạy API).

**Test plan:** TestClient: auth flow; RBAC matrix (2.2); idempotency key;
rate limit 429; openapi.json hợp lệ (mọi route có schema).

### 2.6. M5-W2 — Webhook sink + alert webhook (DEV2, 1d)

- `data/alerts.md` (M3) → thêm webhook `alert` event qua dispatcher (2.4).
- Alert webhook nhận payload chuẩn: `{project, stage, error, run_id}`.
- CLI `storyforge alerts webhook --url ...` tiện hơn raw webhooks.

### 2.7. M5-W3 — Web story editor (DEV2, 3.5d + DEV3 F2)

**Mục đích:** producer sửa premise/characters/scenes qua web, không sửa YAML.

**Read/write với optimistic lock:**

```python
class StoryDocument(BaseModel):
    story: Story
    digest: str          # sha256(model_dump_json) — lock token

# API:
# GET  /projects/{p}/story/editor  → StoryDocument
# PUT  /projects/{p}/story/editor  body={digest, patch} → StoryDocument mới
#   - digest không khớp → 409 Conflict (kèm doc hiện tại)
#   - patch là JSON Patch (RFC 6902) — ghi lại qua ArtifactStore (atomic)
```

- Patch giới hạn: chỉ cho phép sửa `config.*`, `scenes[].narration_text`,
  `scenes[].image_prompt` — không cho xóa scene (server validate).
- Sau mỗi PUT → bump `story_edited_at` → pipeline stage story sẽ detect
  (manifest) và dùng bản mới khi re-run.

**Test plan:** optimistic lock (2 client → 1 thắng 409); patch schema hẹp
(reject xóa scene); atomic write.

### 2.8. M5-W4 — Alias review UI (DEV2, 1.5d + DEV3 F3)

- Reuse API từ M2 CLI (`aliases --pending/confirm/reject/merge`) — thêm
  endpoints: GET /universes/{u}/aliases/pending, POST .../confirm.
- UI: bảng pending + 3 passage context mỗi alias (giống CLI output).

### 2.9. M5-W5 — API docs + runbook (DEV2, 1d)

- `docs/API.md`: auth, từng endpoint, ví dụ curl.
- `docs/ops_m5.md`: start db, migrate, run api, deploy (docker-compose),
  backup/restore Postgres + Qdrant.

### 2.10. DEV3 — Frontend (mới, 10d)

**Stack:** React + Vite + TypeScript + TanStack Query. Repo mới
`web/` (monorepo root, không trộn vào `src/storyforge`).

```
web/
├── src/
│   ├── api/client.ts        # typed fetch wrapper + JWT storage
│   ├── pages/Dashboard.tsx  # jobs + cost + artifacts browse (M4 B6 nâng cấp)
│   ├── pages/StoryEditor.tsx# đọc story + edit form (patch qua 2.7)
│   ├── pages/Aliases.tsx    # review/confirm/reject
│   ├── pages/Universes.tsx  # CRUD + members/roles
│   └── components/...
├── vite.config.ts
└── Dockerfile               # build → nginx static
```

**Test plan:** component tests (Vitest + Testing Library) cho form editor
(dirty state, conflict 409 display); e2e tối thiểu (Playwright) happy path
login → xem project → sửa premise.

### 2.11. M5-F4 — Auth flow (DEV3, 2d)

- Login page → JWT (access+refresh) → lưu localStorage (v1) / httpOnly
  cookie (nếu có thời gian).
- Route guard theo role; 403 → thông báo.

---

## 3. Effort & phân bổ

| Vai | Feature | Effort |
|---|---|---|
| **DEV1** | V1 Postgres 2.5d · V2 RBAC 2.5d · V3 reembed 1.5d · V4 webhook/export 2d · V5 ops tests 1.5d | **10d** |
| **DEV2** | W1 API 3d · W2 alert sink 1d · W3 editor 3.5d · W4 alias UI 1.5d · W5 docs 1d | **10d** |
| **DEV3** | F1 dashboard 3.5d · F2 story editor 2.5d · F3 aliases 2d · F4 auth 2d | **10d** |
| BA | Requirements, user testing 2 producer, acceptance | 5d |
| ARCH | P1 spec + review + API conformance | 6d |

Timeline: P0 2d → P1 3d → P2 15d → P3 3d → P4 1d ≈ **4.5 tuần**.

**Bottleneck:** DEV1/DEV2/DEV3 đều ~10d/15d — không ai quá tải; buffer 5d
cho integration. Nếu M4 chưa làm B6 dashboard → DEV3 F1 +0 (thêm 3.5d,
dashboard là ưu tiên cao nhất của F1 — cắt F3 nếu cần).

## 4. MoSCoW & cắt plan

| Ưu tiên | Feature | Lý do |
|---|---|---|
| **Must** | V1 Postgres, V2 RBAC, W1 API core (auth/universes/jobs/story), F1 dashboard, F4 auth | Platform đứng được |
| **Should** | V4 webhook, W3 editor, W4 alias UI, F2 editor, F3 aliases | Giá trị producer |
| **Could** | V3 reembed, W2 alert sink | Không chặn ai |
| **Stretch** | F3 (nếu cần cắt) | UI phụ |

**Thứ tự cắt:** F3 → W2 → V3 → W4. Không cắt: W1, V1, V2, F1, F4.

## 5. Thứ tự implement

1. **Ngày 1–3:** DEV1 V1 (db + migration) + V2 RBAC; DEV2 W1 auth/universes
   skeleton; DEV3 F4 auth + F1 dashboard skeleton (mock API).
2. **Ngày 4–8:** DEV1 V4 webhook; DEV2 W1 projects/jobs/cost routes; DEV3 F1
   thật (wired API).
3. **Ngày 9–13:** DEV1 V3 reembed; DEV2 W3 editor API + W2; DEV3 F2 editor +
   F3 aliases.
4. **Ngày 14–15:** W5 docs + V5 ops tests + integration; user testing bắt đầu.

## 6. Rủi ro M5

| Rủi ro | Đối sách |
|---|---|
| Postgres migration phá workspace cũ | Migration song song: đọc ưu tiên Postgres, fallback file 2 tuần |
| JWT/refresh phức tạp hút thời gian | v1: access 60m + refresh 7d, không revocation list |
| 2 producer thử nghiệm không dùng web | Interview sớm ngày 10 (trước khi build xong F3) |
| DEV3 mới join chậm ramp | Sprint 0: 2 ngày onboarding (setup, styleguide, mock API) |
