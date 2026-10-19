"""Secret-safe error handling for the field dataset downloader."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from training.field.download import download


@pytest.mark.parametrize("failed_request", ["metadata", "archive"])
def test_http_status_error_does_not_print_api_key(
    failed_request, monkeypatch, tmp_path
) -> None:
    secret = "test-roboflow-secret"
    monkeypatch.setenv("ROBOFLOW_API_KEY", secret)
    request = httpx.Request("GET", f"https://api.roboflow.com/export?api_key={secret}")
    response = httpx.Response(401, request=request)
    status_error = httpx.HTTPStatusError(
        "request failed", request=request, response=response
    )
    client = MagicMock()
    client.__enter__.return_value = client
    if failed_request == "metadata":
        client.get.side_effect = status_error
    else:
        metadata = MagicMock()
        metadata.json.return_value = {"export": {"link": "https://download.test/archive"}}
        metadata.raise_for_status.return_value = None
        archive = MagicMock()
        archive.raise_for_status.side_effect = status_error
        client.get.side_effect = [metadata, archive]

    with patch("httpx.Client", return_value=client):
        with pytest.raises(SystemExit, match="HTTP 401") as exc:
            download(30, tmp_path / "output")

    assert secret not in str(exc.value)
