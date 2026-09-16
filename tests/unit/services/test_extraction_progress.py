"""Reporting how far a GPS extraction has got.

Reading a clip's telemetry seeks through the whole file - minutes on a 17GB
clip - and the merge shows one static line per clip while it happens. ffmpeg
will report its position if asked, which turns that line into a percentage.
"""

import subprocess
from unittest.mock import patch

from gpstitch.services.dji_meta_parser import extract_dji_meta_raw


class FakeProcess:
    """An ffmpeg that emits progress lines on stderr and data on stdout."""

    def __init__(self, stderr_lines: list[str], stdout_bytes: bytes = b"data", returncode: int = 0):
        self._stderr_lines = stderr_lines
        self._stdout_bytes = stdout_bytes
        self.returncode = returncode
        self.stdout = self
        self.stderr = self

    def read(self):
        return self._stdout_bytes

    def readline(self):
        if self._stderr_lines:
            return self._stderr_lines.pop(0)
        return ""

    def wait(self):
        return self.returncode


def _run_with(stderr_lines, on_progress, **kwargs):
    process = FakeProcess(stderr_lines)
    with patch("gpstitch.services.dji_meta_parser.subprocess.Popen", return_value=process):
        return extract_dji_meta_raw(
            __import__("pathlib").Path("/tmp/clip.mp4"),
            2,
            on_progress=on_progress,
            **kwargs,
        )


class TestExtractionProgress:
    def test_seconds_processed_are_reported(self):
        reported = []

        _run_with(["out_time_us=5000000\n", "progress=continue\n", "progress=end\n"], reported.append)

        assert reported == [5.0]

    def test_every_update_is_reported(self):
        reported = []

        _run_with(
            [
                "out_time_us=1000000\n",
                "progress=continue\n",
                "out_time_us=2500000\n",
                "progress=continue\n",
                "progress=end\n",
            ],
            reported.append,
        )

        assert reported == [1.0, 2.5]

    def test_the_older_out_time_ms_key_is_understood(self):
        """ffmpeg build to ffmpeg build this is microseconds under an ms name."""
        reported = []

        _run_with(["out_time_ms=3000000\n", "progress=end\n"], reported.append)

        assert reported == [3.0]

    def test_unrelated_stderr_is_ignored(self):
        reported = []

        _run_with(
            ["frame= 100 fps=0.0\n", "out_time_us=1000000\n", "[mp4 @ 0x0] some warning\n", "progress=end\n"],
            reported.append,
        )

        assert reported == [1.0]

    def test_the_data_still_comes_back(self):
        """Progress reporting must not cost us the payload."""
        result = _run_with(["progress=end\n"], lambda s: None)

        assert result == b"data"

    def test_a_failure_still_raises(self):
        process = FakeProcess([""], returncode=1)
        with patch("gpstitch.services.dji_meta_parser.subprocess.Popen", return_value=process):
            try:
                extract_dji_meta_raw(__import__("pathlib").Path("/tmp/clip.mp4"), 2, on_progress=lambda s: None)
            except RuntimeError as e:
                assert "ffmpeg failed" in str(e)
            else:
                raise AssertionError("a failing ffmpeg must raise")

    def test_without_a_callback_the_simple_path_is_used(self):
        """The preview reads tiny windows constantly; do not make those pay for
        a thread and a pipe reader they have no use for."""
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"data", stderr=b"")

        with (
            patch("gpstitch.services.dji_meta_parser.subprocess.run", return_value=completed) as simple,
            patch("gpstitch.services.dji_meta_parser.subprocess.Popen") as streaming,
        ):
            result = extract_dji_meta_raw(__import__("pathlib").Path("/tmp/clip.mp4"), 2)

        assert result == b"data"
        assert simple.called
        assert not streaming.called

    def test_progress_is_only_requested_when_wanted(self):
        """-progress makes ffmpeg chatter; the simple path should not ask for it."""
        captured = {}
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")

        def capture(cmd, **kwargs):
            captured["cmd"] = cmd
            return completed

        with patch("gpstitch.services.dji_meta_parser.subprocess.run", side_effect=capture):
            extract_dji_meta_raw(__import__("pathlib").Path("/tmp/clip.mp4"), 2)

        assert "-progress" not in captured["cmd"]
