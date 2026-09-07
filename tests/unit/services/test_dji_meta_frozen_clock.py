"""A DJI clip whose GPS clock never advances.

Two clips off one DJI AC003 stamped every one of their 43917 GPS samples with a
single timestamp - measured identical at 0s, 60s, 300s and 600s into the file.
gopro-dashboard then sees a track spanning zero seconds, finds no overlap with
the video's own date range, and refuses to render.

The stream declares its own sample rate (29.97Hz, one sample per frame) and that
field survives the freeze, so the time axis can be rebuilt from the sample index.
Timestamps have one-second resolution, which is why a single short window cannot
tell a frozen clock from ordinary granularity - only a whole-file view can.
"""

import struct
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from gpstitch.services.dji_meta_parser import (
    _DEFAULT_SAMPLE_RATE_HZ,
    DjiMetaPoint,
    dji_meta_clock_frozen,
    parse_dji_meta_window,
    read_declared_sample_rate,
    rebuild_timestamps,
    sample_dji_meta_track,
    timestamps_frozen,
)

FROZEN = datetime(2026, 9, 6, 18, 25, 14)


def point(frame_idx: int, ts: datetime = FROZEN) -> DjiMetaPoint:
    return DjiMetaPoint(
        frame_idx=frame_idx,
        timestamp=ts,
        lat=52.0,
        lon=6.0,
        alt_m=10.0,
        velocity_2d=(1.0, 0.0),
    )


# --- protobuf builders, matching the stream layout f3 > f4 > f1 > f5 ---


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _submessage(field_num: int, content: bytes) -> bytes:
    return _varint((field_num << 3) | 2) + _varint(len(content)) + content


def _float_field(field_num: int, value: float) -> bytes:
    return _varint((field_num << 3) | 5) + struct.pack("<f", value)


def raw_with_declared_rate(rate: float | None) -> bytes:
    device = _submessage(4, b"DJI AC003")
    if rate is not None:
        device += _float_field(5, rate)
    return _submessage(3, _submessage(4, _submessage(1, device)))


class TestFrozenDetection:
    def test_identical_timestamps_are_frozen(self):
        assert timestamps_frozen([point(0), point(1), point(2)]) is True

    def test_advancing_timestamps_are_not_frozen(self):
        points = [point(0), point(30, FROZEN + timedelta(seconds=1))]
        assert timestamps_frozen(points) is False

    def test_a_single_point_is_not_frozen(self):
        """One point spans no time whatever its clock does - nothing to rebuild."""
        assert timestamps_frozen([point(0)]) is False

    def test_no_points_are_not_frozen(self):
        assert timestamps_frozen([]) is False

    def test_unsorted_points_are_still_recognised_as_advancing(self):
        """parse_dji_meta returns stream order, which is not guaranteed sorted."""
        points = [point(30, FROZEN + timedelta(seconds=1)), point(0, FROZEN)]
        assert timestamps_frozen(points) is False


class TestRebuildTimestamps:
    def test_points_are_spread_at_the_declared_rate(self):
        rebuilt = rebuild_timestamps([point(0), point(1), point(2)], sample_rate_hz=30.0)
        assert [p.timestamp for p in rebuilt] == [
            FROZEN,
            FROZEN + timedelta(seconds=1 / 30),
            FROZEN + timedelta(seconds=2 / 30),
        ]

    def test_gaps_in_the_frame_index_keep_their_spacing(self):
        """Samples without a GPS fix are dropped but still advance frame_idx."""
        rebuilt = rebuild_timestamps([point(0), point(30), point(60)], sample_rate_hz=30.0)
        assert [p.timestamp for p in rebuilt] == [
            FROZEN,
            FROZEN + timedelta(seconds=1),
            FROZEN + timedelta(seconds=2),
        ]

    def test_the_first_point_keeps_its_timestamp_after_a_late_gps_lock(self):
        """render_service derives mtime as first timestamp minus frame_idx/fps.

        Anchoring on the first point's own frame index rather than frame zero
        keeps that subtraction from being counted twice.
        """
        rebuilt = rebuild_timestamps([point(600), point(601)], sample_rate_hz=30.0)
        assert rebuilt[0].timestamp == FROZEN
        assert rebuilt[1].timestamp == FROZEN + timedelta(seconds=1 / 30)

    def test_a_window_offset_shifts_the_whole_window(self):
        rebuilt = rebuild_timestamps([point(0), point(30)], sample_rate_hz=30.0, start_offset_s=240.0)
        assert rebuilt[0].timestamp == FROZEN + timedelta(seconds=240)
        assert rebuilt[1].timestamp == FROZEN + timedelta(seconds=241)

    def test_gps_data_is_left_alone(self):
        rebuilt = rebuild_timestamps([point(0), point(1)], sample_rate_hz=30.0)
        assert [(p.lat, p.lon, p.alt_m, p.velocity_2d, p.frame_idx) for p in rebuilt] == [
            (52.0, 6.0, 10.0, (1.0, 0.0), 0),
            (52.0, 6.0, 10.0, (1.0, 0.0), 1),
        ]

    def test_a_missing_declared_rate_falls_back_to_the_default(self):
        rebuilt = rebuild_timestamps([point(0), point(1)], sample_rate_hz=None)
        assert rebuilt[1].timestamp == FROZEN + timedelta(seconds=1 / _DEFAULT_SAMPLE_RATE_HZ)

    def test_a_nonsense_declared_rate_falls_back_to_the_default(self):
        rebuilt = rebuild_timestamps([point(0), point(1)], sample_rate_hz=0.0)
        assert rebuilt[1].timestamp == FROZEN + timedelta(seconds=1 / _DEFAULT_SAMPLE_RATE_HZ)


class TestReadDeclaredSampleRate:
    def test_reads_the_rate_the_stream_declares(self):
        rate = read_declared_sample_rate(raw_with_declared_rate(29.97))
        assert rate == pytest.approx(29.97, abs=1e-4)

    def test_returns_none_when_the_field_is_absent(self):
        assert read_declared_sample_rate(raw_with_declared_rate(None)) is None

    def test_returns_none_for_empty_data(self):
        assert read_declared_sample_rate(b"") is None


class TestCoarseTrackRepair:
    """sample_dji_meta_track sees the whole file, so it can detect a freeze itself."""

    def _extraction(self, per_window: dict[float, list[DjiMetaPoint]], rate: float = 30.0):
        """Fake extraction returning canned points keyed by window start."""
        calls: list[float] = []

        def fake_extract(file_path, stream_index, *, start_s=None, duration_s=None):
            calls.append(start_s or 0.0)
            return raw_with_declared_rate(rate)

        def fake_parse(raw):
            return list(per_window[calls[-1]])

        return fake_extract, fake_parse

    def test_a_frozen_clip_gets_per_window_offsets(self, tmp_path):
        extract, parse = self._extraction({0.0: [point(0), point(30)], 100.0: [point(0), point(30)]})
        with (
            patch("gpstitch.services.dji_meta_parser.extract_dji_meta_raw", side_effect=extract),
            patch("gpstitch.services.dji_meta_parser.parse_dji_meta", side_effect=parse),
        ):
            points = sample_dji_meta_track(tmp_path / "v.mp4", total_duration_s=200.0, samples=2, stream_index=2)

        assert [p.timestamp for p in points] == [
            FROZEN,
            FROZEN + timedelta(seconds=1),
            FROZEN + timedelta(seconds=100),
            FROZEN + timedelta(seconds=101),
        ]

    def test_a_healthy_clip_keeps_its_own_timestamps(self, tmp_path):
        """The regression that matters: a working clip must not be shifted.

        Each window's points share one timestamp because the clock has one-second
        resolution, so per-window detection would wrongly call this frozen.
        """
        late = FROZEN + timedelta(seconds=100)
        extract, parse = self._extraction({0.0: [point(0), point(1)], 100.0: [point(0, late), point(1, late)]})
        with (
            patch("gpstitch.services.dji_meta_parser.extract_dji_meta_raw", side_effect=extract),
            patch("gpstitch.services.dji_meta_parser.parse_dji_meta", side_effect=parse),
        ):
            points = sample_dji_meta_track(tmp_path / "v.mp4", total_duration_s=200.0, samples=2, stream_index=2)

        assert [p.timestamp for p in points] == [FROZEN, FROZEN, late, late]

    def test_a_single_window_is_not_enough_to_call_it_frozen(self, tmp_path):
        """With no second window to compare against, a freeze cannot be told apart
        from a window shorter than the clock's resolution."""
        extract, parse = self._extraction({0.0: [point(0), point(1)]})
        with (
            patch("gpstitch.services.dji_meta_parser.extract_dji_meta_raw", side_effect=extract),
            patch("gpstitch.services.dji_meta_parser.parse_dji_meta", side_effect=parse),
        ):
            points = sample_dji_meta_track(tmp_path / "v.mp4", total_duration_s=0.0, samples=20, stream_index=2)

        assert [p.timestamp for p in points] == [FROZEN, FROZEN]


class TestSingleWindow:
    """One window cannot tell a freeze from one-second granularity, so it is told."""

    def _patched(self, points, rate=30.0):
        return (
            patch(
                "gpstitch.services.dji_meta_parser.extract_dji_meta_raw",
                return_value=raw_with_declared_rate(rate),
            ),
            patch("gpstitch.services.dji_meta_parser.parse_dji_meta", return_value=points),
        )

    def test_by_default_timestamps_are_left_alone(self, tmp_path):
        extract, parse = self._patched([point(0), point(1)])
        with extract, parse:
            points = parse_dji_meta_window(tmp_path / "v.mp4", start_s=240.0, duration_s=2.0, stream_index=2)
        assert [p.timestamp for p in points] == [FROZEN, FROZEN]

    def test_a_frozen_clock_gets_the_window_offset(self, tmp_path):
        extract, parse = self._patched([point(0), point(30)])
        with extract, parse:
            points = parse_dji_meta_window(
                tmp_path / "v.mp4",
                start_s=240.0,
                duration_s=2.0,
                stream_index=2,
                frozen_clock=True,
            )
        assert [p.timestamp for p in points] == [
            FROZEN + timedelta(seconds=240),
            FROZEN + timedelta(seconds=241),
        ]


class TestClockFrozenProbe:
    def _probe(self, results, tmp_path):
        """Drive the probe with one canned result per widening attempt."""
        durations: list[float] = []
        attempts = iter(results)

        def fake_window(file_path, *, start_s, duration_s, stream_index=None, frozen_clock=False):
            durations.append(duration_s)
            return next(attempts, [])

        with patch("gpstitch.services.dji_meta_parser.parse_dji_meta_window", side_effect=fake_window):
            return dji_meta_clock_frozen(tmp_path / "v.mp4", stream_index=2), durations

    def test_a_window_of_identical_timestamps_is_frozen(self, tmp_path):
        frozen, durations = self._probe([[point(0), point(1)]], tmp_path)
        assert frozen is True
        assert durations[0] > 1.0, "the window must outrun the clock's one-second resolution"

    def test_an_advancing_clock_is_not_frozen(self, tmp_path):
        frozen, _ = self._probe([[point(0), point(30, FROZEN + timedelta(seconds=1))]], tmp_path)
        assert frozen is False

    def test_the_window_widens_when_gps_locked_late(self, tmp_path):
        frozen, durations = self._probe([[], [point(0), point(1)]], tmp_path)
        assert frozen is True
        assert len(durations) == 2
        assert durations[1] > durations[0]

    def test_a_clip_with_no_gps_at_all_is_not_called_frozen(self, tmp_path):
        """No evidence of a freeze is not evidence of one."""
        frozen, _ = self._probe([], tmp_path)
        assert frozen is False


class TestPreviewSampling:
    """The dense preview window cannot detect a freeze, so the sampler must tell it."""

    def _sample(self, frozen: bool, tmp_path):
        from gpstitch.services.renderer import _sample_dji_meta_for_frame

        told: list[bool] = []

        def fake_window(file_path, *, start_s, duration_s, stream_index=None, frozen_clock=False):
            told.append(frozen_clock)
            # Mirror the real function: only a told window rebuilds its own axis.
            points = [point(0), point(30)]
            if frozen_clock:
                return rebuild_timestamps(points, sample_rate_hz=30.0, start_offset_s=start_s)
            return points

        def fake_coarse(file_path, *, total_duration_s, stream_index=None):
            # A frozen clip's coarse pass has already rebuilt its own timestamps.
            return [point(0), point(30, FROZEN + timedelta(seconds=200))]

        points = _sample_dji_meta_for_frame(
            tmp_path / "v.mp4",
            at_seconds=100.0,
            total_duration_s=200.0,
            detect=lambda _: 2,
            window=fake_window,
            coarse=fake_coarse,
            frozen_check=lambda _, stream_index=None: frozen,
        )
        return points, told

    def test_the_dense_window_is_told_when_the_clock_is_frozen(self, tmp_path):
        _, told = self._sample(True, tmp_path)
        assert told == [True]

    def test_the_dense_window_is_left_alone_when_the_clock_advances(self, tmp_path):
        _, told = self._sample(False, tmp_path)
        assert told == [False]

    def test_a_frozen_clip_keeps_its_dense_window_points(self, tmp_path):
        """Merged points are deduped by timestamp, so an unrebuilt dense window
        collapses onto the coarse point at the same frozen instant."""
        points, _ = self._sample(True, tmp_path)
        # The window opens at at_seconds - DJI_PREVIEW_WINDOW_S / 2 = 97s.
        at_window = [
            p.timestamp
            for p in points
            if FROZEN + timedelta(seconds=95) < p.timestamp < FROZEN + timedelta(seconds=105)
        ]
        assert at_window == [FROZEN + timedelta(seconds=97), FROZEN + timedelta(seconds=98)]
