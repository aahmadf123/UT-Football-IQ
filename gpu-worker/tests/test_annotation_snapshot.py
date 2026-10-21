import httpx
import pytest

from pipeline import backend


@pytest.mark.parametrize("changed", [False, True])
def test_complete_dense_human_annotation_snapshot_is_revision_bound(monkeypatch, changed):
    events = [
        {
            "id": f"event-{i}",
            "event_type": "protection_envelope",
            "timestamp_seconds": i / 100,
            "attributes": {"source": "human"},
        }
        for i in range(1201)
    ]
    pages = []
    labels = [{"id": f"label-{i}", "source": "human", "label_type": "offensive_formation",
               "annotated_by": "cf128509-d42d-4532-8c4d-67e44150d391",
               "label_value": {"formation": "Pistol"}} for i in range(1201)]
    label_pages = []

    def response(request):
        if request.url.path.endswith("/removed-events"):
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/labels"):
            source = request.url.params["source"]
            offset, limit = int(request.url.params["offset"]), int(request.url.params["limit"])
            label_pages.append((source, offset))
            return httpx.Response(200, json=labels[offset:offset + limit] if source == "human" else [])
        if request.url.path.endswith("/events"):
            assert request.url.params["human_only"] == "true"
            offset, limit = int(request.url.params["offset"]), int(request.url.params["limit"])
            pages.append(offset)
            return httpx.Response(200, json=events[offset : offset + limit])
        raise AssertionError(request.url)

    monkeypatch.setattr(backend, "BACKEND_API_URL", "http://backend.test")
    monkeypatch.setattr(
        backend, "_fetch_clips", lambda **kwargs: [{"id": "clip", "annotation_revision": 2}]
    )
    monkeypatch.setattr(backend, "fetch_video_context", lambda _: {"content_sha256": "a" * 64})
    monkeypatch.setattr(
        backend, "fetch_clip", lambda _: {"annotation_revision": 3 if changed else 2}
    )
    monkeypatch.setattr(
        backend,
        "_client",
        lambda: httpx.Client(
            base_url="http://backend.test", transport=httpx.MockTransport(response)
        ),
    )
    if changed:
        with pytest.raises(ValueError, match="changed during snapshot"):
            backend.fetch_annotation_snapshot("video")
    else:
        result = backend.fetch_annotation_snapshot("video")
        assert result[0]["human_events"] == events
        assert result[0]["human_events"][-1]["id"] == "event-1200"
        assert result[0]["human_labels"] == labels
    assert pages == [0, 500, 1000]
    assert label_pages == [("human", 0), ("human", 500), ("human", 1000), ("manual", 0)]
