"""Deriving the thinning rate from a DJI track's own timing.

Three call sites computed this independently and disagreed about a track that
spans no time: one raised, one kept every point, one assumed 25Hz. One of them
divided by a duration floored at one second, which turned a clip whose clock
never advanced into points[::43917] and left a single-point track.

The clock is now rebuilt upstream when it stalls, so a zero span means a track
that cannot be rendered at all. That is a rendering precondition, not a rate
question, and it belongs to the caller that cares.
"""

from datetime import datetime, timedelta

import pytest

from gpstitch.services.dji_meta_parser import (
    DjiMetaPoint,
    derive_sample_rate,
    track_span_seconds,
)

START = datetime(2026, 9, 6, 18, 25, 14)


def track(count: int, hz: float) -> list[DjiMetaPoint]:
    return [
        DjiMetaPoint(
            frame_idx=i,
            timestamp=START + timedelta(seconds=i / hz),
            lat=52.0,
            lon=6.0,
            alt_m=10.0,
            velocity_2d=(1.0, 0.0),
        )
        for i in range(count)
    ]


class TestTrackSpanSeconds:
    def test_the_span_between_first_and_last(self):
        assert track_span_seconds(track(31, 30.0)) == pytest.approx(1.0)

    def test_a_single_point_spans_nothing(self):
        assert track_span_seconds(track(1, 30.0)) == 0.0

    def test_no_points_span_nothing(self):
        assert track_span_seconds([]) == 0.0

    def test_stream_order_is_not_assumed(self):
        """parse_dji_meta returns the order the samples appear in."""
        points = track(31, 30.0)
        assert track_span_seconds(list(reversed(points))) == pytest.approx(1.0)


class TestDeriveSampleRate:
    def test_a_30hz_track_thins_to_1hz(self):
        assert derive_sample_rate(track(300, 30.0), target_hz=1) == 30

    def test_the_real_clip(self):
        """43917 points over 1465.3s is 29.97Hz."""
        assert derive_sample_rate(track(43917, 29.97), target_hz=1) == 30

    def test_a_track_already_at_the_target_is_not_thinned(self):
        assert derive_sample_rate(track(60, 1.0), target_hz=1) == 1

    def test_a_track_below_the_target_is_not_thinned(self):
        assert derive_sample_rate(track(60, 0.5), target_hz=1) == 1

    def test_a_single_point_is_not_thinned(self):
        assert derive_sample_rate(track(1, 30.0), target_hz=1) == 1

    def test_no_points_are_not_thinned(self):
        assert derive_sample_rate([], target_hz=1) == 1

    def test_a_track_spanning_no_time_keeps_every_point(self):
        """No timing to derive from. Dividing by a floored duration instead gave
        a rate equal to the point count, which discarded all but one point."""
        frozen = [
            DjiMetaPoint(frame_idx=i, timestamp=START, lat=52.0, lon=6.0, alt_m=10.0, velocity_2d=(0.0, 0.0))
            for i in range(43917)
        ]
        assert derive_sample_rate(frozen, target_hz=1) == 1

    def test_a_higher_target_thins_less(self):
        assert derive_sample_rate(track(300, 30.0), target_hz=5) == 6
