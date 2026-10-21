# Native-clock and short-preview correction

The first real game-window engineering run failed after 146 seconds. One valid
source window, `[16.416417, 16.433083)`, produced zero requested proxy pictures:
rounding its duration at 30 output fps returned zero. Four other clean proxies
were signed-downloadable. The run remained failed/partial, with no qualified
football labels, events or metrics. The original failure and its actual integer
ticks are preserved in `real-game-window-engineering-smoke.json` and
`real-game-native-clock-failure-fixture.json`.

The correction retains at least one output picture for a positive, nonempty
native source window. It preserves the semantic source bounds; the existing
measured one-output-frame duration tolerance remains enforced. Empty source
windows do not acquire invented pictures. Actual one-frame H.264 encoding,
source-picture comparison and duration validation are covered by regression.

Timestamp probing now uses integer PTS and the actual stream time base, with
integer/rational origin subtraction before float conversion. Actual frame PTS
take precedence over estimated decoder timestamps. Decimal timestamps remain a
compatibility fallback when exact fields are absent. Full recordings retain the
bounded packet-demux path, without decoding the complete game for an index.

The original MIST recording also exposed two packets explicitly marked for
discard, at PTS -2002 and -1001. Including them shifted the normalized source
index relative to displayed images. Only explicit discard packets are now
excluded before validation and origin selection. Genuine displayed negative
timestamps remain supported. The independent full-packet audit found 301,950
packets and 301,948 non-discard presentation timestamps. The corrected actual
probe is verified, starts at zero and matches the first five displayed decoder
timestamps and indices. This is a measured origin/index check, not verification
of every later camera cut or the whole game's analytical results.

Playback lookup uses a true source-time floor and half-open windows. Decoded
picture lookup uses the nearest actual native timestamp, replacing the nominal
quarter-frame allowance that skipped tightly spaced source ticks. Late or
duplicate decoder pictures are skipped instead of incrementing an invented
source index. Seek, sequential and selective readers retain cancellation checks
and bounded nonadvancing-clock behavior.

The media versions are `pts-proxy-v3-native-window`,
`pts-overlay-v6-native-staff-hud` and timeline revision 3. Checkpoint identities
include these versions, so previous derivatives are regenerated on reprocessing.
Existing historical movies are not rewritten. Changed source clocks can require
staff evidence rebinding/review rather than silently reusing old frame claims.

Independent checks passed 91 PTS/reader/real-proxy/renderer/recovery cases before
the subsequent discard-packet addition, and 50 combined PTS/native-window/reader
cases after it. The actual original packet/origin evidence is in
`mist-original-native-clock-audit.json` and
`mist-original-decoder-origin-audit.json`. Those reports distinguish actual
frame PTS, decoder best-effort timestamps and the explicitly derived processing
copy. The first game extraction preserves picture identity but changes some
timestamps by one tick; it is not an untouched original-clock acceptance run.

Final repository counts and the repeated real-game outcome belong in the final
continuation verification record, rather than being inferred from these scoped
regressions. No football benchmark or production release is established here.
