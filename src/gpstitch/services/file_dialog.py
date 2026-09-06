"""Open the host's native file picker and return the chosen path.

GPStitch runs on the same machine as the files it renders, so the most useful
"browse" control is the operating system's own dialog - Finder on macOS - rather
than a filesystem browser reimplemented in the page. That also means the server
never exposes a directory listing: one path comes back, nothing is enumerated.

Only macOS is verified end-to-end. The Linux and Windows commands are
constructed and unit-tested here but have not been run against a real desktop.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

# Extensions offered per field, mirroring the FILES panel's two inputs.
KIND_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "video": ("mp4", "mov", "avi"),
    "gps": ("gpx", "fit", "srt"),
}

# A dialog waits on a human, so the timeout is generous rather than snappy.
DEFAULT_TIMEOUT_S = 300.0

# Only one panel may be open at a time; a second request is refused rather than
# stacking dialogs the user has to dismiss one by one.
_dialog_lock = asyncio.Lock()


class FileDialogUnavailable(Exception):
    """No native picker exists for this platform."""


class FileDialogBusy(Exception):
    """A picker is already open."""


def _extensions_for(kind: str) -> tuple[str, ...]:
    try:
        return KIND_EXTENSIONS[kind]
    except KeyError:
        raise ValueError(f"Unknown file kind: {kind!r}") from None


def build_command(platform: str, kind: str) -> list[str]:
    """Build the native picker command for `platform`."""
    extensions = _extensions_for(kind)
    label = "Video" if kind == "video" else "GPS data"

    if platform == "darwin":
        of_type = ", ".join(f'"{e}"' for e in extensions)
        return [
            "osascript",
            # Without this the panel can open behind the browser window.
            "-e",
            'tell application "System Events" to activate',
            "-e",
            f'POSIX path of (choose file with prompt "Choose {label}" of type {{{of_type}}})',
        ]

    if platform.startswith("linux"):
        patterns = " ".join(f"*.{e}" for e in extensions)
        return [
            "zenity",
            "--file-selection",
            f"--title=Choose {label}",
            f"--file-filter={label} | {patterns}",
        ]

    if platform.startswith("win"):
        patterns = ";".join(f"*.{e}" for e in extensions)
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$d = New-Object System.Windows.Forms.OpenFileDialog; "
            f"$d.Title = 'Choose {label}'; "
            f"$d.Filter = '{label}|{patterns}'; "
            "if ($d.ShowDialog() -eq 'OK') { $d.FileName }"
        )
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]

    raise FileDialogUnavailable(f"No native file dialog for platform {platform!r}")


def parse_result(platform: str, returncode: int, stdout: str, stderr: str) -> str | None:
    """Turn a picker's exit into a path, or None if the user dismissed it.

    Cancelling is a normal outcome, not a failure: every backend signals it
    differently, which is why this is a separate, tested step.
    """
    path = stdout.strip()

    if platform == "darwin":
        if returncode == 0:
            return path or None
        # AppleScript spells it "canceled"; -128 is the user-cancelled code.
        if "user canceled" in stderr.lower() or "-128" in stderr:
            return None
        raise RuntimeError(stderr.strip() or "The file dialog closed unexpectedly")

    if platform.startswith("linux"):
        # zenity exits 1 on dismiss, with nothing on stdout.
        if returncode != 0:
            if path:
                raise RuntimeError(stderr.strip() or "The file dialog closed unexpectedly")
            return None
        return path or None

    if platform.startswith("win"):
        # PowerShell exits 0 either way; an empty result means dismissed.
        if returncode != 0:
            raise RuntimeError(stderr.strip() or "The file dialog closed unexpectedly")
        return path or None

    raise FileDialogUnavailable(f"No native file dialog for platform {platform!r}")


async def _subprocess_runner(cmd: list[str], timeout: float) -> tuple[int, str, str]:
    """Run `cmd`, never blocking the event loop while the dialog is open."""
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError(f"File dialog timed out after {timeout:.0f}s") from None
    return process.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def choose_file(
    kind: str,
    *,
    runner=None,
    platform: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> str | None:
    """Open the native picker for `kind`. Returns the path, or None if cancelled."""
    import sys

    resolved_platform = sys.platform if platform is None else platform
    cmd = build_command(resolved_platform, kind)

    if _dialog_lock.locked():
        raise FileDialogBusy("A file dialog is already open")

    run = runner if runner is not None else _subprocess_runner
    async with _dialog_lock:
        returncode, stdout, stderr = await run(cmd, timeout)

    return parse_result(resolved_platform, returncode, stdout, stderr)
