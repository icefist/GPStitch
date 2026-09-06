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


FOLDER_KIND = "folder"

# zenity --multiple joins its results with this rather than newlines.
ZENITY_SEPARATOR = "|"


def _extensions_for(kind: str) -> tuple[str, ...]:
    try:
        return KIND_EXTENSIONS[kind]
    except KeyError:
        raise ValueError(f"Unknown file kind: {kind!r}") from None


def _label_for(kind: str) -> str:
    if kind == FOLDER_KIND:
        return "output folder"
    return "Video" if kind == "video" else "GPS data"


def _folder_command(platform: str) -> list[str]:
    label = _label_for(FOLDER_KIND)
    if platform == "darwin":
        return [
            "osascript",
            "-e",
            'tell application "System Events" to activate',
            "-e",
            f'POSIX path of (choose folder with prompt "Choose {label}")',
        ]
    if platform.startswith("linux"):
        return ["zenity", "--file-selection", "--directory", f"--title=Choose {label}"]
    if platform.startswith("win"):
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
            f"$d.Description = 'Choose {label}'; "
            "if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath }"
        )
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    raise FileDialogUnavailable(f"No native file dialog for platform {platform!r}")


def build_command(platform: str, kind: str, multiple: bool = False) -> list[str]:
    """Build the native picker command for `platform`."""
    if kind == FOLDER_KIND:
        if multiple:
            # The three platforms disagree on multi-folder selection and nothing
            # here needs it; refuse rather than guess.
            raise ValueError("Multiple selection is not supported for folders")
        return _folder_command(platform)

    extensions = _extensions_for(kind)
    label = _label_for(kind)

    if platform == "darwin":
        of_type = ", ".join(f'"{e}"' for e in extensions)
        # Without the activate the panel can open behind the browser window.
        activate = 'tell application "System Events" to activate'
        if multiple:
            # choose file returns a list here, so walk it into newline-separated
            # POSIX paths - AppleScript will not coerce a list for us.
            return [
                "osascript",
                "-e",
                activate,
                "-e",
                f'set chosen to choose file with prompt "Choose {label}" '
                f"of type {{{of_type}}} with multiple selections allowed",
                "-e",
                'set out to ""',
                "-e",
                "repeat with f in chosen",
                "-e",
                "set out to out & (POSIX path of f) & linefeed",
                "-e",
                "end repeat",
                "-e",
                "return out",
            ]
        return [
            "osascript",
            "-e",
            activate,
            "-e",
            f'POSIX path of (choose file with prompt "Choose {label}" of type {{{of_type}}})',
        ]

    if platform.startswith("linux"):
        patterns = " ".join(f"*.{e}" for e in extensions)
        cmd = [
            "zenity",
            "--file-selection",
            f"--title=Choose {label}",
            f"--file-filter={label} | {patterns}",
        ]
        if multiple:
            cmd += ["--multiple", f"--separator={ZENITY_SEPARATOR}"]
        return cmd

    if platform.startswith("win"):
        patterns = ";".join(f"*.{e}" for e in extensions)
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$d = New-Object System.Windows.Forms.OpenFileDialog; "
            f"$d.Title = 'Choose {label}'; "
            f"$d.Filter = '{label}|{patterns}'; "
        )
        if multiple:
            script += "$d.Multiselect = $true; if ($d.ShowDialog() -eq 'OK') { $d.FileNames }"
        else:
            script += "if ($d.ShowDialog() -eq 'OK') { $d.FileName }"
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]

    raise FileDialogUnavailable(f"No native file dialog for platform {platform!r}")


def _split_paths(platform: str, raw: str) -> list[str]:
    """Split a picker's output into paths, dropping blanks."""
    # zenity joins multiple results with "|"; the others use newlines.
    parts = raw.split(ZENITY_SEPARATOR) if platform.startswith("linux") else [raw]
    lines: list[str] = []
    for part in parts:
        lines.extend(part.splitlines())
    return [line.strip() for line in lines if line.strip()]


def parse_result(platform: str, returncode: int, stdout: str, stderr: str) -> list[str]:
    """Turn a picker's exit into the chosen paths. An empty list means cancelled.

    Cancelling is a normal outcome, not a failure, and each backend signals it
    differently - which is why this is a separate, tested step.
    """
    raw = stdout.strip()

    if platform == "darwin":
        if returncode == 0:
            return _split_paths(platform, raw)
        # AppleScript spells it "canceled"; -128 is the user-cancelled code.
        if "user canceled" in stderr.lower() or "-128" in stderr:
            return []
        raise RuntimeError(stderr.strip() or "The file dialog closed unexpectedly")

    if platform.startswith("linux"):
        # zenity exits 1 on dismiss, with nothing on stdout.
        if returncode != 0:
            if raw:
                raise RuntimeError(stderr.strip() or "The file dialog closed unexpectedly")
            return []
        return _split_paths(platform, raw)

    if platform.startswith("win"):
        # PowerShell exits 0 either way; an empty result means dismissed.
        if returncode != 0:
            raise RuntimeError(stderr.strip() or "The file dialog closed unexpectedly")
        return _split_paths(platform, raw)

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


async def choose_paths(
    kind: str,
    *,
    multiple: bool = False,
    runner=None,
    platform: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> list[str]:
    """Open the native picker for `kind`. Returns chosen paths; empty if cancelled."""
    import sys

    resolved_platform = sys.platform if platform is None else platform
    cmd = build_command(resolved_platform, kind, multiple=multiple)

    if _dialog_lock.locked():
        raise FileDialogBusy("A file dialog is already open")

    run = runner if runner is not None else _subprocess_runner
    async with _dialog_lock:
        returncode, stdout, stderr = await run(cmd, timeout)

    return parse_result(resolved_platform, returncode, stdout, stderr)


async def choose_file(
    kind: str,
    *,
    runner=None,
    platform: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> str | None:
    """Open the native picker for a single `kind`. None if cancelled."""
    paths = await choose_paths(kind, runner=runner, platform=platform, timeout=timeout)
    return paths[0] if paths else None
