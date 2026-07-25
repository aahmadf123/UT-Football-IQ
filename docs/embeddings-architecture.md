# Play Embeddings & Similar-Rep Search Architecture

**Issue:** #77 (design for #8)
**Phase:** 3
**Status:** Design — no code changes
**Dependencies:** #8 (similar-rep search scope), #12 (coverage labels closed),
#74 (SAM 3.1 — optional enrichment), #76 (GPU ceilings: 6 GB same-session,
16 GB nightly)

---

## 1. Purpose

Issue #8 asks for learned play embeddings, "find me reps like this" search,
and zero-shot concept discovery. NVIDIA's Video Search and Summarization
(VSS) blueprint is a useful reference shape: **chunk video → enrich each
chunk with metadata → embed → store retrievable vectors → query by example
or natural language**.

This doc maps that shape onto Football-IQ's existing Python + Postgres +
pgvector stack. It is intentionally not a port of the NVIDIA platform: we
adopt the *pattern*, not the NIM/VSS services. Every component lands on a
table, stage, or router that already exists or has a clear seam to add.

### Non-goals

- Implementing the training loop in this issue.
- Adopting NVIDIA enterprise NIM/VSS services.
- Surfacing zero-shot labels to coach dashboards without human review.
- Exceeding the hardware ceilings from issue #76 (6 GB same-session,
  16 GB nightly).

---

## 2. Football-IQ ground truth (today)

The pipeline already produces the substrate embeddings need. Relevant
surfaces (see `backend/app/models.py`, `gpu-worker/pipeline/stage_*.py`,
`backend/app/routers/`):

- **`clips`** — play-scoped video segments with `start_time`, `end_time`,
  `boundary_source`, `boundary_confidence`, `label_data` (JSON),
  `model_version_id`, `calibration_version_id`. Each clip is one play.
- **`tracklets` / `track_points`** — per-player IDs and per-frame
  `(field_x, field_y, bbox, detection_confidence)` already projected to
  field yards through homography.
- **`pose_keypoints`** — 17-COCO or 34-BodyPose3D keypoints per
  tracklet-frame, plus `head_yaw_degrees`, `biomechanics` (JSON).
- **`labels`** — model- and human-sourced labels (`label_type`,
  `label_value`, `source`, `model_version_id`).
- **`coach_corrections`** — typed human overrides, with
  `exported_as_label` lineage to training datasets.
- **`events`** — snap, motion, penalty, etc.; carries the snap frame that
  anchors most retrieval queries.
- **`model_versions`** — registry with `promoted_stage`
  (experimental → staging → production → retired).
- **`training_datasets`** — versioned snapshots tying labels + corrections
  back to model artifacts.

There is no embedding storage, no vector index, and no search router yet.
`gpu-worker/pipeline/model_router.py` already reserves an `embeddings`
stage that currently routes to `"none"`; this design fills that slot.

---

## 3. Architecture overview

```
       ┌────────────────────────────────────────────────────────────┐
       │ Ingest → Segment → Detect → Track → ReID → Pose → Labels    │ (existing)
       │ → Events → Metrics → Render                                 │
       └─────────────────────────────┬──────────────────────────────┘
                                     │
                                     ▼  (new: nightly by default)
       ┌────────────────────────────────────────────────────────────┐
       │ stage_embed                                                 │
       │   1. Chunk clip (snap-anchored windows)                     │
       │   2. Collect metadata (labels, tracks, pose, opt. masks)    │
       │   3. Run baseline encoders (visual + structured)            │
       │   4. Fuse → 256-d play embedding + sub-embeddings           │
       │   5. Write to `playembeddings` (pgvector)                   │
       └─────────────────────────────┬──────────────────────────────┘
                                     │
                                     ▼
       ┌────────────────────────────────────────────────────────────┐
       │ Retrieval API (`/api/v1/search/*`)                          │
       │   • POST /search/similar  (clip_id → ranked clips)          │
       │   • POST /search/text     (NL query, gated, experimental)   │
       │   • Filter by date, opponent, formation, coverage, etc.     │
       └─────────────────────────────┬──────────────────────────────┘
                                     │
                                     ▼
       ┌────────────────────────────────────────────────────────────┐
       │ Coach-review surface (front end + corrections)              │
       │   experimental concept labels never promote without sign-off│
       └────────────────────────────────────────────────────────────┘
```

The flow follows VSS's chunk → enrich → embed → store → retrieve →
review shape but reuses Football-IQ's existing snap-anchored clips, label
taxonomy, and `coach_corrections` review loop.

---

## 4. Mapping: NVIDIA VSS-style components → Football-IQ

| VSS-style component                  | Football-IQ landing point                                                                                  | Status      |
| ------------------------------------ | ---------------------------------------------------------------------------------------------------------- | ----------- |
| Video ingestion + storage            | `stage_ingest` + `videos`, `clips`                                                                         | exists      |
| Scene / shot segmentation            | `stage_segment` (optical-flow play boundaries) + `clips.boundary_source`                                   | exists      |
| Chunking strategy                    | Snap-anchored windows over `clips` + `events` (snap frame); see §5                                         | new (spec)  |
| Per-chunk metadata enrichment        | `tracklets`, `track_points`, `pose_keypoints`, `labels`, `events`, `metrics`, optional SAM masks (#74)     | exists + new fuser |
| Visual encoder                       | `stage_embed` baseline: frozen CLIP ViT-B/32 over snap-window keyframes                                    | new         |
| Structured encoder                   | `stage_embed` baseline: hand-designed feature vector (formation, coverage, tracklet geometry, pose stats)  | new         |
| Embedding fusion                     | Concat + L2-norm into 256-d play embedding (plus retained sub-embeddings)                                  | new         |
| Vector store                         | Postgres + pgvector — new table `playembeddings`                                                           | new (DDL)   |
| Vector index                         | `ivfflat` (cosine) on `playembeddings.vector`, switchable to `hnsw` once row count justifies it            | new         |
| Retrieval API (example-based)        | New `backend/app/routers/search.py`: `POST /search/similar`                                                | new         |
| Retrieval API (natural-language)     | Same router, `POST /search/text`; gated behind experimental flag and coach review                          | new (gated) |
| Re-ranking / filters                 | Postgres joins on `clips.label_data`, `labels`, `events.attributes`, opponent/date metadata                | exists      |
| Zero-shot concept discovery          | Offline clustering job over `playembeddings`; clusters surface as experimental `labels` (source=`model`)   | new         |
| Human review / promotion             | `coach_corrections` (`CorrectionType.formation_tag`, etc.) + `labels.source` flip from `model` to `human`  | exists      |
| Training-dataset lineage             | `training_datasets.source_label_ids` already captures embedding-promoted labels                            | exists      |
| Model registry / version pinning     | `model_versions` (new entries: `play-embed-clip-vitb32-baseline`, etc.) + `playembeddings.model_version_id`| exists + new rows |
| GPU routing                          | `model_router.py` stage `embeddings`: nightly only by default, same-session = `none`                       | exists (slot) |
| Audit trail                          | `processing_jobs.output_artifacts["model_routing"]` already records per-stage variant                      | exists      |

VSS components **skipped** for now (not justified at our scale): NIM
microservice packaging, dedicated VSS UI, Triton-served encoder pool,
managed Milvus, summary LLM. We can revisit any of these when the
retrieval workload outgrows pgvector on a single Postgres node.

---

## 5. Chunking — how a clip becomes embeddable units

A "play" is the unit a coach asks about ("find me reps like this rep"),
so the primary embedding is **per clip**, not per arbitrary time window.
But VSS-style chunking still matters because plays have internal
structure (pre-snap, snap, post-snap) and not all moments are equally
informative.

### 5.1 Primary chunk: snap-anchored play window

For every `clips` row we form one **play chunk**:

- **Anchor:** the snap event from `events` where
  `event_type = 'snap'` and `clip_id = clip.id`. Fallback: midpoint of
  `(clip.start_time, clip.end_time)` if no snap event exists; flag the
  embedding row `snap_anchor = false`.
- **Window:** `[snap_frame − 30, snap_frame + 90]` at the native FPS
  (typical 60 FPS → 0.5 s pre-snap, 1.5 s post-snap). Clipped to clip
  bounds.
- **Keyframes:** uniformly subsample 8 frames inside the window for the
  visual encoder. 8 frames is the CLIP-style budget that keeps a single
  embedding job inside ~1.5 GB VRAM with ViT-B/32 (well under the 6 GB
  same-session ceiling, though embeddings default to nightly anyway).

### 5.2 Optional sub-chunks (deferred)

We define but do **not** implement two sub-chunks in v1; they have a
documented seam for later:

- **Pre-snap chunk** — `[snap − 60, snap]` for formation/motion search.
- **Post-snap action chunk** — `[snap + 5, snap + 90]` for
  pursuit/coverage execution search.

When added, each becomes its own row in `playembeddings` with a
`chunk_kind` discriminator. v1 stores only `chunk_kind = 'play'`.

---

## 6. Metadata enrichment — what rides with each chunk

Each chunk pulls structured context from the existing pipeline. None of
this requires SAM masks; masks are treated as optional enrichment that
*improves* but does not *gate* embedding generation.

| Source                | Field(s) used                                                                          | Required? |
| --------------------- | -------------------------------------------------------------------------------------- | --------- |
| `clips`               | `label_data` (formation, coverage, personnel), `model_version_id`, opponent/date FKs   | required  |
| `events`              | `event_type='snap'` frame; motion, penalty markers                                     | required  |
| `tracklets`           | per-tracklet `team_label`, `position_group`, `side_of_ball`                            | required  |
| `track_points`        | `field_x`, `field_y` over the chunk window (yards, not pixels)                         | required  |
| `pose_keypoints`      | snap-frame keypoints + `head_yaw_degrees` per offensive skill / defensive back         | required  |
| `labels`              | model-sourced and human-confirmed labels at clip scope                                 | required  |
| `metrics`             | `stride_length`, `hip_flexion`, etc., where `analytics_safe = true`                    | optional  |
| `coach_corrections`   | latest correction per `CorrectionType` is preferred over the model label               | required when present |
| SAM masks (Issue #74) | per-frame instance masks for visual encoder background suppression                     | optional  |

When SAM masks are absent (every same-session clip, and any nightly clip
processed with `ENABLE_SAM3_NIGHTLY=0`), the visual encoder operates on
raw 8-frame stacks. When masks are present, the encoder uses them to
zero out non-player pixels before encoding — same code path as the
SAM-improved jersey crop discussed in `docs/reid-research-note.md`.

This means: **embeddings are functional with bbox-only tracking.** SAM
is a quality multiplier, not a prerequisite.

---

## 7. Baseline embedding approach (before any frontier model)

Goal: a defensible v1 that runs on a single 6–16 GB GPU and produces
embeddings useful enough to validate the retrieval UX. We deliberately
choose mature, open-licensed components.

### 7.1 Visual sub-embedding (512-d → projected to 192-d)

- **Encoder:** OpenAI CLIP ViT-B/32, frozen weights (MIT-equivalent
  license, no fine-tune required for v1).
- **Input:** 8 keyframes per chunk (§5.1), each 224×224. When SAM masks
  are present, background is zeroed before resize.
- **Pooling:** mean over the 8 frame embeddings → 512-d.
- **Projection:** linear projection to 192-d (random init in v1, learned
  later when we have correction-derived positive pairs).

CLIP ViT-B/32 fits comfortably below 1 GB VRAM and processes a 1-second
window in well under a second on the same hardware as `stage_pose`.
This stays inside the issue #76 ceilings without contention with
detect/track/pose stages because `stage_embed` runs nightly.

**Retaining the raw 512-d CLIP image embedding (Issue #195).** The 192-d
projection above is fused into the play vector, but the *unprojected* 512-d
CLIP image embedding — which **is** in CLIP's shared text-image space — is also
persisted, to `playembeddings.clip_vector(512)` (nullable; cosine `ivfflat`
index). A CLIP-aware visual encoder surfaces both in a single forward pass via
`stage_embed.VisualEncoding(projected, clip_image)`; the default
`ZeroVisualEncoder` produces no CLIP embedding, so its rows keep
`clip_vector = NULL` and are simply invisible to text search. This is what lets
`POST /api/v1/search/text` cosine-compare a CLIP text-tower query against the
image embedding (the fused `vector` cannot serve that — its visual half is the
random-init projection in §7.1). Backfill is the existing incremental nightly
re-embed: a re-run upserts `clip_vector` for clips embedded before a real CLIP
encoder was mounted. **No second vector store** is introduced — `clip_vector`
is another column on the existing `playembeddings` table.

### 7.2 Structured sub-embedding (variable → fixed 64-d)

A hand-designed feature vector derived directly from existing tables.
This is the part that makes Football-IQ retrieval distinctive: it is
not opaque CLIP features, it is the labels and tracks coaches already
trust.

Concretely (illustrative, not final shape):

- **Formation one-hot** (from `clips.label_data.formation.generic`).
- **Coverage one-hot** (from `clips.label_data.coverage.generic`).
- **Personnel one-hot** (e.g., "11", "12", "21").
- **Hash mark / field position** (left, middle, right; own/opponent territory).
- **Backfield/skill geometry** at snap: relative offsets in yards for
  QB, RB, slot, X, Z (zero-filled when absent), L2-normalised.
- **Defensive box geometry** at snap: count and avg. x/y of defenders
  within 5 yards of LOS.
- **Pose summary** at snap: mean head_yaw per DB position group; mean
  stance angle per OL position.

Concatenate, then project (linear → ReLU → linear) to a fixed 64-d
vector. The structured sub-embedding is the part that benefits *most*
from coach corrections, because every correction directly edits one of
its inputs.

### 7.3 Fusion → 256-d play embedding

```
play_embedding = L2_norm( concat( visual_192d, structured_64d ) )   # 256-d
```

The 192/64 split deliberately weights the visual side higher in raw
dimensions but the structured side is L2-normalised before concat so
its contribution is balanced in cosine space. We retain both
sub-embeddings as separate columns in `playembeddings` so we can
experiment with re-weighting at query time without re-running the
encoder.

### 7.4 What we explicitly defer

- **Video-native encoders** (VideoMAE, InternVideo, X-CLIP): higher
  quality, but ≥ 3–6 GB VRAM and require warm-up that does not amortise
  over a small nightly batch. Revisit once `playembeddings` has > ~5k
  rows and we have a benchmark set.
- **Contrastive fine-tuning on Toledo data:** depends on accumulating
  positive pairs from coach corrections (same logic as the torchreid
  recommendation in `docs/reid-research-note.md`). Defer to a follow-up
  issue.
- **Natural-language text encoder for `/search/text`:** the endpoint ships
  behind `ENABLE_EMBEDDING_TEXT_SEARCH` and is now wired (Issue #195) — it
  cosine-matches a CLIP text-tower query against the raw `clip_vector(512)`
  (§7.1). The backend gets the query vector either from a deployment-injected
  CLIP text tower or, with **no CLIP weights in the container**, from the
  precomputed concept catalog (`app.concept_catalog`): the query is grounded to
  American-football concepts lexically, then their committed CLIP text vectors
  are averaged. Outputs are always `experimental = true` / `approximate = true`
  and do not promote to labels. The *zero-shot-first* concept search that ships
  for coaches today (Issue #144) does **not** require this path — it grounds on
  structured labels and expands via the image-derived fused embeddings; see
  [`docs/concept-search.md`](concept-search.md).
- **Contrastive fine-tuning of a Football-CLIP on Toledo pairs** remains a
  separate, later optional step gated on corrected-data volume (Issue #195
  non-goal).

---

## 8. Storage — `playembeddings` table

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE playembeddings (
    id                  UUID PRIMARY KEY,
    clip_id             UUID NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    chunk_kind          TEXT NOT NULL DEFAULT 'play',  -- 'play' | 'pre_snap' | 'post_snap'
    snap_anchor         BOOLEAN NOT NULL,              -- false when snap event missing

    -- The retrievable vector
    vector              vector(256) NOT NULL,

    -- Retained sub-embeddings for query-time re-weighting & debugging
    visual_vector       vector(192),
    structured_vector   vector(64),

    -- Raw CLIP image embedding in CLIP shared text-image space (Issue #195).
    -- Nullable: only a real CLIP visual encoder fills it. Powers /search/text.
    clip_vector         vector(512),

    -- Lineage
    model_version_id    UUID NOT NULL REFERENCES model_versions(id),
    calibration_version_id UUID REFERENCES field_calibrations(id),
    source_label_ids    UUID[] NOT NULL DEFAULT '{}',  -- labels read at embed time
    used_sam_masks      BOOLEAN NOT NULL DEFAULT false,

    -- Quality / review
    embedding_confidence REAL,                          -- 0..1; low if snap missing, sparse pose, etc.
    is_experimental      BOOLEAN NOT NULL DEFAULT true, -- flips to false only via coach review path

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (clip_id, chunk_kind, model_version_id)
);

CREATE INDEX playembeddings_vector_ivfflat
    ON playembeddings USING ivfflat (vector vector_cosine_ops) WITH (lists = 100);

-- Cosine ivfflat over the raw CLIP image embedding for /search/text (Issue #195).
CREATE INDEX playembeddings_clip_vector_ivfflat
    ON playembeddings USING ivfflat (clip_vector vector_cosine_ops) WITH (lists = 100);

CREATE INDEX playembeddings_clip_id ON playembeddings (clip_id);
CREATE INDEX playembeddings_model_version_id ON playembeddings (model_version_id);
```

Notes:

- `UNIQUE (clip_id, chunk_kind, model_version_id)` is the upsert key.
  Re-running `stage_embed` with the same model version is a no-op;
  upgrading the encoder produces a new row, not a replacement, so we
  keep version-over-version diffability.
- `source_label_ids` snapshots which `labels` rows fed the structured
  encoder. If a label is later corrected, we can target re-embed only
  the affected rows.
- `is_experimental = true` is the default. The flag flips only when a
  cluster derived from these vectors is reviewed and accepted by a
  coach (see §10).
- Start with `ivfflat`. Switch to `hnsw` when row count exceeds ~50k or
  when recall degrades — both are pgvector built-ins and the switch is
  a single ALTER.

---

## 9. Retrieval — "find me reps like this rep" flow

End-to-end, what happens when a coach taps **Find similar reps** on a
clip in the film room:

```
Coach UI                Backend search router          Postgres + pgvector       Existing clip APIs
   │                            │                              │                          │
   │ POST /search/similar       │                              │                          │
   │ { clip_id, filters }       │                              │                          │
   │───────────────────────────▶│                              │                          │
   │                            │ 1. Load anchor embedding     │                          │
   │                            │    WHERE clip_id = ?         │                          │
   │                            │      AND chunk_kind='play'   │                          │
   │                            │      AND model_version_id    │                          │
   │                            │        = production_embed    │                          │
   │                            │─────────────────────────────▶│                          │
   │                            │◀── vector v (256-d) ─────────│                          │
   │                            │                              │                          │
   │                            │ 2. ORDER BY vector <=> v     │                          │
   │                            │    + WHERE filters           │                          │
   │                            │      (date, opponent,        │                          │
   │                            │       formation, coverage)   │                          │
   │                            │─────────────────────────────▶│                          │
   │                            │◀── top-K clip_ids + scores ──│                          │
   │                            │                              │                          │
   │                            │ 3. Hydrate clip metadata     │                          │
   │                            │    (existing clips router)   │                          │
   │                            │──────────────────────────────────────────────────────▶│
   │                            │◀────── clip cards ──────────────────────────────────── │
   │                            │                              │                          │
   │◀──── ranked clip list ─────│                              │                          │
   │      with similarity score │                              │                          │
   │      + filter facets       │                              │                          │
```

API sketch (lives at `backend/app/routers/search.py`):

```http
POST /api/v1/search/similar
{
  "clip_id": "…",
  "k": 20,
  "filters": {
    "since": "2026-08-01",
    "opponent": "Bowling Green",
    "formation": "trips_right",
    "side_of_ball": "defense"
  },
  "include": ["score", "labels", "thumbnail"]
}

→ 200 OK
{
  "anchor_clip_id": "…",
  "model_version_id": "play-embed-clip-vitb32-baseline@1.0",
  "results": [
    { "clip_id": "…", "score": 0.91, "labels": {…}, "thumbnail_uri": "…" },
    …
  ]
}
```

Filter semantics: filters are Postgres `WHERE` clauses applied *before*
the vector ORDER BY (Postgres can plan this efficiently when the
filter predicate is selective; for low-selectivity filters we apply
them as a re-rank pass on the top-K\*2). The model version used is
explicit in the response so the front-end can warn if it changes
under a coach mid-session.

`POST /search/text` follows the same shape but the request supplies
`{ "query": "trips right cover 3" }`. The router resolves the query into a
CLIP text-space vector (injected CLIP text tower, else the precomputed concept
catalog grounded via the American-football lexicon — Issue #195), then runs the
same cosine search **against `clip_vector(512)`** (not the fused 256-d
`vector`). Rows whose `clip_vector` is NULL are skipped. Results carry
`experimental: true` *and* `approximate: true` in the envelope (plus the
grounded `matched_concept_ids`), and the front-end labels them accordingly. The
surface is gated behind `ENABLE_EMBEDDING_TEXT_SEARCH` (503 when off) and never
falls back to similar-by-clip.

---

## 10. Coach review & promotion — keeping experimental separate from official

This design treats embeddings the same way `docs/label-taxonomy-v1.md`
treats labels: **nothing model-generated becomes an "official" label
without a coach sign-off.**

Three concrete promotion paths:

1. **Per-clip relabel from search.** A coach reviews a `/search/similar`
   result and clicks "tag this rep as Cover 3 like the anchor". This
   creates a `coach_corrections` row of the appropriate
   `CorrectionType` (e.g. `coverage_tag`), and the existing export job
   eventually flips `labels.source` to `'human'` in the next training
   dataset snapshot. Embeddings stay `is_experimental = true` until the
   cluster is reviewed (path 3).

2. **Zero-shot concept discovery.** A nightly job (separate from
   `stage_embed`, runs once per week or on demand) clusters
   `playembeddings.vector` with HDBSCAN. Each cluster surfaces as an
   *experimental* `labels` row with `source = 'model'` and
   `label_type = 'embedding_cluster'`. These are visible only in the
   coach review surface, never in production dashboards.

3. **Cluster promotion.** A coach reviewing a cluster can either:
   - **Accept** it as a real concept → write a `coach_corrections` row
     naming the cluster (e.g. "mesh-like RPO read"); the export job
     creates `labels.source = 'human'` rows for every member; the
     cluster's embeddings flip `is_experimental = false`.
   - **Reject** it → the cluster is hidden from review and the
     experimental labels are not exported.

In both cases the existing `training_datasets.source_correction_ids`
captures the lineage so the next embedding model version can train on
coach-confirmed pairs.

The router contract: `POST /search/text` results and any
`label_type = 'embedding_cluster'` rows are always returned with
`experimental: true`. Production dashboards (self-scout, alerts,
analytics export) filter on `is_experimental = false AND source =
'human'` — they never see raw cluster output.

---

## 11. GPU & scheduling

Per issue #76's ceilings:

| Stage         | Same-session (≤ 6 GB) | Nightly (≤ 16 GB)                                   |
| ------------- | --------------------- | --------------------------------------------------- |
| `embeddings`  | `none` (unchanged)    | `play-embed-clip-vitb32-baseline` (new)             |

Rationale for nightly-only:

- The "find me reps like this" coach flow operates on *previously
  ingested* clips. There is no value in computing an embedding for a
  clip the coach hasn't watched yet, and the period-break 5–10 min
  budget is reserved for detect/track/pose corrections.
- CLIP ViT-B/32 + the structured projector together stay under ~1.5 GB
  VRAM even with an 8-frame batch, so the embed stage cohabitates the
  16 GB nightly bucket comfortably with YOLOv8m + RTMPose-m. SAM 3.1
  (when `ENABLE_SAM3_NIGHTLY=1`) and `stage_embed` are mutually
  exclusive within a single job slot — the scheduler runs SAM-using
  jobs in a separate slot to stay under ceiling.
- Re-embedding is incremental: only clips with a label/correction
  delta since the last embed, or whose `model_version_id` lags
  production, are re-processed.

`model_router.py` change required (out of scope for this design, listed
here for the implementation issue):

```diff
- "embeddings":  {"same_session": "none", "nightly": "none"},
+ "embeddings":  {"same_session": "none", "nightly": "play-embed-clip-vitb32-baseline"},
```

with the variant added to `NIGHTLY_ONLY_VARIANTS` so accidental
same-session promotion is rejected at config load.

---

## 12. Model versioning & rollout

- Each baseline encoder ships as a `model_versions` row with
  `model_type = 'play_embedding'` and `promoted_stage` walking the
  standard `experimental → staging → production → retired` ladder.
- v1 ships at `experimental`. It is allowed to write to
  `playembeddings` but the retrieval router defaults to
  `promoted_stage = 'production' OR 'staging'`. While no production
  version exists, search returns an empty result with
  `reason: "no production embedding model"`.
- Promotion to `staging` requires the cluster-review path above to
  have surfaced at least one accepted concept end-to-end.
- Promotion to `production` requires:
  1. A held-out benchmark set built from coach corrections
     (anchor → known-positive clip pairs).
  2. Recall@10 ≥ 0.6 on that set (placeholder; tune once we have data).
  3. p95 query latency ≤ 300 ms at expected season-scale row count.

---

## 13. Open questions to resolve before implementation

These are the questions to answer in the implementation issue, *not* in
this design:

1. Concrete shape of the structured feature vector — the §7.2 list is
   illustrative; the implementation issue should freeze the schema
   (field order, one-hot vocabularies) and write a fixture-backed
   golden test.
2. Benchmark dataset construction — which corrections constitute
   positive pairs? Same play across opponents? Same call across reps?
3. Pre-snap vs. play vs. post-snap default — start with `play` only,
   but pencil in the eval to decide whether to add `pre_snap`.
4. Backfill policy — at first roll-out, do we embed the entire
   historical corpus in one nightly burst, or only the last N games?
5. Search router auth — should `/search/text` be coach-only while
   `/search/similar` is generally available?

---

## 14. Updates to neighboring docs

When the implementation lands:

- `docs/model-routing.md` — update the routing table row for
  `embeddings` and add a section describing the nightly-only variant
  and its VRAM budget, matching the SAM 3.1 section's style.
- `docs/label-taxonomy-v1.md` — add `embedding_cluster` to the list of
  `label_type` values, noting that it is always experimental until
  promoted via `coach_corrections`.
- Issue #8 acceptance criteria — cross-link this document and narrow
  #8's scope to: (a) implement `stage_embed` with the §7 baseline,
  (b) ship the `playembeddings` migration in §8, (c) ship the
  `/search/similar` router in §9, (d) wire the cluster-review surface
  in §10. Frontier encoders and `/search/text` promotion remain
  follow-ups.

---

*Last updated: 2026-05-26*
