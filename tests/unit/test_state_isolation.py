"""Test runs must not touch the machine's shared job store.

JobManager is constructed at import time against `settings.temp_dir / "jobs"`,
which defaults to a machine-wide temp dir. Running the suite therefore wrote
into whatever a live server was using - 768 job files had accumulated there, and
the load sweep read back that server's jobs. tests/conftest.py redirects
GPSTITCH_TEMP_DIR before gpstitch is imported; this is the guard on that.
"""

import tempfile
from pathlib import Path


def test_the_job_store_is_not_the_machine_wide_one():
    from gpstitch.services.job_manager import job_manager

    shared = Path(tempfile.gettempdir()) / "gpstitch" / "jobs"
    assert job_manager.state_dir.resolve() != shared.resolve(), (
        "the singleton is pointing at the shared job store; a test run would litter it and read a live server's jobs"
    )


def test_the_job_store_lives_under_this_session_dir():
    from gpstitch.services.job_manager import job_manager
    from tests.conftest import _TEST_STATE_DIR

    # Resolve both sides: on macOS the temp dir is reached through a /var symlink.
    assert _TEST_STATE_DIR.resolve() in job_manager.state_dir.resolve().parents
