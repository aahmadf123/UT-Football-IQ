"""Small native launcher; the film-room experience stays in the browser."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from local_app.config import LocalConfig, atomic_json, default_data_directory


def run_gui(data: Path | None = None) -> int:
    directory = (data or default_data_directory()).expanduser().absolute()
    window = tk.Tk()
    window.title("Football-IQ")
    window.geometry("510x350")
    window.minsize(480, 330)
    window.configure(background="#071729")
    style = ttk.Style(window)
    style.configure("TFrame", background="#071729")
    style.configure(
        "TLabel", background="#071729", foreground="#e8eef4", font=("Arial", 11)
    )
    style.configure("Title.TLabel", font=("Arial", 22, "bold"))
    style.configure("TButton", font=("Arial", 11), padding=10)
    frame = ttk.Frame(window, padding=25)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="Football-IQ", style="Title.TLabel").pack(anchor="w")
    ttk.Label(frame, text="Your film. Your computer. Ready to review.").pack(
        anchor="w", pady=(4, 20)
    )
    state = tk.StringVar(value="Open a local library, or connect to your team library.")
    ttk.Label(frame, textvariable=state, wraplength=445).pack(anchor="w", pady=(0, 12))
    child: subprocess.Popen[bytes] | None = None
    log = None
    current_mode = "local"
    current_frontend: str | None = None
    current_arguments: list[str] = []

    def start(
        extra: list[str] | None = None, credentials: dict[str, str] | None = None
    ) -> None:
        nonlocal child, log, current_mode, current_frontend, current_arguments
        requested_mode = "connected" if "--server-url" in (extra or []) else "local"
        if child and child.poll() is None:
            if requested_mode != current_mode or (
                requested_mode == "connected" and (extra or []) != current_arguments
            ):
                state.set("Quit the current launcher before changing its connection.")
                return
            if current_mode == "connected":
                if current_frontend:
                    webbrowser.open(current_frontend)
                state.set("Processing is already connected to your team library.")
                return
            from local_app.reopen import reopen

            reopen(LocalConfig.load(directory))
            return
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        (directory / "logs").mkdir(exist_ok=True)
        arguments = [sys.executable]
        if not getattr(sys, "frozen", False):
            arguments += ["-m", "local_app"]
        arguments += ["start", "--data-dir", str(directory), *(extra or [])]
        env = os.environ.copy()
        env.update(credentials or {})
        options: dict[str, object] = {}
        if sys.platform == "win32":
            options["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        else:
            options["start_new_session"] = True
        if log:
            log.close()
        log = (directory / "logs/launcher.log").open("ab")
        try:
            child = subprocess.Popen(
                arguments, env=env, stdout=log, stderr=subprocess.STDOUT, **options
            )
        except OSError:
            log.close()
            log = None
            state.set("The library could not start. Open diagnostics for details.")
            return
        current_mode = requested_mode
        current_arguments = list(extra or [])
        current_frontend = None
        if extra and "--frontend-url" in extra:
            current_frontend = extra[extra.index("--frontend-url") + 1]
        state.set("Opening your library…")

    def connect() -> None:
        dialog = tk.Toplevel(window)
        dialog.title("Connect a team library")
        dialog.geometry("440x390")
        pane = ttk.Frame(dialog, padding=20)
        pane.pack(fill="both", expand=True)
        values: dict[str, tk.StringVar] = {}
        for key, label in (
            ("api", "Team API address"),
            ("website", "Team film-room address"),
            ("email", "Processing account email"),
            ("password", "Password"),
        ):
            ttk.Label(pane, text=label).pack(anchor="w", pady=(5, 2))
            value = tk.StringVar()
            values[key] = value
            ttk.Entry(
                pane, textvariable=value, show="•" if key == "password" else ""
            ).pack(fill="x")

        def submit() -> None:
            if not all(
                values[key].get().strip() for key in ("api", "email", "password")
            ):
                messagebox.showerror(
                    "Connection details",
                    "Enter the team address and processing account.",
                    parent=dialog,
                )
                return
            extra = ["--server-url", values["api"].get().strip()]
            if values["website"].get().strip():
                extra += ["--frontend-url", values["website"].get().strip()]
            start(
                extra,
                {
                    "WORKER_EMAIL": values["email"].get().strip(),
                    "WORKER_PASSWORD": values["password"].get(),
                },
            )
            values["password"].set("")
            dialog.destroy()

        ttk.Button(
            pane, text="Connect and process on this computer", command=submit
        ).pack(fill="x", pady=16)

    def choose_library() -> None:
        nonlocal directory
        if child and child.poll() is None:
            state.set("Quit the current launcher before switching libraries.")
            return
        selected = filedialog.askdirectory(title="Choose your local library directory")
        if selected:
            directory = Path(selected).absolute()
            state.set("Library folder selected. Open your library to continue.")

    def diagnostics() -> None:
        target = directory / "logs"
        target.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(target)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])

    def quit_app() -> None:
        if child and child.poll() is None:
            try:
                atomic_json(
                    directory / "runtime/controls/launcher-shutdown.json",
                    {"action": "shutdown"},
                )
            except OSError:
                child.send_signal(
                    signal.CTRL_BREAK_EVENT
                    if sys.platform == "win32"
                    else signal.SIGTERM
                )
        if log:
            log.close()
        window.destroy()

    ttk.Button(frame, text="Open my local library", command=start).pack(
        fill="x", pady=4
    )
    ttk.Button(frame, text="Connect my team library", command=connect).pack(
        fill="x", pady=4
    )
    row = ttk.Frame(frame)
    row.pack(fill="x", pady=(12, 0))
    ttk.Button(row, text="Library folder", command=choose_library).pack(side="left")
    ttk.Button(row, text="Diagnostics", command=diagnostics).pack(side="left", padx=5)
    ttk.Button(row, text="Quit", command=quit_app).pack(side="right")

    def refresh() -> None:
        if child and child.poll() is not None:
            if child.returncode:
                state.set("The library could not start. Open diagnostics for details.")
            else:
                state.set("The launcher has closed. Open your library to continue.")
            window.after(1500, refresh)
            return
        try:
            current = json.loads((directory / "runtime/status.json").read_text())
            if not child:
                window.after(1500, refresh)
                return
            if current["state"] == "running":
                state.set(
                    "Processing is connected to your team library."
                    if current_mode == "connected"
                    else "Your library is running. Processing uses this computer."
                )
            elif current["state"] == "needs_attention":
                state.set(
                    "A service needs attention. Open diagnostics "
                    "or use This computer in the film room."
                )
        except (OSError, ValueError, KeyError):
            if child and child.poll() not in {None, 0}:
                state.set("The library could not start. Open diagnostics for details.")
        window.after(1500, refresh)

    window.protocol("WM_DELETE_WINDOW", quit_app)
    window.after(1500, refresh)
    window.mainloop()
    return 0
