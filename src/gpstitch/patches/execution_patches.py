"""Wait for ffmpeg for as long as it is working, not five minutes.

Once the last overlay frame is drawn, gopro-overlay closes ffmpeg's input and
waits for it - but for at most five minutes, then raises TimeoutExpired and the
render is reported failed. Its own comment admits the wait is a guess ("really
long wait as FFMPEG processes all the mpeg input file - not sure how to prevent
this atm").

Five minutes is not enough. GPStitch asks for `+faststart`, so after encoding
ffmpeg rewrites the entire file to move its index to the front: a 24GB ride on a
drive running at USB 2.0 speed took 28 minutes, and a complete, correct video
came back as a failure while ffmpeg carried on and finished it regardless.

There is nothing to gain from a limit. ffmpeg either finishes or fails and says
so in its exit code, and a cancelled render kills the whole process group, so a
wait with no cap cannot leave a cancelled job hanging. What a long wait does
need is to be visible: a job sitting at 100% with a silent log looks hung.
"""

import contextlib
import logging
import subprocess
from collections.abc import Callable

logger = logging.getLogger(__name__)

# How often to say ffmpeg is still busy. The job log keeps its last 500 lines;
# every 30 seconds is 60 lines for a half-hour finish.
_HEARTBEAT_S = 30


def wait_for_ffmpeg(process, log: Callable[[str], None], heartbeat_s: float = _HEARTBEAT_S) -> int:
    """Wait until `process` exits, logging every `heartbeat_s` seconds. Returns its exit code."""
    waited = 0.0
    while True:
        try:
            return process.wait(heartbeat_s)
        except subprocess.TimeoutExpired:
            waited += heartbeat_s
            minutes, seconds = divmod(int(waited), 60)
            log(f"ffmpeg is still finishing the file ({minutes}m{seconds:02d}s so far)...")


def patch_ffmpeg_wait() -> None:
    """Replace gopro-overlay's five-minute wait for ffmpeg with an uncapped one."""
    from gopro_overlay import execution
    from gopro_overlay.log import log

    if getattr(execution.InProcessExecution, "_ts_patient", False):
        logger.debug("ffmpeg wait already patched, skipping")
        return

    # The original, line for line, except for the wait at the end.
    def execute(self, cmd):
        try:
            log(f"Executing {cmd}")
            if self.redirect:
                with open(self.redirect, "w") as std:
                    process = self.popen(cmd, stdin=subprocess.PIPE, stdout=std, stderr=std)
            else:
                process = self.popen(cmd, stdin=subprocess.PIPE, stdout=None, stderr=None)

            try:
                yield process.stdin
            except Exception:
                # Already failing: the original error matters more than this one.
                with contextlib.suppress(Exception):
                    process.terminate()
                raise
            finally:
                if not process.stdin.closed:
                    process.stdin.flush()
                    process.stdin.close()
                log("Waiting for ffmpeg to complete...")
                returncode = wait_for_ffmpeg(process, log, heartbeat_s=_HEARTBEAT_S)
                log(f"FFMPEG Exited with status code: {returncode}")
        except FileNotFoundError:
            raise OSError(f"Unable to execute the process - is '{cmd[0]}' installed") from None
        except BrokenPipeError:
            if self.redirect:
                log("FFMPEG Output:")
                with open(self.redirect) as f:
                    log("".join(f.readlines()))
            raise OSError(f"Process {cmd[0]} failed") from None

    execution.InProcessExecution.execute = execute
    execution.InProcessExecution._ts_patient = True
    logger.debug("Patched gopro-overlay to wait for ffmpeg without a time limit")
