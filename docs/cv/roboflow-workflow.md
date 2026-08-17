# Roboflow workflow — dataset, labeling, and training loop

This supersedes the manual-export-only recommendation in
`reports/spike-issue167-roboflow-statsbomb-amf.md` for **first-party data**:
the workspace is now connected and drives the training loop for models
trained on *our own footage*. The external-dataset rubric
(`docs/external-resource-rubric.md`) still governs any third-party dataset.

## Zero-touch principle

Coaches never label and never train. They upload film from any device; the
pipeline processes it automatically. Everything below is **owner-side
tooling** that feeds the models behind that experience, plus an automated
improvement loop that only ever asks a human to *review* proposals.

## The one project

- Workspace: `ahmads-workspace-zlz9h`
- Project: `american-football-analyst-dzig0` (`ROBOFLOW_PROJECT`) — repurposed
  as the single consolidated project because the workspace plan caps project
  count. Rename it to "Football IQ Detect" in the UI if desired; slugs are
  stable.
- **Locked taxonomy: `player`, `official`, `ball`** — exactly the canonical
  classes `pipeline/stage_detect.py` emits. Team and position are NOT
  detector classes (k-means team classification and downstream analytics own
  those). `roboflow_ops/taxonomy.py` is the single source of truth.
- The three other legacy projects (`ballgame3-v2t4p`,
  `football-players-zm06l-kkwtl`, `find-american-football-qvszp`) are kept
  untouched as the audit trail; their annotations are imported (remapped) by
  the consolidation CLI.

## Environment

In the gpu-worker `.env` (see `.env.example`, "Roboflow" section):
`ROBOFLOW_API_KEY` (private — never logged/committed), `ROBOFLOW_WORKSPACE`,
`ROBOFLOW_PROJECT`, `ROBOFLOW_DATA_DIR` (gitignored scratch).
Install the extra deps once: `pip install -r requirements-roboflow.txt`.

## Bootstrap (run once, owner)

1. **Consolidate legacy annotations** into the project:

       python -m roboflow_ops.consolidate --dry-run   # reconciliation table
       python -m roboflow_ops.consolidate

   Class remap happens at import time; every image carries a `src:<project>`
   provenance tag; re-runs skip already-uploaded content hashes.
   The consolidated project's own native annotations (its legacy
   `football-players`/`referee`/`ball`/`balllls` classes) are remapped at
   **version generation** with Roboflow's *Modify Classes* step — configure
   it once when generating a version: `football-players→player`,
   `referee→official`, `balllls→ball`.

2. **Sample frames from footage** (tags make regime balance a query, not a
   reshoot — this is how "any camera, any angle, any resolution" becomes a
   balanced training set):

       python -m roboflow_ops.frames --input "../Drone Footage" \
           --per-video 12 --regime drone_follow --session practice

3. **Auto-label + review** (the primary labeling flow — nobody draws boxes
   from scratch):

       python -m roboflow_ops.autolabel --batch frames

   Then review in the Roboflow UI (approve/fix beats draw-from-zero by an
   order of magnitude). Prompts are pinned in `roboflow_ops/autolabel.py`:
   player → "football player", official → "referee", ball → "football".

4. **Generate a version** (UI: Versions → Generate) with the Modify Classes
   remap above. Keep preprocessing minimal (auto-orient only); augmentation
   happens in the trainer.

## Training

Two lanes, per the platform decision:

- **Hosted (fast iteration):** `python -m roboflow_ops.hosted_train
  --version N` — answers "is the dataset good enough?" without occupying the
  GPU box. Evaluate in the Roboflow UI / model evals.
- **Local (deployable weights):**

      python -m roboflow_ops.download --version N
      python -m training.detect.train_yolo --data <dataset>/data.yaml
      python -m training.detect.register --weights runs/.../best.pt \
          --model-name detect-yolov8

  `register` uploads `best.pt` to the R2 `artifacts` bucket and registers an
  **experimental** model version via `POST /api/v1/mlops/models`. Promotion
  is a human decision (`POST /api/v1/mlops/models/{id}/promote`) gated on the
  eval below; once promoted, `pipeline/model_registry_client.resolve("detect")`
  serves the weights automatically — no router changes.

**Eval gate before promotion:** run the pipeline on the 30-clip
`Drone Footage/` corpus with old vs. new weights and compare against
`gpu-worker/eval-baselines/` (tracklet counts, det confidence distributions).
No regression on the baseline corpus, or it stays experimental.

## The improvement loop (recurring, mostly automated)

1. Coaches upload film → pipeline runs → uncertain clips land in the backend
   active-learning queue (calibrated uncertainty; already wired).
2. `python -m roboflow_ops.active_learning` exports the weakest frames to
   Roboflow tagged `active-learning` (schedule after the nightly window).
3. `python -m roboflow_ops.autolabel --batch active-learning`, review the
   handful of proposals, accept.
4. Regenerate a version, retrain, register, eval-gate, promote.

Coach corrections (`coach_corrections` → nightly `training_datasets` export)
remain the second label source; both feed the same versions.

## Ball honesty

The ball is **non-observable** in 720p drone practice footage
(`docs/cv/ball-observability.md`): geometrically occluded at the snap and
~15 px when visible. Do not train or promote ball weights from that corpus —
the numbers will look plausible and mean nothing. Ball data comes from
≥1080p game/sideline footage; see `docs/capture-guidance.md`. The player and
official classes train fine on all regimes.
