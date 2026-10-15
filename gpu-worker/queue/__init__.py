# This package is named 'queue' to match the gpu-worker/queue/ directory layout
# required by ISSUE16_PLAN.md.  To avoid shadowing stdlib's queue module we
# forward all stdlib attributes here so that code doing `import queue; queue.Queue`
# continues to work when gpu-worker is on sys.path.

import importlib.util
import sys
import sysconfig
from pathlib import Path


def _load_stdlib_queue() -> None:
    """Inject stdlib queue attributes into this namespace."""
    bundle_root = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) or bundle_root:
        queue_py = Path(bundle_root or "") / "_stdlib" / "queue.py"
    else:
        queue_py = Path(sysconfig.get_path("stdlib") or "") / "queue.py"
    if not queue_py.is_file():
        raise ImportError(f"Required standard-library queue implementation is missing: {queue_py}")
    spec = importlib.util.spec_from_file_location("_stdlib_queue_impl", queue_py)
    if spec is None or spec.loader is None:
        raise ImportError("Unable to load the standard-library queue implementation")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_stdlib_queue_impl"] = mod
    spec.loader.exec_module(mod)

    g = globals()
    for name in ("Empty", "Full", "Queue", "LifoQueue", "PriorityQueue", "SimpleQueue"):
        if not hasattr(mod, name):
            raise ImportError(f"Standard-library queue is missing required class {name}")
        g[name] = getattr(mod, name)


_load_stdlib_queue()
del _load_stdlib_queue
