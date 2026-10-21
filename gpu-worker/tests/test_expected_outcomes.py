"""Contract fixtures exercise mechanics only; these are not model validation data."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from pipeline.expected_outcomes.data import (
    DataError,
    definitions,
    validate_dataset,
    validate_snapshot,
)
from pipeline.expected_outcomes.model import evaluate, fit, load_model, predict, save_model


def snapshot(task="completion", index=0, split="train"):
    spec = definitions(task)
    digest = hashlib.sha256(f"fixture-{split}-{index}".encode()).hexdigest()
    return {
        "schema_version": 1,
        "feature_version": spec.version,
        "task": task,
        "source": "real",
        "capture_regime": "fixed_sideline",
        "eligible_population": spec.population,
        "subject": {"scope": "team", "reason": "contextual_front_prediction"}
        if task == "pressure"
        else {
            "scope": "player",
            "role": {"completion": "QB", "rushing_gain": "rusher", "yac": "receiver"}[task],
            "tracklet_id": f"fixture-{split}-{index}-actor",
        },
        "lineage": {
            "session_id": split,
            "game_id": split,
            "recording_id": f"r-{split}",
            "canonical_play_id": f"{split}-{index}",
            "media_sha256": digest,
        },
        "observation_clock": {"event": spec.clock, "timestamp_seconds": 2.0},
        "features": {
            name: {
                "value": (1 + index % 2 if unit == "count" else float(index % 2) + 0.1),
                "unit": unit,
                "observed_at_seconds": 2.0,
                "evidence_sha256": digest,
                "qualified": True,
            }
            for name, unit in spec.features
        },
        "split": split,
        "target": {
            "value": index % 2,
            "source": "human",
            "reviewed": True,
            "reviewer_id": "test-reviewer",
            "annotation_sha256": digest,
            "reviewed_at": "2026-10-21T12:00:00Z",
        },
    }


def dataset(task="completion"):
    return [
        snapshot(task, i, split) for split in ("train", "calibration", "holdout") for i in range(4)
    ]


def test_refuses_missing_review_future_features_and_wrong_units():
    for mutate in (
        lambda row: row["target"].update(reviewed=False),
        lambda row: next(iter(row["features"].values())).update(observed_at_seconds=2.01),
        lambda row: next(iter(row["features"].values())).update(unit="pixels"),
        lambda row: row["features"].update(final_yards={"value": 8}),
    ):
        rows = dataset()
        mutate(rows[0])
        with pytest.raises(DataError):
            validate_dataset(rows)
    with pytest.raises(DataError):
        validate_dataset([])


@pytest.mark.parametrize(
    "key", ["session_id", "game_id", "media_sha256", "canonical_play_id", "recording_id"]
)
def test_entire_groups_and_alternate_views_cannot_cross_splits(key):
    rows = dataset()
    rows[-1]["lineage"][key] = rows[0]["lineage"][key]
    with pytest.raises(DataError, match="leakage"):
        validate_dataset(rows)


@pytest.mark.parametrize("task", ["completion", "rushing_gain", "yac", "pressure"])
def test_reproducible_fitting_inference_and_heldout_metrics(task, tmp_path):
    rows = validate_dataset(dataset(task))
    candidate = fit(rows, task)
    changed = copy.deepcopy(dataset(task))
    for row in changed:
        if row["split"] == "holdout":
            row["target"]["value"] = 1 - row["target"]["value"]
    assert fit(validate_dataset(changed), task)["parameters"] == candidate["parameters"]
    path = tmp_path / "checkpoint.json"
    digest = save_model(candidate, path)
    model = load_model(path, digest)
    result = predict(model, validate_snapshot(snapshot(task)))
    assert result["experimental"] is True and result["analytics_safe"] is False
    assert result["weights_sha256"] == digest
    assert result["value"] is not None
    report = evaluate(model, rows)
    assert report["weights_sha256"] == digest
    assert report["split"] == "holdout" and report["promoted"] is False
    assert report["baseline"]["definition"] == "empirical_training_population_mean"
    assert (
        "brier" in report["metrics"]
        if task in {"completion", "pressure"}
        else "mae" in report["metrics"]
    )
    with pytest.raises(DataError, match="hash"):
        load_model(path, "0" * 64)
    with pytest.raises(FileExistsError):
        save_model(candidate, path)


def test_checkpoint_approval_does_not_promote(tmp_path):
    candidate = fit(validate_dataset(dataset()), "completion")
    candidate.update(approved=True, validated=True, analytics_safe=True)
    path = tmp_path / "candidate.json"
    digest = save_model(candidate, path)
    assert predict(load_model(path, digest), validate_snapshot(snapshot()))["experimental"]


def test_cli_refuses_hash_mismatch_and_evidence_overwrite(tmp_path):
    from training.expected_outcomes import train_manifest

    data = tmp_path / "rows.jsonl"
    data.write_text("\n".join(json.dumps(row) for row in dataset()), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset": {"path": data.name, "sha256": "0" * 64},
                "evidence": [],
            }
        ),
        encoding="utf-8",
    )
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    with pytest.raises(DataError, match="hash"):
        train_manifest(manifest, digest, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_stage_routes_and_abstains_without_evidence(monkeypatch):
    from pipeline import model_registry_client, model_router, stage_expected_outcomes

    monkeypatch.setattr(model_registry_client, "resolve", lambda _: None)
    calls = []
    monkeypatch.setattr(
        model_router,
        "select_model",
        lambda stage, priority: calls.append((stage, priority)) or "expected-outcomes-linear-v1",
    )
    result = stage_expected_outcomes.run("clip", "job", 10, persist=False)
    assert calls == [("expected_outcomes", 10)]
    assert len(result["predictions"]) == 4
    assert all(p["suppressed"] and p["value"] is None for p in result["predictions"].values())


def _manifest(tmp_path, task="completion"):
    from pipeline.expected_outcomes.data import annotation_artifact, feature_artifact

    rows = dataset(task)
    evidence = []
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    media_hashes = {}
    for color, split in enumerate(("train", "calibration", "holdout"), 1):
        file = tmp_path / f"fixture-{split}.mp4"
        writer = cv2.VideoWriter(str(file), cv2.VideoWriter_fourcc(*"mp4v"), 30.0, (64, 48))
        assert writer.isOpened()
        for _ in range(90):
            writer.write(np.full((48, 64, 3), color * 50, dtype=np.uint8))
        writer.release()
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
        evidence.append({"path": file.name, "sha256": digest, "kind": "media"})
        media_hashes[split] = digest
    for row in rows:
        split, index = row["split"], row["lineage"]["canonical_play_id"].rsplit("-", 1)[1]
        row["lineage"]["media_sha256"] = media_hashes[split]
        for kind, body in (
            ("features", feature_artifact(row)),
            ("annotation", annotation_artifact(row)),
        ):
            artifact_file = tmp_path / f"{split}-{index}.{kind}.json"
            artifact_file.write_text(
                json.dumps(body, sort_keys=True, separators=(",", ":")), encoding="utf-8"
            )
            artifact_hash = hashlib.sha256(artifact_file.read_bytes()).hexdigest()
            evidence.append({"path": artifact_file.name, "sha256": artifact_hash, "kind": kind})
            if kind == "features":
                for feature in row["features"].values():
                    feature["evidence_sha256"] = artifact_hash
            else:
                row["target"]["annotation_sha256"] = artifact_hash
    data = tmp_path / "dataset.jsonl"
    data.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset": {
                    "path": data.name,
                    "sha256": hashlib.sha256(data.read_bytes()).hexdigest(),
                },
                "evidence": evidence,
            }
        ),
        encoding="utf-8",
    )
    return manifest, hashlib.sha256(manifest.read_bytes()).hexdigest()


def test_manifest_training_evaluation_and_no_overwrites(tmp_path):
    from training.expected_outcomes import evaluate_manifest, train_manifest

    manifest, digest = _manifest(tmp_path)
    result = train_manifest(manifest, digest, tmp_path / "candidate")
    artifact = result["models"]["completion"]
    report = evaluate_manifest(
        manifest,
        digest,
        Path(artifact["checkpoint_path"]),
        artifact["artifact_sha256"],
        tmp_path / "repeat-evaluation.json",
    )
    assert report["metrics"] == artifact["report"]["metrics"]
    with pytest.raises(DataError, match="exists"):
        train_manifest(manifest, digest, tmp_path / "candidate")
    before = manifest.read_bytes()
    with pytest.raises(DataError, match="exists"):
        evaluate_manifest(
            manifest,
            digest,
            Path(artifact["checkpoint_path"]),
            artifact["artifact_sha256"],
            manifest,
        )
    assert manifest.read_bytes() == before
    (tmp_path / "fixture-train.mp4").write_bytes(b"tampered")
    with pytest.raises(DataError, match="hash"):
        train_manifest(manifest, digest, tmp_path / "another-candidate")


def test_entire_immutable_holdout_is_required(tmp_path):
    rows = validate_dataset(dataset())
    path = tmp_path / "model.json"
    digest = save_model(fit(rows, "completion"), path)
    model = load_model(path, digest)
    with pytest.raises(DataError, match="complete immutable"):
        evaluate(
            model,
            [
                r
                for r in rows
                if not (
                    r.split == "holdout" and dict(r.lineage)["canonical_play_id"] == "holdout-3"
                )
            ],
        )
    changed = dataset()
    changed[-1]["target"]["value"] = 1 - changed[-1]["target"]["value"]
    with pytest.raises(DataError, match="complete immutable"):
        evaluate(model, validate_dataset(changed))


def _observed_release():
    event = {
        "event_type": "pass_release",
        "source": "human",
        "timestamp_seconds": 2.0,
        "shot_id": "shot",
        "frame_number": 60,
        "attributes": {
            "feature_context_available_at_seconds": 1.9,
            "play_design": "pass",
            "forward_pass": True,
            "intended_receiver_id": "receiver",
            "qb_id": "qb",
            "defender_track_ids": ["defender"],
            "defender_population_complete": True,
        },
    }
    tracks = [
        {
            "id": name,
            "source": "human",
            "track_points": [
                {
                    "field_x": x,
                    "field_y": 0.0,
                    "timestamp_seconds": 2.0,
                    "frame_number": 60,
                    "shot_id": "shot",
                    "visibility_state": "observed",
                },
                {
                    "field_x": 100.0,
                    "field_y": 0.0,
                    "timestamp_seconds": 3.0,
                    "frame_number": 90,
                    "shot_id": "shot",
                    "visibility_state": "observed",
                },
            ],
        }
        for name, x in (("qb", 10.0), ("receiver", 20.0), ("defender", 22.0))
    ]
    return event, tracks


def test_normal_observed_artifacts_extract_and_reach_inference(tmp_path, monkeypatch):
    from pipeline import model_registry_client, stage_expected_outcomes
    from pipeline.expected_outcomes.extract import extract_snapshot

    event, tracks = _observed_release()
    lineage = snapshot()["lineage"]
    packet = extract_snapshot("completion", tracks, [event], lineage, analytics_safe=True)
    assert packet["features"]["target_distance_to_qb"]["value"] == 10.0
    assert packet["features"]["nearest_defender_distance"]["value"] == 2.0
    assert packet["subject"] == {"scope": "player", "role": "QB", "tracklet_id": "qb"}
    assert all(
        len(t["track_points"]) == 1 for t in packet["feature_evidence"]["observations"]["tracks"]
    )
    path = tmp_path / "model.json"
    digest = save_model(fit(validate_dataset(dataset()), "completion"), path)
    monkeypatch.setattr(model_registry_client, "resolve", lambda _: None)
    result = stage_expected_outcomes.run(
        "clip",
        "job",
        10,
        tracklets=tracks,
        events=[event],
        lineage=lineage,
        analytics_safe=True,
        model_paths={"completion": (path, digest)},
        persist=False,
    )
    assert result["predictions"]["completion"]["value"] is not None
    assert result["predictions"]["completion"]["weights_sha256"] == digest
    assert "completion" in result["feature_snapshots"]
    assert result["predictions"]["completion"]["subject"] == packet["subject"]
    writes = []
    monkeypatch.setattr(
        stage_expected_outcomes.backend, "create_metric", lambda **kw: writes.append(kw)
    )
    stage_expected_outcomes.run(
        "clip",
        "job",
        10,
        tracklets=tracks,
        events=[event],
        lineage=lineage,
        analytics_safe=True,
        model_paths={"completion": (path, digest)},
        persist=True,
    )
    assert writes[0]["tracklet_id"] == "qb"
    event["attributes"]["feature_context_available_at_seconds"] = 2.01
    with pytest.raises(DataError, match="prediction time"):
        extract_snapshot("completion", tracks, [event], lineage, analytics_safe=True)


def test_no_future_interpolation_or_incomplete_population():
    from pipeline.expected_outcomes.extract import extract_snapshot

    event, tracks = _observed_release()
    tracks[0]["track_points"][0]["timestamp_seconds"] = 1.95
    with pytest.raises(DataError, match="exact"):
        extract_snapshot("completion", tracks, [event], snapshot()["lineage"], analytics_safe=True)


def test_unique_reviewed_release_alias_supersedes_approved_machine_throw():
    from pipeline.expected_outcomes.extract import extract_snapshot

    event, tracks = _observed_release()
    machine = copy.deepcopy(event)
    machine.update(id="machine-throw", event_type="throw", source="model")
    machine["provenance"] = {
        "validated": True,
        "approved_evaluation_id": "approved",
        "artifact_sha256": "a" * 64,
        "quality_checks_passed": True,
    }
    packet = extract_snapshot(
        "completion", tracks, [machine, event], snapshot()["lineage"], analytics_safe=True
    )
    assert packet["feature_evidence"]["observations"]["event"]["source"] == "human"
    second = copy.deepcopy(event)
    second.update(id="other-reviewed", event_type="forward_pass_release", timestamp_seconds=2.1)
    with pytest.raises(DataError, match="ambiguous"):
        extract_snapshot(
            "completion",
            tracks,
            [machine, event, second],
            snapshot()["lineage"],
            analytics_safe=True,
        )
    event, tracks = _observed_release()
    event["attributes"]["defender_population_complete"] = False
    with pytest.raises(DataError, match="Complete"):
        extract_snapshot("completion", tracks, [event], snapshot()["lineage"], analytics_safe=True)


@pytest.mark.parametrize(
    "approval", ["valid", "wrong_feature", "wrong_regime", "missing_evaluation", "wrong_artifact"]
)
def test_serving_requires_external_exact_artifact_task_approval(tmp_path, monkeypatch, approval):
    from pipeline import model_registry_client, stage_expected_outcomes

    path = tmp_path / "model.json"
    digest = save_model(fit(validate_dataset(dataset()), "completion"), path)
    publication = {
        "feature_version": definitions("completion").version,
        "capture_regimes": ["fixed_sideline"],
        "definition_version": "protocol-only",
    }
    if approval == "wrong_feature":
        publication["feature_version"] = "obsolete"
    elif approval == "wrong_regime":
        publication["capture_regimes"] = ["broadcast"]
    resolved = model_registry_client.ResolvedModel(
        "candidate",
        "expected",
        "test",
        path,
        "0" * 64 if approval == "wrong_artifact" else digest,
        True,
        None if approval == "missing_evaluation" else "approved",
        {"expected_completion": publication},
    )
    monkeypatch.setattr(
        model_registry_client,
        "resolve",
        lambda kind: resolved if kind == "expected_completion" else None,
    )
    event, tracks = _observed_release()
    out = stage_expected_outcomes.run(
        "clip",
        "job",
        10,
        tracklets=tracks,
        events=[event],
        lineage=snapshot()["lineage"],
        analytics_safe=True,
        capture_regime="fixed_sideline",
        persist=False,
    )
    prediction = out["predictions"]["completion"]
    assert prediction["analytics_safe"] is (approval == "valid")
    assert prediction["experimental"] is (approval != "valid")
    if approval == "valid":
        assert prediction["model_version_id"] == "candidate"
        assert prediction["subject"]["tracklet_id"] == "qb"


@pytest.mark.parametrize("layer", ["event", "track", "point"])
def test_human_wrapper_cannot_launder_synthetic_feature_observations(layer):
    from pipeline.expected_outcomes.extract import extract_snapshot

    event, tracks = _observed_release()
    target = (
        event
        if layer == "event"
        else tracks[0]
        if layer == "track"
        else tracks[0]["track_points"][0]
    )
    target["provenance"] = {"source": "synthetic_test"}
    with pytest.raises(DataError, match="synthetic"):
        extract_snapshot("completion", tracks, [event], snapshot()["lineage"], analytics_safe=True)


def test_manually_queued_training_adapter_registers_candidate_only(tmp_path, monkeypatch):
    from pipeline import backend, storage
    from training.expected_outcomes import train_expected_outcomes_job

    manifest, digest = _manifest(tmp_path)
    registrations = []
    monkeypatch.setattr(backend, "BACKEND_API_URL", "http://backend.test")
    monkeypatch.setattr(storage, "upload_file", lambda path, key: f"local://models/{key}")
    monkeypatch.setattr(
        backend,
        "register_model_version",
        lambda **kw: registrations.append(kw) or {"id": "candidate"},
    )
    result = train_expected_outcomes_job(
        {
            "manifest_path": str(manifest),
            "manifest_sha256": digest,
            "output_dir": str(tmp_path / "candidate"),
        },
        "job",
    )
    assert result["promoted"] is False
    assert registrations[0]["model_type"] == "expected_completion"
    assert registrations[0]["metrics"]["release_gate_passed"] is False
    assert (
        registrations[0]["metrics"]["weights_sha256"]
        == result["models"]["completion"]["artifact_sha256"]
    )


def test_existing_worker_dispatch_reaches_expected_outcome_training(monkeypatch):
    import importlib.util
    from pathlib import Path

    from training import expected_outcomes

    path = Path(__file__).resolve().parents[1] / "__main__.py"
    spec = importlib.util.spec_from_file_location("expected_outcomes_worker_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    monkeypatch.setattr(
        expected_outcomes,
        "train_expected_outcomes_job",
        lambda inputs, job_id: calls.append((inputs, job_id)) or {"trained": True},
    )
    payload = {"training_variant": "expected_outcomes", "manifest_path": "reviewed-manifest"}
    assert module._dispatch("train", "video", "clip", "", payload, "job")["trained"]
    assert calls == [(payload, "job")]


def test_orchestrator_and_processing_profiles_include_reachable_stage(monkeypatch):
    from pipeline import stage_expected_outcomes
    from pipeline.lightweight_config import profile_stages
    from pipeline.orchestrator import CLIP_STAGES, PipelineContext, _run_clip_stage

    assert "expected_outcomes" in CLIP_STAGES
    assert "expected_outcomes" in profile_stages("quick")
    assert "expected_outcomes" in profile_stages("detailed")
    context = PipelineContext(
        "video",
        "",
        lineage={"session_id": "session", "recording_id": "recording", "source_sha256": "a" * 64},
    )
    called = []
    monkeypatch.setattr(
        stage_expected_outcomes, "run", lambda *args, **kwargs: called.append(kwargs) or {}
    )
    _run_clip_stage("expected_outcomes", context, {"id": "clip", "canonical_play_id": "play"})
    assert called[0]["lineage"] == {
        "session_id": "session",
        "recording_id": "recording",
        "media_sha256": "a" * 64,
        "canonical_play_id": "play",
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("reviewer_id", True),
        ("reviewed_at", True),
        ("reviewed_at", "yesterday"),
        ("reviewed_at", "2026-10-21T12:00:00"),
    ],
)
def test_review_attribution_requires_strings_and_explicit_timezones(field, value):
    row = snapshot()
    row["target"][field] = value
    with pytest.raises(DataError):
        validate_snapshot(row, require_target=True)


def test_schema_boolean_and_malformed_checkpoint_are_refused(tmp_path):
    row = snapshot()
    row["schema_version"] = True
    with pytest.raises(DataError):
        validate_snapshot(row)
    candidate = fit(validate_dataset(dataset()), "completion")
    candidate["split_lineage"]["train"] = [True]
    path = tmp_path / "malformed.json"
    digest = save_model(candidate, path)
    with pytest.raises(DataError, match="lineage"):
        load_model(path, digest)


@pytest.mark.parametrize("tamper", ["target", "feature"])
def test_hash_verified_artifacts_must_bind_exact_row_values(tmp_path, tamper):
    from training.expected_outcomes import train_manifest

    manifest, _digest = _manifest(tmp_path)
    data_path = tmp_path / "dataset.jsonl"
    rows = [json.loads(line) for line in data_path.read_text().splitlines()]
    if tamper == "target":
        rows[0]["target"]["value"] = 1
    else:
        next(iter(rows[0]["features"].values()))["value"] = 123.0
    data_path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    body = json.loads(manifest.read_text())
    body["dataset"]["sha256"] = hashlib.sha256(data_path.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(body), encoding="utf-8")
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    with pytest.raises(DataError, match="bind exact"):
        train_manifest(manifest, digest, tmp_path / "candidate")
    assert not (tmp_path / "candidate").exists()


def test_unknown_or_disagreeing_shots_abstain():
    from pipeline.expected_outcomes.extract import extract_snapshot

    event, tracks = _observed_release()
    for track in tracks:
        del track["track_points"][0]["shot_id"]
    with pytest.raises(DataError, match="exact"):
        extract_snapshot("completion", tracks, [event], snapshot()["lineage"], analytics_safe=True)
    event, tracks = _observed_release()
    event["shot_id"] = "other-shot"
    with pytest.raises(DataError, match="different shots"):
        extract_snapshot("completion", tracks, [event], snapshot()["lineage"], analytics_safe=True)


@pytest.mark.parametrize("action", ["missing", "changed", "during_probe"])
def test_verifier_reopens_actual_sources_after_training(tmp_path, action, monkeypatch):
    from training.expected_outcomes import train_manifest
    from training.verify_expected_outcomes import verify_local

    manifest, digest = _manifest(tmp_path)
    artifact = train_manifest(manifest, digest, tmp_path / "candidate")["models"]["completion"]
    source = tmp_path / "fixture-holdout.mp4"
    if action == "missing":
        source.unlink()
    elif action == "changed":
        source.write_bytes(b"changed-source")
    else:
        from pipeline.video_timeline import VideoTimeline

        probe = VideoTimeline.from_path

        def changing_probe(path):
            timeline = probe(path)
            if Path(path) == source:
                with source.open("ab") as stream:
                    stream.write(b"changed-during-probe")
            return timeline

        monkeypatch.setattr(VideoTimeline, "from_path", changing_probe)
    with pytest.raises(DataError):
        verify_local(
            Path(artifact["checkpoint_path"]),
            artifact["artifact_sha256"],
            Path(artifact["report_path"]),
            artifact["report_sha256"],
            Path(artifact["release_evidence_path"]),
            artifact["release_evidence_sha256"],
        )


def test_prepare_freezes_exports_and_staff_targets_without_filling_labels(tmp_path):
    import csv

    from training.expected_outcomes import load_manifest
    from training.prepare_expected_outcomes import prepare_manifest, targets_template

    original, _digest = _manifest(tmp_path)
    manifest = json.loads(original.read_text())
    raw = [json.loads(line) for line in (tmp_path / "dataset.jsonl").read_text().splitlines()]
    features = {
        entry["sha256"]: json.loads((tmp_path / entry["path"]).read_text())
        for entry in manifest["evidence"]
        if entry["kind"] == "features"
    }
    packets = {}
    for row in raw:
        packet = {key: value for key, value in row.items() if key not in {"target", "split"}}
        packet["feature_evidence"] = features[
            next(iter(row["features"].values()))["evidence_sha256"]
        ]
        packets[row["lineage"]["canonical_play_id"]] = packet
    export = tmp_path / "stage-export.json"
    export.write_text(json.dumps({"feature_snapshots": packets}), encoding="utf-8")
    exports = [(export, hashlib.sha256(export.read_bytes()).hexdigest())]
    csv_path = tmp_path / "staff-targets.csv"
    targets_template(exports, csv_path)
    with csv_path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        columns, human = reader.fieldnames, list(reader)
    assert all(row["value"] == row["reviewed"] == row["source"] == "" for row in human)
    originals = {row["lineage"]["canonical_play_id"]: row for row in raw}
    # Explicit fixture annotations stand in for actual staff entry; the helper fills none.
    for row in human:
        truth = originals[row["canonical_play_id"]]
        row.update(
            value=str(truth["target"]["value"]),
            source="human",
            reviewed="true",
            reviewer_id="fixture-reviewer",
            reviewed_at="2026-10-21T12:00:00Z",
            split=truth["split"],
        )
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(human)
    media = [tmp_path / f"fixture-{split}.mp4" for split in ("train", "calibration", "holdout")]
    digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    result = prepare_manifest(exports, csv_path, digest, media, tmp_path / "frozen")
    assert result["targets_invented"] is False and result["reviewed_rows"] == 12
    assert len(load_manifest(Path(result["manifest_path"]), result["manifest_sha256"])[0]) == 12
    human[0]["value"] = ""
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(human)
    with pytest.raises(DataError, match="blank targets"):
        prepare_manifest(
            exports,
            csv_path,
            hashlib.sha256(csv_path.read_bytes()).hexdigest(),
            media,
            tmp_path / "blank",
        )
    assert not (tmp_path / "blank").exists()
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[column for column in columns if column != "split"],
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(human)
    with pytest.raises(DataError, match="CSV columns"):
        prepare_manifest(
            exports,
            csv_path,
            hashlib.sha256(csv_path.read_bytes()).hexdigest(),
            media,
            tmp_path / "no-split",
        )


@pytest.mark.parametrize("sentinel", ["unknown", "unverified", "unresolved"])
def test_prediction_features_require_known_shots_and_visibility(sentinel):
    from pipeline.expected_outcomes.extract import extract_snapshot

    event, tracks = _observed_release()
    event["shot_id"] = sentinel
    for track in tracks:
        track["track_points"][0]["shot_id"] = sentinel
    with pytest.raises(DataError, match="known"):
        extract_snapshot("completion", tracks, [event], snapshot()["lineage"], analytics_safe=True)
    event, tracks = _observed_release()
    tracks[0]["track_points"][0].pop("visibility_state")
    with pytest.raises(DataError, match="exact"):
        extract_snapshot("completion", tracks, [event], snapshot()["lineage"], analytics_safe=True)


@pytest.mark.parametrize(
    "fault", [None, "missing_calls", "los_x", "ball_y", "bool_down", "bool_direction"]
)
def test_pressure_uses_original_roles_and_actual_calls_without_depth_inference(fault):
    from pipeline.expected_outcomes.extract import extract_snapshot

    ol = {role: role.lower() for role in ("LT", "LG", "C", "RG", "RT")}
    defensive = {"dt": "DT", "de": "DE", "nt": "NT", "shallow_lb": "LB", "deep_lb": "LB"}
    coordinates = {
        **{identity: (49.0, y) for identity, y in zip(ol.values(), (6.0, 3.0, 0.0, -3.0, -6.0))},
        "dt": (51.0, 1.0),
        "de": (51.0, 7.0),
        "nt": (51.0, -1.0),
        "shallow_lb": (50.5, 2.0),
        "deep_lb": (54.5, -2.0),
    }
    roles = {identity: role for role, identity in ol.items()} | defensive
    tracks = [
        {
            "id": identity,
            "source": "human",
            "observed_role": role,
            "role_resolved": True,
            "side_of_ball": "offense" if identity in ol.values() else "defense",
            "track_points": [
                {
                    "timestamp_seconds": 2.0,
                    "frame_number": 60,
                    "field_x": coordinates[identity][0],
                    "field_y": coordinates[identity][1],
                    "shot_id": "shot",
                    "visibility_state": "observed",
                }
            ],
        }
        for identity, role in roles.items()
    ]
    attrs = {
        "play_design": "pass",
        "feature_context_available_at_seconds": 1.9,
        "ol_roles": ol,
        "defensive_roles": defensive,
        "defender_track_ids": list(defensive),
        "defender_population_complete": True,
        "front_population_complete": True,
        "call_context_known": True,
        "call_context_available_at_seconds": 1.8,
        "called_rusher_ids": ["dt", "de", "nt"],
        "called_blitzer_ids": [],
        "protection_blocker_ids": list(ol.values()),
        "dl_gap_alignment": {"dt": "a", "de": "edge", "nt": "a"},
        "down": 2,
        "distance_yards": 7.0,
        "attacking_direction": 1,
        "los_x": 50.0,
        "ball_y": 0.0,
    }
    event = {
        "event_type": "snap",
        "source": "human",
        "timestamp_seconds": 2.0,
        "frame_number": 60,
        "shot_id": "shot",
        "attributes": attrs,
    }
    if fault is not None:
        if fault == "missing_calls":
            attrs.pop("called_blitzer_ids")
        elif fault == "bool_down":
            attrs["down"] = True
        elif fault == "bool_direction":
            attrs["attacking_direction"] = True
        else:
            attrs[fault] = 101.0
        with pytest.raises(DataError):
            extract_snapshot(
                "pressure",
                tracks,
                [event],
                snapshot("pressure")["lineage"],
                analytics_safe=True,
                attacking_direction=1,
                down=1 if fault == "bool_down" else 2,
                distance_yards=7.0,
            )
        return
    result = extract_snapshot(
        "pressure",
        tracks,
        [event],
        snapshot("pressure")["lineage"],
        analytics_safe=True,
        attacking_direction=1,
        down=2,
        distance_yards=7.0,
    )
    assert result["feature_version"] == "expected-outcomes-2"
    assert (
        extract_snapshot(
            "pressure", tracks, [event], snapshot("pressure")["lineage"], analytics_safe=True
        )["features"]
        == result["features"]
    )
    with pytest.raises(DataError):
        extract_snapshot(
            "pressure",
            tracks,
            [event],
            snapshot("pressure")["lineage"],
            analytics_safe=True,
            attacking_direction=-1,
            down=2,
            distance_yards=7.0,
        )
    assert result["features"]["lb_count"]["value"] == 2
    assert result["features"]["dl_count"]["value"] == 3
    assert (
        result["features"]["blitzers"]["value"] == 0
    )  # The shallow LB is not relabeled as a blitzer.
    te = copy.deepcopy(tracks[0])
    te.update(id="optional-te", observed_role="TE", side_of_ball="offense")
    tracks.append(te)
    attrs["ol_roles"]["LTE"] = "optional-te"
    with_te = extract_snapshot(
        "pressure",
        tracks,
        [event],
        snapshot("pressure")["lineage"],
        analytics_safe=True,
        attacking_direction=1,
        down=2,
        distance_yards=7.0,
    )
    assert (
        with_te["features"]["mean_gap_width"]["value"]
        == result["features"]["mean_gap_width"]["value"]
    )
    assert (
        with_te["feature_evidence"]["observations"]["context"]["ol_roles"]["LTE"] == "optional-te"
    )
    te["side_of_ball"] = "defense"
    with pytest.raises(DataError, match="team side"):
        extract_snapshot(
            "pressure",
            tracks,
            [event],
            snapshot("pressure")["lineage"],
            analytics_safe=True,
            attacking_direction=1,
            down=2,
            distance_yards=7.0,
        )


def test_uploaded_candidate_can_reach_separate_fenced_verification(tmp_path, monkeypatch):
    import shutil
    import tempfile

    from pipeline import backend, storage
    from training import verify_expected_outcomes
    from training.expected_outcomes import train_expected_outcomes_job

    store, registered = {}, []

    def upload(path, key):
        target = tmp_path / "object-store" / key
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        uri = f"s3://artifacts/{key}"
        store[uri] = target
        return uri

    def download(uri):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".json") as handle:
            target = Path(handle.name)
        shutil.copyfile(store[uri], target)
        return target

    monkeypatch.setattr(storage, "upload_file", upload)
    monkeypatch.setattr(storage, "download_to_temp", download)
    monkeypatch.setattr(
        backend, "register_model_version", lambda **kw: registered.append(kw) or {"id": "candidate"}
    )
    monkeypatch.setattr(
        verify_expected_outcomes,
        "lease_payload",
        lambda _: {
            "worker_id": "trusted-worker",
            "lease_token": "fixture-lease",
            "attempt_count": 1,
        },
    )
    manifest, digest = _manifest(tmp_path)
    trained = train_expected_outcomes_job(
        {
            "manifest_path": str(manifest),
            "manifest_sha256": digest,
            "output_dir": str(tmp_path / "candidate"),
        },
        "train-job",
    )
    model = registered[0]
    artifact = trained["models"]["completion"]
    inputs = {
        "artifact_uri": model["artifact_uri"],
        "artifact_sha256": artifact["artifact_sha256"],
        "report_uri": model["metrics"]["report_uri"],
        "report_sha256": artifact["report_sha256"],
        "release_evidence_uri": model["metrics"]["release_evidence_uri"],
        "release_evidence_sha256": artifact["release_evidence_sha256"],
        "task": "completion",
        "model_id": "candidate",
        "challenge_id": "server-challenge",
        "dataset_identity": json.loads(Path(artifact["checkpoint_path"]).read_text())[
            "dataset_identity"
        ],
    }
    result = verify_expected_outcomes.verify_expected_outcomes_job(inputs, "verify-job")
    receipt_path = store[result["verification_receipt_uri"]]
    assert (
        hashlib.sha256(receipt_path.read_bytes()).hexdigest()
        == result["verification_receipt_sha256"]
    )
    receipt = json.loads(receipt_path.read_text())
    assert receipt["forward_outputs_matched"] and receipt["source_media_verified"]
    assert (
        receipt["challenge_id"] == "server-challenge"
        and receipt["verification_job_id"] == "verify-job"
    )


def test_independent_verifier_reloads_weights_and_refuses_target_copied_scores(tmp_path):
    from training.expected_outcomes import train_manifest
    from training.verify_expected_outcomes import verify_local

    manifest, manifest_hash = _manifest(tmp_path)
    artifact = train_manifest(manifest, manifest_hash, tmp_path / "candidate")["models"][
        "completion"
    ]
    verified = verify_local(
        Path(artifact["checkpoint_path"]),
        artifact["artifact_sha256"],
        Path(artifact["report_path"]),
        artifact["report_sha256"],
        Path(artifact["release_evidence_path"]),
        artifact["release_evidence_sha256"],
    )
    assert verified["forward_outputs_matched"] is True
    audit_path = Path(artifact["release_evidence_path"])
    audit = json.loads(audit_path.read_text())
    for i, prediction in enumerate(audit["predictions"]):
        prediction["value"] = i % 2
    audit_path.write_text(json.dumps(audit), encoding="utf-8")
    with pytest.raises(DataError, match="forward"):
        verify_local(
            Path(artifact["checkpoint_path"]),
            artifact["artifact_sha256"],
            Path(artifact["report_path"]),
            artifact["report_sha256"],
            audit_path,
            hashlib.sha256(audit_path.read_bytes()).hexdigest(),
        )


def test_named_human_clocks_survive_reconciliation_and_refuse_ambiguity():
    from pipeline.expected_outcomes.extract import extract_snapshot
    from pipeline.orchestrator import _merge_reviewed_event

    first, tracks = _observed_release()
    first["id"] = "reviewed-first"
    second = copy.deepcopy(first)
    second.update(id="reviewed-second", timestamp_seconds=2.1)
    events = []
    _merge_reviewed_event(events, first)
    _merge_reviewed_event(events, second)
    _merge_reviewed_event(events, first)  # retry replaces only its own ID
    assert {e["id"] for e in events} == {"reviewed-first", "reviewed-second"}
    with pytest.raises(DataError, match="ambiguous"):
        extract_snapshot("completion", tracks, events, snapshot()["lineage"], analytics_safe=True)


@pytest.mark.parametrize("key", ["feature_context_timebase", "call_context_timebase"])
def test_context_clock_tags_are_recording_and_hash_bound(key):
    from pipeline.expected_outcomes.extract import extract_snapshot

    event, tracks = _observed_release()
    event["attributes"][key] = "clip"
    with pytest.raises(DataError, match="recording timebase"):
        extract_snapshot("completion", tracks, [event], snapshot()["lineage"], analytics_safe=True)
    event["attributes"][key] = "recording"
    packet = extract_snapshot(
        "completion", tracks, [event], snapshot()["lineage"], analytics_safe=True
    )
    assert packet["feature_evidence"]["observations"]["context"][key] == "recording"
