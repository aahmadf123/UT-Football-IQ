"""Child service entry point used by both source and native distributions."""

from __future__ import annotations

import argparse
import os
import runpy
import sys
import threading
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "service", choices=("api", "worker", "migrate", "install-detector")
    )
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    root = Path(
        os.environ.get("FOOTBALL_IQ_BUNDLE_ROOT", Path(__file__).resolve().parents[1])
    )
    if args.service in {"api", "migrate"}:
        sys.path.insert(0, str(root / "backend"))
        if args.service == "migrate":
            from alembic import command
            from alembic.config import Config

            config = Config(str(root / "backend" / "alembic.ini"))
            config.set_main_option(
                "script_location", str(root / "backend" / "migrations")
            )
            command.upgrade(config, "head")
        else:
            import uvicorn

            server = uvicorn.Server(
                uvicorn.Config(
                    "app.local_entry:app",
                    host="127.0.0.1",
                    port=args.port,
                    log_level="info",
                    access_log=False,
                )
            )
            done = threading.Event()

            def watch_stop() -> None:
                stop_file = os.environ.get("LOCAL_APP_STOP_FILE")
                if not stop_file:
                    return
                while not done.wait(0.25):
                    if Path(stop_file).exists():
                        server.should_exit = True
                        return

            watcher = threading.Thread(
                target=watch_stop, daemon=True, name="local-api-stop"
            )
            watcher.start()
            try:
                server.run()
            finally:
                done.set()
                watcher.join(timeout=1)
    elif args.service == "install-detector":
        from ultralytics import YOLO

        directory = Path(os.environ["MODEL_DETECT_PATH"]).parent
        directory.mkdir(parents=True, exist_ok=True)
        os.chdir(directory)
        model = YOLO("yolov8n.pt")
        # Require a real inference before accepting the model installation.
        import numpy as np

        model.predict(
            np.zeros((64, 64, 3), dtype=np.uint8), device="cpu", verbose=False
        )
        print("CPU player model installed and verified")
    else:
        sys.path.insert(0, str(root / "gpu-worker"))
        runpy.run_path(str(root / "gpu-worker" / "__main__.py"), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
