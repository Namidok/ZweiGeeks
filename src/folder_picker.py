"""Native 'choose a folder' dialog, opened on the machine running ZweiGeeks.

A browser can't hand a web page a folder's real path, but ZweiGeeks is a
local tool: the server and the browser are the same computer, so the server
can show the operating system's own dialog and return the path.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Optional

PROMPT = "Choose the git repository to audit"
TIMEOUT_SECONDS = 600


class PickerUnavailable(Exception):
    """No folder dialog can be opened on this system."""


def _command() -> list[str]:
    if sys.platform == "darwin":
        return [
            "osascript",
            "-e", "try",
            "-e", f'POSIX path of (choose folder with prompt "{PROMPT}")',
            "-e", "on error number -128",  # the user pressed Cancel
            "-e", 'return ""',
            "-e", "end try",
        ]
    if sys.platform.startswith("win"):
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            f"$d = New-Object System.Windows.Forms.FolderBrowserDialog; $d.Description = '{PROMPT}';"
            "if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath }"
        )
        return ["powershell", "-NoProfile", "-Command", script]
    if shutil.which("zenity"):
        return ["zenity", "--file-selection", "--directory", f"--title={PROMPT}"]
    raise PickerUnavailable("No folder dialog available here (install zenity), so type the path instead.")


def pick_folder() -> Optional[str]:
    """The chosen folder, or None if the user cancelled."""
    try:
        proc = subprocess.run(_command(), capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    except FileNotFoundError as exc:
        raise PickerUnavailable(f"Couldn't open a folder dialog ({exc.filename} not found).") from exc
    except subprocess.TimeoutExpired:
        return None
    path = proc.stdout.strip()
    if proc.returncode != 0 and not path:
        return None
    if not path:
        return None
    return path.rstrip("/") if len(path) > 1 else path  # macOS returns "/Users/me/proj/"
