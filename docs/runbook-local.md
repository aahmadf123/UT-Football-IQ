# Local film room runbook

The staff application runs the UI, API, PostgreSQL/pgvector, storage and worker
on one computer. CPU processing is the default; paid GPU services are optional.
Model availability and footage quality determine which analysis is possible.

## Staff application

A target-OS native build includes Python/service dependencies, PostgreSQL with
pgvector, FFmpeg/ffprobe and the built UI. Staff need no Node, Docker, separate
Python installation or migration commands.

1. Unpack the build and open `Start Football-IQ.cmd` on Windows or
   `Start Football-IQ.command` on macOS. Choose **Open local film room**.
2. Create a session in Film Room. Add independent recordings and camera labels,
   such as Drone, Sideline or End zone. Session/date/team context persists.
3. Choose Review for playback preparation without models, Quick for real player
   tracking, or Detailed for optional additional stages. Originals are available
   while work runs. Missing capabilities and partial results appear explicitly.
4. Correct boundaries, identities, tags and events, then mark clips reviewed.
   Edits apply immediately with revisioned history. They invalidate dependent
   results; **Reprocess film** regenerates derived observations.
5. Manually match corresponding clips from recordings to a canonical play when
   appropriate. This prevents duplicate session/report play counts; it does not
   establish synchronized cameras or triangulated positions.
6. Export scoped reports or a portable session archive. Import previews stable
   ID conflicts and validates media hashes before applying; it preserves existing
   local human work on conflict.

The launcher uses a private per-user library:

| OS | Default location |
|---|---|
| Windows | `%LOCALAPPDATA%\Football-IQ` |
| macOS | `~/Library/Application Support/Football-IQ` |
| Linux | `~/.local/share/football-iq` |

The launcher can select another dedicated folder. `config.json` holds private
install credentials: keep it with the library and never share it. Originals and
artifacts are under `storage/`, weights under `models/`, durable stage records
under `checkpoints/`, and diagnostics under `logs/`. Archives exclude accounts,
secrets, absolute machine paths and weights.

Use **Quit** for clean service/database shutdown. Restarting resumes interrupted
work; explicit coach pauses remain paused. The supervisor bounds restart attempts
and exposes actionable failures. **This computer** provides targeted repair.
Disposable-cache cleanup preserves original film, annotations, weights and
durable checkpoints.

Initial detector installation uses the network once and verifies actual CPU
inference. Review remains usable if installation fails; retry in **This computer**.
For offline use place licensed real `yolov8n.pt` weights in the library's `models/`
folder. Dedicated ball/pose weights are optional. No missing model is replaced
with fabricated football observations.

## Source development

Install Python 3.12 for the backend, Python 3.11 for the worker, Node 20+, native
PostgreSQL 16 with pgvector, and FFmpeg/ffprobe with H264 `libx264` support.
PostgreSQL tools must be on PATH or in a native bundle. Use separate environments:

```bash
python3.12 -m venv backend/.venv
backend/.venv/bin/python -m pip install -r backend/requirements.txt
python3.11 -m venv gpu-worker/.venv
gpu-worker/.venv/bin/python -m pip install -r gpu-worker/requirements-cpu.txt
cd frontend
npm ci
npm run build
cd ..
python3.12 -m local_app start
```

Windows equivalents use `py -3.12` / `py -3.11` and `Scripts\python.exe`.
The launcher discovers these environments. Override paths with
`FOOTBALL_IQ_BACKEND_PYTHON` / `FOOTBALL_IQ_WORKER_PYTHON` if necessary.
It initializes the owned database, migrates, seeds private accounts, starts
services and opens the built UI. `python -m local_app doctor` checks readiness;
`python -m local_app gui` opens the launcher. `--no-browser` and
`--skip-model-install` support offline automated operation.

To use an existing development database explicitly:

```bash
python -m local_app start --database-url "$FOOTBALL_IQ_DEV_DATABASE_URL"
```

Use a `postgresql+asyncpg://` URL. This explicit option runs migrations on that
database. Otherwise the launcher owns a local database and deliberately ignores
hosted `DATABASE_URL` settings for database selection. Automatic migration
snapshots apply to the owned database; back up supplied development databases
separately.

## Existing Cloudflare/team library

The existing Cloudflare edge, Hyperdrive/PostgreSQL, R2 and hosted frontend wiring
remains available. Connect local processing to the API without database/R2 keys:

```bash
WORKER_EMAIL=processing-account@example.com \
WORKER_PASSWORD='your-processing-account-password' \
python -m local_app start --server-url https://your-team-api.example \
  --frontend-url https://your-team-film-room.example
```

Or use **Connect to team library** in the native launcher. Connected mode starts
only the local CPU worker; authorized media and result writes travel through the
API. It initializes no database and runs no hosted migrations. The team API must
run this version's worker/storage routes. Deploying those changes is separate
from local implementation. Independent libraries can exchange session archives
now; simultaneous automatic library synchronization is not implemented by
archive transfer.

## Labeling and learning

Human review supports formation, route, coverage, personnel, identity, boundary
and event labels. The first supported real local training family is coverage;
other training families remain unavailable until their real adapters exist.

In **This computer → Learning**, select distinct training, calibration and
holdout sessions. Preview joins actual Detailed feature artifacts to reviewed
human coverage labels, checking source, observation revision, bounds, canonical
play identity, supported classes and session separation. Minimums are 30 training,
10 calibration and 20 holdout plays. Insufficient or stale data blocks creation
with review/reprocessing guidance.

Build an immutable cohort, queue CPU training and evaluate the saved candidate
against its immutable human holdout. The worker fits real weights and saves real
predictions. Evaluation measures accuracy, macro F1 and abstentions. Promotion
requires passing evidence bound to unchanged model bytes and benchmark revisions,
followed by an administrator's action; rollback restores a previously validated
version. Reserved examples stay out of subsequent ordinary training exports.
Automatic training is off by default. See [CPU learning](cv/local-cpu-learning.md).

## Backups and recovery

Before changed migrations alter a nonempty owned local database, the launcher
writes a consistent custom-format dump to `backups/`. Backup failure prevents
migration. Dumps contain records, not original media. Copy the entire library,
including `config.json` and `storage/`, while stopped for a complete backup.
Pre-migration snapshots are retained.

Keep a damaged folder intact and restore a complete stopped-library copy to a
new folder. Select it in the launcher. Database-only recovery uses `pg_restore`
into a separate database with pgvector and matching media; do not overwrite the
only copy of coach work. Databases in this Alembic lineage migrate in place;
unrelated prototypes require explicit import/conversion.

## Native builds and verification

Build on the target OS. The manual **Native CPU application** GitHub Actions
workflow prepares Windows and Apple Silicon macOS packages, checks the packaged
workflow and uploads engineering artifacts. It never publishes a release or
changes a hosted team database. Packages are unsigned; Apple notarization and
Windows signing remain release tasks. Target-OS execution is required before
calling those packages verified.

```bash
python scripts/prepare_native.py --postgres-root /path/to/postgres \
  --ffmpeg /path/to/ffmpeg --ffprobe /path/to/ffprobe --output /tmp/native
python -m pip install -r requirements-native-build.txt
backend/.venv/bin/python -m pip install -r requirements-native-build.txt
gpu-worker/.venv/bin/python -m pip install -r requirements-native-build.txt
python scripts/build_native.py --backend-python backend/.venv/bin/python \
  --worker-python gpu-worker/.venv/bin/python \
  --postgres-root /tmp/native/postgres --ffmpeg /tmp/native/bin/ffmpeg \
  --ffprobe /tmp/native/bin/ffprobe --native-lib /tmp/native/lib
backend/.venv/bin/python scripts/verify_native.py --bundle dist/Football-IQ \
  --data-dir /tmp/football-iq-disposable-native-library
```

To verify the packaged Quick workflow with real footage and existing licensed
detector weights, use a separate disposable library:

```bash
backend/.venv/bin/python scripts/verify_native.py --bundle dist/Football-IQ \
  --data-dir /tmp/football-iq-real-native-check \
  --input /path/to/practice.mp4 --model /path/to/yolov8n.pt
```

Supplying `--model` selects Quick and requires `--input`; without weights the
check exercises Review. Both verify saved work and recovery after terminating
the worker and API and restarting the library. Keep private configuration and
verification evidence outside the repository.

Installed dependency notices are retained. Distributors must include exact native
build sources and required notices, especially GPL FFmpeg/libx264 builds. Models
are not bundled. See [LICENSES](../LICENSES.md).

`scripts/verify_local_flow.py` exercises a disposable loopback library with real
supplied film: import, byte-range playback, CPU processing, correction, review
and actual report download. The native smoke also terminates a worker and
restarts the owned library to verify recovery and persisted work. These checks
do not establish football accuracy. Representative Toledo labels/benchmarks and
staff hardware trials determine accuracy and throughput claims.
