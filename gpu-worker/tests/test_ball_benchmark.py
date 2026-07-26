"""The benchmark has to score a bad model badly and refuse to invent a number.

Both failure modes are live. `usfootballdataset/us-football-ball-detection-
instant-1` returned 190+ "ball" boxes at 0.70-0.89 confidence on a single 224px
crop, including one covering the whole image -- a scorer that credits any
overlapping box would rate that highly. And on footage where the ball is never
confidently locatable there are no positives to recall, which must read as
undefined rather than as a pass.
"""

from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eval.ball_benchmark import (
    BenchmarkRun,
    LabelledFrame,
    Prediction,
    RegimeMetrics,
    evaluate,
    score_frame,
    split_by_video,
    tile_origins,
)


def _visible(video="v1", frame=0, regime="drone_follow", centre=(100.0, 100.0)):
    return LabelledFrame(video=video, frame=frame, regime=regime, visible=True, centre=centre)


def _absent(video="v1", frame=1, regime="drone_follow"):
    return LabelledFrame(video=video, frame=frame, regime=regime, visible=False)


class TestLabelIntegrity:
    def test_a_visible_label_needs_a_centre(self) -> None:
        with pytest.raises(ValueError, match="visible with no centre"):
            LabelledFrame(video="v", frame=0, regime="drone_follow", visible=True)

    def test_an_occluded_ball_may_not_carry_a_box(self) -> None:
        # Synthesising a position for a ball nobody can see trains and measures
        # a fiction, and it is the single easiest way to get a good-looking
        # number that means nothing.
        with pytest.raises(ValueError, match="measures a fiction"):
            LabelledFrame(
                video="v", frame=0, regime="drone_follow", visible=False, centre=(1.0, 2.0)
            )


class TestScoreFrame:
    def test_a_hit_near_the_labelled_centre(self) -> None:
        tp, fn, fp, err = score_frame(_visible(), [Prediction(105, 100, 0.9)], 0.5)
        assert (tp, fn, fp) == (1, 0, 0)
        assert err == pytest.approx(5.0)

    def test_a_box_in_the_wrong_place_is_a_miss_and_a_false_positive(self) -> None:
        tp, fn, fp, _ = score_frame(_visible(), [Prediction(400, 400, 0.9)], 0.5)
        assert (tp, fn, fp) == (0, 1, 1)

    def test_only_one_box_can_be_right(self) -> None:
        # There is one ball. A model spraying boxes must not earn credit for the
        # one that happens to land -- that is exactly how a degenerate model
        # scores well.
        preds = [Prediction(100, 100, 0.9), Prediction(102, 101, 0.9), Prediction(99, 100, 0.9)]
        tp, fn, fp, _ = score_frame(_visible(), preds, 0.5)
        assert (tp, fn, fp) == (1, 0, 2)

    def test_everything_on_an_absent_frame_is_a_false_positive(self) -> None:
        tp, fn, fp, err = score_frame(_absent(), [Prediction(1, 1, 0.9)] * 4, 0.5)
        assert (tp, fn, fp, err) == (0, 0, 4, None)

    def test_the_threshold_filters_before_scoring(self) -> None:
        tp, _, fp, _ = score_frame(_visible(), [Prediction(100, 100, 0.2)], 0.5)
        assert (tp, fp) == (0, 0)


class TestDegenerateModelScoresBadly:
    def test_a_model_that_calls_everything_a_ball_is_not_rewarded(self) -> None:
        # Modelled on the observed instant-1 behaviour: ~190 high-confidence
        # boxes per frame, one of which lands on the ball by chance.
        spray = [Prediction(float(i % 200), float(i // 200 * 10), 0.85) for i in range(190)]
        spray.append(Prediction(100.0, 100.0, 0.85))
        labels = [_visible(frame=0), _absent(frame=1)]
        metrics = evaluate(labels, lambda f: spray, model="degenerate", thresholds=[0.30])[0]
        assert metrics.recall == 1.0, "it does find the ball"
        assert metrics.precision < 0.02, "but precision collapses"
        assert metrics.fp_per_absent_frame > 100
        assert not metrics.clears_gate()


class TestAbsentFrameFalsePositivesAreCountedApart:
    """Precision and the gate ask different questions of the same detections.

    Every stray box costs precision. The gate asks something narrower -- how
    often does the model invent a ball where there is none -- and pooling the
    two lets a merely duplicative model fail a bar it should clear.
    """

    def test_duplicates_on_visible_frames_do_not_count_as_invented_balls(self) -> None:
        # One correct box and one spare on every visible frame, and silence on
        # every empty one. Precision suffers, but it never claims a ball that
        # is not there.
        def predict(frame):
            return [Prediction(100.0, 100.0, 0.9), Prediction(400.0, 300.0, 0.9)] if frame.visible else []

        labels = [_visible(frame=0), _visible(frame=1), _absent(frame=2), _absent(frame=3)]
        m = evaluate(labels, predict, model="duplicative", thresholds=[0.30])[0]
        assert m.false_positives == 2, "the spare box on each visible frame"
        assert m.absent_false_positives == 0
        assert m.fp_per_absent_frame == 0.0
        assert m.precision == pytest.approx(0.5), "and precision still records it"

    def test_detections_on_empty_frames_are_what_the_gate_measures(self) -> None:
        def predict(frame):
            return [] if frame.visible else [Prediction(50.0, 50.0, 0.9)]

        labels = [_visible(frame=0), _absent(frame=1), _absent(frame=2)]
        m = evaluate(labels, predict, model="hallucinating", thresholds=[0.30])[0]
        assert m.absent_false_positives == 2
        assert m.fp_per_absent_frame == pytest.approx(1.0)
        assert not m.clears_gate()


class TestUndefinedRatherThanPassing:
    def test_no_visible_balls_gives_undefined_recall(self) -> None:
        # The non-observable case. Nothing to find means the measurement says
        # nothing -- it must not read as a pass.
        metrics = evaluate([_absent(frame=i) for i in range(5)], lambda f: [], "m", [0.10])[0]
        assert math.isnan(metrics.recall)
        assert not metrics.clears_gate()
        assert metrics.as_dict()["recall"] is None, "JSON has no NaN"

    def test_a_silent_model_on_absent_frames_is_not_a_pass_either(self) -> None:
        # Perfect on false positives, but with no recall there is no evidence
        # it can find anything.
        metrics = evaluate([_absent(frame=i) for i in range(5)], lambda f: [], "m", [0.10])[0]
        assert metrics.fp_per_absent_frame == 0.0
        assert not metrics.clears_gate()


class TestRegimesStaySeparate:
    def test_results_are_reported_per_regime(self) -> None:
        labels = [
            _visible(video="a", frame=0, regime="drone_follow"),
            _visible(video="b", frame=0, regime="fixed_sideline"),
        ]
        results = evaluate(labels, lambda f: [Prediction(100, 100, 0.9)], "m", [0.10])
        assert {r.regime for r in results} == {"drone_follow", "fixed_sideline"}
        for r in results:
            assert r.visible_frames == 1, "one regime's frames must not leak into the other"

    def test_a_regime_failing_is_not_hidden_by_one_succeeding(self) -> None:
        labels = [
            _visible(video="a", frame=0, regime="drone_follow", centre=(100.0, 100.0)),
            _visible(video="b", frame=0, regime="fixed_sideline", centre=(500.0, 500.0)),
        ]
        # Finds the sideline ball, misses the drone one entirely.
        results = evaluate(labels, lambda f: [Prediction(500, 500, 0.9)], "m", [0.10])
        by_regime = {r.regime: r for r in results}
        assert by_regime["fixed_sideline"].recall == 1.0
        assert by_regime["drone_follow"].recall == 0.0


class TestSplitByVideo:
    def test_holdout_videos_go_entirely_to_test(self) -> None:
        labels = [_visible(video=v, frame=f) for v in ("a", "b", "c") for f in range(3)]
        train, test = split_by_video(labels, holdout={"c"})
        assert {f.video for f in train} == {"a", "b"}
        assert {f.video for f in test} == {"c"}

    def test_no_video_appears_on_both_sides(self) -> None:
        # Adjacent frames of one play are near duplicates; a video on both sides
        # reports a number that has nothing to do with new footage.
        labels = [_visible(video=v, frame=f) for v in ("a", "b") for f in range(5)]
        train, test = split_by_video(labels, holdout={"b"})
        assert not ({f.video for f in train} & {f.video for f in test})


class TestTiling:
    def test_tiles_cover_the_full_width(self) -> None:
        origins = tile_origins(1280, 720)
        assert max(x for x, _ in origins) + 128 == 1280, "the last column must reach the edge"
        assert max(y for _, y in origins) + 128 == 720

    def test_tiles_overlap(self) -> None:
        xs = sorted({x for x, _ in tile_origins(1280, 720)})
        assert xs[1] - xs[0] < 128, "a ball on a tile seam needs to land whole in a neighbour"

    def test_a_frame_smaller_than_one_tile_still_yields_a_tile(self) -> None:
        assert tile_origins(100, 100) == [(0, 0)]


class TestRunArtifact:
    def test_it_stores_by_version(self, tmp_path) -> None:
        run = BenchmarkRun(version="2026-07-26", footage="toledo-drone")
        path = run.write(tmp_path)
        assert path.name == "ball-benchmark-2026-07-26.json"

    def test_the_gate_is_recorded_with_the_result(self, tmp_path) -> None:
        # A number without the bar it was judged against cannot be compared to
        # a later run whose bar may have moved.
        import json

        payload = json.loads(BenchmarkRun(version="v1", footage="f").to_json())
        assert payload["gate"]["min_recall"] == 0.70
        assert payload["gate"]["max_fp_per_absent_frame"] == 0.10

    def test_best_per_regime_is_none_when_nothing_scored(self) -> None:
        run = BenchmarkRun(version="v1", footage="f")
        run.metrics = [
            RegimeMetrics("m", "drone_follow", 0.1, 0, 5, 0, 0, 0, None),
        ]
        assert run.best_per_regime()["drone_follow"] is None
