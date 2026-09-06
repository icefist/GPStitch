"""Tests for the native OS file picker.

No test opens a real dialog: the subprocess runner is injected.
"""

import asyncio

import pytest

from gpstitch.services.file_dialog import (
    KIND_EXTENSIONS,
    FileDialogBusy,
    FileDialogUnavailable,
    build_command,
    choose_file,
    choose_paths,
    parse_result,
)


def _runner(returncode=0, stdout="", stderr=""):
    """An injectable runner that records the command it was given."""

    async def run(cmd, timeout):
        run.command = cmd
        run.timeout = timeout
        return returncode, stdout, stderr

    run.command = None
    return run


class TestKinds:
    def test_video_and_gps_kinds_are_disjoint(self):
        assert set(KIND_EXTENSIONS["video"]).isdisjoint(KIND_EXTENSIONS["gps"])

    def test_gps_kind_covers_the_documented_gps_formats(self):
        assert set(KIND_EXTENSIONS["gps"]) == {"gpx", "fit", "srt"}


class TestBuildCommand:
    def test_macos_uses_osascript_and_asks_for_a_posix_path(self):
        cmd = build_command("darwin", "video")
        assert cmd[0] == "osascript"
        joined = " ".join(cmd)
        assert "choose file" in joined
        assert "POSIX path" in joined

    def test_macos_constrains_the_offered_file_types(self):
        joined = " ".join(build_command("darwin", "gps"))
        for ext in ("gpx", "fit", "srt"):
            assert f'"{ext}"' in joined

    def test_macos_activates_first_so_the_dialog_comes_to_front(self):
        """Otherwise the panel opens behind the browser window."""
        assert any("activate" in part for part in build_command("darwin", "video"))

    def test_linux_uses_zenity_with_a_matching_filter(self):
        cmd = build_command("linux", "video")
        assert cmd[0] == "zenity"
        joined = " ".join(cmd)
        assert "--file-selection" in joined
        assert "*.mp4" in joined

    def test_windows_uses_powershell_open_file_dialog(self):
        cmd = build_command("win32", "gps")
        assert cmd[0].lower().startswith("powershell")
        joined = " ".join(cmd)
        assert "OpenFileDialog" in joined
        assert "*.gpx" in joined

    def test_unknown_platform_is_unavailable(self):
        with pytest.raises(FileDialogUnavailable):
            build_command("plan9", "video")

    def test_unknown_kind_is_rejected(self):
        with pytest.raises(ValueError):
            build_command("darwin", "spreadsheet")


class TestParseResult:
    """Per-platform exit handling. parse_result reports a list; [] means cancelled."""

    def test_macos_success_returns_the_trimmed_path(self):
        assert parse_result("darwin", 0, "/Users/me/clip.mp4\n", "") == ["/Users/me/clip.mp4"]

    def test_macos_cancel_returns_nothing(self):
        """osascript exits 1 with 'User canceled.' - not an error."""
        assert parse_result("darwin", 1, "", "execution error: User canceled. (-128)") == []

    def test_macos_real_error_raises(self):
        with pytest.raises(RuntimeError):
            parse_result("darwin", 1, "", "execution error: something actually broke (-1700)")

    def test_linux_cancel_returns_nothing(self):
        """zenity exits 1 with no output when dismissed."""
        assert parse_result("linux", 1, "", "") == []

    def test_linux_success_returns_the_path(self):
        assert parse_result("linux", 0, "/home/me/track.gpx\n", "") == ["/home/me/track.gpx"]

    def test_windows_cancel_returns_nothing(self):
        """PowerShell exits 0 with empty stdout when dismissed."""
        assert parse_result("win32", 0, "\r\n", "") == []

    def test_windows_success_strips_crlf(self):
        assert parse_result("win32", 0, "C:\\Users\\me\\clip.mp4\r\n", "") == ["C:\\Users\\me\\clip.mp4"]


class TestChooseFile:
    async def test_returns_the_chosen_path(self):
        got = await choose_file("video", runner=_runner(0, "/Users/me/clip.mp4\n"), platform="darwin")
        assert got == "/Users/me/clip.mp4"

    async def test_cancel_returns_none_rather_than_raising(self):
        got = await choose_file(
            "video", runner=_runner(1, "", "execution error: User canceled. (-128)"), platform="darwin"
        )
        assert got is None

    async def test_passes_a_timeout_to_the_runner(self):
        """A dialog left open must not pin a worker forever."""
        run = _runner(0, "/x.mp4\n")
        await choose_file("video", runner=run, platform="darwin", timeout=42)
        assert run.timeout == 42

    async def test_unavailable_platform_raises(self):
        with pytest.raises(FileDialogUnavailable):
            await choose_file("video", runner=_runner(), platform="plan9")

    async def test_only_one_dialog_at_a_time(self):
        """A second concurrent request must not stack another panel."""
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(cmd, timeout):
            started.set()
            await release.wait()
            return 0, "/Users/me/clip.mp4\n", ""

        first = asyncio.create_task(choose_file("video", runner=slow, platform="darwin"))
        await started.wait()
        with pytest.raises(FileDialogBusy):
            await choose_file("video", runner=_runner(0, "/other.mp4\n"), platform="darwin")
        release.set()
        assert await first == "/Users/me/clip.mp4"

    async def test_lock_is_released_after_a_failure(self):
        """A crashed dialog must not wedge the feature until restart."""
        with pytest.raises(RuntimeError):
            await choose_file("video", runner=_runner(1, "", "execution error: boom (-1700)"), platform="darwin")
        got = await choose_file("video", runner=_runner(0, "/ok.mp4\n"), platform="darwin")
        assert got == "/ok.mp4"


class TestFolderCommand:
    def test_macos_chooses_a_folder_not_a_file(self):
        joined = " ".join(build_command("darwin", "folder"))
        assert "choose folder" in joined
        assert "choose file" not in joined

    def test_linux_asks_zenity_for_a_directory(self):
        assert "--directory" in build_command("linux", "folder")

    def test_windows_uses_the_folder_browser_dialog(self):
        assert "FolderBrowserDialog" in " ".join(build_command("win32", "folder"))

    def test_folder_command_carries_no_extension_filter(self):
        """A folder picker filtering on .mp4 would show nothing."""
        joined = " ".join(build_command("darwin", "folder"))
        assert "mp4" not in joined


class TestMultiSelectCommand:
    def test_macos_allows_multiple_selections(self):
        joined = " ".join(build_command("darwin", "video", multiple=True))
        assert "multiple selections allowed" in joined

    def test_macos_single_select_does_not(self):
        joined = " ".join(build_command("darwin", "video"))
        assert "multiple selections allowed" not in joined

    def test_linux_passes_multiple_and_a_separator(self):
        cmd = build_command("linux", "video", multiple=True)
        assert "--multiple" in cmd

    def test_windows_sets_multiselect(self):
        assert "Multiselect" in " ".join(build_command("win32", "video", multiple=True))

    def test_folders_cannot_be_multi_selected(self):
        """Out of scope, and the platforms disagree on how - refuse rather than guess."""
        with pytest.raises(ValueError):
            build_command("darwin", "folder", multiple=True)


class TestParseReturnsAllPaths:
    """parse_result reports every chosen path; an empty list means cancelled."""

    def test_single_path_comes_back_as_a_one_item_list(self):
        assert parse_result("darwin", 0, "/Users/me/clip.mp4\n", "") == ["/Users/me/clip.mp4"]

    def test_cancel_is_an_empty_list(self):
        assert parse_result("darwin", 1, "", "execution error: User canceled. (-128)") == []

    def test_macos_multi_select_splits_on_newlines(self):
        stdout = "/Users/me/a.mp4\n/Users/me/b.mp4\n/Users/me/c.mp4\n"
        assert parse_result("darwin", 0, stdout, "") == [
            "/Users/me/a.mp4",
            "/Users/me/b.mp4",
            "/Users/me/c.mp4",
        ]

    def test_blank_lines_are_dropped(self):
        assert parse_result("darwin", 0, "/a.mp4\n\n/b.mp4\n\n", "") == ["/a.mp4", "/b.mp4"]

    def test_linux_multi_select_splits_on_the_pipe_separator(self):
        """zenity --multiple joins paths with '|', not newlines."""
        assert parse_result("linux", 0, "/home/me/a.mp4|/home/me/b.mp4\n", "") == [
            "/home/me/a.mp4",
            "/home/me/b.mp4",
        ]

    def test_paths_containing_spaces_survive(self):
        assert parse_result("darwin", 0, "/Users/me/My Trip/clip one.mp4\n", "") == ["/Users/me/My Trip/clip one.mp4"]


class TestChoosePaths:
    async def test_returns_every_selected_path(self):
        got = await choose_paths("video", runner=_runner(0, "/a.mp4\n/b.mp4\n"), platform="darwin", multiple=True)
        assert got == ["/a.mp4", "/b.mp4"]

    async def test_cancel_returns_an_empty_list(self):
        got = await choose_paths(
            "video",
            runner=_runner(1, "", "execution error: User canceled. (-128)"),
            platform="darwin",
            multiple=True,
        )
        assert got == []

    async def test_folder_selection_returns_one_path(self):
        got = await choose_paths("folder", runner=_runner(0, "/Users/me/Exports\n"), platform="darwin")
        assert got == ["/Users/me/Exports"]

    async def test_choose_file_still_returns_a_single_path(self):
        """The existing single-file callers must be unaffected."""
        got = await choose_file("video", runner=_runner(0, "/a.mp4\n"), platform="darwin")
        assert got == "/a.mp4"
