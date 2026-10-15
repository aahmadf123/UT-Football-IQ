"""Open an already-running local library with a fresh one-use sign-in."""

import json
import urllib.error
import urllib.request
import webbrowser
from urllib.parse import quote, urlparse

from local_app.config import LocalConfig


def local_login_url(config: LocalConfig, base: str | None = None) -> str:
    """Mint a fresh browser nonce only after the current API is ready."""
    base = (base or config.api_url).rstrip("/")
    parsed = urlparse(base)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("The running library is a connected processing agent")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        base + "/api/v1/local/bootstrap",
        data=json.dumps({"launcher_key": config.secrets["launcher_key"]}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(request, timeout=5) as response:
        token = json.load(response)["bootstrap_token"]
    return base + "/login/?localToken=" + quote(token)


def reopen(config: LocalConfig, *, no_browser: bool = False) -> int:
    try:
        status = json.loads((config.data / "runtime/status.json").read_text())
        base = status["api_url"]
        # Validate/mint even without opening a browser so readiness is checked.
        login_url = local_login_url(config, base)
        print(f"Football-IQ is already running at {base}")
        if not no_browser:
            webbrowser.open(login_url)
        return 0
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        print(
            "The library is starting or needs attention. Check its local diagnostics."
        )
        return 1
