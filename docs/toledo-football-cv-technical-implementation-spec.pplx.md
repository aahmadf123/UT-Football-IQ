# Toledo Football CV Technical Implementation Spec

> **Historical research document — the infrastructure sections are superseded.**
> The schema, pipeline, API, and validation-gate content here still tracks the
> implementation. The hosting and infrastructure sections (Cloudflare Pages /
> Workers / R2 / Queues, Fly.io, Redis/Celery queues) describe a deployment
> that no longer exists in this repo. Football-IQ now ships **no deployment
> configuration at all**: the backend's `processing_jobs` table is the job
> queue, object storage is any S3-compatible endpoint (or local disk), and
> hosting is an open decision. Treat every vendor named below as an option
> that was considered, never as a requirement. See `README.md` for the
> architecture that actually exists.

## Purpose

This document converts the Toledo Football computer vision blueprint into an implementation-ready technical plan. It defines the phased must-haves, database schema, model pipeline, API endpoints, dashboard surfaces, validation gates, MVP sprint plan, and Phase 3 advanced-learning extensions needed to build a budget-conscious football analytics platform from drone and practice film.

The guiding principle is simple: ship a coach-trusted system before chasing frontier research. Every model output must connect to a clip, confidence score, correction workflow, and model version. If a coach cannot verify it, correct it, and teach from it, it should not be treated as a production metric.

## System goals

### Primary goals

- **Turn practice film into structured football data**: Convert long drone videos into play clips, calibrated field coordinates, player tracks, football events, and coach-reviewed labels.
- **Deliver trusted coaching workflows**: Give staff clip-linked dashboards for formation, motion, routes, leverage, effort, practice tempo, self-scout, and player development.
- **Create a Toledo-owned data flywheel**: Capture coach corrections as structured labels that improve future models and preserve Toledo terminology.
- **Build toward individualized athlete-development intelligence**: Add player profiles, pose-lite biomechanics, workload proxies, development goals, coaching notes, best clips, and longitudinal trend views once the tracking foundation is trusted.
- **Enable zero-shot football discovery**: Use zero-shot and open-vocabulary models to find concepts that have not yet been fully labeled, while requiring coach confirmation before labels become official.
- **Stay budget-conscious**: Use burst GPU processing, open-source model stacks, object storage, and staged feature gates instead of building a costly always-on enterprise system.

### Technical principles

- **Evidence-first**: Every number opens the exact video clip, frame range, and overlay that produced it.
- **Confidence-aware**: Every tag and metric carries a confidence score and reason codes for uncertainty.
- **Human-correctable**: Every automated label can be edited by an authorized staff member.
- **Versioned by default**: Every metric is tied to a model version, calibration version, input video, and processing job.
- **Phase-gated**: Advanced analytics do not begin until foundational accuracy and adoption thresholds are met.

## Phased implementation plan with must-haves

### Phase 0: Capture and validation sprint

Phase 0 proves whether Toledo’s film source is good enough for the analytics stack. Do not buy major infrastructure or promise advanced features before this phase passes.

#### Must-haves

| Must-have | Acceptance gate | Owner |
|---|---|---|
| Standard capture protocol | A written protocol exists for drone height, framing, FPS, resolution, exposure, naming, and upload process. | Video lead |
| Evaluation clip set | 50 to 100 representative clips are selected across run, pass, motion, red zone, bad lighting, and crowded box situations. | Analyst lead |
| First label taxonomy | Formation, motion, route, coverage, front, pressure, run concept, pass concept, and event labels are defined in Toledo and generic terms. | Football ops |
| Calibration feasibility | At least 90% of evaluation clips have visible field markings sufficient for field mapping. | CV lead |
| Detection feasibility | Initial detector can find players well enough to support formation and spacing review on most evaluation clips. | ML lead |
| Coach review checkpoint | At least one offensive coach, defensive coach, and analyst review prototype overlays and approve MVP direction. | Project sponsor |

#### Outputs

- Capture protocol v1.
- Toledo label taxonomy v1.
- Ground-truth evaluation set v1.
- Baseline detection and calibration report.
- MVP scope freeze recommendation.

### Phase 1: Foundation MVP

Phase 1 is the first internal product. It should be useful to analysts and selected coaches even if advanced models are not ready.

#### Must-haves

| Must-have | Acceptance gate | Notes |
|---|---|---|
| Video ingestion | Staff can upload or register a full practice video and see processing status. | Object storage plus job queue. |
| Play segmentation | System proposes editable play boundaries for continuous practice film. | Manual override required. |
| Field calibration | System maps pixels to standardized field coordinates and stores calibration confidence. | Suppress precise metrics when confidence is low. |
| Player detection | System produces player bounding boxes or masks per frame. | MVP can begin with team-level tracks before full identity. |
| Player tracking | System creates tracklets with frame ranges, coordinates, and track confidence. | Identity continuity can be uncertain at first. |
| Clip-linked metrics | Every displayed metric links to the exact clip and overlay. | Non-negotiable trust requirement. |
| Coach correction UI | Staff can correct clip boundaries, tags, and player identities. | Corrections become training labels. |
| Model/data versioning | Every output records model version, calibration version, and job ID. | Required for longitudinal comparison. |
| Job observability | Failed jobs show error reason, stage, input file, and retry option. | Prevents silent failure. |
| Role-based access | Coaches, analysts, sports performance, and admins have separate permissions. | Especially important before health data. |

#### Exit criteria

- 90% of reviewed plays have acceptable clip boundaries after correction.
- Formation-family tagging can be reviewed and corrected in the UI.
- Player tracks are good enough for team spacing, motion, and effort metrics on at least 85% of usable clips.
- Coaches can open a metric and reach its evidence clip within two clicks.
- Analysts can export corrected labels for model training.

### Phase 2: Coach-trusted football layer

Phase 2 makes the system valuable for weekly coaching workflows. The key is not full automation; it is fast review, correction, and clip-backed insight.

#### Must-haves

| Must-have | Acceptance gate | Notes |
|---|---|---|
| Formation and motion recognition | Common formations, shifts, and motions are proposed with confidence and editable labels. | Start with high-frequency Toledo formations. |
| Route classification | Eligible receiver tracks receive route proposals and route depth landmarks. | Review-first, not auto-grade-first. |
| Coverage shell and leverage | System proposes press/off, inside/outside leverage, and coverage shell. | Separate observed behavior from inferred call. |
| Practice tempo metrics | Rep count, time between plays, formation speed, and return-to-huddle speed are available by period. | High value and low model complexity. |
| Effort metrics | Sprint-to-ball, downfield blocking participation, and pursuit effort are computed with clips. | Use position-specific definitions. |
| Offensive line spacing | Gap width, pass-set depth, double-team candidates, and second-level release candidates are surfaced. | Requires cautious confidence display. |
| Self-scout exposure dashboard | Formation-to-play, motion-to-play, field-zone, and personnel tendencies are shown. | This is an early strategic differentiator. |
| Correction analytics | Dashboard shows which labels are most often corrected. | Directs model improvement. |

#### Exit criteria

- At least one position group uses the system weekly.
- Self-scout dashboard produces at least three staff-reviewed actionable tendency findings.
- Correction rate for repeated high-frequency labels declines over four weeks.
- Staff can create cutups from model tags and corrected labels.

### Phase 3: Toledo differentiator layer

Phase 3 uses Toledo’s private data advantage: practice film, coach corrections, internal terminology, athlete context, and player development history.

#### Must-haves

| Must-have | Acceptance gate | Notes |
|---|---|---|
| Pose-lite biomechanics | Coarse pose metrics are available for selected positions and drills. | Pad level, torso angle, hip height, stance depth, stride asymmetry. |
| Individualized player profile | Each player has a longitudinal profile with roster bio, role, development goals, best clips, coach notes, corrected metrics, benchmarks, and player-facing summaries. | This becomes the core player-development and recruiting narrative asset. |
| Similar-rep search | Coaches can select a rep and retrieve similar reps by movement, formation, or concept. | Uses embeddings once enough data exists. |
| Zero-shot concept discovery | Coaches can search for football concepts that were not fully pre-labeled, then confirm or reject model suggestions. | Experimental until coach-confirmed. |
| Playbook overlay | Selected Toledo concepts have ideal landmarks/routes for assignment comparison. | Begin with a small concept set. |
| Workload proxy dashboard | Distance, accelerations, high-speed running proxies, and fatigue indicators are visible with confidence caveats. | Not a medical diagnosis. |
| Athlete data governance | Health-related views have role-based permissions and audit logs. | Required before wellness or injury-history joins. |
| Quantum AI/ML experimental track | A sandboxed R&D module tests quantum-inspired or hybrid quantum methods against classical baselines for clustering, similar-rep search, optimization, or scheduling. | Must not block or destabilize the production MVP. |

#### Exit criteria

- Pose-lite metrics are approved by relevant position coaches for at least one position group.
- Individualized player profiles show meaningful trends across multiple practice weeks.
- Similar-rep search returns useful clips in coach review.
- Zero-shot suggestions are reviewed by coaches before becoming official labels.
- Quantum AI/ML experiments show a measurable improvement over a classical baseline before production consideration.
- Sports performance staff approve wording and access rules for workload views.

### Phase 4: Frontier analytics and R&D

Phase 4 should not begin until the platform has reliable tracks, labels, and adoption. These features can create major edge, but they can also damage trust if introduced too early.

#### Must-haves

| Must-have | Acceptance gate | Notes |
|---|---|---|
| Expected-value models | xSep, xYards, xPressure, or xCompletion are benchmarked against simple baselines. | Do not show as grades until validated. |
| Defensive intent modeling | System estimates likely called coverage and flags possible busts with examples. | Must separate “called” from “played.” |
| Counterfactual simulator | Alternative target or play outcomes are shown only in experimental mode. | R&D until proven. |
| Opponent concept matching | Opponent concepts map to Toledo terminology through embeddings and review. | Requires opponent film workflow. |
| Advanced health fusion | CV load joins wellness, S&C, calendar, and injury-history features under governance. | Staff-supervised only. |

#### Exit criteria

- Each expected model beats a transparent baseline.
- Coaches can inspect false positives and false negatives.
- Experimental metrics are clearly labeled and hidden from player-facing views unless approved.

## Technical architecture

### High-level flow

```text
Practice video upload
  -> object storage
  -> ingestion job
  -> play segmentation
  -> field calibration
  -> detection and tracking
  -> identity association
  -> event detection
  -> football label models
  -> metric computation
  -> overlay rendering
  -> dashboard indexing
  -> coach review and correction
  -> training dataset export
  -> nightly model evaluation
```

### Recommended stack

| Layer | Recommended choice | Rationale |
|---|---|---|
| Frontend | Next.js or React | Fast dashboard iteration and video review UX. |
| Backend API | FastAPI or Node/TypeScript | FastAPI fits Python ML teams; Node fits TypeScript full-stack teams. |
| Database | Postgres | Strong relational integrity for clips, labels, corrections, users, and metrics. |
| Object storage | Cloudflare R2, S3-compatible storage, or institutional storage | Low-cost storage for raw video, clips, overlays, and artifacts. |
| Queue | Redis Queue, Celery, BullMQ, or Cloudflare Queues plus worker orchestrator | Needed for long-running jobs. |
| GPU workers | Local workstation first, then burst cloud GPU | Keeps costs controlled. |
| Model registry | MLflow, lightweight internal registry, or versioned database table | Required for reproducibility. |
| Vector search | pgvector first | Avoid adding separate infrastructure until embeddings matter. |
| Auth | University SSO if available, otherwise role-based app auth | Needed for staff/player/health separation. |

### Hosting and deployment architecture

GitHub is not a hosting option for this platform. Use GitHub only for private source control, issue tracking, code review, and CI/CD. The application needs video storage, databases, queues, long-running workers, model artifacts, private player data, and GPU training jobs, so runtime hosting should be separated from the repository.

The preferred architecture is Cloudflare-centered at the edge with separate backend and GPU compute. Cloudflare Pages can host the frontend dashboard, and Cloudflare Workers can handle full-stack routing, lightweight API logic, signed upload/download URLs, access checks, and job submission ([Cloudflare Pages](https://developers.cloudflare.com/pages/), [Cloudflare Workers full-stack applications](https://developers.cloudflare.com/workers/static-assets/routing/full-stack-application/)). Cloudflare R2 is a strong fit for raw video, clips, overlays, model artifacts, and exports because Cloudflare positions R2 as object storage with zero egress fees ([Cloudflare R2](https://www.cloudflare.com/developer-platform/products/r2/)). Cloudflare Queues can buffer and batch work between edge requests and backend/GPU workers ([Cloudflare Queues](https://developers.cloudflare.com/queues/)).

Do not run heavy computer vision processing or model training directly on Cloudflare Workers. Workers have execution limits, including 128 MB memory and paid-plan CPU time that can be raised up to 5 minutes per HTTP request, which is not suitable for long-running GPU video inference or training jobs ([Cloudflare Workers limits](https://developers.cloudflare.com/workers/platform/limits/)). The edge layer should submit jobs, store metadata, and stream results; GPU workers should perform the actual decoding, detection, tracking, pose estimation, training, embedding generation, and overlay rendering.

#### Recommended production split

| Component | Phase 1 recommendation | Phase 2+ recommendation |
|---|---|---|
| Source control | Private GitHub repository | Private GitHub repository with branch protection and CI/CD. |
| Frontend | Cloudflare Pages | Cloudflare Pages. |
| Edge routing | Cloudflare Workers | Cloudflare Workers with role-based checks and signed URLs. |
| Object storage | Cloudflare R2 | Cloudflare R2 with lifecycle rules and separated raw/processed/model buckets. |
| Queue | Cloudflare Queues or Redis queue | Cloudflare Queues for edge dispatch plus Redis/Celery or BullMQ for backend processing. |
| Backend API | Managed app host such as Fly.io, Render, Railway, Google Cloud Run, AWS ECS, or university VM | Containerized backend with private networking to database and worker services. |
| Database | Managed Postgres | Managed Postgres with backups, point-in-time recovery, and restricted access. |
| GPU inference | One local NVIDIA GPU workstation or one cloud GPU instance | Burst GPU workers on RunPod, Modal, Lambda Labs, CoreWeave, or similar GPU provider. |
| Training | Manual or scheduled batch runs | Versioned training jobs with dataset snapshots and model registry promotion. |
| Model registry | Internal table plus artifact storage | MLflow or stronger internal registry when model count grows. |
| Secrets | Cloudflare secrets plus backend secrets manager | Dedicated secrets manager with rotation policy. |

#### Provider decision

Use Cloudflare as the default edge and storage platform unless the university requires another approved cloud. Use a separate GPU provider because GPU platforms such as Modal, RunPod, Lambda Labs, and CoreWeave are designed for AI/ML workloads with GPU access, while Cloudflare should remain the edge, storage, and routing layer ([Modal GPU docs](https://modal.com/docs/guide/gpu), [RunPod Serverless docs](https://docs.runpod.io/serverless/overview), [Lambda Cloud docs](https://docs.lambda.ai/public-cloud/), [CoreWeave docs](https://docs.coreweave.com)).

The recommended Phase 1 deployment is:

```text
Private GitHub repo
  -> CI/CD
  -> Cloudflare Pages frontend
  -> Cloudflare Workers edge API
  -> Cloudflare R2 video/artifact storage
  -> Cloudflare Queue or Redis queue
  -> Backend API service
  -> Managed Postgres
  -> Local or cloud GPU worker
  -> Model registry and artifacts in R2
```

This keeps the expensive part, GPU compute, variable and replaceable. It also keeps the user experience fast because the dashboard, signed URLs, and video delivery sit close to users at the edge.

## Database schema

The schema below is intentionally normalized around clips, jobs, tracks, labels, metrics, corrections, and model versions. The goal is to preserve evidence and model lineage.

### Core identity tables

#### `users`

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | Primary key. |
| `email` | text | Unique. |
| `name` | text | Display name. |
| `role` | enum | `admin`, `analyst`, `coach`, `sports_performance`, `player`, `viewer`. |
| `position_group` | text nullable | Optional coach/player grouping. |
| `created_at` | timestamp | Audit. |
| `last_login_at` | timestamp nullable | Audit. |

#### `players`

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | Primary key. |
| `season` | int | Roster season. |
| `athlete_id_external` | text nullable | Link to athletics system if allowed. |
| `first_name` | text | Player identity. |
| `last_name` | text | Player identity. |
| `jersey_number` | int nullable | Can change by season. |
| `position` | text | WR, CB, QB, etc. |
| `position_group` | text | Skill, OL, DL, LB, DB, QB, ST. |
| `height_in` | int nullable | Useful prior for Re-ID. |
| `weight_lb` | int nullable | Useful prior for Re-ID. |
| `active` | boolean | Roster status. |

#### `player_profiles`

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | Primary key. |
| `player_id` | uuid | Parent player. |
| `season` | int | Profile season. |
| `role_summary` | text nullable | Coach-approved role description. |
| `development_goals` | jsonb | Position-specific goals and target metrics. |
| `coach_notes_private` | text nullable | Restricted to coaches and analysts. |
| `player_summary` | text nullable | Player-facing summary approved by staff. |
| `benchmark_group` | text nullable | Position, class year, or role comparison group. |
| `favorite_clip_ids` | uuid[] | Best teaching or recruiting clips. |
| `restricted_context_flags` | text[] | Flags such as workload, rehab, or medical restrictions. |
| `visibility_status` | enum | `staff_only`, `player_approved`, `recruiting_approved`, `archived`. |
| `updated_by` | uuid | Last editor. |
| `updated_at` | timestamp | Audit. |

#### `player_profile_snapshots`

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | Primary key. |
| `player_profile_id` | uuid | Parent profile. |
| `snapshot_date` | date | Snapshot date. |
| `summary_metrics` | jsonb | Position-specific trend metrics. |
| `strengths` | text[] | Coach-approved strengths. |
| `development_focus` | text[] | Coach-approved improvement areas. |
| `clip_ids` | uuid[] | Evidence clips. |
| `generated_by` | enum | `manual`, `model_assisted`, `imported`. |
| `approved_by` | uuid nullable | Staff approval before player-facing use. |

### Video and clip tables

#### `videos`

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | Primary key. |
| `season` | int | Season. |
| `practice_date` | date | Practice or game date. |
| `session_type` | enum | `practice`, `scrimmage`, `game`, `opponent`, `drill`. |
| `source_type` | enum | `drone`, `endzone`, `sideline`, `broadcast`, `uploaded_clip`. |
| `storage_uri` | text | Raw video URI. |
| `duration_ms` | int | From probe. |
| `fps` | numeric | From probe. |
| `width` | int | Video width. |
| `height` | int | Video height. |
| `codec` | text | Video codec. |
| `uploaded_by` | uuid | `users.id`. |
| `status` | enum | `uploaded`, `queued`, `processing`, `processed`, `failed`. |
| `created_at` | timestamp | Audit. |

#### `clips`

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | Primary key. |
| `video_id` | uuid | Parent video. |
| `clip_index` | int | Play order. |
| `start_ms` | int | Start boundary. |
| `end_ms` | int | End boundary. |
| `period_name` | text nullable | Practice period or quarter. |
| `drill_name` | text nullable | Inside run, 7-on-7, team, etc. |
| `yard_line` | int nullable | Field position if known. |
| `hash` | enum nullable | `left`, `middle`, `right`. |
| `play_direction` | enum nullable | `left`, `right`, `unknown`. |
| `boundary_source` | enum | `model`, `manual`, `imported`. |
| `boundary_confidence` | numeric | 0 to 1. |
| `review_status` | enum | `unreviewed`, `reviewed`, `needs_review`, `approved`. |

### Processing and model lineage tables

#### `processing_jobs`

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | Primary key. |
| `video_id` | uuid nullable | Job may target video. |
| `clip_id` | uuid nullable | Job may target clip. |
| `job_type` | enum | `ingest`, `segment`, `calibrate`, `detect`, `track`, `pose`, `labels`, `metrics`, `render`. |
| `status` | enum | `queued`, `running`, `succeeded`, `failed`, `cancelled`. |
| `priority` | int | Higher for same-session processing. |
| `started_at` | timestamp nullable | Runtime tracking. |
| `finished_at` | timestamp nullable | Runtime tracking. |
| `error_stage` | text nullable | Debug. |
| `error_message` | text nullable | Debug. |
| `input_artifacts` | jsonb | URIs and parameters. |
| `output_artifacts` | jsonb | URIs and result summaries. |

#### `model_versions`

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | Primary key. |
| `model_name` | text | `player_detector`, `route_classifier`, etc. |
| `version` | text | Semantic or git-based version. |
| `model_type` | text | YOLO, RTMPose, transformer, rules, etc. |
| `artifact_uri` | text nullable | Weights or config location. |
| `training_dataset_id` | uuid nullable | Dataset lineage. |
| `metrics` | jsonb | Validation metrics. |
| `created_at` | timestamp | Audit. |
| `promoted_stage` | enum | `experimental`, `staging`, `production`, `retired`. |

### Calibration, tracking, and event tables

#### `field_calibrations`

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | Primary key. |
| `clip_id` | uuid | Parent clip. |
| `job_id` | uuid | Processing job. |
| `model_version_id` | uuid nullable | Calibration model or rules. |
| `homography_matrix` | jsonb | 3x3 matrix. |
| `field_points` | jsonb | Detected yard lines, hash marks, boundaries. |
| `confidence` | numeric | 0 to 1. |
| `reprojection_error` | numeric nullable | Field-coordinate quality. |
| `analytics_safe` | boolean | Whether precise spatial metrics can show. |
| `reason_codes` | text[] | `low_field_visibility`, `motion_blur`, `drone_drift`, etc. |

#### `tracklets`

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | Primary key. |
| `clip_id` | uuid | Parent clip. |
| `track_id_local` | int | Per-clip tracker ID. |
| `player_id` | uuid nullable | Linked after Re-ID or manual correction. |
| `team_side` | enum nullable | `offense`, `defense`, `unknown`. |
| `start_frame` | int | First frame. |
| `end_frame` | int | Last frame. |
| `mean_confidence` | numeric | Tracker confidence. |
| `identity_confidence` | numeric nullable | Re-ID confidence. |
| `source` | enum | `model`, `manual`, `merged`, `split`. |

#### `track_points`

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | Primary key. |
| `tracklet_id` | uuid | Parent tracklet. |
| `frame_index` | int | Frame number. |
| `timestamp_ms` | int | Time within clip. |
| `bbox` | jsonb | x, y, width, height. |
| `mask_uri` | text nullable | Optional segmentation mask. |
| `field_x` | numeric nullable | Calibrated coordinate. |
| `field_y` | numeric nullable | Calibrated coordinate. |
| `speed_yps` | numeric nullable | Yards per second. |
| `accel_yps2` | numeric nullable | Yards per second squared. |
| `confidence` | numeric | Point confidence. |

#### `events`

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | Primary key. |
| `clip_id` | uuid | Parent clip. |
| `event_type` | enum | `snap`, `handoff`, `throw`, `catch`, `contact`, `tackle`, `whistle`, `motion_start`, `motion_end`. |
| `timestamp_ms` | int | Event time. |
| `frame_index` | int | Event frame. |
| `player_id` | uuid nullable | Related player. |
| `tracklet_id` | uuid nullable | Related track. |
| `confidence` | numeric | 0 to 1. |
| `source` | enum | `model`, `manual`, `imported`. |

### Labels, corrections, and metrics tables

#### `labels`

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | Primary key. |
| `clip_id` | uuid | Parent clip. |
| `scope` | enum | `clip`, `team`, `player`, `tracklet`, `event`. |
| `entity_id` | uuid nullable | Player, tracklet, or event ID. |
| `label_type` | enum | `formation`, `motion`, `route`, `coverage`, `front`, `pressure`, `run_concept`, `pass_concept`, `assignment`, `technique`. |
| `generic_label` | text | Portable football label. |
| `toledo_label` | text nullable | Internal terminology. |
| `confidence` | numeric | 0 to 1. |
| `source` | enum | `model`, `manual`, `imported`, `corrected`. |
| `model_version_id` | uuid nullable | Lineage. |
| `review_status` | enum | `unreviewed`, `reviewed`, `approved`, `rejected`. |

#### `coach_corrections`

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | Primary key. |
| `user_id` | uuid | Who corrected. |
| `clip_id` | uuid | Parent clip. |
| `target_table` | text | `labels`, `events`, `tracklets`, `clips`, etc. |
| `target_id` | uuid | Corrected row. |
| `field_name` | text | Corrected field. |
| `old_value` | jsonb | Previous value. |
| `new_value` | jsonb | Corrected value. |
| `correction_reason` | text nullable | Optional reason. |
| `created_at` | timestamp | Audit. |
| `training_eligible` | boolean | Whether label can enter training set. |

#### `metrics`

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | Primary key. |
| `clip_id` | uuid | Parent clip. |
| `player_id` | uuid nullable | Player-level metric. |
| `position_group` | text nullable | Group-level metric. |
| `metric_type` | text | `max_speed`, `cushion`, `separation`, `formation_speed`, `pad_level`, etc. |
| `value` | numeric | Metric value. |
| `unit` | text | yards, seconds, degrees, confidence, count. |
| `time_window` | jsonb nullable | Start/end frame or event-relative window. |
| `confidence` | numeric | 0 to 1. |
| `analytics_safe` | boolean | Whether shown in dashboards. |
| `model_version_id` | uuid nullable | Lineage. |
| `evidence_uri` | text nullable | Overlay or clip URL. |

### Embeddings and search tables

#### `play_embeddings`

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | Primary key. |
| `clip_id` | uuid | Parent clip. |
| `embedding_type` | enum | `trajectory`, `pose`, `concept`, `multimodal`. |
| `embedding` | vector | pgvector column. |
| `model_version_id` | uuid | Embedding model. |
| `created_at` | timestamp | Audit. |

#### `zero_shot_queries`

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | Primary key. |
| `user_id` | uuid | Coach or analyst making the query. |
| `query_text` | text | Example: “find outside-zone-like runs from pistol.” |
| `query_type` | enum | `text`, `example_clip`, `hybrid`. |
| `example_clip_id` | uuid nullable | Used for example-based search. |
| `model_version_id` | uuid | Zero-shot or embedding model. |
| `created_at` | timestamp | Audit. |

#### `zero_shot_results`

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | Primary key. |
| `query_id` | uuid | Parent query. |
| `clip_id` | uuid | Matched clip. |
| `score` | numeric | Similarity or confidence score. |
| `rationale` | text nullable | Model-generated explanation if available. |
| `coach_verdict` | enum | `unreviewed`, `confirmed`, `rejected`, `maybe`. |
| `promoted_label_id` | uuid nullable | Created label if confirmed. |

#### `quantum_experiments`

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | Primary key. |
| `experiment_name` | text | Example: quantum-inspired route clustering. |
| `problem_type` | enum | `clustering`, `similarity_search`, `optimization`, `scheduling`, `feature_selection`. |
| `classical_baseline` | jsonb | Baseline method and score. |
| `quantum_method` | jsonb | Quantum-inspired or hybrid method details. |
| `dataset_description` | text | Dataset and scope. |
| `result_metrics` | jsonb | Accuracy, latency, cost, or quality comparison. |
| `promotion_status` | enum | `research_only`, `candidate`, `rejected`, `production_approved`. |
| `created_at` | timestamp | Audit. |

## Model pipeline specification

### Stage 1: Ingestion

Input: Raw video file.  
Output: `videos` row, probed metadata, storage URI, queued segmentation job.

Implementation details:

- Probe FPS, resolution, codec, duration, and corruption.
- Reject or warn on low-resolution, very low FPS, missing metadata, or unsupported codec.
- Generate thumbnail contact sheet for human review.

### Stage 2: Play segmentation

Input: Long practice video.  
Output: `clips` rows with start/end boundaries and confidence.

Approach:

- Start with simple heuristics: camera stillness, player clustering, huddle break, snap-like movement bursts, and whistle/end-of-play pauses.
- Add manual correction before advanced segmentation.
- Later train a segmentation model using corrected boundaries.

### Stage 3: Field calibration

Input: Clip frames.  
Output: `field_calibrations` row and coordinate transform.

Approach:

- Detect sidelines, yard lines, hash marks, numbers if visible, and end-zone boundaries.
- Estimate homography into standard football field coordinates.
- Store confidence and reason codes.
- Suppress speed, distance, separation, and workload metrics if `analytics_safe=false`.

### Stage 4: Player detection

Input: Clip frames.  
Output: bounding boxes or masks for players, officials, and ball candidates.

Approach:

- MVP: detector trained/fine-tuned on football practice footage.
- Use augmentation for lighting, motion blur, player scale, and directional flips.
- Track detection metrics by source angle and drill type.

### Stage 5: Tracking

Input: detections plus calibrated frames.  
Output: `tracklets` and `track_points`.

Approach:

- Start with ByteTrack or BoT-SORT.
- Use global motion compensation if the camera moves.
- Use track buffers for short occlusions.
- Use manual merge/split tools for track errors.
- Add Re-ID only after enough identity labels exist.

### Stage 6: Identity association

Input: tracklets, roster, jersey OCR candidates, position priors, and corrections.  
Output: `player_id` links and identity confidence.

Approach:

- Start with manual or semi-automated identity assignment by position group and jersey number.
- Add OCR for jersey numbers when view allows.
- Add roster priors: position, height, body size, practice grouping, drill participation.
- Add gait/appearance embeddings later.

### Stage 7: Event detection

Input: tracks, ball candidates, motion patterns, and audio if available.  
Output: `events`.

Initial events:

- Snap.
- Motion start and end.
- Handoff or mesh point.
- Throw.
- Catch or target.
- Contact.
- Tackle/end of play.

Use manual event correction in Phase 1. Event timing drives time-to-throw, separation at arrival, speed at contact, route phase splits, and practice tempo.

### Stage 8: Football labels

Input: tracks, events, field coordinates, roster roles, and context.  
Output: `labels`.

Initial labels:

- Offensive formation.
- Defensive front or shell.
- Motion and shift.
- Route family.
- Coverage shell.
- Press/off.
- Leverage.
- Blitz or pressure candidate.
- Run/pass and run concept family.

### Stage 9: Metric computation

Input: calibrated tracks, events, labels, and confidence.  
Output: `metrics`.

Initial metrics:

- Cushion at snap.
- Separation at throw/catch/arrival.
- Distance traveled.
- Max speed and speed at contact.
- Formation speed.
- Return-to-huddle speed.
- Time to throw.
- Dropback depth.
- Gap width proxy.
- Pass-set depth.
- Pursuit effort.
- Downfield blocking participation.

### Stage 10: Overlay rendering and dashboard indexing

Input: clips, tracks, labels, metrics.  
Output: reviewable video overlays, dashboards, and searchable clips.

Overlays:

- Field coordinate grid.
- Player tracks.
- Formation label.
- Motion arrow.
- Route path.
- Coverage shell.
- Metric callouts.
- Confidence warnings.

### Stage 11: Individualized player profile generation

Input: clips, approved labels, player identities, position-specific metrics, coach notes, and selected evidence clips.  
Output: updated `player_profiles` and `player_profile_snapshots`.

Approach:

- Generate staff-only player profiles first.
- Separate staff-only, player-approved, and recruiting-approved content.
- Use only approved clips and approved metrics in player-facing summaries.
- Include position-specific development goals, trend lines, best reps, teach-tape clips, and improvement areas.
- Store every generated profile snapshot so coaches can compare development over time.

### Stage 12: Zero-shot concept discovery

Input: text query, example clip, labels, tracks, play embeddings, and optional playbook terminology.  
Output: ranked zero-shot search results requiring coach review.

Approach:

- Use zero-shot learning for discovery, not official grading.
- Support text prompts such as “find clips that look like mesh,” “find outside-zone-like runs,” or “find two-high rotation after motion.”
- Support example-based search where a coach selects one clip and asks for similar reps.
- Store every query and result.
- Require coach verdicts before any zero-shot result becomes an official label.
- Feed confirmed and rejected examples into the next supervised training set.

### Stage 13: Quantum AI/ML experimental sandbox

Input: approved embeddings, labels, tracks, and anonymized or restricted datasets when needed.  
Output: `quantum_experiments` records and comparison against classical baselines.

Approach:

- Treat quantum AI/ML as an R&D module in Phase 3, not a dependency of the main product.
- Start with quantum-inspired methods that can run on classical hardware, such as optimization, clustering, feature selection, or high-dimensional similarity search.
- If university research partnerships or cloud quantum credits are available, test hybrid quantum experiments on small, well-defined problems.
- Require a classical baseline for every experiment.
- Promote nothing to production unless it improves quality, speed, cost, or insight clarity over the classical method.

## API specification

The API can be REST first. GraphQL can come later if the dashboard needs flexible querying.

### Auth and users

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/auth/login` | Start login flow. |
| `POST` | `/auth/logout` | End session. |
| `GET` | `/me` | Current user and permissions. |
| `GET` | `/users` | Admin list users. |
| `PATCH` | `/users/{user_id}` | Update role or permissions. |

### Video and job management

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/videos` | Create video record and upload target. |
| `GET` | `/videos` | List videos by date, type, status. |
| `GET` | `/videos/{video_id}` | Video details and processing state. |
| `POST` | `/videos/{video_id}/process` | Queue processing pipeline. |
| `GET` | `/jobs/{job_id}` | Job status, logs, artifacts. |
| `POST` | `/jobs/{job_id}/retry` | Retry failed job. |

### Clips

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/videos/{video_id}/clips` | List clips for video. |
| `POST` | `/clips` | Create manual clip. |
| `GET` | `/clips/{clip_id}` | Clip detail. |
| `PATCH` | `/clips/{clip_id}` | Correct boundary, period, drill, hash, yard line. |
| `GET` | `/clips/{clip_id}/overlay` | Get rendered overlay or overlay config. |

### Tracking and events

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/clips/{clip_id}/tracks` | Tracklets and identity confidence. |
| `PATCH` | `/tracklets/{tracklet_id}` | Assign player, split, merge, or mark unknown. |
| `GET` | `/clips/{clip_id}/events` | Snap, throw, catch, contact, etc. |
| `POST` | `/clips/{clip_id}/events` | Add manual event. |
| `PATCH` | `/events/{event_id}` | Correct event type or timing. |

### Labels and corrections

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/clips/{clip_id}/labels` | Clip labels. |
| `POST` | `/clips/{clip_id}/labels` | Add label. |
| `PATCH` | `/labels/{label_id}` | Correct label. |
| `POST` | `/corrections` | Record correction generically. |
| `GET` | `/corrections` | Review correction history. |

### Metrics and dashboards

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/clips/{clip_id}/metrics` | Metrics for one clip. |
| `GET` | `/players/{player_id}/metrics` | Player metrics over time. |
| `GET` | `/players/{player_id}/profile` | Individualized player profile with goals, clips, notes, and approved metrics. |
| `PATCH` | `/players/{player_id}/profile` | Update profile goals, notes, visibility, or approved summaries. |
| `POST` | `/players/{player_id}/profile/snapshots` | Generate or save a profile snapshot. |
| `GET` | `/players/{player_id}/profile/snapshots` | Review historical profile snapshots. |
| `GET` | `/dashboards/practice-tempo` | Rep count, tempo, formation speed, return-to-huddle. |
| `GET` | `/dashboards/self-scout` | Formation, motion, field-zone, personnel tendencies. |
| `GET` | `/dashboards/player-development` | Longitudinal player trends. |
| `GET` | `/dashboards/model-quality` | Accuracy, correction rates, drift, failed jobs. |

### Search

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/search/clips` | Filter by labels, player, date, metric, situation. |
| `POST` | `/search/similar-reps` | Find similar plays to a selected clip. |
| `POST` | `/search/zero-shot` | Run text or example-based zero-shot concept discovery. |
| `GET` | `/search/zero-shot/{query_id}` | Retrieve zero-shot results and review status. |
| `PATCH` | `/search/zero-shot/results/{result_id}` | Confirm, reject, or promote a zero-shot result into a label. |
| `POST` | `/cutups` | Create cutup from search result clips. |
| `GET` | `/cutups/{cutup_id}` | View/export cutup. |

### Quantum AI/ML experiments

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/experiments/quantum` | Create a quantum-inspired or hybrid quantum experiment. |
| `GET` | `/experiments/quantum` | List experiments and promotion status. |
| `GET` | `/experiments/quantum/{experiment_id}` | Review method, baseline, results, and artifacts. |
| `PATCH` | `/experiments/quantum/{experiment_id}` | Update promotion status after review. |

## Dashboard surfaces

### Practice inbox

Must show:

- Uploaded videos.
- Processing status.
- Failed stage and retry.
- Number of detected clips.
- Calibration-safe percentage.
- Clips needing review.

### Clip review

Must show:

- Video player.
- Overlays.
- Track list.
- Labels.
- Confidence warnings.
- Correction controls.
- Export button.
- Evidence links for every metric.

### Self-scout

Must show:

- Formation-to-play tendency.
- Motion-to-play tendency.
- Field-zone tendency.
- Personnel tendency if personnel labels exist.
- Pre-snap tell flags.
- Clip list behind every chart cell.

### Position group dashboard

Must show:

- WR/DB: separation, cushion, leverage, route labels, press/off, stack wins.
- OL/DL: pass-set depth, gap width, double-team candidates, block shed timing proxies.
- QB: time to throw, dropback depth, target location, release field position.
- RB: speed at contact, cutback timing proxy, pre-contact and post-contact proxy.
- Team: effort, tempo, sprint-to-ball, formation speed.

### Individualized player profile

Must show:

- Roster bio, jersey, position, role, and season.
- Development goals and coach-approved benchmarks.
- Best teaching clips and recent improvement clips.
- Position-specific trend lines.
- Corrected model outputs that were approved for profile use.
- Staff-only notes and player-facing summaries as separate sections.
- Visibility controls for staff-only, player-approved, and recruiting-approved views.
- Workload or health-adjacent context only when the user has permission.

### Zero-shot discovery workspace

Must show:

- Text query box for football concepts.
- Example-clip search option.
- Ranked results with similarity score and rationale.
- Side-by-side clip review.
- Coach verdict controls: confirmed, rejected, maybe.
- Promotion button to convert confirmed result into official label.
- List of zero-shot queries that produced useful training examples.

### Quantum AI/ML R&D workspace

Must show:

- Experiment name, problem type, dataset, and owner.
- Classical baseline method and score.
- Quantum-inspired or hybrid quantum method and score.
- Cost, runtime, and quality comparison.
- Promotion status.
- Clear warning that research-only outputs are not coach-facing production metrics.

### Model quality dashboard

Must show:

- Detection precision/recall sample results.
- Track fragmentation.
- Identity correction rate.
- Label correction rate by type.
- Calibration failure reasons.
- Model version comparison.
- Weekly drift warnings.

## MVP sprint plan

Assume two-week sprints. A smaller team can stretch this timeline; a larger team can parallelize frontend, backend, and ML work.

### Sprint 1: Project scaffold and capture protocol

Must-haves:

- Repo and environment setup.
- Database migrations for `users`, `players`, `videos`, `clips`, `processing_jobs`, and `model_versions`.
- Object storage bucket structure.
- Capture protocol v1.
- Toledo label taxonomy v1.
- Upload video manually through admin UI or CLI.

### Sprint 2: Ingestion and job system

Must-haves:

- Video metadata probing.
- Job queue.
- Job status UI.
- Retry failed job.
- Store raw video and thumbnails.
- Basic role-based access.

### Sprint 3: Play segmentation MVP

Must-haves:

- Candidate clip generation.
- Clip review UI.
- Manual boundary correction.
- `coach_corrections` table and correction logging.
- Export clips list.

### Sprint 4: Field calibration MVP

Must-haves:

- Field-line detection prototype.
- Homography storage.
- Calibration confidence and reason codes.
- Analytics-safe flag.
- Field grid overlay in clip review.

### Sprint 5: Player detection and tracking MVP

Must-haves:

- Baseline player detector.
- Tracking with ByteTrack or BoT-SORT.
- `tracklets` and `track_points` storage.
- Track overlay.
- Manual player assignment or unknown marking.

### Sprint 6: Core metrics and event markers

Must-haves:

- Manual snap marker.
- Motion start/end marker.
- Basic speed, distance, cushion, and separation metrics when calibration-safe.
- Evidence links from metric to clip.
- Metric suppression when confidence is low.

### Sprint 7: Formation and motion labels

Must-haves:

- Manual labels for formation and motion.
- First model or rules-based proposal.
- Confidence display.
- Correction loop.
- Dashboard for formation and motion tendency.

### Sprint 8: Coach-ready MVP review

Must-haves:

- Practice inbox.
- Clip review.
- Self-scout v1.
- Model quality dashboard v1.
- Export cutups or clip lists.
- Coach review session.
- Written Phase 1 exit report.

### Sprint 9: Route, coverage, and effort expansion

Must-haves:

- Route proposals for selected concepts.
- Press/off and leverage labels.
- Effort metrics.
- Position group dashboard v1.
- Correction analytics by label type.

### Sprint 10: Pose-lite pilot

Must-haves:

- Pose extraction on selected drills or high-quality clips.
- Pad level or torso angle metric for one position group.
- Coach review and approval process.
- Pose metrics hidden behind experimental flag until approved.

### Sprint 11: Individualized player profile pilot

Must-haves:

- `player_profiles` and `player_profile_snapshots` tables implemented.
- Player profile page for one position group.
- Development goals, coach notes, best clips, and trend metrics.
- Staff-only and player-approved visibility modes.
- At least five player profiles reviewed by position coach.

### Sprint 12: Zero-shot discovery pilot

Must-haves:

- Text-based concept search.
- Example-clip similar-rep search.
- Zero-shot result review workflow.
- Confirm/reject/maybe verdict buttons.
- Promotion from confirmed zero-shot result into official label.
- Export confirmed and rejected examples into training dataset.

### Sprint 13: Quantum AI/ML research sandbox

Must-haves:

- `quantum_experiments` table and experiment dashboard.
- At least one quantum-inspired clustering, optimization, or similarity-search experiment.
- Classical baseline recorded before the quantum-style method is evaluated.
- Production guardrail that prevents research-only outputs from appearing in coach-facing dashboards.
- Written decision on whether the method is rejected, kept as research, or advanced to candidate status.

## Testing and validation

### Unit tests

- Coordinate transformation utilities.
- Metric calculations.
- API permission checks.
- Label taxonomy validation.
- Correction logging.

### Integration tests

- Upload video -> process -> create clips -> correct clip -> save correction.
- Clip -> calibration -> track -> metric -> dashboard.
- Label correction -> training export.
- Failed job -> retry -> success.

### Model validation

Maintain a fixed validation set across the season. Report:

- Player detection precision, recall, and mAP.
- Tracking ID switches.
- Track fragmentation.
- Calibration error.
- Formation accuracy.
- Motion accuracy.
- Route accuracy.
- Coverage/leverage accuracy.
- Event timing error.
- Correction rate by label type.

### Football validation

Run weekly coach review:

- 10 offensive clips.
- 10 defensive clips.
- 5 special or edge-case clips.
- 5 low-confidence clips.

The weekly review should answer:

- Was the output useful?
- Was it correct enough to trust?
- Did the confidence warnings match human concern?
- Which corrections should become model-training examples?
- Which metrics should remain hidden?

## Data governance and access

### Roles

| Role | Access |
|---|---|
| Admin | Full system access, user management, data export. |
| Analyst | Video, clips, labels, corrections, dashboards, model quality. |
| Coach | Team and position dashboards, clips, corrections for assigned scope. |
| Sports performance | Workload and approved health-adjacent metrics. |
| Player | Approved player-facing development clips and metrics only. |
| Viewer | Read-only access to approved dashboards. |

### Health-related data rules

- Do not show injury-risk language to players unless approved by sports medicine.
- Store health joins in separate restricted tables if integrated later.
- Audit every access to health-adjacent dashboards.
- Use “workload signal,” “movement trend,” or “staff review flag,” not diagnostic language.

## Procurement guidance

### Buy only after Phase 0

Do not purchase major hardware until the evaluation clips prove that capture is good enough.

### Minimum viable hardware

- One high-quality drone or elevated camera setup already approved by athletics operations.
- One workstation with a modern NVIDIA GPU for early experimentation.
- External SSD or NAS for local staging.
- Cloud object storage for durable archive.

### Single-camera scope boundary

Football-IQ is explicitly scoped to one elevated capture source (`DRONEA`) for this phase. Multi-camera capture and camera switching are out of scope until a future product decision changes the core pipeline.

## Immediate build checklist

1. Approve the Phase 0 capture protocol.
2. Build the first Toledo label taxonomy.
3. Select 50 to 100 evaluation clips.
4. Scaffold database and object storage.
5. Build video ingestion and job status.
6. Build clip review before advanced models.
7. Add calibration and tracking prototypes.
8. Add correction logging.
9. Review with coaches.
10. Define individualized player profile fields with position coaches.
11. Decide which labels and metrics are allowed in player-facing views.
12. Define zero-shot discovery prompts that coaches actually want to test.
13. Identify one narrow quantum AI/ML experiment with a classical baseline.
14. Freeze Phase 1 scope based on actual film quality and coach feedback.

## Definition of success

The MVP succeeds if Toledo staff can upload practice film, get editable play clips, see calibrated player tracks and basic metrics, correct model outputs, and use the resulting clip-linked dashboards in a real football workflow. The advanced version succeeds only if the system compounds: every practice improves the dataset, every correction improves the model, every player accumulates a richer individualized development profile, zero-shot discovery helps staff find useful concepts faster, and quantum AI/ML experiments prove value against classical baselines before they touch production workflows.
