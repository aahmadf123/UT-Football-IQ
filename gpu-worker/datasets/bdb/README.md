# NFL Big Data Bowl (BDB) offline adapter — Issue #164

**Status: OFFLINE PRETRAINING / EVALUATION ONLY.** This adapter normalizes
locally-downloaded NFL Big Data Bowl Kaggle data into a stable artifact schema
that downstream offline model work consumes. It is **not** production runtime,
**not** wired into the model router (`pipeline.model_router`), and produces **no**
coach-facing output. BDB is NFL tracking data — **not Toledo film**.

Part of the American-football external-data epic
([#160](https://github.com/aahmadf123/Football-IQ/issues/160)), governed by the
external-resource rubric ([#166](https://github.com/aahmadf123/Football-IQ/issues/166)).

## Why this exists

BDB ships clean, hand-corrected NFL player tracking. Football-IQ derives field
coordinates from **single-camera** video through calibration / detection /
tracking ([#127](https://github.com/aahmadf123/Football-IQ/issues/127) /
[#128](https://github.com/aahmadf123/Football-IQ/issues/128) /
[#129](https://github.com/aahmadf123/Football-IQ/issues/129)). The two are
**analogous but not interchangeable**. BDB is therefore useful for *offline
pretraining and evaluation* of movement / formation / route / coverage /
counterfactual models, and only that, until Toledo validation proves transfer:

| Downstream | Feature this adapter surfaces |
|---|---|
| [#139](https://github.com/aahmadf123/Football-IQ/issues/139) coverage GNN | formation distribution, nearest-defender separation |
| [#140](https://github.com/aahmadf123/Football-IQ/issues/140) pre-snap pressure | pre-snap spacing (width / depth / on-LOS), motion flags |
| [#141](https://github.com/aahmadf123/Football-IQ/issues/141) counterfactual sim | route distribution, per-frame kinematics |
| [#150](https://github.com/aahmadf123/Football-IQ/issues/150) self-distillation | coverage separation, normalized tracking — **offline movement prior only**; the real cross-regime signal is paired Toledo practice/game clips ([`docs/cross-regime-distillation.md`](../../../docs/cross-regime-distillation.md)) |

## Hard rules

- **Never** commit Kaggle data. Raw CSVs stay in a gitignored local/cache path.
- **Never** print, log, or commit `KAGGLE_USERNAME` / `KAGGLE_API_TOKEN`.
- The Kaggle secret is **`KAGGLE_API_TOKEN`** — *not* `KAGGLE_KEY`.
- No same-session / production inference. No model-router or registry bypass
  (this adapter introduces **no model code** — it is a data normalizer).
- No claim that BDB labels/coordinates equal Toledo labels/coordinates.

## Kaggle auth & download flow (no secrets in the repo)

These competitions require accepting each competition's rules on Kaggle while
signed in. Credentials live only in your shell / CI secret store, never in Git.

The official `kaggle` CLI reads `KAGGLE_USERNAME` and (historically) `KAGGLE_KEY`.
Football-IQ standardizes the token secret as **`KAGGLE_API_TOKEN`**, so bridge it
to the CLI's expected variable at download time — without persisting it:

```bash
# Credentials come from your environment / CI secret store. Nothing is committed.
export KAGGLE_USERNAME="$KAGGLE_USERNAME"
export KAGGLE_KEY="$KAGGLE_API_TOKEN"   # bridge: our secret name -> CLI's var

# One-time per machine/CI: install the official client.
pip install kaggle

# Accept the competition rules in the Kaggle UI first, then download to a
# LOCAL, gitignored path (never inside the repo tree that gets committed):
DEST="${BDB_DATA_DIR:-$HOME/.cache/football-iq/bdb}/nfl-big-data-bowl-2025"
mkdir -p "$DEST"
kaggle competitions download -c nfl-big-data-bowl-2025 -p "$DEST"
unzip -o "$DEST"/*.zip -d "$DEST"
```

`kaggle.json` is an equivalent path (`~/.kaggle/kaggle.json`, mode `600`) — also
never committed. The adapter itself **never calls Kaggle and never downloads**;
it only reads a local directory you point it at.

## Normalize → benchmark

Run from `gpu-worker/`:

```bash
# Normalize a local copy into gitignored artifacts (JSONL + manifest.json).
python -m datasets.bdb normalize \
  --input-dir "$BDB_DATA_DIR/nfl-big-data-bowl-2025" \
  --output-dir "${BDB_ARTIFACT_DIR:-.cache/bdb}/2025" \
  --competition nfl-big-data-bowl-2025 --season 2025

# Emit the offline benchmark / schema report.
python -m datasets.bdb report --artifact-dir "${BDB_ARTIFACT_DIR:-.cache/bdb}/2025" --markdown

# Self-contained demo on the bundled SYNTHETIC sample (no Kaggle data needed).
python -m datasets.bdb demo --output-dir .cache/bdb/demo
```

## Normalized artifact schema

Each `normalize` run writes newline-delimited JSON (`*.jsonl`) plus a
`manifest.json` (provenance: competition, season, source-file SHA-256s,
license/coordinate/label caveats, the `offline-pretraining-evaluation-only`
usage marker, and the field map). Tables:

- `games.jsonl` — `game_id, season, week, game_date, home_team, away_team`
- `plays.jsonl` — `game_id, play_id, quarter, down, yards_to_go, possession_team, defensive_team, offense_formation, receiver_alignment, yardline_side, yardline_number, los_absolute_yard, pass_result, play_direction`
- `players.jsonl` — `nfl_id, display_name, position, height, weight, college`
- `player_plays.jsonl` — `game_id, play_id, nfl_id, team, route_ran, was_running_route, motion_since_lineset, in_motion_at_snap`
- `tracking.jsonl` — `game_id, play_id, nfl_id, frame_id, time, jersey_number, club, side, play_direction, x, y, s, a, dis, o, dir, event`

### BDB → Football-IQ field map

| Football-IQ concept | BDB source | Normalized table |
|---|---|---|
| Session / game metadata | `games.csv` | `games` |
| Play | `plays.csv` | `plays` |
| Player / roster identity | `players.csv` | `players` |
| Route / label (offline analogue) | `player_play.csv` | `player_plays` |
| Tracklet frame sample | `tracking_week_*.csv` | `tracking` |

`tracking.side` (`offense` / `defense` / `ball`) is derived by joining each
track row's club to the play's possession / defensive team. Coordinates are
**BDB field yards** (`x` 0–120, `y` 0–53.3), recorded in the manifest as
`coordinate_frame: bdb_field_yards` — **not** pixels and **not** a calibrated
Football-IQ homography output.

## Season coverage & header aliasing

The adapter tolerates both **BDB 2025** camelCase headers (`gameId`, `nflId`,
`frameId`, …) and **BDB 2026** snake_case headers (`game_id`, `nfl_id`,
`frame_id`, …) via the alias map in `schema.py`. Missing tables are skipped, so a
partial download degrades gracefully instead of crashing. BDB 2026
(`nfl-big-data-bowl-2026-prediction`) is a player-movement *prediction*
competition with an input/output tracking split; its tracking columns normalize
through the same path, and a follow-up can add prediction-target extraction if a
downstream model needs it.

## License / terms caveats

See [`../../../LICENSES.md`](../../../LICENSES.md) (NFL Big Data Bowl row). In
short: governed by each Kaggle competition's rules — typically usable for the
competition and non-commercial research, **redistribution generally not
permitted**. Verify the specific competition's rules before any use beyond
offline research. Raw data is never committed here.
