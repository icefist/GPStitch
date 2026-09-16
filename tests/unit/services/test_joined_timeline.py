"""Placing several clips' GPS on the timeline of the joined video.

The joined video's timeline is the sum of the clip durations, so a clip's points
belong at the cumulative duration of everything before it - not at their own
wall-clock time. For auto-split parts the two are the same. For clips with a real
gap between them the gap is compressed out, which is what keeps the overlay
matching the picture rather than the clock.
"""

from datetime import datetime, timedelta

import pytest

from gpstitch.services.dji_meta_parser import DjiMetaPoint, points_on_joined_timeline

START = datetime(2026, 9, 13, 14, 32, 6)


def clip(first_ts: datetime, count: int, hz: float = 30.0, start_idx: int = 0) -> list[DjiMetaPoint]:
    return [
        DjiMetaPoint(
            frame_idx=start_idx + i,
            timestamp=first_ts + timedelta(seconds=i / hz),
            lat=52.0 + i * 0.0001,
            lon=6.0,
            alt_m=10.0,
            velocity_2d=(1.0, 0.0),
        )
        for i in range(count)
    ]


class TestPointsOnJoinedTimeline:
    def test_a_single_clip_is_unchanged(self):
        points = clip(START, 10)

        joined = points_on_joined_timeline([(points, 10 / 30)])

        assert [p.timestamp for p in joined] == [p.timestamp for p in points]

    def test_the_second_clip_starts_where_the_first_ends(self):
        first = clip(START, 30)
        second = clip(START + timedelta(seconds=1), 30)

        joined = points_on_joined_timeline([(first, 1.0), (second, 1.0)])

        assert joined[30].timestamp == START + timedelta(seconds=1)

    def test_a_gap_between_clips_is_compressed_out(self):
        """The joined video has no gap, so the track must not have one either."""
        first = clip(START, 30)
        second = clip(START + timedelta(minutes=15), 30)

        joined = points_on_joined_timeline([(first, 1.0), (second, 1.0)])

        assert joined[30].timestamp == START + timedelta(seconds=1)

    def test_timing_within_a_clip_is_preserved(self):
        first = clip(START, 30)
        second = clip(START + timedelta(minutes=15), 30)

        joined = points_on_joined_timeline([(first, 1.0), (second, 1.0)])

        offsets = [(p.timestamp - joined[30].timestamp).total_seconds() for p in joined[30:]]
        assert offsets[1] == pytest.approx(1 / 30, abs=1e-6)
        assert offsets[-1] == pytest.approx(29 / 30, abs=1e-6)

    def test_the_result_is_ordered(self):
        joined = points_on_joined_timeline([(clip(START, 30), 1.0), (clip(START, 30), 1.0)])

        assert [p.timestamp for p in joined] == sorted(p.timestamp for p in joined)

    def test_frame_indices_keep_increasing_across_clips(self):
        """Downstream code derives timing from frame_idx when a clock is stalled."""
        joined = points_on_joined_timeline([(clip(START, 30), 1.0), (clip(START, 30), 1.0)])

        indices = [p.frame_idx for p in joined]
        assert indices == sorted(indices)
        assert len(set(indices)) == len(indices)

    def test_gps_values_are_untouched(self):
        first = clip(START, 5)

        joined = points_on_joined_timeline([(first, 1.0)])

        assert [(p.lat, p.lon, p.alt_m) for p in joined] == [(p.lat, p.lon, p.alt_m) for p in first]

    def test_a_clip_with_no_points_leaves_a_hole_but_still_advances_time(self):
        """A clip whose GPS never locked must not shift the clips after it."""
        first = clip(START, 30)
        third = clip(START + timedelta(seconds=2), 30)

        joined = points_on_joined_timeline([(first, 1.0), ([], 1.0), (third, 1.0)])

        assert joined[30].timestamp == START + timedelta(seconds=2)

    def test_no_segments_gives_no_points(self):
        assert points_on_joined_timeline([]) == []

    def test_segments_with_no_points_at_all_give_no_points(self):
        assert points_on_joined_timeline([([], 1.0), ([], 1.0)]) == []
