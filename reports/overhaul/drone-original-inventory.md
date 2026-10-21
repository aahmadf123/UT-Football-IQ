# Drone footage source audit

Observed metadata at **2026-10-21T19:22:59.179064+00:00**; full source reads, CPU frame decoding and native PTS checks completed **2026-10-21T19:26:16.630885+00:00**.

The user-supplied folder `C:\Users\firas\OneDrive - University of Toledo\Drone Footage` contains **30 files, all MP4**, totaling **300,495,546 bytes**. All 30 source files were readable in full, have distinct SHA-256 hashes, and decoded without reported errors. All **11,065 decoded frames** carry strictly increasing native presentation timestamps. All source file sizes and modification timestamps remained unchanged. No file had an offline or recall-on-data-access flag before opening, and there were no OneDrive placeholder/read failures.

Every source is **1280 x 720**, H.264, with both declared rates **30000/1001 fps** (about 29.97). The decoded frame dimensions agree. Observed native PTS increments across the sources range from 0.033366000 to 0.033367000 seconds; these are measured PTS, not a generated FPS time grid.

Individual durations range from **6.973633 to 23.623600 seconds**. Total duration is **369.202164 seconds**, spread across 30 separate clips. The longest is `Dji 20260416111445 0282 D.mp4` (23.623600 seconds; SHA-256 `477452a5af83e3d8e14337ec879cc1cdc3f4caafb82bf2bd0ef41c65a49ac727`).

**No qualifying continuous >=300-second 1080p source exists in this folder.** Concatenating or upscaling these clips would not establish the required original continuous 1080p benchmark. The five-minute 1080p processing-speed gate remains missing actual input evidence.

Methods: read-only file inventory, FFprobe stream/container metadata, full-byte SHA-256 reads, and single-thread CPU frame decoding (at most two FFprobe processes). No source files were copied, moved or edited. No GPU inference was run; the live processor was not controlled or modified. This audit establishes media readability/timebase/resolution only, not football annotation quality or pipeline accuracy.

Per-source paths, hashes, stream/container metadata, frame counts and PTS statistics: [drone-original-inventory.json](drone-original-inventory.json).

## Existing source catalog comparison

At 2026-10-21T19:27:31.658712+00:00, all **30/30 sources** matched full SHA-256 hashes in `reports/overhaul/labeling-package/sources-content-verified.json` (300,495,546 duplicate bytes; catalog SHA-256 `996e52fab5db5e72bb05acc2b2f14d31d52374f54331375b919bb3ac6075a939`). **No new source bytes** were found at the supplied alternate folder path. These copies do not add independent game/session populations, and the original catalog's proposed/unconfirmed source grouping remains unchanged. Per-source matches are recorded in the JSON report.
