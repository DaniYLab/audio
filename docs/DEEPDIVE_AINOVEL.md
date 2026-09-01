# Deep Dive: ainovel-cli — Kiến trúc & Định hướng cho StoryForge

> **Nguồn:** `github.com/kentjuno/ainovel-cli` (fork Việt hóa của `github.com/voocel/ainovel-cli`)
> **Ngôn ngữ:** Go (≥1.22), CLI + TUI (Bubble Tea)
> **Mục đích:** Công cụ sáng tác tiểu thuyết dài kỳ tự động bằng AI với kiến trúc Đa Agent
> **Ngày phân tích:** 08/2026

---

## 1. Tổng quan hệ thống

ainovel-cli là một **trình sáng tác tiểu thuyết dài kỳ multi-agent** hoàn chỉnh:

- **Entry:** TUI (Bubble Tea, full Việt hóa) + headless mode + eval CLI
- **Kiến trúc lõi:** 3 worker (Architect → Writer → Editor) + 1 Arbiter (semantic rulings) + 1 Engine (deterministic loop) + 1 Flow Router (pure function, 120k combos tested)
- **Lưu trữ:** Filesystem với atomic writes (tmp+rename) + Saga cho chapter commit
- **Tính năng nổi bật:** Import pipeline semantic compile, prompt caching 3 tầng, context compression 4 cấp, style statistics, user rules runtime, step-level checkpoint recovery, eval system, editor 7-dimension review

---

## 2. Kiến trúc & Luồng dữ liệu

### 2.1. Nguyên lý nền tảng: "Phân tam" (3-way split)

Mọi quyết định trong hệ thống được xếp vào đúng một trong ba loại, không chồng lấn:

| Loại | Xử lý bởi | Ví dụ | Kiểm chứng |
|---|---|---|---|
| **Enum state transitions** | Code (`flow.Route`) | "Sau commit, có arc review chưa? Chưa thì gửi editor" | 120k exhaustive test |
| **Semantic judgments** | LLM function (`Arbiter`) | "Chọn architect short hay long?" | decisions.jsonl replay |
| **Open creative** | LLM Worker | "Viết chương 5" | CheckpointDeltaGuard |

Hai mặt phẳng đối xứng (deterministic vs semantic) là xương sống kiến trúc.

### 2.2. 4 Iron Laws

1. **Tool chỉ trả fact, không trả cross-scheduling instruction**
2. **Flow Router chịu trách nhiệm routing, Engine chịu trách nhiệm thực thi** — Route là pure function, Engine gọi Worker trực tiếp (không LLM tool forwarding)
3. **Semantic rulings → Arbiter, mỗi ruling được lưu audit** (decisions.jsonl, replayable)
4. **Hardcode boundaries, không hardcode unenumerable semantic judgments** — không dùng keyword/score threshold thay cho model understanding

### 2.3. Data Flow (đầy đủ)

```
User Input (TUI prompt / headless)
  │
  ▼
[Entry Layer]
  ├─ TUI (bubbletea)
  └─ headless
  │
  flow.Route(LoadState)──────► Instruction (Agent + Task + Reason + Chapter)
  │  (pure function, no IO)     │
  │                              ▼
  ▼                   [Engine Loop — single goroutine, serial]
[Host/Engine]          ├─ precheck (Phase=complete? Target chapter expanded?)
                       ├─ advanceGate.Allow (review-door policy)
                       ├─ trackDeadlock (repeat 3→Arbiter, 5→hard stop)
                       ├─ runWorker(inst)────►[Worker: Architect/Writer/Editor]
                       │                         │
                       │                         ├─ Tool calls:
                       │                         │   novel_context, read_chapter,
                       │                         │   plan_chapter, draft_chapter,
                       │                         │   edit_chapter, check_consistency,
                       │                         │   commit_chapter, save_review,
                       │                         │   save_arc_summary, save_book,
                       │                         │   save_foundation, ...
                       │                         │
                       │                         └─ [Store: filesystem tmp+rename]
                       │                              ├─ Progress (writing status)
                       │                              ├─ Checkpoints (step-level)
                       │                              ├─ Artifacts (chapters, outlines, etc.)
                       │                              └─ Decisions (Arbiter audit)
                       │
                       └─ Error handling:
                            ├─ retryable → subagent retry 7
                            ├─ worker error → Engine retry 1 → Arbiter
                            └─ deterministic → pause + notify
  │
  └─ Observer ──► Events ──► TUI rendering / diag / logs
```

### 2.4. Layers

```
Entry (TUI/headless)              ← prompt, steer
Host (engine, observer, usage)    ← programmatic subagent.Runner.Run
  ├─ flow.Router (pure function, IO boundary via LoadState)
  ├─ arbiter (LLM functions, decisions.jsonl)
  └─ Workers (architect/writer/editor, each independent run + model + context)
        └─ Tools (single-file atomic IO + idempotent + explicit errors)
              └─ Store (filesystem: Progress, Checkpoints, Artifacts)
                    └─ Domain (pure data models, phase/flow state transition rules)
```

Dependency: `entry → host → agents/arbiter → tools → store → domain`
Flow: `flow` package is pure strategy layer (above store, below host)

---

## 3. Các cơ chế chính

### 3.1. Flow Router (`internal/flow/router.go`)

Pure function `Route(State) -> *Instruction`. 11 decision points (priority-ordered, first-match).

```go
type Instruction struct {
    Agent   string  // architect_long / writer / editor
    Task    string  // task description
    Reason  string  // routing reason (events, logs, arbitration)
    Chapter int     // 0 = no specific chapter
}
```

State loaded entirely by `LoadState()` (IO boundary) before Route — Route never reads Store.

### 3.2. Engine Loop (`internal/host/engine.go`)

Single goroutine serial loop:
1. Apply intervention (hold/reopen/dispatch queue → boundary commit)
2. `advanceGate.HandleBoundary()` (review hold + permit reconciliation)
3. `inst = intervention ?? Route(LoadState) ?? planStartFallback`
4. `nil -> return` (complete book / semantic stop)
5. precheck(inst) — deterministically reject impossible commands
6. `advanceGate.Allow(inst)` — block unpermitted forward chapters
7. `trackDeadlock(inst)` — repeat 3→Arbiter, 5→hard stop
8. `runWorker(inst)` — subagent.Runner.Run + progress relay + DISPATCH event
9. Error classification: deterministic→pause; retry 1→Arbiter→retry/reroute/abort

### 3.3. Arbiter (`internal/arbiter/`)

4 scenarios, each with `Collect*Facts` (IO boundary) + `Decide*` (pure, replayable) + typed Decision type:

| Scenario | Trigger | Decision type |
|---|---|---|
| `plan_start` | New book start | Short/long planner + extend short demand |
| `intervention` | User steer | answer/rules/hold/reopen/dispatch |
| `worker_failure` | Worker error + no deterministic exit | retry/reroute/abort |
| `deadlock` | Same instruction repeating | retry/reroute/abort |

### 3.4. Tools (`internal/tools/`)

11 tools, 5 read + 6 write. All write tools:
- Single-file atomic (tmp + rename)
- Idempotent (checkpoint digest check)
- Cross-file: commit uses PendingCommit Saga

Key tools:
- `novel_context(scope)`: builds complete context for agent (essentially our brief compiler)
- `plan_chapter`, `draft_chapter`, `edit_chapter`: writing lifecycle
- `check_consistency`: deterministic consistency check (our reviewer lint)
- `commit_chapter`: PendingCommit Saga, returns structured facts (arc_end, needs_expansion, etc.)
- `save_review`: 7-dimension structured review

### 3.5. Import Pipeline (`internal/host/imp/`)

External novel → continue writing. 5-stage semantic compile:
```
Ingest → Segment → Analyze → Synthesize → Publish
```
- Deterministic: pure state + input fingerprint binding
- Idempotent: SourceUnit digest → workspaces → final publish
- No formal state polluted until publication
- This is exactly our J1/J2/J3 → story bridge concept

### 3.6. Eval System (`internal/eval/`)

Case-based offline regression:
- Cases: smoke (full pipeline), prompt/voice A/B
- Runner: drives host end-to-end with fake model or real model
- Grade: dimension-based scoring
- Report: collects results

### 3.7. Context Management (ctxpack)

4-level compression for Writer context:
- StoreSummaryCompact: replaces old messages with store summaries
- Per-strategy `KeepRecentTokens` + `SummaryTokenBudget`
- Append-only, projection must `CommitOnProject`

### 3.8. Style Statistics (stylestat)

Deterministic code-computed style stats injected into `novel_context`:
- Sentence pattern frequency
- Chapter-ending type ratios
- Repeated long sentences across chapters
- Title prefix consistency
- Injected as `episodic_memory.style_stats` for editor (numeric judgment) and writer (self-avoidance)

---

## 4. Mapping đến StoryForge

| ainovel-cli concept | StoryForge equivalent | Trạng thái |
|---|---|---|
| 3-way split (deterministic/arbiter/worker) | Prompts + rules + LLM (chưa tách bạch) | Cần áp dụng |
| flow.Router (pure function, 120k tests) | Pipeline stage ordering (hardcoded CLI) | Có thể cải thiện |
| Tools as fact layer | Stage artifacts (workspace files) | Đã có, nhưng không có Saga |
| Checkpoint step-level recovery | manifest per-stage resume | M1 có, nhưng coarse-grained |
| Import pipeline (ingest→segment→analyze→synthesize→publish) | J1/J2/J3 brief compiler + KB→ story | Design đúng hướng, chưa implement |
| Editor 7-dimension review | M2 rubric 6 chiều + M3 reviewer pass | Đang thiết kế |
| Arbiter with decisions.jsonl | M3 conflict verdict (twist vs hallucination) | Đang thiết kế (rule-based) |
| stylestat deterministic style stats | Chưa có | Mới |
| ctxpack 4-level context compression | Context budget trong brief compiler (M2 mục 4.2) | Design đơn giản hơn |
| User rules runtime + snapshots | StoryConfig + grounding | Có thể nâng cấp |
| Eval system (case-based regression) | Golden set 15 câu + prompt-eval harness | M2 đang thiết kế |
| prompt caching 3-tier | Chưa có | Mới |
| Provider failover + role-based models | config provider (1 LLM client) | Đơn giản hơn |

---

## 5. Định hướng phát triển mới cho StoryForge

### 5.1. Nhận định cốt lõi

ainovel-cli là một **story engine chín và được kiểm chứng** (200+ chapter novels). StoryForge là một **pipeline multimedia** (audio → KB → story → TTS → images → video). Hai dự án bổ sung cho nhau: ainovel-cli làm tốt khâu viết (mà StoryForge cần), StoryForge làm tốt khâu tiền xử lý + hậu kỳ (mà ainovel-cli không có).

### 5.2. Ba phương án chiến lược

| Phương án | Mô tả | Ưu điểm | Rủi ro |
|---|---|---|---|
| **A — Tích hợp** | Dùng ainovel-cli làm story engine của StoryForge, gọi qua Go/Python bridge | Story engine chín, không phải xây lại | Khác ngôn ngữ (Go vs Python), độ phức tạp ops, context compression shared? |
| **B — Mượn pattern** | Port các pattern cốt lõi vào StoryForge: 3-way split, tool-based consistency, import pipeline, stylestat, eval | Giữ Python stack, kiểm soát hoàn toàn | Phải implement lại nhiều thứ từ đầu |
| **C — Co-develop** | StoryForge làm pipeline, ainovel-cli làm story engine, giao tiếp qua file-based store (workspace/artifacts) | Tách biệt, không phụ thuộc runtime | Phải define contract giao tiếp, dual codebase |

### 5.3. Khuyến nghị: Phương án B (mượn pattern) + cửa mở cho A

StoryForge đã có Python codebase với design vững (KB, 2-pass brief, FactLedger, rubric). Danh sách pattern nên port từ ainovel-cli:

**P0 — Port ngay (M2/M3 không đổi scope, nhưng refine design):**
1. **stylestat** → deterministic style stats cho chiều 4 (TTS-ready) + 5 (visual) của rubric. Inject vào story prompt để writer tự tránh lặp. Cực rẻ (regex/code, không LLM).
2. **Editor 7-dimension → sử dụng mẫu** cho judge prompt (mục 3.3 m2_design): mỗi chiều bắt buộc trích citation evidence, verdict tự suy từ score (0-100), không tự điền pass/fail.
3. **CheckpointDeltaGuard** → refine artifact check: trước khi end_turn scene, bắt buộc có scene artifact mới, reject nếu writer chỉ trả output trong chat.

**P1 — M3 scope (thay thế design đơn giản):**
4. **Import pipeline semantic compile** → J1/J2/J3 brief compiler implement theo đúng 5-stage: ingest facts → segment (chunk) → analyze (entity + dossier) → synthesize (brief) → publish (into prompt). Đã khớp design, chỉ cần tham khảo cách họ input fingerprint + idempotent publish.
5. **Arbiter pattern** → nâng cấp find_conflicts: thay vì rule-based đơn thuần, thêm LLM arbiter (writer model) khi rule không quyết được, với decisions.jsonl để replay. Điều này đặc biệt quan trọng cho twist/conflict phân biệt.

**P2 — Hậu M3 (opt-in):**
6. **ctxpack** → context compression strategy cho brief compiler khi serial episode dài (>20 tập). StoreSummaryCompact (replace old messages with store summaries) là pattern phù hợp.
7. **Provider failover + role-based models** → mở rộng config: architect/writer/editor dùng model khác nhau (như roles config của ainovel). Writer dùng model rẻ, architect dùng model mạnh.

### 5.4. Cái KHÔNG nên port

- **Không port Go → Python** toàn bộ engine. flow.Router pure function có thể port nhưng 120k test không đáng.
- **Không dùng Saga** cho commit (quá nặng cho video 10 phút). manifest JSON hiện tại đủ.
- **Không áp dụng 3-way split cứng nhắc** — StoryForge có ít agent hơn, pipeline ngắn hơn. Áp dụng tinh thần (tách bạch code vs LLM vs creative) nhưng không cứng nhắc.

### 5.5. Cơ hội tích hợp ngược (ainovel-cli hưởng lợi từ StoryForge)

- **KB universe memory**: ainovel-cli có `import` pipeline nhưng không có cross-novel memory. StoryForge's KB/Ledger system có thể backport thành "kiến thức vũ trụ" cho ainovel-cli.
- **TTS + Image + Video**: ainovel-cli chỉ xuất text (TXT/EPUB). StoryForge's multimedia pipeline có thể là extension.
- **Vietnamese text normalization**: ainovel-cli's editor có anti-ai-tone; StoryForge's TextNormalizer + pacing lint có thể backport.

---

## 6. Kết luận

ainovel-cli là một story engine mạnh, đã qua kiểm chứng 200+ chương, với kiến trúc 3-way split cực kỳ kỷ luật. Nó chứng minh rằng:

- **Quyết định định lượng → code** (Route pure function, 120k test)
- **Quyết định bán cấu trúc → LLM có replay** (Arbiter + decisions.jsonl)
- **Sáng tạo mở → LLM Agent** (Worker + CheckpointDeltaGuard)

StoryForge không cần copy toàn bộ, nhưng nên hấp thụ **pattern** (đặc biệt là stylestat, arbiter, import pipeline, editor rubric) để nâng cấp chất lượng story mà không phá vỡ architecture hiện có. Cửa mở tích hợp (phương án A/C) nên để khi serial storytelling chứng minh được giá trị ở M2 pilot.