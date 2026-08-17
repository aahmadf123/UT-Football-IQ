"""Roboflow operations for the Football-IQ dataset/training loop.

Owner-side tooling — coaches never touch any of this. The product loop stays
zero-touch (upload → pipeline → results); these CLIs feed and improve the
models behind it:

    python -m roboflow_ops.consolidate      merge legacy projects (remapped)
    python -m roboflow_ops.frames           sample + upload video frames
    python -m roboflow_ops.autolabel        start/describe auto-label runs
    python -m roboflow_ops.active_learning  export low-confidence frames
    python -m roboflow_ops.download         pull a dataset version for training
    python -m roboflow_ops.hosted_train     kick off Roboflow-hosted training

The package is named ``roboflow_ops`` (not ``roboflow``) so it can never
shadow the ``roboflow`` pip SDK on sys.path. The SDK is an optional
dependency (requirements-roboflow.txt) imported lazily — CI's stub install
must keep passing without it.
"""
