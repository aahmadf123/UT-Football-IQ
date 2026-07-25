# Spike report — Roboflow NFL datasets & StatsBomb American Football (AMF) open data (Issue #167)

**Status: EVALUATION-ONLY spike.** No production model training, no hosted
Roboflow inference, no StatsBomb-derived production feature. Recommendations
below. Governed by [#166](https://github.com/aahmadf123/Football-IQ/issues/166)
(governance / soccer denylist) and `docs/external-resource-rubric.md`; must not
distract from Phase-CV (#125) or CFBD work (#160–#163).

Harness:
[`gpu-worker/eval/roboflow_statsbomb_eval.py`](../gpu-worker/eval/roboflow_statsbomb_eval.py)
(offline coverage report; synthetic CI sample, no data committed, no API key, no
download) · tests:
[`gpu-worker/tests/test_roboflow_statsbomb_eval.py`](../gpu-worker/tests/test_roboflow_statsbomb_eval.py).

Sources reviewed (2026-05-31):

- Roboflow NFL competition dataset: https://universe.roboflow.com/home-mxzv1/nfl-competition
- Roboflow NFLFootball workspace: https://universe.roboflow.com/nflfootball
- Roboflow REST API docs: https://docs.roboflow.com/developer/rest-api/using-the-rest-api
- StatsBomb AMF open data: https://github.com/statsbomb/amf-open-data

> **Important caveat on Roboflow Universe.** Roboflow Universe is a *platform*,
> not a single dataset. License, classes, and image perspective vary **per
> project and per version**. Every figure below is the snapshot observed on the
> review date and **must be re-verified on the exact project/version** before
> any use. The harness exists precisely so a candidate's class list can be
> re-checked offline at adoption time.

---

## 1. External-resource rubric (#166 / `docs/external-resource-rubric.md`)

### Roboflow NFL datasets

| Field | Answer |
|---|---|
| **Sport coverage** | American football ✅ for the NFL-competition / NFLFootball projects. But "football" on Roboflow Universe **also returns soccer** projects — the harness flags soccer classes (`goalkeeper`, `goal post`, …) and a bare-"football" search must be filtered per §2 denylist. |
| **Toledo / MAC relevance** | **Low for direct transfer.** Observed examples are mostly **broadcast / NFL** imagery (and the NFL "competition" set is largely sideline/helmet-impact framing). None are Toledo single-camera (`DRONEA`) practice angles. A model trained on broadcast NFL will see a domain gap on Toledo MP4 film. |
| **Runtime category** | Offline / manual-export only for this spike. **No hosted inference** in Football-IQ until license + cost approved (issue "out of scope"). |
| **License / access terms** | **Varies per project/version.** The NFL-competition project was observed as **CC BY 4.0** (attribution required; commercial allowed) on the review date, but other NFLFootball projects show `unknown`/unspecified licenses. **Do not assume CC BY** — record the exact license string of the exact version you export. |
| **Secret / key requirement** | **API key required** for programmatic download/export and for the hosted inference API (`detect.roboflow.com?api_key=…`). A logged-in **manual export** (COCO/YOLO/VOC zip) from the project page does **not** put a key in Football-IQ. Prefer manual export — no key, no runtime third-party dependency, no per-call cost. |
| **Data privacy / biometric risk** | The standard NFL projects label `player` / `helmet` / `ball` / `referee` — **no face/identity classes**. The harness **hard-flags** any `face`/`identity`/biometric class so a face-recognition dataset can never be adopted silently (#167). Any candidate that flags must be **rejected or have the class dropped**. |
| **Model-router / registry path** | N/A for the spike. If a Roboflow-trained detector were ever adopted it routes via `select_model(stage, priority)`, stays nightly-only until benchmarked, and weights live in R2 `artifacts/` (never committed). |
| **Overlap with closed decisions** | Single-camera (`docs/capture-protocol-v1.md`) means broadcast multi-angle NFL frames are an *external probe*, never a Football-IQ capture mode. No second vector DB. No second SAM path. |

### StatsBomb American Football (AMF) open data

| Field | Answer |
|---|---|
| **Sport coverage** | American football ✅ — **NFL only** (regular season + playoffs), seasons ~2016–2022. **No college / MAC / Toledo** coverage. This is the *distinct* StatsBomb AMF product, **not** the soccer open data that §2 rejects. |
| **Toledo / MAC relevance** | **Indirect only.** It is structured **tracking/event** data (coordinates + events), **not video**. It cannot be a pretrained CV model for Toledo MP4 and does **not** transfer to single-camera frame detection. Its value is *research* (route/formation/coverage priors, label taxonomy sanity), not coach-facing pipeline output. |
| **Runtime category** | Offline / local research only. **No StatsBomb-derived production feature** until license cleared (issue "out of scope"). |
| **License / access terms** | **StatsBomb Public Data User Agreement** (`LICENSE.pdf` in the repo). **Non-commercial / research & education only**, **attribution + StatsBomb logo required** on any published work. This is a **hard non-commercial restriction** — gates any coach-facing/commercial use. |
| **Secret / key requirement** | **None.** Public GitHub download (JSON/CSV/Parquet). No API key, not read by the backend. |
| **Data privacy / biometric risk** | Positional tracking + event data on professional NFL athletes. **No imagery, no faces, no biometric identification** — no face-recognition implication. Player identity is roster-level (public), not biometric. |
| **Data structure** | Four layers: `plays` (play metadata), `events` (in-play events), `lft` (low-frequency tracking), `tracking` (high-frequency tracking). The harness reports which layers a local copy exposes and notes route/coverage research needs the hi-freq `tracking` layer. |
| **Overlap with closed decisions** | None. Not a vector store; not a capture mode; not a model variant. Single-camera decision (#101) untouched — AMF is external tracking, not Toledo capture. |

---

## 2. Transfer to Toledo MP4 clips (the owner's updated constraint)

The decisive question is **not** "does the dataset exist?" but "does it transfer
to single-camera Toledo MP4 practice/game film and improve first-pass
pretrained/frozen behaviour without manual Toledo training first?"

| Question | Roboflow NFL | StatsBomb AMF |
|---|---|---|
| Transfers to single-camera Toledo MP4? | ⚠️ Partial. Classes (`player`/`helmet`/`ball`/`referee`) match, but imagery is broadcast/NFL → **domain gap** vs Toledo single-camera angles. | ❌ No. It is tracking/event data, **not video** — nothing to run on a Toledo frame. |
| Improves frozen/pretrained first pass without Toledo training? | ⚠️ Maybe for **helmet/ball** priors as a *frozen comparison*, but expect lower recall on Toledo angles; not a drop-in win. | ❌ N/A — no CV inference. |
| Useful classes/labels | `player`, `ball`, `helmet`, `official` ✅ (`team` usually absent). | `plays`, `events`, `lft`, `tracking` → route/formation/coverage **research priors**, not detections. |
| Face-recognition implication? | Only if a candidate carries a face/identity class — **harness hard-flags and we reject it**. | None. |

**Bottom line:** Roboflow is a *possible offline pretraining / comparison*
source for object classes, **not** a coach-ready Toledo model. StatsBomb AMF is a
*research* dataset for route/coverage/formation priors, **not** a CV model and
**not** Toledo-transferable. Neither replaces Toledo-clip validation.

---

## 3. Validation plan before any coach-visible use

Same gate the rest of the project uses: *offline → Toledo clip validation →
coach-facing thresholds*. Stays EXPERIMENTAL until it passes.

1. **Offline coverage check.** Run the harness on the **exact** candidate
   Roboflow version's class list
   (`python -m eval.roboflow_statsbomb_eval report --roboflow <local.jsonl>`) and
   on the local AMF layers (`--statsbomb-layers plays,events,lft,tracking`).
   Confirm: classes match Football-IQ needs, **no soccer class**, **no
   face/biometric class**, and the exact license string is recorded.
2. **Toledo validation set.** Before any coach-visible use, build a held-out set
   of **corrected Toledo MP4 clips** (single-camera `DRONEA`) with ground-truth
   `player`/`helmet`/`ball`/`official` boxes — the authoritative signal. A
   Roboflow-trained or frozen detector is measured here (recall/precision per
   class), **never** on broadcast NFL accuracy.
3. **Frozen-first comparison.** Compare the Roboflow-pretrained detector against
   the current YOLOv8n same-session baseline **on Toledo clips only**. Adopt only
   if it beats the baseline within the same-session latency budget on the GTX
   1660 Ti class GPU.
4. **Coach-facing threshold.** Only after Toledo-clip validation passes the
   project's confidence/calibration thresholds does the output lose its
   EXPERIMENTAL marker — exactly like frontier analytics and concept search.
5. **StatsBomb AMF** never becomes a coach-facing feature under its
   non-commercial license; it is used **offline for research priors only**, with
   attribution, and is re-scoped only if a commercial license is obtained.

---

## 4. License / storage / redistribution / model-weight constraints

| Constraint | Roboflow NFL | StatsBomb AMF |
|---|---|---|
| **License** | Per-project/version; NFL-competition observed as **CC BY 4.0** (verify each). | **StatsBomb Public Data User Agreement** — non-commercial, attribution + logo. |
| **Commercial use** | CC BY 4.0 allows commercial **with attribution** — but only if that specific version is actually CC BY; otherwise unknown → treat as not-permitted until verified. | **Not permitted.** Research / education only. |
| **API key / account** | Free Roboflow account; **API key required** for API export/hosted inference. **Manual export needs no key in the repo.** Document `ROBOFLOW_API_KEY` as backend-only *only if* API export is ever adopted. | **None** — public download. |
| **Storage** | Raw exported images/labels stay **local / gitignored**; if ever needed at scale they go to R2 `artifacts/`. **Never commit images or `.pt`/`.onnx`/`.safetensors` weights** (`.gitignore` enforces). | Raw JSON/CSV/Parquet stays **local / external**, **gitignored**; do not commit the dump. |
| **Redistribution** | CC BY 4.0 permits redistribution **with attribution + license link + change note**; non-CC versions: do not redistribute. | **Do not redistribute** the raw data; share only derived, attributed analysis per the agreement. |
| **Derived model weights** | Weights trained on a CC BY set may be used; **attribute the dataset** in `LICENSES.md`; weights live in R2, never in git, nightly-only until benchmarked. | Models/derivations are **non-commercial** and must carry attribution; no coach-facing/commercial weights under this license. |
| **Face recognition** | **No face-recognition path.** Harness hard-flags any face/biometric class; such datasets are rejected or the class dropped. | None — no imagery. |

---

## 5. Recommendations

| Resource | Recommendation |
|---|---|
| **Roboflow NFL datasets** | **Use offline only (manual export), defer hosted inference.** Permissive *when the specific version is CC BY 4.0* and class coverage (`player`/`helmet`/`ball`/`official`) matches Football-IQ. **Prefer manual export over runtime API calls** (no key in the repo, no per-call cost, no third-party runtime dependency). Treat it as an **offline pretraining / comparison** source only — broadcast/NFL imagery has a real domain gap vs single-camera Toledo film, so it must pass Toledo-clip validation (§3) before any coach-visible use. **No hosted Roboflow inference** in Football-IQ until license + cost are explicitly approved. **Reject** any version whose license is unverifiable, that carries soccer classes, or that the harness hard-flags for face/biometric classes. |
| **StatsBomb AMF open data** | **Use offline only (research), defer any production feature.** Genuinely American football (NFL), distinct from the rejected soccer open data, zero secrets, rich plays/events/tracking layers useful for **route/formation/coverage research priors**. But it is **non-commercial / attribution-required** and **NFL-only (no Toledo/MAC)**, and it is **tracking/event data, not video**, so it does **not** transfer to Toledo MP4 CV. Keep it **local, attributed, research-only**; **no StatsBomb-derived production/coach-facing feature** until a commercial license is cleared. |

Neither is **rejected** (both are genuinely American football and useful
offline) and neither is **adopted into production now**. Both stay **offline,
local-only, evaluation/research-only**. **No face-recognition path is
introduced** by either resource, and the harness enforces that for any future
Roboflow candidate.

---

## 6. Required keys / accounts (summary)

- **Roboflow:** free account. **API key only if** API export/hosted inference is
  ever adopted — then add a backend-only `ROBOFLOW_API_KEY` to
  `backend/app/config.py` `Settings` and `.env.example` (never `NEXT_PUBLIC_*`,
  never in the Worker/frontend). **Manual export needs no key.**
- **StatsBomb AMF:** none — public GitHub download.

---

## 7. Suggested follow-ups (only if/when adopted)

- *Offline Roboflow → Toledo transfer eval*: export one CC BY-verified NFL
  project, run the harness on its exact class list, then measure a
  frozen/pretrained detector against the YOLOv8n baseline **on a corrected
  Toledo MP4 validation set** (§3). Gate on license verification + soccer/face
  harness checks. Do not wire hosted inference.
- *StatsBomb AMF route/coverage research note*: offline, attributed, local-only
  analysis of `tracking`/`events` layers to inform the `label-taxonomy` and
  coverage/route research — explicitly non-commercial, no coach-facing surface.
- Until then: no data committed, no weights committed, no hosted inference, no
  coach-facing surface, no face recognition.
