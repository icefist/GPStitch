"""A frame or two of a DJI clip stamped by a different clock.

A real ride's clip 0058 had 79,726 GPS samples on the GPS remote's clock - and
two, at frames 263 and 48,883, stamped about four hours earlier (the camera's
own clock). Their positions were right; only the time was wrong.

That was enough to lose the whole clip from a merged render. The joined
timeline starts each clip at its earliest timestamp, so the clip started at the
stray 17:40:31 instead of the real 21:38:01, and every real point landed four
hours past the end of the video. The map drew a straight line from the clip
before to the clip after.
"""

import struct
from datetime import datetime, timedelta
from unittest.mock import patch

from gpstitch.services.dji_meta_parser import (
    DjiMetaPoint,
    parse_dji_meta,
    points_on_joined_timeline,
    repair_clock_blips,
)

GPS_CLOCK = datetime(2026, 10, 20, 21, 38, 1)
CAMERA_OFFSET = timedelta(hours=-3, minutes=-57, seconds=-30)
FPS = 30


def track(frames: int, start: datetime = GPS_CLOCK) -> list[DjiMetaPoint]:
    """One sample a frame, stamped to the second like the real stream."""
    return [
        DjiMetaPoint(
            frame_idx=i,
            timestamp=start + timedelta(seconds=i // FPS),
            lat=53.40 - i * 1e-6,
            lon=6.08 + i * 1e-6,
            alt_m=40.0,
            velocity_2d=(1.0, 0.0),
        )
        for i in range(frames)
    ]


def stray(points: list[DjiMetaPoint], *frames: int) -> list[DjiMetaPoint]:
    """Restamp the given frames with the camera's clock, as the remote did."""
    out = list(points)
    for f in frames:
        p = out[f]
        out[f] = DjiMetaPoint(p.frame_idx, p.timestamp + CAMERA_OFFSET, p.lat, p.lon, p.alt_m, p.velocity_2d)
    return out


def seconds_from(points, frame, reference):
    return abs((points[frame].timestamp - reference[frame].timestamp).total_seconds())


class TestRepairClockBlips:
    def test_a_single_frame_on_another_clock_is_put_back(self):
        healthy = track(600)

        repaired = repair_clock_blips(stray(healthy, 263))

        assert seconds_from(repaired, 263, healthy) <= 1

    def test_its_position_is_left_alone(self):
        """The fix was right; only its time was not."""
        healthy = track(600)

        repaired = repair_clock_blips(stray(healthy, 263))

        assert (repaired[263].lat, repaired[263].lon) == (healthy[263].lat, healthy[263].lon)

    def test_every_blip_in_a_clip_is_put_back(self):
        healthy = track(3000)

        repaired = repair_clock_blips(stray(healthy, 263, 2400))

        assert seconds_from(repaired, 263, healthy) <= 1
        assert seconds_from(repaired, 2400, healthy) <= 1

    def test_a_short_burst_is_put_back(self):
        healthy = track(600)

        repaired = repair_clock_blips(stray(healthy, 300, 301, 302))

        assert all(seconds_from(repaired, f, healthy) <= 1 for f in (300, 301, 302))

    def test_a_blip_on_the_first_frame_is_put_back(self):
        """The first point is what a clip's start time is read from."""
        healthy = track(600)

        repaired = repair_clock_blips(stray(healthy, 0))

        assert seconds_from(repaired, 0, healthy) <= 1

    def test_a_blip_on_the_last_frame_is_put_back(self):
        healthy = track(600)

        repaired = repair_clock_blips(stray(healthy, 599))

        assert seconds_from(repaired, 599, healthy) <= 1

    def test_a_clock_that_really_jumps_is_left_alone(self):
        """Clip 0024 jumped four hours and stayed there: not a blip, and not ours."""
        jumped = stray(track(3000), *range(1000, 3000))

        assert repair_clock_blips(jumped) == jumped

    def test_a_stalled_clock_is_left_for_the_stall_repair(self):
        stalled = [DjiMetaPoint(i, GPS_CLOCK, 53.4, 6.08, 40.0, (0.0, 0.0)) for i in range(900)]

        assert repair_clock_blips(stalled) == stalled

    def test_a_healthy_clip_comes_back_untouched(self):
        healthy = track(3000)

        assert repair_clock_blips(healthy) == healthy

    def test_no_point_is_dropped_or_added(self):
        assert len(repair_clock_blips(stray(track(600), 263))) == 600


def raw_samples(count: int) -> bytes:
    """`count` top-level field-3 messages, which is all parse_dji_meta iterates."""
    message = b"\x08\x01"
    return (struct.pack("B", (3 << 3) | 2) + struct.pack("B", len(message)) + message) * count


class TestParsingRepairsBlips:
    def test_every_reader_gets_the_repair(self):
        """parse_dji_meta is under every reader: whole file, windows, first point."""
        points = stray(track(600), 263)

        with patch(
            "gpstitch.services.dji_meta_parser._parse_gps_from_sample",
            side_effect=lambda data, frame_idx: points[frame_idx],
        ):
            parsed = parse_dji_meta(raw_samples(600))

        assert seconds_from(parsed, 263, track(600)) <= 1


class TestMergedRide:
    def test_a_blip_no_longer_throws_its_clip_off_the_joined_timeline(self):
        """The reported bug: clip two's points must land in clip two's stretch."""
        first = track(1800, start=GPS_CLOCK - timedelta(hours=1))
        second = repair_clock_blips(stray(track(1800), 263))

        joined = points_on_joined_timeline([(first, 60.0), (second, 60.0)])
        second_on_timeline = joined[1800:]
        start = joined[0].timestamp

        offsets = [(p.timestamp - start).total_seconds() for p in second_on_timeline]
        assert min(offsets) >= 59
        assert max(offsets) <= 121
