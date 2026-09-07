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
    clock_unreliable,
    dji_meta_clock_unreliable,
    parse_dji_meta_file,
    parse_dji_meta_window,
    position_frozen,
    read_declared_sample_rate,
    rebuild_timestamps,
    sample_dji_meta_track,
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
        """Each window is placed by its own offset into the clip.

        The windows carry ten seconds of samples each, since a handful of
        repeated timestamps is the clock's one-second resolution rather than a
        stall.
        """
        stalled = [point(i) for i in range(300)]
        extract, parse = self._extraction({0.0: list(stalled), 100.0: list(stalled)})
        with (
            patch("gpstitch.services.dji_meta_parser.extract_dji_meta_raw", side_effect=extract),
            patch("gpstitch.services.dji_meta_parser.parse_dji_meta", side_effect=parse),
        ):
            points = sample_dji_meta_track(tmp_path / "v.mp4", total_duration_s=200.0, samples=2, stream_index=2)

        assert len(points) == 600
        assert points[0].timestamp == FROZEN
        assert points[299].timestamp == FROZEN + timedelta(seconds=299 / 30.0)
        assert points[300].timestamp == FROZEN + timedelta(seconds=100)
        assert points[-1].timestamp == FROZEN + timedelta(seconds=100 + 299 / 30.0)

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

    def test_a_stalled_clock_gets_the_window_offset(self, tmp_path):
        extract, parse = self._patched([point(0), point(30)])
        with extract, parse:
            points = parse_dji_meta_window(
                tmp_path / "v.mp4",
                start_s=240.0,
                duration_s=2.0,
                stream_index=2,
                rebuild_clock=True,
            )
        assert [p.timestamp for p in points] == [
            FROZEN + timedelta(seconds=240),
            FROZEN + timedelta(seconds=241),
        ]


class TestClockUnreliableProbe:
    """The preview asks whether the clock stalls, from windows at both ends.

    A stall is a long run of one timestamp, which a single window can see. Both
    ends are probed because the stall can sit at either: one clip stalled for
    its first 93% and resumed only in its last minute, while another was stuck
    throughout.
    """

    def _stall(self, n: int = 300) -> list[DjiMetaPoint]:
        return [point(i) for i in range(n)]

    def _healthy(self, base_s: int = 0, n: int = 300) -> list[DjiMetaPoint]:
        return [point(i, FROZEN + timedelta(seconds=base_s + i // 30)) for i in range(n)]

    def _probe(self, per_start, tmp_path, total_duration_s=600.0):
        seen: list[tuple[float, float]] = []

        def fake_window(file_path, *, start_s, duration_s, stream_index=None, rebuild_clock=False, anchor_ts=None):
            seen.append((start_s, duration_s))
            for key, points in per_start:
                if key == start_s:
                    return points
            return []

        with patch("gpstitch.services.dji_meta_parser.parse_dji_meta_window", side_effect=fake_window):
            unreliable = dji_meta_clock_unreliable(
                tmp_path / "v.mp4",
                total_duration_s=total_duration_s,
                stream_index=2,
            )
        return unreliable, seen

    def test_a_stall_at_the_start_is_found(self, tmp_path):
        unreliable, seen = self._probe([(0.0, self._stall())], tmp_path)
        assert unreliable is True
        assert len(seen) == 1, "a stall at the start settles it without a second read"

    def test_a_stall_only_at_the_end_is_found(self, tmp_path):
        """The real shape: healthy at the start, stalled by the end."""
        unreliable, seen = self._probe(
            [(0.0, self._healthy()), (570.0, self._stall())],
            tmp_path,
        )
        assert unreliable is True
        assert max(start for start, _ in seen) >= 540.0, "the tail window must reach the end of the clip"

    def test_a_clip_healthy_at_both_ends_is_reliable(self, tmp_path):
        unreliable, _ = self._probe(
            [(0.0, self._healthy()), (570.0, self._healthy(base_s=560))],
            tmp_path,
        )
        assert unreliable is False

    def test_granularity_is_not_mistaken_for_a_stall(self, tmp_path):
        """A window holding two points on one timestamp is one-second resolution."""
        unreliable, _ = self._probe([(0.0, [point(0), point(1)]), (570.0, [point(0), point(1)])], tmp_path)
        assert unreliable is False

    def test_the_first_window_widens_when_gps_locked_late(self, tmp_path):
        attempts = []

        def fake_window(file_path, *, start_s, duration_s, stream_index=None, rebuild_clock=False, anchor_ts=None):
            attempts.append((start_s, duration_s))
            if start_s == 0.0 and len(attempts) == 1:
                return []
            return [point(i) for i in range(300)]

        with patch("gpstitch.services.dji_meta_parser.parse_dji_meta_window", side_effect=fake_window):
            unreliable = dji_meta_clock_unreliable(tmp_path / "v.mp4", total_duration_s=600.0, stream_index=2)

        assert unreliable is True
        assert attempts[1][1] > attempts[0][1], "the empty window should widen"

    def test_a_clip_with_no_gps_at_all_is_reliable(self, tmp_path):
        """No evidence of a stall is not evidence of one."""
        unreliable, _ = self._probe([], tmp_path)
        assert unreliable is False

    def test_an_unknown_duration_cannot_be_judged(self, tmp_path):
        unreliable, seen = self._probe([(0.0, self._stall())], tmp_path, total_duration_s=0.0)
        assert unreliable is False
        assert seen == [], "without a duration there is no window to place"


class TestPreviewSampling:
    """The dense preview window cannot detect a freeze, so the sampler must tell it."""

    def _sample(self, frozen: bool, tmp_path):
        from gpstitch.services.renderer import _sample_dji_meta_for_frame

        told: list[bool] = []

        def fake_window(file_path, *, start_s, duration_s, stream_index=None, rebuild_clock=False, anchor_ts=None):
            told.append((rebuild_clock, anchor_ts))
            # Mirror the real function: only a told window rebuilds its own axis.
            points = [point(0), point(30)]
            if rebuild_clock:
                return rebuild_timestamps(points, sample_rate_hz=30.0, start_offset_s=start_s, anchor_ts=anchor_ts)
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
            frozen_check=lambda _, total_duration_s=0.0, stream_index=None: frozen,
        )
        return points, told

    def test_the_dense_window_is_told_when_the_clock_stalls(self, tmp_path):
        _, told = self._sample(True, tmp_path)
        assert [flag for flag, _ in told] == [True]

    def test_the_dense_window_shares_the_coarse_anchor(self, tmp_path):
        """Both must land on one axis, or a jumped stretch drags the window away."""
        _, told = self._sample(True, tmp_path)
        assert [anchor for _, anchor in told] == [FROZEN]

    def test_the_dense_window_is_left_alone_when_the_clock_advances(self, tmp_path):
        _, told = self._sample(False, tmp_path)
        assert [flag for flag, _ in told] == [False]

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


class TestPositionFrozen:
    """A stuck GPS fix reports the same coordinates for the whole recording.

    Three clips off one card did this - the Bluetooth remote lost its link and
    kept re-reporting its last known fix, so the clock, position, altitude and
    velocity were all constant. Rebuilding the time axis makes such a clip
    render, but its map and place name have nothing to show.
    """

    def _at(self, lat: float, lon: float, frame_idx: int = 0) -> DjiMetaPoint:
        return DjiMetaPoint(
            frame_idx=frame_idx,
            timestamp=FROZEN,
            lat=lat,
            lon=lon,
            alt_m=45.27,
            velocity_2d=(0.0, 0.0),
        )

    def test_identical_coordinates_are_frozen(self):
        points = [self._at(53.215096, 6.614336, i) for i in range(5)]
        assert position_frozen(points) is True

    def test_a_moving_track_is_not_frozen(self):
        points = [self._at(53.215096 + i * 0.001, 6.614336, i) for i in range(5)]
        assert position_frozen(points) is False

    def test_a_metre_of_jitter_still_counts_as_frozen(self):
        """A fix that wobbles in its last digits has still not gone anywhere."""
        points = [self._at(53.215096 + i * 1e-6, 6.614336 - i * 1e-6, i) for i in range(5)]
        assert position_frozen(points) is True

    def test_moving_in_longitude_alone_is_not_frozen(self):
        points = [self._at(53.215096, 6.614336 + i * 0.001, i) for i in range(5)]
        assert position_frozen(points) is False

    def test_a_single_point_is_not_judged(self):
        """One point says nothing about whether the fix was stuck."""
        assert position_frozen([self._at(53.215096, 6.614336)]) is False

    def test_no_points_are_not_judged(self):
        assert position_frozen([]) is False


class TestClockUnreliable:
    """A clock that stalls for a long stretch cannot be trusted anywhere.

    One clip stalled for 26483 of its 28344 samples and then resumed roughly
    four hours ahead of where it stopped, so its 30 distinct timestamps claimed
    a four-hour track for a 16-minute video. Timeseries is keyed by datetime, so
    that rendered as 30 GPS points.

    Timestamps carry one-second resolution, so a healthy clip repeats each value
    for about one sample rate's worth of samples. A run far longer than that is
    a stalled clock, and it is detectable locally - unlike "every timestamp is
    identical", which a short window cannot tell from ordinary granularity.
    """

    def _stalled(self, run_length: int, then: int = 60) -> list[DjiMetaPoint]:
        points = [point(i) for i in range(run_length)]
        points += [point(run_length + i, FROZEN + timedelta(seconds=14000 + i // 30)) for i in range(then)]
        return points

    def test_a_healthy_clock_is_reliable(self):
        """One second of samples per timestamp at 30Hz is normal."""
        points = [point(i, FROZEN + timedelta(seconds=i // 30)) for i in range(300)]
        assert clock_unreliable(points, sample_rate_hz=30.0) is False

    def test_a_fully_frozen_clock_is_unreliable(self):
        assert clock_unreliable([point(i) for i in range(300)], sample_rate_hz=30.0) is True

    def test_a_long_stall_followed_by_a_jump_is_unreliable(self):
        """The real shape: stalled for most of the clip, then resumed elsewhere."""
        assert clock_unreliable(self._stalled(900), sample_rate_hz=30.0) is True

    def test_a_stall_within_the_clock_resolution_is_reliable(self):
        """Two seconds on one value is granularity, not a stall."""
        points = [point(i) for i in range(60)]
        points += [point(60 + i, FROZEN + timedelta(seconds=1 + i // 30)) for i in range(120)]
        assert clock_unreliable(points, sample_rate_hz=30.0) is False

    def test_a_gap_in_coverage_is_not_a_stall(self):
        """GPS dropping out leaves missing samples, not repeated timestamps.

        Rebuilding here would compress two real segments together, so this must
        stay reliable.
        """
        early = [point(i, FROZEN + timedelta(seconds=i // 30)) for i in range(300)]
        late = [point(20000 + i, FROZEN + timedelta(seconds=600 + i // 30)) for i in range(300)]
        assert clock_unreliable(early + late, sample_rate_hz=30.0) is False

    def test_too_few_points_to_judge(self):
        assert clock_unreliable([point(0)], sample_rate_hz=30.0) is False
        assert clock_unreliable([], sample_rate_hz=30.0) is False

    def test_a_missing_rate_falls_back_to_the_default(self):
        long_run = [point(i) for i in range(int(_DEFAULT_SAMPLE_RATE_HZ * 10))]
        assert clock_unreliable(long_run, sample_rate_hz=None) is True


class TestRebuildAnchor:
    """Windows taken from different offsets have to land on one axis."""

    def test_an_explicit_anchor_overrides_the_first_point(self):
        anchor = FROZEN - timedelta(seconds=100)
        rebuilt = rebuild_timestamps(
            [point(0), point(30)],
            sample_rate_hz=30.0,
            start_offset_s=240.0,
            anchor_ts=anchor,
        )
        assert rebuilt[0].timestamp == anchor + timedelta(seconds=240)
        assert rebuilt[1].timestamp == anchor + timedelta(seconds=241)

    def test_without_an_anchor_the_first_point_is_used(self):
        rebuilt = rebuild_timestamps([point(0), point(30)], sample_rate_hz=30.0)
        assert rebuilt[0].timestamp == FROZEN


class TestFullParseUsesReliability:
    """The render path rebuilds whenever the clock stalls, not only when it never moves."""

    def _patched(self, points, rate=30.0):
        return (
            patch(
                "gpstitch.services.dji_meta_parser.extract_dji_meta_raw",
                return_value=raw_with_declared_rate(rate),
            ),
            patch("gpstitch.services.dji_meta_parser.parse_dji_meta", return_value=points),
            patch("gpstitch.services.dji_meta_parser.detect_dji_meta_stream", return_value=2),
        )

    def test_a_stalled_clock_that_jumps_is_rebuilt(self, tmp_path):
        """The real clip: stalled for most of its length, then four hours ahead.

        Left alone, 30 distinct timestamps claimed a four-hour track for a
        16-minute video and Timeseries kept only 30 points.
        """
        stalled = [point(i) for i in range(900)]
        stalled += [point(900 + i, FROZEN + timedelta(seconds=14000 + i // 30)) for i in range(60)]

        extract, parse, detect = self._patched(stalled)
        with extract, parse, detect:
            points = parse_dji_meta_file(tmp_path / "v.mp4")

        span = (points[-1].timestamp - points[0].timestamp).total_seconds()
        assert span == pytest.approx(959 / 30.0, abs=0.01), "the axis should follow the sample index"
        assert len({p.timestamp for p in points}) == len(points), "every sample gets its own instant"

    def test_a_healthy_clock_is_left_alone(self, tmp_path):
        healthy = [point(i, FROZEN + timedelta(seconds=i // 30)) for i in range(300)]

        extract, parse, detect = self._patched(healthy)
        with extract, parse, detect:
            points = parse_dji_meta_file(tmp_path / "v.mp4")

        assert [p.timestamp for p in points] == [p.timestamp for p in healthy]

    def test_a_gap_in_coverage_is_preserved(self, tmp_path):
        """Rebuilding here would compress two real segments together."""
        early = [point(i, FROZEN + timedelta(seconds=i // 30)) for i in range(300)]
        late = [point(20000 + i, FROZEN + timedelta(seconds=600 + i // 30)) for i in range(300)]

        extract, parse, detect = self._patched(early + late)
        with extract, parse, detect:
            points = parse_dji_meta_file(tmp_path / "v.mp4")

        span = (points[-1].timestamp - points[0].timestamp).total_seconds()
        assert span == pytest.approx(609.0, abs=1.0), "the real gap must survive"


class TestCoarseTrackAnchoring:
    """Coarse windows must land on one axis even when the clock jumped mid-clip.

    Anchoring each window on its own first timestamp breaks down once one window
    sits in a stretch where the clock had jumped hours ahead: that window lands
    hours away from its neighbours. Every window is anchored on the clip's first
    timestamp instead, so the axis stays the sample index throughout.
    """

    def _extraction(self, per_window, rate=30.0):
        calls: list[float] = []

        def fake_extract(file_path, stream_index, *, start_s=None, duration_s=None):
            calls.append(start_s or 0.0)
            return raw_with_declared_rate(rate)

        def fake_parse(raw):
            return list(per_window[calls[-1]])

        return fake_extract, fake_parse

    def test_a_window_in_a_jumped_stretch_is_pulled_onto_the_axis(self, tmp_path):
        stalled = [point(i) for i in range(300)]
        jumped = [point(i, FROZEN + timedelta(seconds=14000 + i // 30)) for i in range(300)]

        extract, parse = self._extraction({0.0: stalled, 100.0: jumped})
        with (
            patch("gpstitch.services.dji_meta_parser.extract_dji_meta_raw", side_effect=extract),
            patch("gpstitch.services.dji_meta_parser.parse_dji_meta", side_effect=parse),
        ):
            points = sample_dji_meta_track(tmp_path / "v.mp4", total_duration_s=200.0, samples=2, stream_index=2)

        span = (points[-1].timestamp - points[0].timestamp).total_seconds()
        assert span < 200.0, f"windows should sit inside the clip, not hours apart (got {span}s)"
        assert points[0].timestamp == FROZEN
        assert points[-1].timestamp == FROZEN + timedelta(seconds=100 + 299 / 30.0)

    def test_a_healthy_coarse_track_is_untouched(self, tmp_path):
        early = [point(i, FROZEN + timedelta(seconds=i // 30)) for i in range(300)]
        late = [point(i, FROZEN + timedelta(seconds=100 + i // 30)) for i in range(300)]

        extract, parse = self._extraction({0.0: early, 100.0: late})
        with (
            patch("gpstitch.services.dji_meta_parser.extract_dji_meta_raw", side_effect=extract),
            patch("gpstitch.services.dji_meta_parser.parse_dji_meta", side_effect=parse),
        ):
            points = sample_dji_meta_track(tmp_path / "v.mp4", total_duration_s=200.0, samples=2, stream_index=2)

        assert points[0].timestamp == FROZEN
        assert points[-1].timestamp == FROZEN + timedelta(seconds=109)
