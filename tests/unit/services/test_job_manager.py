"""Unit tests for JobManager service."""

from datetime import UTC, datetime, timedelta

from gpstitch.models.job import JobStatus


class TestJobManagerCreate:
    """Tests for job creation."""

    async def test_create_job_generates_id(self, clean_job_manager, sample_job_config):
        """Created job should have a UUID."""
        job = await clean_job_manager.create_job(sample_job_config)

        assert job.id
        assert len(job.id) == 36

    async def test_create_job_status_pending(self, clean_job_manager, sample_job_config):
        """New job should have PENDING status."""
        job = await clean_job_manager.create_job(sample_job_config)

        assert job.status == JobStatus.PENDING

    async def test_create_job_persisted(self, clean_job_manager, sample_job_config):
        """Job should be persisted to disk."""
        job = await clean_job_manager.create_job(sample_job_config)
        job_file = clean_job_manager._job_file_path(job.id)

        assert job_file.exists()

    async def test_create_job_stores_config(self, clean_job_manager, sample_job_config):
        """Job should store configuration."""
        job = await clean_job_manager.create_job(sample_job_config)

        assert job.config.layout == sample_job_config.layout
        assert job.config.output_file == sample_job_config.output_file

    async def test_create_job_with_batch(self, clean_job_manager, sample_job_config):
        """Create job with batch ID."""
        batch_id = "batch-123"

        job = await clean_job_manager.create_job_with_batch(sample_job_config, batch_id)

        assert job.batch_id == batch_id


class TestJobManagerQuery:
    """Tests for job queries."""

    async def test_get_job(self, clean_job_manager, sample_job_config):
        """Get job by ID."""
        job = await clean_job_manager.create_job(sample_job_config)

        retrieved = await clean_job_manager.get_job(job.id)

        assert retrieved is not None
        assert retrieved.id == job.id

    async def test_get_job_nonexistent(self, clean_job_manager):
        """Get nonexistent job returns None."""
        result = await clean_job_manager.get_job("nonexistent")

        assert result is None

    async def test_list_jobs(self, clean_job_manager, sample_job_config):
        """List jobs returns all jobs."""
        job1 = await clean_job_manager.create_job(sample_job_config)
        job2 = await clean_job_manager.create_job(sample_job_config)

        jobs = await clean_job_manager.list_jobs()

        assert len(jobs) >= 2
        job_ids = [j.id for j in jobs]
        assert job1.id in job_ids
        assert job2.id in job_ids

    async def test_list_jobs_newest_first(self, clean_job_manager, sample_job_config):
        """List jobs should return newest first."""
        await clean_job_manager.create_job(sample_job_config)
        job2 = await clean_job_manager.create_job(sample_job_config)

        jobs = await clean_job_manager.list_jobs()

        # job2 created after job1, should be first
        assert jobs[0].id == job2.id

    async def test_get_next_pending_job_fifo(self, clean_job_manager, sample_job_config):
        """Get next pending job in FIFO order."""
        job1 = await clean_job_manager.create_job(sample_job_config)
        await clean_job_manager.create_job(sample_job_config)

        next_job = await clean_job_manager.get_next_pending_job()

        assert next_job.id == job1.id  # First created

    async def test_has_pending_jobs(self, clean_job_manager, sample_job_config):
        """Check if there are pending jobs."""
        assert not await clean_job_manager.has_pending_jobs()

        await clean_job_manager.create_job(sample_job_config)

        assert await clean_job_manager.has_pending_jobs()

    async def test_has_active_job(self, clean_job_manager, sample_job_config):
        """Check if there's an active job."""
        assert not await clean_job_manager.has_active_job()

        job = await clean_job_manager.create_job(sample_job_config)
        await clean_job_manager.update_job_status(job.id, JobStatus.RUNNING)

        assert await clean_job_manager.has_active_job()

    async def test_get_current_job(self, clean_job_manager, sample_job_config):
        """Get currently running job."""
        job = await clean_job_manager.create_job(sample_job_config)

        # No current job initially
        assert await clean_job_manager.get_current_job() is None

        await clean_job_manager.update_job_status(job.id, JobStatus.RUNNING)

        current = await clean_job_manager.get_current_job()
        assert current is not None
        assert current.id == job.id


class TestJobManagerUpdate:
    """Tests for job updates."""

    async def test_update_job_status_to_running(self, clean_job_manager, sample_job_config):
        """Update job status to RUNNING sets started_at."""
        job = await clean_job_manager.create_job(sample_job_config)

        await clean_job_manager.update_job_status(job.id, JobStatus.RUNNING)

        updated = await clean_job_manager.get_job(job.id)
        assert updated.status == JobStatus.RUNNING
        assert updated.started_at is not None

    async def test_update_job_status_to_completed(self, clean_job_manager, sample_job_config):
        """Update job status to COMPLETED sets completed_at."""
        job = await clean_job_manager.create_job(sample_job_config)
        await clean_job_manager.update_job_status(job.id, JobStatus.RUNNING)

        await clean_job_manager.update_job_status(job.id, JobStatus.COMPLETED)

        updated = await clean_job_manager.get_job(job.id)
        assert updated.status == JobStatus.COMPLETED
        assert updated.completed_at is not None

    async def test_update_job_status_with_error(self, clean_job_manager, sample_job_config):
        """Update job status with error message."""
        job = await clean_job_manager.create_job(sample_job_config)

        await clean_job_manager.update_job_status(job.id, JobStatus.FAILED, error="Test error")

        updated = await clean_job_manager.get_job(job.id)
        assert updated.status == JobStatus.FAILED
        assert updated.error == "Test error"

    async def test_update_job_progress(self, clean_job_manager, sample_job_config):
        """Update job progress."""
        job = await clean_job_manager.create_job(sample_job_config)

        await clean_job_manager.update_job_progress(
            job.id,
            percent=50.0,
            current_frame=500,
            total_frames=1000,
            fps=30.0,
            eta_seconds=16.7,
        )

        updated = await clean_job_manager.get_job(job.id)
        assert updated.progress.percent == 50.0
        assert updated.progress.current_frame == 500
        assert updated.progress.total_frames == 1000
        assert updated.progress.fps == 30.0
        assert updated.progress.eta_seconds == 16.7

    async def test_update_job_progress_capped_at_100(self, clean_job_manager, sample_job_config):
        """Progress should be capped at 100%."""
        job = await clean_job_manager.create_job(sample_job_config)

        await clean_job_manager.update_job_progress(job.id, percent=150.0)

        updated = await clean_job_manager.get_job(job.id)
        assert updated.progress.percent == 100.0

    async def test_append_job_log(self, clean_job_manager, sample_job_config):
        """Append log lines to job."""
        job = await clean_job_manager.create_job(sample_job_config)

        await clean_job_manager.append_job_log(job.id, "Line 1")
        await clean_job_manager.append_job_log(job.id, "Line 2")

        updated = await clean_job_manager.get_job(job.id)
        assert "Line 1" in updated.log_lines
        assert "Line 2" in updated.log_lines

    async def test_append_job_log_truncates_at_500(self, clean_job_manager, sample_job_config):
        """Log should be truncated to 500 lines max."""
        job = await clean_job_manager.create_job(sample_job_config)

        # Add 600 lines
        for i in range(600):
            await clean_job_manager.append_job_log(job.id, f"Line {i}")

        updated = await clean_job_manager.get_job(job.id)
        assert len(updated.log_lines) == 500
        assert "Line 599" in updated.log_lines  # Latest should be kept

    async def test_set_job_pid(self, clean_job_manager, sample_job_config):
        """Set process ID for job."""
        job = await clean_job_manager.create_job(sample_job_config)

        await clean_job_manager.set_job_pid(job.id, 12345)

        updated = await clean_job_manager.get_job(job.id)
        assert updated.pid == 12345


class TestJobManagerBatch:
    """Tests for batch job operations."""

    async def test_count_batch_jobs(self, clean_job_manager, sample_job_config):
        """Count jobs in a batch."""
        batch_id = "batch-test"

        await clean_job_manager.create_job_with_batch(sample_job_config, batch_id)
        await clean_job_manager.create_job_with_batch(sample_job_config, batch_id)
        await clean_job_manager.create_job(sample_job_config)  # Different batch

        counts = await clean_job_manager.count_batch_jobs(batch_id)

        assert counts["total"] == 2
        assert counts["pending"] == 2
        assert counts["running"] == 0
        assert counts["completed"] == 0

    async def test_cancel_batch_pending_jobs(self, clean_job_manager, sample_job_config):
        """Cancel all pending jobs in batch."""
        batch_id = "batch-cancel"

        job1 = await clean_job_manager.create_job_with_batch(sample_job_config, batch_id)
        job2 = await clean_job_manager.create_job_with_batch(sample_job_config, batch_id)

        cancelled = await clean_job_manager.cancel_batch_pending_jobs(batch_id)

        assert cancelled == 2

        job1_updated = await clean_job_manager.get_job(job1.id)
        job2_updated = await clean_job_manager.get_job(job2.id)

        assert job1_updated.status == JobStatus.CANCELLED
        assert job2_updated.status == JobStatus.CANCELLED

    async def test_get_running_batch_job(self, clean_job_manager, sample_job_config):
        """Get running job in batch."""
        batch_id = "batch-running"

        job = await clean_job_manager.create_job_with_batch(sample_job_config, batch_id)
        await clean_job_manager.update_job_status(job.id, JobStatus.RUNNING)

        running = await clean_job_manager.get_running_batch_job(batch_id)

        assert running is not None
        assert running.id == job.id


class TestJobManagerCleanup:
    """Tests for cleanup operations."""

    async def test_cleanup_old_jobs(self, clean_job_manager, sample_job_config):
        """Cleanup old completed jobs."""
        job = await clean_job_manager.create_job(sample_job_config)
        await clean_job_manager.update_job_status(job.id, JobStatus.COMPLETED)

        # Manually set completed_at to old date
        updated_job = await clean_job_manager.get_job(job.id)
        updated_job.completed_at = datetime.now(UTC) - timedelta(hours=48)
        clean_job_manager._persist_job(updated_job)

        cleaned = await clean_job_manager.cleanup_old_jobs(max_age_hours=24)

        assert cleaned == 1
        assert await clean_job_manager.get_job(job.id) is None

    async def test_cleanup_old_jobs_keeps_fresh(self, clean_job_manager, sample_job_config):
        """Fresh completed jobs are not cleaned."""
        job = await clean_job_manager.create_job(sample_job_config)
        await clean_job_manager.update_job_status(job.id, JobStatus.COMPLETED)

        cleaned = await clean_job_manager.cleanup_old_jobs(max_age_hours=24)

        assert cleaned == 0
        assert await clean_job_manager.get_job(job.id) is not None

    async def test_cleanup_orphaned_pending_jobs(self, clean_job_manager, sample_job_config):
        """Cleanup pending jobs with nonexistent sessions."""
        job = await clean_job_manager.create_job(sample_job_config)

        # Valid sessions don't include job's session
        valid_sessions = {"other-session-1", "other-session-2"}

        cleaned = await clean_job_manager.cleanup_orphaned_pending_jobs(valid_sessions)

        assert cleaned == 1
        updated = await clean_job_manager.get_job(job.id)
        assert updated.status == JobStatus.FAILED
        assert "orphaned" in updated.error.lower()


class TestJobManagerPersistence:
    """Tests for job persistence and recovery."""

    async def test_job_survives_restart(self, clean_job_manager, sample_job_config, temp_dir):
        """Jobs should be loadable after simulated restart."""
        from gpstitch.services.job_manager import JobManager

        job = await clean_job_manager.create_job(sample_job_config)
        job_id = job.id

        # Create new manager with same state_dir
        new_manager = JobManager(state_dir=clean_job_manager.state_dir)

        restored = await new_manager.get_job(job_id)

        assert restored is not None
        assert restored.config.session_id == sample_job_config.session_id

    async def test_persist_load_handles_non_ascii_log_lines(self, clean_job_manager, sample_job_config, monkeypatch):
        """Non-ASCII log lines (e.g. pillarbox '→') must round-trip on Windows-like locales.

        Reproduces the cp1252 UnicodeEncodeError seen on Windows by forcing Path.write_text /
        Path.read_text to default to cp1252 when encoding is not explicitly passed. The fix
        is to pass encoding='utf-8' in _persist_job / _load_jobs.
        """
        from pathlib import Path as _P

        from gpstitch.services.job_manager import JobManager

        orig_wt = _P.write_text
        orig_rt = _P.read_text

        def wt(self, data, encoding=None, errors=None, newline=None):
            return orig_wt(self, data, encoding=encoding or "cp1252", errors=errors, newline=newline)

        def rt(self, encoding=None, errors=None, newline=None):
            return orig_rt(self, encoding=encoding or "cp1252", errors=errors, newline=newline)

        monkeypatch.setattr(_P, "write_text", wt)
        monkeypatch.setattr(_P, "read_text", rt)

        job = await clean_job_manager.create_job(sample_job_config)
        # The exact log line emitted by render_service._create_pillarboxed_video.
        job.log_lines.append("Video: 3840x2880 → Canvas: 3840x2880")

        # Without the fix this raises UnicodeEncodeError under the cp1252 simulation.
        clean_job_manager._persist_job(job)

        # And reload must preserve the arrow.
        new_mgr = JobManager(state_dir=clean_job_manager.state_dir)
        restored = await new_mgr.get_job(job.id)
        assert restored is not None
        assert any("→" in line for line in restored.log_lines)

    async def test_running_job_marked_failed_on_restart(self, clean_job_manager, sample_job_config, temp_dir):
        """Running jobs should be marked as failed on restart.

        A restart means the owning process is gone. Constructing a second manager
        inside this one is not that - the owner is still very much alive - so the
        job's owner is pointed at a process that has exited.
        """
        import subprocess

        from gpstitch.services.job_manager import JobManager

        job = await clean_job_manager.create_job(sample_job_config)
        await clean_job_manager.update_job_status(job.id, JobStatus.RUNNING)
        job_id = job.id

        exited = subprocess.Popen(["true"])
        exited.wait()
        job.owner_pid = exited.pid
        clean_job_manager._persist_job(job)

        # Create new manager (simulates restart)
        new_manager = JobManager(state_dir=clean_job_manager.state_dir)

        restored = await new_manager.get_job(job_id)

        assert restored.status == JobStatus.FAILED
        assert "restarted" in restored.error.lower()


class TestRestartSweepRespectsTheOwner:
    """Only the process that owned a job may declare it orphaned.

    JobManager is constructed at import time against a shared state dir, so any
    second process - a test run, a CLI call, a second server - used to rewrite a
    live server's RUNNING job to FAILED just by importing the module. That was
    reachable in practice: running the test suite during a real render marked it
    failed on disk.
    """

    def _write_job(self, state_dir, status, owner_pid, session_id="s1"):
        from datetime import UTC, datetime
        from uuid import uuid4

        from gpstitch.models.job import Job, RenderJobConfig

        job = Job(
            id=str(uuid4()),
            status=status,
            config=RenderJobConfig(session_id=session_id, layout="default-1920x1080", output_file="/tmp/o.mp4"),
            created_at=datetime.now(UTC),
            owner_pid=owner_pid,
        )
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / f"{job.id}.json").write_text(job.model_dump_json(), encoding="utf-8")
        return job.id

    def _dead_pid(self):
        """A pid that has certainly exited."""
        import subprocess

        process = subprocess.Popen(["true"])
        process.wait()
        return process.pid

    def _load(self, state_dir):
        from gpstitch.services.job_manager import JobManager

        return JobManager(state_dir=state_dir)

    def test_a_running_job_owned_by_a_live_process_is_left_alone(self, temp_dir):
        import os

        from gpstitch.models.job import JobStatus

        state_dir = temp_dir / "jobs"
        job_id = self._write_job(state_dir, JobStatus.RUNNING, owner_pid=os.getpid())

        manager = self._load(state_dir)

        assert manager._jobs[job_id].status == JobStatus.RUNNING

    def test_a_running_job_whose_owner_died_is_failed(self, temp_dir):
        from gpstitch.models.job import JobStatus

        state_dir = temp_dir / "jobs"
        job_id = self._write_job(state_dir, JobStatus.RUNNING, owner_pid=self._dead_pid())

        manager = self._load(state_dir)

        assert manager._jobs[job_id].status == JobStatus.FAILED
        assert "restart" in (manager._jobs[job_id].error or "").lower()

    def test_a_running_job_from_before_this_field_existed_is_failed(self, temp_dir):
        """Legacy files carry no owner, so the old conservative sweep applies."""
        from gpstitch.models.job import JobStatus

        state_dir = temp_dir / "jobs"
        job_id = self._write_job(state_dir, JobStatus.RUNNING, owner_pid=None)

        manager = self._load(state_dir)

        assert manager._jobs[job_id].status == JobStatus.FAILED

    def test_a_pending_local_job_owned_by_a_live_process_is_left_alone(self, temp_dir):
        """Its in-memory session belongs to the owner, not to us."""
        import os

        from gpstitch.models.job import JobStatus

        state_dir = temp_dir / "jobs"
        job_id = self._write_job(state_dir, JobStatus.PENDING, owner_pid=os.getpid(), session_id="local:abc")

        manager = self._load(state_dir)

        assert manager._jobs[job_id].status == JobStatus.PENDING

    def test_a_pending_local_job_whose_owner_died_is_failed(self, temp_dir):
        from gpstitch.models.job import JobStatus

        state_dir = temp_dir / "jobs"
        job_id = self._write_job(state_dir, JobStatus.PENDING, owner_pid=self._dead_pid(), session_id="local:abc")

        manager = self._load(state_dir)

        assert manager._jobs[job_id].status == JobStatus.FAILED

    async def test_a_new_job_records_this_process_as_its_owner(self, clean_job_manager, sample_job_config):
        import os

        job = await clean_job_manager.create_job(sample_job_config)

        assert job.owner_pid == os.getpid()
