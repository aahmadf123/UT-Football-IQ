# Supplied full-game source inventory

2026-10-21. Read-only inspection of `C:\Users\firas\OneDrive - University of Toledo\UT Game Film 2026` found **six MP4 files, 25,000,377,750 bytes (25.00 GB / 23.28 GiB)**. Their combined container duration is **9:23:19.282**. Every file is **1280×720 H.264/yuv420p**, declares **60000/1001 fps (about 59.94)** with native video time base **1/60000**, and contains **no audio stream**. No 1080p source was found.

| Supplied filename | Single-file duration | Declared frames | Bytes |
| --- | ---: | ---: | ---: |
| 26 01 OHTO FULL GAME @ MIST.mp4 | 83:57.535 | 301,950 | 3,742,691,239 |
| 26 02 OHTO FULL GAME vs CTCE.mp4 | 93:33.110 | 336,450 | 4,143,692,321 |
| 26 03 OHTO FULL GAME vs PATE.mp4 | 102:32.149 | 368,760 | 4,549,285,620 |
| 26 04 OHTO FULL GAME vs CASS.mp4 | 94:42.179 | 340,590 | 4,193,532,469 |
| 26 05 OHTO @ INBS FULL GAME.mp4 | 98:26.904 | 354,060 | 4,374,633,343 |
| 26 06 OHTO FULL GAME vs NYBU .mp4 | 90:07.405 | 324,120 | 3,996,542,758 |

**Five of six files exceed 90 minutes.** These are actual long single-file timelines, suitable inputs for future long-recording engineering checks. The file lengths do not establish uninterrupted live action, one camera shot, complete game coverage or independent evaluation populations. Sparse MIST pictures visibly include sideline and end-zone camera views; a file can contain edits and repeated views. Filenames are retained as supplied, without independent opponent/session reconciliation.

Fresh FFprobe metadata matched the existing `labeling-package/sources-content-verified.json` catalog for all six paths, byte sizes, durations, dimensions and declared/average rates. Its previously full-byte-verified SHA-256 values were reused; **25 GB was not rehashed**. The catalog did not retain the prior filesystem modification-time tuple, so this is explicitly not a new cryptographic validation of every current byte. The new JSON records current size/mtime before and after inspection for future comparisons; both remained unchanged for every source. Originals were not moved, rewritten or re-encoded by the inventory.

Bounded single-thread CPU decoding sampled approximately two seconds near the beginning, middle and end of each file: **2,123 decoded frames across 18 samples**, all with native presentation timestamps and strictly increasing PTS within each sample. Declared total frame count is **2,025,930**; that total was not fully decoded in this inventory. Most sampled increments are about 16.683 ms, but NYBU's middle sample includes a 33.367 ms gap. The exact average-rate rationals also vary between sources. Native PTS must therefore remain authoritative; a nominal FPS grid is insufficient. Seek-based sample boundaries are recorded by actual decoded timestamps and are not assumed to equal the requested interval start exactly.

An engineering canary candidate is **MIST source 300–320 seconds**. Sampled pictures at 300, 307.5 and 315 seconds show actual field footage with a sideline-to-end-zone view change and visible pole obstruction. This is useful for checking cuts, visibility loss and conservative abstention. It is not a reviewed play boundary, synchronized multi-view identity, annotation truth or proof that all players are visible. A subsequent separately authorized bounded canary uses an explicit parent/native-frame mapping; it does not process the full game.

At inventory preflight the RTX 5060 Laptop GPU reported **0 MiB / 0% utilization and no compute process**, with about 8 GiB total memory. Existing worker PID 28716 and another local worker service remained alive and untouched. An idle snapshot is not a resource reservation or a speed prediction. These roughly 60-fps game sources have substantially more frames than the earlier seven-second, roughly 30-fps practice smoke; scaling its wall time would not establish 90-minute runtime, memory bounds or p95. No full GPU canary, deployment, restart or core-code edit was performed by this inventory operation.

Machine-readable stream, native-PTS sample, filesystem and retained-catalog-hash evidence: [fullgame-original-inventory.json](fullgame-original-inventory.json). Existing full-SHA catalog: [sources-content-verified.json](labeling-package/sources-content-verified.json). Sparse inspection frames and bounded sampling details remain under ignored `runs/`.
