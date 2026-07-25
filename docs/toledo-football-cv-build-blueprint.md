# Toledo Football Computer Vision Build Blueprint

> **Historical research document — the hosting sections are superseded.**
> This blueprint captures the original product research and remains a good
> reference for *what to build* and *in what order*. Its infrastructure
> recommendations (Cloudflare Pages / Workers / R2 / Queues, Fly.io for the
> backend) describe a deployment that no longer exists in this repo and that
> nothing here is wired to. Football-IQ now ships **no deployment
> configuration at all**: the backend's `processing_jobs` table is the job
> queue, object storage is any S3-compatible endpoint (or local disk), and
> hosting is an open decision. Treat every vendor named below as an option
> that was considered, never as a requirement. See `README.md` for the
> architecture that actually exists.

## Executive summary

Toledo should build this as a coach-trusted football intelligence system, not as a generic computer vision demo. The attached plans already aim high with drone-based overhead capture, homography, player tracking, pose-based biomechanics, Re-ID, play embeddings, self-scouting, opponent scouting, health fusion, and same-session feedback. The critical next step is to stage those ideas into a system that can earn trust quickly on a small-school budget.

The correct first product is a narrow but reliable practice-film platform that turns overhead drone video into per-play clips, player coordinates, basic speed and spacing metrics, formation and motion tags, and coach-correctable labels. That foundation should then expand into route and coverage analysis, offensive line spacing, play embeddings, technique metrics, athlete development trends, and eventually counterfactual decision support.

The biggest risk is trying to ship the whole frontier at once. Hudl IQ already claims AI and computer vision capabilities for formations, routes, coverage, blitzes, player locations at 30 frames per second, pass placement, catch radius, expected completion, CPOE, CROE, and EPA-style models, so Toledo cannot win by copying the same commercial feature list at lower reliability ([Hudl IQ](https://www.hudl.com/products/football-iq)). Toledo can win by focusing on three local advantages: overhead practice footage, coach-correction loops tied to Toledo terminology, and integration with existing athlete health and injury-risk work.

The best differentiator is the “Toledo Coach-in-the-Loop Flywheel”: every metric links to the exact clip, every wrong tag can be fixed by a coach or analyst in seconds, every correction becomes training data overnight, and every player receives a longitudinal development profile. This is harder for outside vendors to replicate because it depends on Toledo’s practice film, internal playbook language, coaching preferences, and athlete-management data.

## Competitive benchmark

### What the market already does

Hudl IQ is the closest public benchmark for college and pro football video intelligence. Hudl says its product is powered by AI, computer vision, and expert data collectors, and that it automatically identifies formations, routes, coverage, and blitzes while collecting exact player locations and movements at frame rate from football footage ([Hudl IQ](https://www.hudl.com/products/football-iq)). Hudl also advertises game-planning visuals, route tendency models, offensive-line pressure and run tendencies, expected completion, CPOE, CROE, and EPA models, which means a Toledo system must either be cheaper, faster for practice feedback, more customized to Toledo, or more deeply integrated into player development than generic market tooling ([Hudl IQ](https://www.hudl.com/products/football-iq)).

Catapult’s Pro Video benchmark is less about custom CV models and more about operational workflow. Catapult emphasizes multi-angle capture, synchronized live streaming, data-to-video integration, no-code analysis workflows, cloud sharing, and presentation tools for coaching delivery ([Catapult Pro Video](https://www.catapult.com/solutions/pro-video)). This matters because Toledo’s platform must not stop at model outputs; the staff must be able to view, verify, cut up, share, and teach from the output as quickly as they already use film.

The NFL and AWS Digital Athlete shows the long-term frontier. AWS describes an NFL system that combines Next Gen Stats, video, equipment and stadium data, weather, play type, and player tracking to model performance and injury risk ([AWS](https://aws.amazon.com/blogs/media/building-a-digital-athlete-using-ai-to-rewrite-the-playbook-on-nfl-player-safety/)). AWS says the NFL system can use 38 cameras capturing 5K video at 60 frames per second, identify player cores and extremities, build virtual skeletons, process roughly 6.8 million frames and about 100 million player positions in a game week, and process over 500 million practice tracking datapoints per week ([AWS](https://aws.amazon.com/blogs/media/building-a-digital-athlete-using-ai-to-rewrite-the-playbook-on-nfl-player-safety/)). Toledo should treat that as a direction of travel, not as a near-term infrastructure target.

### What research says is feasible

Overhead formation recognition is feasible, but it depends on data quality and dataset growth. Newman et al. reported more than 90% accuracy for player detection and labeling and 84.8% accuracy for formation identification from pre-play American football imagery, while noting that larger real-world datasets are needed for further improvement ([Automated Pre-Play Analysis of American Football Formations Using Deep Learning](https://www.mdpi.com/2079-9292/12/3/726)). That supports making formation and alignment recognition an early product area, provided Toledo builds a correction and labeling loop from day one.

Modern pose estimation is viable enough for a staged sports biomechanics product. RTMPose reports 75.8% average precision on COCO for RTMPose-m with flipping, and its deployment tests report 430+ FPS on an NVIDIA GTX 1660 Ti GPU for RTMPose-m, giving a credible open-source basis for real-time or near-real-time pose extraction if the camera angle and player scale are adequate ([RTMPose](https://arxiv.org/html/2303.07399v2)). The first Toledo pose product should focus on reliable, coarse measures such as pad level, torso angle, hip height, stride symmetry, and stance, rather than trying to infer every fine-grained joint outcome immediately.

Player tracking in football remains hard because of occlusion, similar uniforms, and players leaving the frame. A SAM plus CSRT tracking paper reports 100% tracking success in light occlusion, 90% success in heavy 5+ player crowding, 7.6 to 7.7 FPS, and around 1.88 GB memory usage, but it drops to 8.66% tracking success when a player is off-screen for longer-term visibility loss ([Team-Aware Football Player Tracking with SAM](https://arxiv.org/html/2512.08467v1)). This means Toledo should not promise perfect identity continuity at first; it should build explicit uncertainty, coach correction, and track-stitching workflows.

A practical football tracking MVP can start with common detection and tracking patterns. Roboflow’s 2026 football tracking example uses RF-DETR Small with ByteTrack and reports mAP@50 of 74.8%, F1 of 77.9%, precision of 90.8%, and recall of 71.5%, with dense scrums still challenging recall ([Roboflow](https://blog.roboflow.com/american-football-player-tracker/)). Ultralytics YOLO supports BoT-SORT and ByteTrack for video tracking, with configuration options for lost-track buffers, global motion compensation, and Re-ID in BoT-SORT ([Ultralytics](https://docs.ultralytics.com/modes/track/)). The lesson is that Toledo can prototype quickly with off-the-shelf components, but the final value will come from football-specific calibration, labels, and evaluation.

## Product north star

### Mission

Build a Toledo-owned computer vision platform that converts drone and practice film into verified football intelligence: faster coaching feedback, better self-scout, better opponent prep, objective player development, and health-aware workload context.

### Core users

- **Head coach and coordinators**: Need fast, trusted answers on tendencies, execution, practice efficiency, and opponent plans.
- **Position coaches**: Need clip-linked metrics they can teach from, such as route precision, separation, pad level, pass-set depth, release timing, and pursuit effort.
- **Analysts and GAs**: Need automated tagging, bulk review, coach correction tools, and exports into existing football workflows.
- **Strength, conditioning, and sports medicine staff**: Need workload proxies, fatigue signals, asymmetry trends, and health-context overlays.
- **Players**: Need simple development views that show objective progress without overwhelming them.
- **Recruiting staff**: Need player-development proof points, not raw model outputs.

### Non-goals

- **Do not build a full Hudl replacement in year one**: Keep Hudl or existing film systems as the source of truth for routine film exchange and team workflow until Toledo’s platform proves superior in selected workflows.
- **Do not promise automated grading with no human review**: Coaches will reject metrics that cannot be verified against clips.
- **Do not chase true eye tracking from drone footage**: Use head orientation and body orientation as proxies, and label the confidence accordingly.
- **Do not treat injury prediction as a medical diagnostic product**: Use CV features as workload and movement-risk signals that support staff judgment.
- **Do not depend on one expensive always-on GPU stack**: Batch and burst processing should keep GPU costs variable and controllable.

## Build strategy

### The recommended product wedge

The first release should be “Practice Intelligence from Drone Film.” It should take a continuous overhead practice video, split it into plays, map the field, track players, identify formations and motion, calculate basic spacing and effort metrics, and deliver clip-linked review pages within the same day. If same-day becomes reliable, then same-session feedback can be piloted for selected periods.

This wedge is better than starting with counterfactual models or quarterback physics because it builds the data spine. Every advanced feature depends on accurate clips, field coordinates, player tracks, labels, model versions, and coach trust. Once that spine works, Toledo can layer better analytics on top without rebuilding the system.

### The trust contract

Every output should show four things:

- **Metric**: The value, tag, or model output.
- **Evidence**: The clip, frame range, overlay, and player track that produced it.
- **Confidence**: The model confidence and any known risk, such as occlusion, weak calibration, or missing jersey number.
- **Correction path**: A one-click way for staff to correct the tag, identity, play boundary, or concept.

Without this trust contract, the platform will look impressive but fail adoption. With it, even imperfect models become useful because the system improves from staff feedback.

## MVP scope

### P0 requirements

| Requirement | Acceptance criteria | Why it matters |
|---|---|---|
| Video ingestion | Given a full practice upload, when processing starts, then the system creates a job, validates video metadata, and stores the source file. | Prevents lost film and broken runs. |
| Play segmentation | Given a continuous practice video, when the job completes, then the system proposes per-play clips with editable boundaries. | Coaches think in plays, not long videos. |
| Field calibration | Given a drone or overhead clip, when yard lines and field boundaries are visible, then the system maps pixels to standardized field coordinates and reports calibration confidence. | Every distance, speed, and separation metric depends on this. |
| Player detection and tracking | Given a clip, when players are visible, then the system creates track IDs, bounding boxes or masks, and x/y positions per frame. | Foundation for every downstream model. |
| Coach correction UI | Given a wrong identity, play boundary, formation, or route tag, when staff corrects it, then the correction is saved as a labeled training example. | Creates Toledo’s private data advantage. |
| Clip-linked metrics | Given any metric, when a coach clicks it, then the exact clip and overlay open within two clicks. | Coaches will not trust black-box analytics. |
| Model and data versioning | Given a metric from Week 3 and Week 10, when comparing them, then the system records which model and calibration version produced each value. | Prevents misleading longitudinal trends. |

### P1 requirements

| Requirement | Acceptance criteria | Why it matters |
|---|---|---|
| Formation and motion tags | Given pre-snap frames, the system labels common offensive formations, defensive structure, motion, and shifts with confidence scores. | Strong early coaching value. |
| Basic route classification | Given eligible receiver tracks, the system proposes route labels and route depth landmarks for review. | Supports self-scout and WR coaching. |
| Coverage shell and leverage | Given defender alignment and movement, the system proposes man/zone, press/off, inside/outside leverage, and coverage shell. | Supports offensive and defensive game planning. |
| Effort and practice-tempo metrics | Given player tracks, the system calculates sprint-to-ball, formation speed, return-to-huddle speed, and downfield blocking participation. | High coach adoption for accountability. |
| Pose-lite biomechanics | Given usable player scale and camera angle, the system calculates coarse pose features such as torso angle, hip height, pad level, stride asymmetry, and stance depth. | Differentiates Toledo from tag-only systems. |
| Self-scout exposure dashboard | Given Toledo’s own practices and games, the system shows pre-snap tells and formation-to-play tendencies. | Helps staff break predictable patterns. |

### P2 requirements

| Requirement | Acceptance criteria | Why it matters |
|---|---|---|
| Learned play embeddings | Given a play, the system finds similar reps even when labels differ. | Creates discovery and opponent concept matching. |
| Expected-value models | Given route, coverage, leverage, cushion, and situation, the system estimates xSep, xYards, xPressure, or xCompletion. | Enables objective grading beyond raw outcomes. |
| Defensive intent modeling | Given full-team movement, the system estimates likely coverage call and flags likely busts. | Valuable but requires more labeled data. |
| Counterfactual decision support | Given a play, the system estimates plausible alternate outcomes. | High upside, but should wait until tracking and labels are strong. |
| Health fusion | Given CV load, wellness, strength, injury history, and calendar context, the system supports individualized workload review. | Powerful differentiator when governed carefully. |

## Differentiating features Toledo should add

### Calibration confidence score

Every clip should receive an “analytics-safe” score before metrics are shown. The score should combine field-line visibility, homography stability, player scale, motion blur, drone altitude stability, and occlusion density. If calibration is weak, the platform should still show film and tags but suppress precise speed and separation claims.

### Coach correction flywheel

Build the correction interface before building advanced models. Coaches and analysts should correct formation, motion, route, coverage, player identity, play boundary, and assignment labels in the same screen where they watch the clip. The nightly pipeline should promote high-confidence corrections into training datasets and regression tests.

### Toledo terminology layer

The platform should store generic football labels and Toledo-specific labels separately. For example, an opponent concept might be generically classified as “mesh,” while Toledo terminology maps it to the staff’s internal call family. This keeps the machine learning portable while making the product feel native to coaches.

### Self-scout exposure index

This feature should show what Toledo is unintentionally giving away. It can score formation-to-play tendency, motion-to-play tendency, field-zone tendency, personnel tendency, and pre-snap stance tells. This is a practical edge because it helps the staff break tendencies before opponents exploit them.

### Practice efficiency dashboard

Small programs need to maximize every rep. Track time between plays, period duration, rep count per position group, formation speed, substitution delays, and return-to-huddle speed. These are easier to compute than advanced biomechanics and can change practice behavior quickly.

### Player development passport

Each player should get a private, longitudinal profile with position-specific metrics, best clips, improvement charts, and coach-approved notes. For recruiting and development, this can become a powerful internal asset that shows measurable growth over time.

### Individualized player profile

The player development passport should become a full individualized player profile, not just a stats page. Each profile should include roster bio, position group, role, development goals, coach notes, best teaching clips, corrected model outputs, position-specific benchmarks, injury/workload permissions, trend lines, and player-facing summaries. The staff version can include deeper analytics and restricted workload context, while the player-facing version should show only coach-approved development clips and simple progress indicators.

### Zero-shot football concept discovery

Add zero-shot learning as an advanced model strategy so Toledo can search for and classify concepts that were not fully labeled in the original dataset. The first use case should not be fully automated grading; it should be assisted discovery, such as “find clips that look like mesh,” “find outside-zone-like runs,” “find two-high rotations,” or “find reps similar to this opponent concept.” Zero-shot outputs should always be marked experimental until a coach confirms the label, and every confirmed or rejected result should become training data.

### “Find me reps like this”

Learned play embeddings should power a search box where coaches can select one clip and find similar reps by movement, spacing, formation, or route concept. This should work even when labels are missing or inconsistent. The NFL Big Data Bowl uses Next Gen Stats data for player movement prediction tasks, including 2023 and 2024 seasons and evaluation against later outcomes, which supports using public tracking data as a benchmark source for movement modeling ideas ([NFL Football Operations](https://operations.nfl.com/gameday/analytics/big-data-bowl/)).

## Technical architecture

### Hosting recommendation

The recommended hosting model is a hybrid Cloudflare-centered architecture, not GitHub hosting. GitHub should be used for private source control, issue tracking, CI/CD, and code review only. It should not host the application runtime, video archive, databases, model artifacts, training jobs, or player data.

Cloudflare is a strong fit for the edge-facing parts of the system: frontend hosting, access control, routing, CDN, object storage, queues, and lightweight orchestration. Cloudflare Pages is designed for deploying frontend applications to Cloudflare’s global network, while Cloudflare Workers supports full-stack application routing and server-side logic at the edge ([Cloudflare Pages](https://developers.cloudflare.com/pages/), [Cloudflare Workers full-stack applications](https://developers.cloudflare.com/workers/static-assets/routing/full-stack-application/)). Cloudflare Queues can buffer or batch work between Workers and downstream services, which fits video-processing job dispatch and webhook-style workflows ([Cloudflare Queues](https://developers.cloudflare.com/queues/)).

Cloudflare should not be treated as the whole compute platform for model training or heavy video inference. Workers have memory and CPU-time limits, including 128 MB memory and a paid-plan CPU-time limit that can be raised up to 5 minutes per HTTP request, which is not appropriate for long-running GPU training or large-scale video inference ([Cloudflare Workers limits](https://developers.cloudflare.com/workers/platform/limits/)). The production design should therefore use Cloudflare for edge, storage, and queueing, then dispatch heavy jobs to GPU workers on a local workstation, RunPod, Modal, Lambda Labs, CoreWeave, or similar GPU infrastructure.

Recommended deployment split:

| Component | Recommended host | Reason |
|---|---|---|
| Frontend app | Cloudflare Pages | Fast global hosting for the dashboard and review UI. |
| Edge API/routing | Cloudflare Workers | Auth checks, signed URLs, job submission, lightweight API routing. |
| Raw video and artifacts | Cloudflare R2 | Low-cost object storage for raw video, clips, overlays, model artifacts, and exports. |
| Queue | Cloudflare Queues or Redis-backed queue | Buffers processing jobs and prevents uploads from blocking on ML work. |
| Main backend API | Fly.io, Render, Railway, Google Cloud Run, AWS ECS, or university-hosted VM | Better fit for long-lived API processes, database connections, and internal services. |
| Database | Managed Postgres such as Neon, Supabase, Railway, AWS RDS, Cloud SQL, or university-hosted Postgres | Relational store for clips, labels, metrics, corrections, profiles, and permissions. |
| GPU inference | Local GPU workstation first; then RunPod, Modal, Lambda Labs, CoreWeave, or similar | Heavy CV jobs need GPU access and longer runtimes. |
| Training jobs | Local or cloud GPU batch jobs | Training should be scheduled, monitored, and versioned outside the edge runtime. |
| Model registry | MLflow or internal registry on backend storage | Tracks model versions, datasets, metrics, and promotion status. |

The best Phase 1 choice is likely Cloudflare Pages + Workers + R2 for the user-facing edge, a managed Postgres database, and one controlled GPU worker for processing. As usage grows, Toledo can add burst GPU providers for busy weeks or same-session processing without changing the core product architecture.

### Capture layer

Start with stable overhead practice capture. The preferred setup is one high, wide drone or elevated tactical camera that keeps all 22 players and field markings visible. Multi-camera capture is out of scope for the current Football-IQ product.

Minimum capture standards:

- 4K where possible, 60 FPS preferred, 30 FPS acceptable for initial tagging.
- Manual or locked exposure when lighting is stable.
- Consistent altitude and field framing.
- Field markings visible throughout the play.
- Standard naming convention for practice date, period, drill, and fixed source tag (`DRONEA`).

### Data layer

Use object storage for raw video, processed clips, overlays, and HLS outputs. Cloudflare R2 is well aligned for low-cost storage and distribution because Cloudflare positions R2 as object storage without the costly egress bandwidth fees associated with typical cloud storage services ([Cloudflare R2](https://developers.cloudflare.com/r2/)). Store relational metadata in Postgres and high-dimensional play embeddings in a vector database or Postgres extension once the product reaches embeddings.

Core tables should include:

- `videos`: raw source files, practice metadata, upload source, and processing status.
- `clips`: play boundaries, period, drill, hash, yard line, and confidence.
- `tracks`: player track IDs, frame ranges, coordinates, confidence, and identity status.
- `events`: snap, handoff, throw, catch, contact, tackle, whistle, and manual markers.
- `labels`: formation, motion, route, coverage, pressure, run concept, pass concept, and correction source.
- `metrics`: speed, separation, cushion, distance, workload, pad level, and model version.
- `model_runs`: model name, version, parameters, input data, output artifacts, and QA status.
- `coach_corrections`: the most valuable training data in the system.

### Processing layer

The pipeline should run in stages:

1. Ingest video and validate metadata.
2. Segment the video into candidate plays.
3. Detect field lines and estimate homography.
4. Detect players, ball when possible, officials, and relevant equipment.
5. Track players and create tracklets.
6. Associate identities with jersey OCR, roster priors, position priors, and coach correction.
7. Extract events such as snap, handoff, throw, contact, and whistle.
8. Compute field-coordinate metrics.
9. Run formation, motion, route, coverage, and concept models.
10. Generate overlays, dashboards, and review queues.

Use GPU acceleration for decode and inference. NVIDIA’s FFmpeg hardware acceleration documentation describes NVDEC and NVENC support and shows that `-hwaccel cuda -hwaccel_output_format cuda` can keep frames in GPU memory during processing, which is important for avoiding unnecessary CPU-GPU transfer overhead in high-throughput video workflows ([NVIDIA Video Codec SDK](https://docs.nvidia.com/video-technologies/video-codec-sdk/13.0/ffmpeg-with-nvidia-gpu/index.html)).

### Model stack

| Layer | Recommended starting point | Notes |
|---|---|---|
| Detection | YOLO or RF-DETR family | Start with player, official, ball, sideline objects. |
| Tracking | ByteTrack for speed, BoT-SORT with Re-ID for identity work | Ultralytics supports both ByteTrack and BoT-SORT, including Re-ID options in BoT-SORT ([Ultralytics](https://docs.ultralytics.com/modes/track/)). |
| Segmentation | SAM-family model for difficult clips and annotation assist | Use selectively because full-frame segmentation can be slow. |
| Pose | RTMPose via MMPose | Use pose-lite metrics first, then expand. |
| Formation and motion | Graph or transformer model over pre-snap coordinates | Start with rules plus supervised labels. |
| Route and coverage | Trajectory classifier plus coach correction | Avoid claiming full automation until labeled data grows. |
| Embeddings | Temporal transformer or graph neural net over coordinates | P2, after enough corrected Toledo data exists. |
| Zero-shot learning | Vision-language and trajectory-language retrieval over clips, labels, and embeddings | Use for discovery and search first; coach confirmation required before production labels. |
| Video search and retrieval | Vector search over labels, tracks, and embeddings | Add after the data model is stable. |
| Quantum AI/ML R&D | Quantum-inspired optimization or hybrid quantum experiments for clustering, scheduling, and high-dimensional search | Phase 3 experimental track only; do not make the core product depend on quantum hardware. |

NVIDIA’s video analytics blueprint is useful as an architectural pattern because it fuses CV metadata such as object positions, masks, tracking IDs, captions, timestamps, vector retrieval, and graph retrieval across long-form or chunked video ([NVIDIA](https://developer.nvidia.com/blog/advance-video-analytics-ai-agents-using-the-nvidia-ai-blueprint-for-video-search-and-summarization/)). Toledo should copy the concept of chunked processing and metadata fusion, not necessarily the expensive enterprise hardware stack.

### Application layer

Build four core screens:

- **Practice inbox**: Uploaded videos, processing status, failed jobs, and QA flags.
- **Clip review**: Video, overlays, confidence, labels, corrections, and export.
- **Coach dashboards**: Formation tendencies, route metrics, effort metrics, player development, and self-scout exposure.
- **Labeling queue**: Low-confidence plays, identity conflicts, boundary errors, and model-regression examples.

The UI should be boring, fast, and coach-centered. Fancy visuals are less important than fast filtering, reliable clips, and easy correction.

## Budget-conscious implementation plan

### Phase 0: Two-week validation sprint

Goal: Prove the capture setup and field calibration are usable.

Deliverables:

- Capture 3 to 5 practices or controlled scrimmage periods from the intended drone angle.
- Build a small labeled evaluation set with 50 to 100 clips.
- Measure field-line visibility, homography stability, player detection quality, and play segmentation accuracy.
- Decide whether one camera is enough for MVP.

Exit criteria:

- More than 90% of selected clips have usable field markings.
- Player detection recall is good enough for formation and spacing metrics.
- Coaches agree the clips represent useful practice situations.

### Phase 1: MVP foundation

Goal: Ship a usable internal system for analysts and selected coaches.

Deliverables:

- Ingestion, clip segmentation, calibration, player tracks, basic metrics, and clip review.
- Manual correction tools for clip boundaries, formation labels, route labels, and player IDs.
- Basic dashboards for practice tempo, formation distribution, motion, and effort metrics.
- Model/data registry.

Recommended staffing:

- 1 technical lead.
- 1 ML/CV engineer or strong student researcher.
- 1 full-stack engineer.
- 1 football analyst or GA as labeling lead.
- 1 coaching sponsor who reviews weekly output.

### Phase 2: Coach-trusted football layer

Goal: Make the system directly useful for position groups.

Deliverables:

- Formation and motion recognition.
- Route classification with coach correction.
- Coverage shell and leverage tagging.
- Offensive line spacing and gap creation metrics.
- Ball-carrier contact point and pre-contact/post-contact proxy.
- Self-scout exposure dashboard.

Exit criteria:

- Coaches use at least one dashboard in weekly prep.
- Corrections decrease week over week for repeated concepts.
- Every dashboard metric links to a clip.

### Phase 3: Toledo differentiator layer

Goal: Build the features commercial systems are less likely to customize deeply.

Deliverables:

- Pose-lite biomechanics for selected drills and positions.
- Individualized player profile and player development passport.
- Workload proxies and athlete health integration.
- Similar-rep search.
- Playbook overlay and assignment/execution scoring for a limited set of concepts.
- Zero-shot concept discovery for coach-confirmed search and labeling.
- Quantum AI/ML experimental track for clustering, similar-rep search, optimization, and research partnerships.

Exit criteria:

- At least one position group uses the system for player development.
- Sports performance staff can review workload trends with confidence caveats.
- Similar-rep search returns useful clips for common Toledo concepts.
- Zero-shot suggestions are reviewed by coaches before being promoted into official labels.
- Quantum AI/ML experiments demonstrate measurable improvement over classical baselines before any production use.

### Phase 4: Frontier R&D

Goal: Test advanced models only after the foundation is trusted.

Deliverables:

- Expected separation, expected yards, expected pressure, and expected completion models.
- Defensive intent modeling and bust detection.
- Counterfactual “what if” simulations.
- Opponent concept matching from embeddings.

Exit criteria:

- Sufficient labeled data exists for each model.
- Baselines are documented.
- Coaches can inspect examples where the model is right and wrong.

## Evaluation plan

### Model metrics

Track these every week:

- Player detection precision, recall, and mAP.
- Track fragmentation and identity switches.
- Homography reprojection error or field-coordinate error.
- Formation label accuracy.
- Route label accuracy.
- Coverage label accuracy.
- Event timing error for snap, throw, catch, and contact.
- Coach correction rate by label type.
- Dashboard usage and clip-open rate.

### Football validation

Every metric needs football validation, not only machine learning validation. For example, separation at ball arrival should be reviewed by WR and DB coaches. Pad-level metrics should be reviewed by OL and DL coaches. Self-scout tendencies should be compared with what staff already sees manually.

### Minimum launch thresholds

Use these as initial launch gates:

- Play segmentation: 90% acceptable clips after analyst review.
- Formation family: 85% accuracy on common formations.
- Motion/no-motion: 90% accuracy.
- Player tracks: useful for team-level spacing on 85% of clips.
- Identity: do not require full automation at MVP; require correction workflow.
- Pose-lite: only show position-specific metrics once reviewed by position coach.

## Risks and mitigations

| Risk | Why it matters | Mitigation |
|---|---|---|
| Drone drift and bad field calibration | Breaks all spatial metrics. | Calibration confidence score, field-line QA, and suppression of unsafe metrics. |
| Occlusion and identity switches | Creates wrong player grades. | Track uncertainty, jersey OCR, roster priors, coach correction, and Re-ID staging. |
| Coaches distrust black-box outputs | Kills adoption. | Every insight links to clip evidence and confidence. |
| Labeling burden gets too high | The model never improves. | Label only high-value fields first and use active learning queues. |
| Budget gets consumed by GPUs | Unsustainable operations. | Batch jobs, spot/burst GPU usage, small models first, and edge only for routing/UI. |
| Advanced features distract from MVP | Delays usefulness. | Freeze P2/P3 work until P0 and P1 thresholds are hit. |
| Health data governance | Sensitive athlete data requires care. | Role-based access, audit logs, minimal medical claims, and staff-approved workflows. |

## What is missing from the current plan

The current plan is strong technically, but it needs more operational discipline. Add these missing pieces before implementation:

- **Metric governance**: Define which metrics are safe, experimental, or hidden from coaches.
- **Confidence scoring**: Every model output needs confidence and failure modes.
- **Label taxonomy**: Create a Toledo football ontology for formations, motions, routes, coverages, fronts, pressures, run concepts, pass concepts, and assignments.
- **Correction UX**: Make coach corrections a first-class product feature, not an admin afterthought.
- **Data rights and privacy**: Define who can view player health, workload, and development data.
- **Ground-truth protocol**: Decide how many plays per week are manually reviewed for accuracy.
- **Integration plan**: Decide how this coexists with Hudl, Catapult, wearable data, spreadsheets, and existing athletics systems.
- **Failure handling**: Define what the system does when a video is too blurry, the drone misses the snap, or calibration fails.
- **Procurement guardrails**: Avoid buying expensive hardware until Phase 0 proves the capture setup.

## Immediate next steps

1. Pick one spring-practice or controlled-practice dataset as the Phase 0 evaluation set.
2. Create the first Toledo label taxonomy with coaches and analysts.
3. Label 50 to 100 representative clips across formations, motions, runs, passes, special situations, and bad visual conditions.
4. Prototype detection, tracking, play segmentation, and field calibration on those clips.
5. Review the prototype with coaches using clips, not slides.
6. Freeze MVP scope around the features coaches actually say they would use weekly.

The winning version of this project is not the one with the most impressive model list. The winning version is the one that the coaching staff trusts, corrects, and uses every week because it saves time, reveals tendencies, and improves player development.
