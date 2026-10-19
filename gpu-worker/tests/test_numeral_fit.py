"""Numeral-anchored field registration on rendered game-sideline views."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from pipeline.homography import field_template as ft
from pipeline.homography import numeral_fit as nf
from pipeline.homography.project import apply_homography_many

W, H = 1280, 720
HW = ft.HALF_WIDTH


def _camera(x_lo: float, x_hi: float, squeeze: float) -> np.ndarray:
    """Field (yd) -> image (px) for an elevated sideline camera: the near
    sideline spans the frame bottom, the far one a narrower band at the top."""
    field = np.float32([[x_lo, -HW], [x_hi, -HW], [x_lo, HW], [x_hi, HW]])
    image = np.float32([[-120, 760], [W + 120, 760], [squeeze, 30], [W - squeeze, 30]])
    return cv2.getPerspectiveTransform(field, image)


def _project(m: np.ndarray, pts: list[tuple[float, float]]) -> np.ndarray:
    return cv2.perspectiveTransform(np.float32([pts]), m)[0]


def _render(m: np.ndarray) -> np.ndarray:
    img = np.zeros((H, W, 3), np.uint8)
    img[:] = (40, 140, 40)  # turf
    for x in range(0, 101, 5):
        a, b = _project(m, [(x, -HW), (x, HW)])
        cv2.line(img, tuple(map(int, a)), tuple(map(int, b)), (255, 255, 255), 3)
    for y in (-HW, HW):
        a, b = _project(m, [(0, y), (100, y)])
        cv2.line(img, tuple(map(int, a)), tuple(map(int, b)), (255, 255, 255), 3)
    for y in (ft.HASH_Y_SOUTH, ft.HASH_Y_NORTH):
        for x in range(1, 100):
            if x % 5:
                a, b = _project(m, [(x - 0.25, y), (x + 0.25, y)])
                cv2.line(img, tuple(map(int, a)), tuple(map(int, b)), (255, 255, 255), 3)
    return img


def _numeral_detections(m: np.ndarray, xs: list[int]) -> list[tuple[str, float, list[float]]]:
    dets = []
    for x in xs:
        for near in (True, False):
            y0, y1 = (-HW + 7, -HW + 9) if near else (HW - 9, HW - 7)
            corners = _project(m, [(x - 2, y0), (x + 2, y0), (x - 2, y1), (x + 2, y1)])
            if corners[:, 0].min() < 0 or corners[:, 0].max() > W:
                continue
            tens = x if x <= 50 else 100 - x
            side = "" if x == 50 else ("l" if x < 50 else "r")
            name = f"{'n' if near else 'f'}{side}{tens // 10}"
            box = [*corners.min(axis=0).tolist(), *corners.max(axis=0).tolist()]
            dets.append((name, 0.9, box))
    return dets


@pytest.mark.parametrize(
    ("x_lo", "x_hi", "squeeze", "numbers"),
    [
        (15, 55, 260, [20, 30, 40, 50]),  # wide, like the Michigan State sideline
        (30, 60, 330, [40, 50, 60]),  # panned across midfield, tighter
        (55, 85, 200, [60, 70, 80]),  # right of the 50, steeper
    ],
)
def test_recovers_field_positions_on_sideline_views(x_lo, x_hi, squeeze, numbers):
    m = _camera(x_lo, x_hi, squeeze)
    frame = _render(m)
    numerals = nf.numerals_from_detections(_numeral_detections(m, numbers))
    fit = nf.fit_numerals(frame.shape, numerals, nf.frame_lines(frame))
    assert fit.homography is not None, fit.reason_codes
    assert "numeral_anchored" in fit.reason_codes
    truth = [(x, y) for x in np.linspace(x_lo + 3, x_hi - 3, 6) for y in (-18, -6, 0, 6, 18)]
    pixels = _project(m, truth)
    recovered = np.asarray(apply_homography_many(fit.homography, [tuple(p) for p in pixels]))
    error = np.linalg.norm(recovered - np.asarray(truth), axis=1)
    assert float(np.median(error)) < 0.3
    assert float(error.max()) < 0.8
    assert fit.keypoints.fit_confidence >= 0.75


def test_class_names_map_to_yard_lines():
    assert nf.class_to_field("nl3") == (30.0, True)
    assert nf.class_to_field("fr1") == (90.0, False)
    assert nf.class_to_field("n5") == (50.0, True)
    assert nf.class_to_field("0") is None


def test_one_numeral_is_not_enough():
    m = _camera(15, 55, 260)
    frame = _render(m)
    numerals = nf.numerals_from_detections(_numeral_detections(m, [30])[:1])
    fit = nf.fit_numerals(frame.shape, numerals, nf.frame_lines(frame))
    assert fit.homography is None and fit.reason_codes == ["insufficient_numerals"]


def test_frame_fitter_falls_back_without_numerals():
    sentinel = object()
    fit = nf.make_frame_fitter(lambda _frame: [], fallback=lambda frames: sentinel)
    assert fit([np.zeros((H, W, 3), np.uint8)]) is sentinel


def test_broadcast_calibration_gives_a_per_frame_series_for_one_play(tmp_path, monkeypatch):
    from pipeline import backend, stage_calibrate
    from pipeline.video_timeline import VideoTimeline

    monkeypatch.setattr(backend, "BACKEND_API_URL", "")
    monkeypatch.setenv("DETECT_FRAME_STEP", "2")
    m = _camera(15, 55, 260)
    frame = _render(m)
    path = tmp_path / "game.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 30, (W, H))
    for _ in range(150):  # 5 s
        writer.write(frame)
    writer.release()
    detections = _numeral_detections(m, [20, 30, 40, 50])
    timeline = VideoTimeline([i / 30 for i in range(150)], 5.0, 30.0, verified=True)

    out = stage_calibrate._calibrate_broadcast(
        "video",
        path,
        "job",
        "fixed_sideline",
        lambda _frame: detections,
        timeline=timeline,
        start_time=1.0,
        end_time=3.0,
    )

    series = out["calibration_series"]
    frames = [p["frame_number"] for p in series]
    assert frames and frames[0] >= 30 and frames[-1] < 90
    assert all(f % 2 == 0 for f in frames)
    assert out["analytics_safe"] is True
    assert out["calibration_mode"] == "time_indexed"
    sample = next(p for p in series if p["analytics_safe"])
    fitted = np.asarray(sample["homography"]).reshape(3, 3)
    truth = [(30.0, -6.665), (45.0, 18.0)]
    recovered = np.asarray(apply_homography_many(fitted, [tuple(p) for p in _project(m, truth)]))
    assert np.allclose(recovered, truth, atol=0.4)


def test_a_misread_digit_is_outvoted_by_its_row():
    m = _camera(15, 55, 260)
    frame = _render(m)
    dets = _numeral_detections(m, [20, 30, 40, 50])
    # The near "30" read as "40": the row's order and the other votes fix it.
    dets = [("nl4", 0.6, box) if name == "nl3" else (name, conf, box) for name, conf, box in dets]
    fit = nf.fit_numerals(frame.shape, nf.numerals_from_detections(dets), nf.frame_lines(frame))
    assert fit.homography is not None, fit.reason_codes
    truth = [(25.0, -6.665), (45.0, 6.665), (35.0, 0.0)]
    recovered = np.asarray(
        apply_homography_many(fit.homography, [tuple(p) for p in _project(m, truth)])
    )
    assert np.allclose(recovered, truth, atol=0.5)


def test_one_numeral_row_with_hash_rows_still_registers():
    m = _camera(30, 60, 300)
    frame = _render(m)
    far_only = [d for d in _numeral_detections(m, [40, 50]) if d[0].startswith("f")]
    fit = nf.fit_numerals(frame.shape, nf.numerals_from_detections(far_only), nf.frame_lines(frame))
    assert fit.homography is not None, fit.reason_codes
    truth = [(35.0, -6.665), (45.0, 6.665), (55.0, 0.0)]
    recovered = np.asarray(
        apply_homography_many(fit.homography, [tuple(p) for p in _project(m, truth)])
    )
    assert np.allclose(recovered, truth, atol=0.6)


def test_off_row_detections_are_dropped_before_labelling():
    row = [
        nf.Numeral("nl2", 0.9, (100, 600, 180, 640), 20.0, True),
        nf.Numeral("nl3", 0.9, (400, 595, 480, 635), 30.0, True),
        nf.Numeral("nl3", 0.5, (550, 380, 620, 410), 30.0, True),  # a logo, far above the row
        nf.Numeral("nl4", 0.9, (700, 590, 780, 630), 40.0, True),
    ]
    kept = nf._on_row(row)
    assert [n.box[0] for n in kept] == [100, 400, 700]


def test_detector_input_matches_the_training_export():
    from pipeline.field_model import preprocess

    frame = np.zeros((720, 1280, 3), np.uint8)
    frame[:, :640] = (40, 160, 40)
    frame[:, 640:] = (200, 220, 200)
    out = preprocess(frame, 640)
    assert out.shape == (640, 640, 3)
    assert np.all(out[..., 0] == out[..., 1])  # grayscale
    assert out.min() == 0 and out.max() == 255  # contrast-stretched
