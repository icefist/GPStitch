"""Wait for ffmpeg for as long as it is working, not five minutes.

gopro-overlay closes ffmpeg's input once the last overlay frame is drawn and
then waits at most five minutes for it to finish. ffmpeg still has to rewrite
the whole file to put its index first, which for a long ride on a slow drive
took 28 minutes - so a complete, correct video was reported as a failed render.
"""

import contextlib
import subprocess
import sys

import pytest
from gopro_overlay import execution

from gpstitch.patches import execution_patches
from gpstitch.patches.execution_patches import patch_ffmpeg_wait, wait_for_ffmpeg


class SlowProcess:
    """A process still busy for the first `busy_checks` waits."""

    def __init__(self, busy_checks, returncode=0):
        self.busy_checks = busy_checks
        self.returncode = returncode
        self.timeouts = []

    def wait(self, timeout=None):
        self.timeouts.append(timeout)
        if self.busy_checks > 0:
            self.busy_checks -= 1
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        return self.returncode


class TestWaitForFfmpeg:
    def test_it_keeps_waiting_while_ffmpeg_works(self):
        """Twenty checks at thirty seconds is ten minutes - twice the old limit."""
        process = SlowProcess(busy_checks=20)

        assert wait_for_ffmpeg(process, log=lambda m: None, heartbeat_s=30) == 0

    def test_it_says_so_while_it_waits(self):
        """A job sitting at 100% with nothing in the log looks hung."""
        lines = []

        wait_for_ffmpeg(SlowProcess(busy_checks=3), log=lines.append, heartbeat_s=30)

        assert len(lines) == 3
        assert "1m30s" in lines[-1]

    def test_ffmpegs_own_exit_code_comes_back(self):
        """Whether it worked is ffmpeg's to say, not a stopwatch's."""
        assert wait_for_ffmpeg(SlowProcess(busy_checks=0, returncode=1), log=lambda m: None) == 1


def running(executor, cmd):
    """How gopro-overlay drives it: a generator wrapped as a context manager."""
    return contextlib.contextmanager(executor.execute)(cmd)


class TestPatchedExecution:
    @pytest.fixture(autouse=True)
    def _patched(self, monkeypatch):
        monkeypatch.setattr(execution.InProcessExecution, "execute", execution.InProcessExecution.execute)
        monkeypatch.setattr(execution.InProcessExecution, "_ts_patient", False, raising=False)
        monkeypatch.setattr(execution_patches, "_HEARTBEAT_S", 0.2)
        patch_ffmpeg_wait()

    def test_a_process_that_outlives_the_heartbeat_still_completes(self):
        """Real process, real pipe: it finishes after several heartbeats."""
        script = "import sys, time; sys.stdin.read(); time.sleep(0.7)"

        with running(execution.InProcessExecution(), [sys.executable, "-c", script]) as stdin:
            stdin.write(b"overlay frames")

    def test_input_still_reaches_the_process(self):
        result = []
        script = "import sys; sys.stdout.write(sys.stdin.read())"

        class Capture:
            def __call__(self, cmd, **kwargs):
                kwargs["stdout"] = subprocess.PIPE
                self.process = subprocess.Popen(cmd, **kwargs)
                return self.process

        capture = Capture()
        with running(execution.InProcessExecution(popen=capture), [sys.executable, "-c", script]) as stdin:
            stdin.write(b"frames")
        result.append(capture.process.stdout.read())

        assert result == [b"frames"]

    def test_a_missing_ffmpeg_is_still_reported_as_missing(self):
        with (
            pytest.raises(OSError, match="installed"),
            running(execution.InProcessExecution(), ["/nonexistent/ffmpeg"]),
        ):
            pass

    def test_patching_twice_is_harmless(self):
        patched = execution.InProcessExecution.execute

        patch_ffmpeg_wait()

        assert execution.InProcessExecution.execute is patched
