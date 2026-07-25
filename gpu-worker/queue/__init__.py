# This package is named 'queue' to match the gpu-worker/queue/ directory layout
# required by ISSUE16_PLAN.md.  To avoid shadowing stdlib's queue module we
# forward all stdlib attributes here so that code doing `import queue; queue.Queue`
# continues to work when gpu-worker is on sys.path.

import importlib.util
import os
import sysconfig


def _load_stdlib_queue() -> None:
    """Inject stdlib queue attributes into this namespace."""
    try:
        stdlib_dir = sysconfig.get_path("stdlib")
        queue_py = os.path.join(stdlib_dir or "", "queue.py")
        spec = importlib.util.spec_from_file_location("_stdlib_queue_impl", queue_py)
        if spec is None or spec.loader is None:
            return
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]

        g = globals()
        for _name in ("Empty", "Full", "Queue", "LifoQueue", "PriorityQueue", "SimpleQueue"):
            if hasattr(mod, _name):
                g[_name] = getattr(mod, _name)
    except Exception:
        pass


_load_stdlib_queue()
del _load_stdlib_queue
