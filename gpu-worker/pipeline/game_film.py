"""Structure of edited coaches' game film: plays, camera angles, scoreboards.

Toledo's full-game exports (and coaches' film generally) are a concatenation
of, for every play::

    [scoreboard card ~3 s] [sideline (press-box) angle] [end-zone angle]

with hard cuts or short dissolves between them. A whole game is ~90 minutes
and ~150 plays. This module recovers that structure so the pipeline can:

* make one clip per camera angle per play, paired by ``play_number`` and a
  shared ``play_call_id`` (each angle gets its own field calibration instead
  of one homography breaking at every cut);
* label each clip's view (``sideline`` / ``endzone``) so calibration and
  metrics use the right geometry;
* read the scoreboard card (quarter, clock, down & distance, ball on) so
  every play carries its game situation and can be matched to CFBD
  play-by-play.

Footage without this structure (practice clips, raw single-camera film) is
detected by :func:`looks_like_edited_film` and left to the optical-flow
segmenter.

Everything here is a heuristic on pixels, not a trained model: views come
from yard-line geometry (sideline views show many steep yard lines, end-zone
views show them flat across the frame), cards from the absence of field
green. Each output carries a confidence so the UI can flag uncertain ones.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import structlog

log = structlog.get_logger(__name__)

# ── Tunables (validated on Toledo 2026 game film, 720p60) ────────────────────

#: Low-resolution decode used for cut detection and field-green measurement.
CUT_FPS = 30
CUT_SIZE = (192, 108)
#: View sampling rate and resolution for yard-line geometry.
VIEW_HZ = 2.0
VIEW_SIZE = (640, 360)
#: A shot whose mean field-green fraction is below this is not a field view
#: (scoreboard card, crowd, graphics).
CARD_GREEN = 0.30
#: Distinct steep yard lines seen in a sideline view vs. an end-zone view.
SIDELINE_MIN_STEEP = 10
ENDZONE_MAX_STEEP = 6
#: Field-view pieces shorter than this are transitions, not angles.
MIN_VIEW_SECONDS = 2.5
#: A real edited game has many cards at roughly play-length intervals.
MIN_CARDS = 8


@dataclass
class Shot:
    start: float
    end: float
    green: float

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class ViewSegment:
    start: float
    end: float
    view: str  # "card" | "sideline" | "endzone" | "unknown"
    confidence: float

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class GamePlay:
    number: int
    views: list[ViewSegment]
    scoreboard: dict[str, Any] | None = None
    card_time: float | None = None


@dataclass
class GameFilmStructure:
    edited: bool
    shots: list[Shot] = field(default_factory=list)
    plays: list[GamePlay] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        views = [v.view for p in self.plays for v in p.views]
        return {
            "edited": self.edited,
            "shots": len(self.shots),
            "plays": len(self.plays),
            "sideline_views": views.count("sideline"),
            "endzone_views": views.count("endzone"),
            "scoreboards_read": sum(1 for p in self.plays if p.scoreboard),
        }


# ── Decoding helpers ─────────────────────────────────────────────────────────


def _ffmpeg_frames(video_path: Path, fps: float, size: tuple[int, int]) -> Any:
    """Yield BGR frames at ``fps`` resampled to ``size`` via an ffmpeg pipe.

    Decodes on the NVIDIA GPU (NVDEC) when it can: a 90-minute 720p60 game
    then takes ~5 minutes per pass instead of ~25 in software
    (``-hwaccel auto`` does not pick NVDEC on Windows). Falls back to
    software decoding if the GPU path produces nothing.
    """
    w, h = size
    frame_bytes = w * h * 3
    for hwaccel in (["-hwaccel", "cuda"], []):
        cmd = [
            "ffmpeg",
            "-v",
            "error",
            *hwaccel,
            "-i",
            str(video_path),
            "-vf",
            f"fps={fps},scale={w}:{h}",
            "-an",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-",
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)  # noqa: S603
        assert proc.stdout is not None
        produced = 0
        try:
            while True:
                buf = proc.stdout.read(frame_bytes)
                if len(buf) < frame_bytes:
                    break
                produced += 1
                yield np.frombuffer(buf, np.uint8).reshape(h, w, 3)
        finally:
            proc.stdout.close()
            proc.kill()
            proc.wait()
        if produced:
            return


def _green_fraction(hsv: np.ndarray) -> float:
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    return float(((h > 30) & (h < 90) & (s > 60) & (v > 40)).mean())


# ── Stage 1: hard cuts ───────────────────────────────────────────────────────


def cuts_from_signal(diffs: list[float], *, min_gap: int = 10) -> list[int]:
    """Frame indices where a hard cut starts a new shot.

    A cut is a histogram jump well above its local neighbourhood, so slow
    pans and lighting changes don't count. Always includes 0.
    """
    d = np.asarray(diffs, dtype=float)
    cuts = [0]
    for i in range(1, len(d)):
        local = float(np.median(d[max(0, i - 15) : i + 15]))
        if d[i] > max(0.5, 4.0 * local) and i - cuts[-1] > min_gap:
            cuts.append(i)
    return cuts


def detect_shots(video_path: Path) -> list[Shot]:
    diffs: list[float] = []
    greens: list[float] = []
    prev: np.ndarray | None = None
    for frame in _ffmpeg_frames(video_path, CUT_FPS, CUT_SIZE):
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [18, 8], [0, 180, 0, 256]).ravel()
        hist /= hist.sum() + 1e-9
        diffs.append(0.0 if prev is None else float(np.abs(hist - prev).sum()))
        prev = hist
        greens.append(_green_fraction(hsv))
    if not diffs:
        return []
    bounds = [*cuts_from_signal(diffs), len(diffs)]
    g = np.asarray(greens)
    return [
        Shot(start=a / CUT_FPS, end=b / CUT_FPS, green=round(float(g[a:b].mean()), 3))
        for a, b in zip(bounds, bounds[1:], strict=False)
        if b > a
    ]


def looks_like_edited_film(shots: list[Shot]) -> bool:
    """Many short non-field cards spaced like plays => edited game film."""
    cards = [s for s in shots if s.green < CARD_GREEN and s.duration <= 6.0]
    if len(cards) < MIN_CARDS:
        return False
    gaps = np.diff([c.start for c in cards])
    # Plays (with both angles) run ~15-90 s between cards.
    return float(np.median(gaps)) >= 10.0 and float(np.median(gaps)) <= 120.0


# ── Stage 2: views ───────────────────────────────────────────────────────────


def steep_line_count(frame: np.ndarray) -> int:
    """Distinct steep (> 35°) white field lines in a 640x360 frame.

    Sideline press-box views show the yard lines as many near-vertical
    stripes; end-zone views show them as horizontal bands, leaving only the
    two converging sidelines (and goal posts) steep.
    """
    small = frame if frame.shape[1] == VIEW_SIZE[0] else cv2.resize(frame, VIEW_SIZE)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    field_mask = cv2.dilate(
        cv2.inRange(hsv, (30, 50, 40), (90, 255, 255)), np.ones((15, 15), np.uint8)
    )
    white = cv2.inRange(hsv, (0, 0, 170), (180, 60, 255)) & field_mask
    lines = cv2.HoughLinesP(white, 1, np.pi / 180, 40, minLineLength=40, maxLineGap=15)
    if lines is None:
        return 0
    columns: set[int] = set()
    for x1, y1, x2, y2 in lines[:, 0]:
        angle = abs(np.degrees(np.arctan2(y2 - y1, x2 - x1)))
        angle = 180 - angle if angle > 90 else angle
        if angle > 35:
            columns.add(int(((x1 + x2) / 2) // 25))
    return len(columns)


def view_label(steep: int) -> tuple[str, float]:
    if steep >= SIDELINE_MIN_STEEP:
        return "sideline", min(1.0, 0.6 + 0.04 * (steep - SIDELINE_MIN_STEEP))
    if steep <= ENDZONE_MAX_STEEP:
        return "endzone", min(1.0, 0.6 + 0.08 * (ENDZONE_MAX_STEEP - steep))
    return "unknown", 0.3


def split_views(shots: list[Shot], samples: list[tuple[float, str, float]]) -> list[ViewSegment]:
    """Turn shots + timed view samples into view segments.

    Cards stay cards. A field shot is split wherever the (smoothed) sample
    label changes, which catches sideline→end-zone dissolves that aren't
    hard cuts. Pieces shorter than ``MIN_VIEW_SECONDS`` merge into a
    neighbour.
    """
    segments: list[ViewSegment] = []
    for shot in shots:
        if shot.green < CARD_GREEN:
            segments.append(ViewSegment(shot.start, shot.end, "card", 1.0 - shot.green))
            continue
        inside = [s for s in samples if shot.start <= s[0] < shot.end]
        labels = [s[1] for s in inside]
        # 3-sample majority smoothing ignores single-frame misreads.
        smooth = [
            max(set(window), key=window.count)
            for window in (labels[max(0, i - 1) : i + 2] for i in range(len(labels)))
        ]
        pieces: list[ViewSegment] = []
        for (t, _, conf), label in zip(inside, smooth, strict=False):
            if pieces and pieces[-1].view == label:
                pieces[-1].end = t + 1 / VIEW_HZ
                pieces[-1].confidence = max(pieces[-1].confidence, conf)
            else:
                start = shot.start if not pieces else t
                if pieces:
                    pieces[-1].end = t
                pieces.append(ViewSegment(start, t + 1 / VIEW_HZ, label, conf))
        if not pieces:
            pieces = [ViewSegment(shot.start, shot.end, "unknown", 0.0)]
        pieces[-1].end = shot.end
        merged: list[ViewSegment] = []
        for piece in pieces:
            if merged and (piece.duration < MIN_VIEW_SECONDS or piece.view == merged[-1].view):
                merged[-1].end = piece.end
            else:
                merged.append(piece)
        if len(merged) > 1 and merged[0].duration < MIN_VIEW_SECONDS:
            merged[1].start = merged[0].start
            merged.pop(0)
        segments.extend(merged)
    return segments


def classify_views(video_path: Path, shots: list[Shot]) -> list[ViewSegment]:
    samples: list[tuple[float, str, float]] = []
    for i, frame in enumerate(_ffmpeg_frames(video_path, VIEW_HZ, VIEW_SIZE)):
        label, conf = view_label(steep_line_count(frame))
        samples.append((i / VIEW_HZ, label, conf))
    return split_views(shots, samples)


# ── Stage 3: plays ───────────────────────────────────────────────────────────


def _resolve_unknown_views(views: list[ViewSegment]) -> None:
    """Fill undecided angles from the film's fixed order.

    Coaches' film shows the sideline angle first, then the end zone. An
    undecided first angle is sideline; one after a sideline angle is the end
    zone. Kept at low confidence so the UI can flag it.
    """
    for index, view in enumerate(views):
        if view.view != "unknown":
            continue
        if index == 0:
            view.view = "sideline"
        elif views[index - 1].view == "sideline":
            view.view = "endzone"
        else:
            continue
        view.confidence = min(view.confidence, 0.4)


def group_plays(segments: list[ViewSegment]) -> list[GamePlay]:
    """A play is the run of field views between two scoreboard cards."""
    plays: list[GamePlay] = []
    current: list[ViewSegment] = []
    card_time: float | None = None
    for seg in segments:
        if seg.view == "card":
            if current:
                _resolve_unknown_views(current)
                plays.append(GamePlay(len(plays) + 1, current, card_time=card_time))
                current = []
            card_time = (seg.start + seg.end) / 2
            continue
        if seg.duration >= MIN_VIEW_SECONDS:
            current.append(seg)
    if current:
        _resolve_unknown_views(current)
        plays.append(GamePlay(len(plays) + 1, current, card_time=card_time))
    return plays


# ── Stage 4: scoreboard ──────────────────────────────────────────────────────

_ORD = {"1ST": 1, "2ND": 2, "3RD": 3, "4TH": 4, "1": 1, "2": 2, "3": 3, "4": 4}
# Down token is anything ordinal-shaped before "&": stylized scoreboard fonts
# make OCR read 2ND as "PND"/"END", 3RD as "BRD"/"3R1", 4TH as "ATH".
_DOWN_RE = re.compile(r"\b([0-9A-Z]{1,3})\s*(?:&|AND)\s*(GOAL|GL|G|\d{1,2})\b")
_QTR_RE = re.compile(r"\b([0-9A-Z]{2,3})\s*(?:QUARTER|QTR)\b|\bQ([1-4])\b")
_CLOCK_RE = re.compile(r"^([0-1]?\d)[:.](\d{2})$")
# Colon dropped by OCR: "1443" -> 14:43, "852" -> 8:52.
_BARE_CLOCK_RE = re.compile(r"^([0-1]?\d)(\d{2})$")
_BALL_RE = re.compile(r"BALL\s*ON\s*[A-Z]{0,4}\s*(\d{1,2})")


def _down_from_token(token: str) -> int | None:
    """Ordinal down from a (possibly misread) token like "PND" or "3R1"."""
    if token in _ORD:
        return _ORD[token]
    if token[0] in "1234":
        return int(token[0])
    if token.endswith("ND") or token.startswith(("P", "E", "Z")):
        return 2
    if "TH" in token or token.startswith("A"):
        return 4
    if "R" in token or token.startswith("B"):
        return 3
    if "ST" in token or token.startswith(("I", "L")):
        return 1
    return None


def parse_scoreboard(texts: list[str]) -> dict[str, Any] | None:
    """Pull quarter, clock, down, distance and ball-on from OCR text.

    Scoreboards differ by stadium, so this matches the common phrasings
    ("1ST & 10", "3RD & GOAL", "2ND QUARTER", "Q3", "12:41", "BALL ON 25")
    anywhere in the recognised text, tolerating the misreads stylized
    scoreboard fonts produce. Returns ``None`` if nothing football shaped
    was found.
    """
    tokens = [
        t.strip().upper().replace("O", "0")
        if re.fullmatch(r"[\dO:.]+", t.strip())
        else t.strip().upper()
        for t in texts
    ]
    blob = " ".join(tokens)
    result: dict[str, Any] = {}
    for m in _DOWN_RE.finditer(blob):
        down = _down_from_token(m.group(1))
        if down is None:
            continue
        dist = m.group(2)
        if dist.isdigit() and not 1 <= int(dist) <= 99:
            continue
        result["down"] = down
        result["distance"] = int(dist) if dist.isdigit() else None
        result["goal_to_go"] = not dist.isdigit()
        break
    if m := _QTR_RE.search(blob):
        token = m.group(1) or m.group(2)
        quarter = 5 if token == "OT" else _down_from_token(token.replace("Z", "2"))
        if quarter is not None:
            result["quarter"] = quarter
    for token in tokens:
        m = _CLOCK_RE.match(token) or _BARE_CLOCK_RE.match(token)
        if m and int(m.group(1)) <= 15 and int(m.group(2)) < 60:
            mins, secs = int(m.group(1)), int(m.group(2))
            if mins == 15 and secs:
                continue
            result["clock"] = f"{mins}:{secs:02d}"
            result["clock_seconds"] = mins * 60 + secs
            break
    if m := _BALL_RE.search(blob):
        yard = int(m.group(1))
        if 1 <= yard <= 50:
            result["ball_on"] = yard
    if not result:
        return None
    result["source"] = "scoreboard_ocr"
    return result


class ScoreboardReader:
    """EasyOCR on scoreboard cards (optional dependency, GPU if available)."""

    def __init__(self) -> None:
        import easyocr  # type: ignore[import-not-found]

        try:
            import torch

            gpu = bool(torch.cuda.is_available())
        except ImportError:
            gpu = False
        self._reader = easyocr.Reader(["en"], gpu=gpu, verbose=False)

    def read(self, frame: np.ndarray) -> dict[str, Any] | None:
        texts = [str(t) for t in self._reader.readtext(frame, detail=0)]
        parsed = parse_scoreboard(texts)
        if parsed is not None:
            parsed["raw"] = texts[:20]
        return parsed


def read_scoreboards(video_path: Path, plays: list[GamePlay]) -> None:
    try:
        reader = ScoreboardReader()
    except Exception as exc:  # noqa: BLE001 - OCR is optional
        log.info("scoreboard_ocr_unavailable", reason=type(exc).__name__)
        return
    cap = cv2.VideoCapture(str(video_path))
    try:
        for play in plays:
            if play.card_time is None:
                continue
            cap.set(cv2.CAP_PROP_POS_MSEC, play.card_time * 1000)
            ok, frame = cap.read()
            if ok:
                play.scoreboard = reader.read(frame)
    finally:
        cap.release()


# ── Entry point ──────────────────────────────────────────────────────────────


def analyze(video_path: Path, *, read_cards: bool = True) -> GameFilmStructure:
    try:
        shots = detect_shots(video_path)
    except (OSError, ValueError) as exc:
        # No ffmpeg, or an undecodable file: not something we can structure;
        # the caller falls back to the generic segmenter.
        log.warning("game_film_analysis_unavailable", reason=type(exc).__name__)
        return GameFilmStructure(edited=False)
    if not looks_like_edited_film(shots):
        log.info("game_film_not_edited", shots=len(shots))
        return GameFilmStructure(edited=False, shots=shots)
    plays = group_plays(classify_views(video_path, shots))
    if read_cards:
        read_scoreboards(video_path, plays)
    structure = GameFilmStructure(edited=True, shots=shots, plays=plays)
    log.info("game_film_structure", **structure.summary())
    return structure


def play_clip_specs(structure: GameFilmStructure) -> list[dict[str, Any]]:
    """One clip spec per (play, view): what ``stage_segment`` writes."""
    specs: list[dict[str, Any]] = []
    for play in structure.plays:
        for index, view in enumerate(play.views):
            spec: dict[str, Any] = {
                "start_time": round(view.start, 3),
                "end_time": round(view.end, 3),
                "play_number": play.number,
                "view": view.view,
                "view_index": index,
                "view_confidence": round(view.confidence, 2),
            }
            if play.scoreboard:
                spec["scoreboard"] = play.scoreboard
            specs.append(spec)
    return specs


def as_dict(structure: GameFilmStructure) -> dict[str, Any]:
    return {
        "summary": structure.summary(),
        "plays": [
            {
                "number": p.number,
                "card_time": p.card_time,
                "scoreboard": p.scoreboard,
                "views": [asdict(v) for v in p.views],
            }
            for p in structure.plays
        ],
    }
