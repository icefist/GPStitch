"""Reporting how far along a clip a slow pass has got.

Both the join and the GPS read take minutes per clip. ffmpeg reports its
position constantly and the job log keeps its last 500 lines, so announcing
every update would push everything useful out of the log.
"""


class TestPercentageReporter:
    """ffmpeg reports its position constantly; the job log keeps 500 lines.

    Reporting every update would flood the log and push the useful lines out, so
    progress is only announced when it has moved a visible amount.
    """

    def test_progress_is_reported_as_a_percentage(self):
        from gpstitch.services.clip_progress import percentage_reporter

        lines = []
        report = percentage_reporter("Reading GPS from a.mp4", 100.0, lines.append)

        report(50.0)

        assert lines == ["Reading GPS from a.mp4 — 50%"]

    def test_small_advances_are_not_announced(self):
        from gpstitch.services.clip_progress import percentage_reporter

        lines = []
        report = percentage_reporter("Reading", 100.0, lines.append)

        report(10.0)
        report(11.0)
        report(12.0)

        assert lines == ["Reading — 10%"]

    def test_each_visible_step_is_announced_once(self):
        from gpstitch.services.clip_progress import percentage_reporter

        lines = []
        report = percentage_reporter("Reading", 100.0, lines.append)

        for second in range(0, 101, 5):
            report(float(second))

        assert lines == [f"Reading — {p}%" for p in range(0, 101, 10)]

    def test_an_unknown_duration_gives_no_reporter(self):
        """There is nothing to take a percentage of, so the extraction stays on
        its quieter path rather than dividing by zero."""
        from gpstitch.services.clip_progress import percentage_reporter

        assert percentage_reporter("Reading", 0.0, [].append) is None

    def test_it_never_claims_more_than_a_hundred(self):
        """ffmpeg can overshoot slightly at the end of a stream."""
        from gpstitch.services.clip_progress import percentage_reporter

        lines = []
        report = percentage_reporter("Reading", 10.0, lines.append)

        report(12.0)

        assert lines == ["Reading — 100%"]
