# Active learning: calibrated-uncertainty review queue

Status: implemented (Issues [#145](https://github.com/aahmadf123/Football-IQ/issues/145)
active-learning annotation queue, [#146](https://github.com/aahmadf123/Football-IQ/issues/146)
calibrated uncertainty everywhere). Related: #139 / #140 (uncertainty-producing
coverage / pressure models).

Football-IQ does **not** ship a separate annotation product. Active learning is
a thin prioritisation layer on top of the existing
[coach-correction loop](governance.md): calibrated uncertainty decides *what a
coach should look at first*; the coach correction they make is, and remains, the
source of truth that flows back as a training label.

```
model output (+ calibrated uncertainty, #146)
        │
        ▼
 active_learning_queue   ──ranked by calibrated priority──▶  coach review
        │                                                        │
        └────────────── correction_id links back ◀── CoachCorrection (source of truth)
                                                                 │
                                                                 ▼
                                                   exported Label → training (#106)
```

## 1. The uncertainty contract (#146)

Every Phase-CV classifier emits a calibrated output via
`gpu-worker/pipeline/calibration/CalibratedOutput`. The payload that travels
with a model label (`label_value["uncertainty"]`) and is stored on a queue row
(`active_learning_queue.metadata["uncertainty"]`) is:

| Field        | Type          | Meaning                                                        |
| ------------ | ------------- | ------------------------------------------------------------- |
| `calibrated` | `bool`        | Was a fitted calibrator (temperature / Platt) applied?        |
| `method`     | `str`         | `"temperature"` \| `"platt"` \| `"uncalibrated"` \| `"none"`  |
| `confidence` | `float\|None` | Calibrated probability — **present only when `calibrated`**.   |
| `entropy`    | `float\|None` | Normalised Shannon entropy in `[0, 1]` (1 = most uncertain).  |
| `raw_score`  | `float\|None` | Uncalibrated magnitude — **ranking only, never a confidence**. |

The backend mirror and the parsing / prioritisation policy live in
[`backend/app/active_learning.py`](../backend/app/active_learning.py). It is a
pure module (no FastAPI / SQLAlchemy) so it is unit-tested in isolation and can
be reused by any producer of queue rows.

### Two honesty rules

1. **An uncalibrated score is never presented as a confidence.** A bare
   `label_value["confidence"]` (legacy) or an uncalibrated payload is demoted to
   `raw_score` (used only to rank) and the API surfaces `confidence = null` with
   `calibrated = false`.
2. **A missing signal is not dropped.** Output with no uncertainty earns a
   defined fallback priority so it stays reviewable rather than appearing
   confident.

## 2. Prioritisation policy

`annotation_priority(signal, ...) -> (priority ∈ [0,1], reason, basis)`:

| Signal             | Priority                                                   | `reason`              | `basis`        |
| ------------------ | ---------------------------------------------------------- | --------------------- | -------------- |
| **calibrated**     | `w·entropy + (1-w)·(1-confidence)`                         | `low_confidence`      | `calibrated`   |
| **uncalibrated**   | `max(1-raw_score, entropy)`; floor when neither is present | `low_confidence` *or* `uncertainty_sampling` | `uncalibrated` |
| **missing**        | `active_learning_missing_priority`                         | `uncertainty_sampling`| `missing`      |

`w` is `active_learning_entropy_weight`. Higher entropy and lower calibrated
confidence both raise priority, so the most ambiguous predictions surface first.

### Tunables (`backend/app/config.py`, `.env.example`)

| Env var                                  | Default | Purpose                                                  |
| ---------------------------------------- | ------- | -------------------------------------------------------- |
| `ACTIVE_LEARNING_ENTROPY_WEIGHT`         | `0.5`   | Entropy vs. `(1-confidence)` blend for calibrated signals. |
| `ACTIVE_LEARNING_UNCALIBRATED_PRIORITY`  | `0.6`   | Priority floor for an uncalibrated, signal-less output.  |
| `ACTIVE_LEARNING_MISSING_PRIORITY`       | `0.5`   | Fallback priority when there is no uncertainty at all.   |

These are backend-only and carry no secrets.

## 3. API surface (existing `mlops` router, #106)

The queue reuses the `active_learning_queue` table (migration
`0003_mlops_registry_and_active_learning`) and the existing endpoints; #145/#146
made them calibration-aware:

* `POST /api/v1/mlops/active-learning/nightly` — analyst+; the nightly loop that
  (1) exports eligible coach corrections to training labels, (2) samples model
  labels below `low_confidence_threshold`, and (3) flags regressions / hard
  negatives where a model label disagrees with a coach correction. The
  low-confidence pass now computes `priority_score` via the calibrated-uncertainty
  policy above and stores the uncertainty payload in `metadata["uncertainty"]`.
* `GET /api/v1/mlops/active-learning/queue` — analyst+; lists items, highest
  priority first. The response is **calibration-honest**: `model_confidence` is
  populated only for a calibrated signal, plus `calibrated`, `entropy`, `basis`,
  and `uncertainty_method` derived from `metadata["uncertainty"]`.

The coach correction itself is created through the normal
[`/api/v1/corrections`](governance.md) flow and is what the nightly regression
pass compares against — the queue never becomes an alternative write path for
ground truth.

## 4. Scope guards

* No separate annotation product — this is prioritisation over the existing
  queue + correction loop.
* No new datastore — `active_learning_queue` + `coach_corrections` only; no
  second vector DB.
* No external dataset/model feeds a coach-visible queue before its license gate
  (see [`external-resource-rubric.md`](external-resource-rubric.md)).
* Logs carry IDs and rounded scores only — no PII, no raw payloads, no secrets.

## 5. Tests

* `backend/tests/test_active_learning.py` — pure policy (calibrated ordering,
  uncalibrated demotion, missing fallback, payload round-trip) plus endpoint
  tests for nightly enqueue and the calibration-honest list response.
* `backend/tests/test_mlops.py` — existing queue ordering / resolve / nightly
  count coverage (unchanged).
