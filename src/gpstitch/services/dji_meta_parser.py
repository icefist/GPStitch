"""DJI Osmo Action embedded GPS telemetry parser.

Parses protobuf-encoded "DJI meta" stream (codec_tag=djmd) from DJI Osmo Action
cameras (Action 4/5/6) that embed GPS telemetry from the DJI GPS Bluetooth
Remote Controller (e.g. "DJI AC004") directly in the MP4 file.

Protobuf wire format per sample:
    f3 (message) — main data frame
    └── f4 (message) — GPS data from remote controller
        ├── f1 (message) — device info
        │   ├── f4 (string) — device name "DJI AC004"
        │   └── f5 (float) — sample rate (25.0)
        ├── f2 (message) — GPS fix
        │   ├── f1 (message)
        │   │   ├── f1 (varint) — GPS fix type (1=3D)
        │   │   ├── f2 (double) — latitude
        │   │   └── f3 (double) — longitude
        │   ├── f2 (varint) — altitude in mm
        │   └── f6 (message)
        │       └── f1 (string) — timestamp "2026-03-15 23:54:17"
        └── f3 (message) — 2D velocity
            ├── f1 (float) — vx (m/s)
            └── f2 (float) — vy (m/s)
"""

import json
import logging
import math
import struct
import subprocess
from dataclasses import dataclass
from dataclasses import replace as dc_replace
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree.ElementTree import Element, ElementTree, SubElement

from gopro_overlay.ffmpeg import FFMPEG
from gopro_overlay.gpmf import GPSFix
from gopro_overlay.point import Point
from gopro_overlay.timeseries import Entry, Timeseries

logger = logging.getLogger(__name__)


# --- Protobuf wire format decoder ---


def _decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Decode a protobuf varint starting at pos. Returns (value, new_pos)."""
    result = 0
    shift = 0
    while pos < len(data) and shift < 70:  # 10 bytes max for 64-bit varint
        b = data[pos]
        result |= (b & 0x7F) << shift
        pos += 1
        shift += 7
        if not (b & 0x80):
            break
    return result, pos


def _decode_field(data: bytes, pos: int) -> tuple[int | None, int, bytes | int | None, int]:
    """Decode one protobuf field. Returns (field_num, wire_type, value, new_pos).

    Wire types:
        0 = varint (value is int)
        1 = 64-bit fixed (value is 8 bytes)
        2 = length-delimited (value is bytes)
        5 = 32-bit fixed (value is 4 bytes)
    """
    if pos >= len(data):
        return None, 0, None, pos
    tag, pos = _decode_varint(data, pos)
    field_num = tag >> 3
    wire_type = tag & 0x07
    if wire_type == 0:  # varint
        value, pos = _decode_varint(data, pos)
        return field_num, wire_type, value, pos
    elif wire_type == 1:  # 64-bit
        if pos + 8 > len(data):
            return None, 0, None, len(data)
        return field_num, wire_type, data[pos : pos + 8], pos + 8
    elif wire_type == 2:  # length-delimited
        length, pos = _decode_varint(data, pos)
        if pos + length > len(data):
            return None, 0, None, len(data)
        return field_num, wire_type, data[pos : pos + length], pos + length
    elif wire_type == 5:  # 32-bit
        if pos + 4 > len(data):
            return None, 0, None, len(data)
        return field_num, wire_type, data[pos : pos + 4], pos + 4
    else:
        return None, 0, None, pos + 1


def _decode_double(data: bytes) -> float:
    """Decode 8 bytes as little-endian double."""
    return struct.unpack("<d", data)[0]


def _decode_float(data: bytes) -> float:
    """Decode 4 bytes as little-endian float."""
    return struct.unpack("<f", data)[0]


def _iter_fields(data: bytes):
    """Iterate over all protobuf fields in data. Yields (field_num, wire_type, value)."""
    pos = 0
    while pos < len(data):
        fn, wt, val, pos = _decode_field(data, pos)
        if fn is None:
            break
        yield fn, wt, val


def _get_submessage(data: bytes, target_field: int) -> bytes | None:
    """Find first length-delimited field with given field number."""
    for fn, wt, val in _iter_fields(data):
        if fn == target_field and wt == 2:
            return val
    return None


def _get_varint(data: bytes, target_field: int) -> int | None:
    """Find first varint field with given field number."""
    for fn, wt, val in _iter_fields(data):
        if fn == target_field and wt == 0:
            return val
    return None


def _get_double(data: bytes, target_field: int) -> float | None:
    """Find first 64-bit fixed field with given field number."""
    for fn, wt, val in _iter_fields(data):
        if fn == target_field and wt == 1:
            return _decode_double(val)
    return None


def _get_float(data: bytes, target_field: int) -> float | None:
    """Find first 32-bit fixed field with given field number."""
    for fn, wt, val in _iter_fields(data):
        if fn == target_field and wt == 5:
            return _decode_float(val)
    return None


def _get_string(data: bytes, target_field: int) -> str | None:
    """Find first length-delimited field and decode as UTF-8 string."""
    val = _get_submessage(data, target_field)
    if val is not None:
        try:
            return val.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None


# --- DJI Meta data structures ---

_DJI_META_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass
class DjiMetaPoint:
    """A single GPS telemetry point from a DJI meta stream."""

    frame_idx: int
    timestamp: datetime  # naive datetime in local time (from GPS remote)
    lat: float
    lon: float
    alt_m: float  # altitude in meters (converted from mm)
    velocity_2d: tuple[float, float]  # (vx, vy) in m/s


# --- Protobuf GPS extraction ---


def _parse_gps_from_sample(sample_data: bytes, frame_idx: int) -> DjiMetaPoint | None:
    """Parse GPS data from a single DJI meta protobuf sample.

    Args:
        sample_data: Raw protobuf bytes for one sample (inner content of top-level field 3)
        frame_idx: Frame index counter

    Returns:
        DjiMetaPoint if GPS data is present and valid, None otherwise
    """
    # f4 = GPS data from remote controller
    gps_msg = _get_submessage(sample_data, 4)
    if gps_msg is None:
        return None

    # f4.f2 = GPS fix data
    fix_msg = _get_submessage(gps_msg, 2)
    if fix_msg is None:
        return None

    # f4.f2.f1 = GPS coordinates (submessage with fix type, lat, lon)
    coords_msg = _get_submessage(fix_msg, 1)
    if coords_msg is None:
        return None

    # f4.f2.f1.f1 = GPS fix type (1=3D lock)
    fix_type = _get_varint(coords_msg, 1)
    if fix_type is None or fix_type == 0:
        return None  # No GPS fix

    # f4.f2.f1.f2 = latitude (double)
    lat = _get_double(coords_msg, 2)
    # f4.f2.f1.f3 = longitude (double)
    lon = _get_double(coords_msg, 3)
    if lat is None or lon is None:
        return None

    # Skip zero coordinates (invalid GPS)
    if lat == 0.0 and lon == 0.0:
        return None

    # f4.f2.f2 = altitude in mm (varint)
    alt_mm = _get_varint(fix_msg, 2)
    alt_m = alt_mm / 1000.0 if alt_mm is not None else 0.0

    # f4.f2.f6.f1 = timestamp string
    timestamp_msg = _get_submessage(fix_msg, 6)
    timestamp = None
    if timestamp_msg is not None:
        ts_str = _get_string(timestamp_msg, 1)
        if ts_str:
            try:
                timestamp = datetime.strptime(ts_str, _DJI_META_TIMESTAMP_FORMAT)
            except ValueError:
                logger.warning("Failed to parse DJI meta timestamp: %s", ts_str)

    if timestamp is None:
        return None

    # f4.f3 = velocity (submessage with f1=vx, f2=vy floats)
    vx, vy = 0.0, 0.0
    vel_msg = _get_submessage(gps_msg, 3)
    if vel_msg is not None:
        vx_val = _get_float(vel_msg, 1)
        vy_val = _get_float(vel_msg, 2)
        if vx_val is not None:
            vx = vx_val
        if vy_val is not None:
            vy = vy_val

    return DjiMetaPoint(
        frame_idx=frame_idx,
        timestamp=timestamp,
        lat=lat,
        lon=lon,
        alt_m=alt_m,
        velocity_2d=(vx, vy),
    )


# --- Stream detection and extraction ---


def detect_dji_meta_stream(file_path: Path) -> int | None:
    """Detect the DJI meta stream index in an MP4 file using ffprobe.

    Looks for a data stream with codec_tag_string=djmd or handler_name="DJI meta".

    Args:
        file_path: Path to the video file

    Returns:
        Stream index (int) if found, None otherwise
    """
    try:
        ffmpeg = FFMPEG()
        output = str(
            ffmpeg.ffprobe()
            .invoke(
                [
                    "-hide_banner",
                    "-print_format",
                    "json",
                    "-show_streams",
                    str(file_path),
                ]
            )
            .stdout
        )
        data = json.loads(output)
    except Exception:
        logger.warning("Failed to run ffprobe on %s", file_path)
        return None

    for stream in data.get("streams", []):
        codec_tag = stream.get("codec_tag_string", "")
        handler = stream.get("tags", {}).get("handler_name", "")
        if codec_tag == "djmd" or handler == "DJI meta":
            idx = stream.get("index")
            if idx is not None:
                return int(idx)

    return None


def extract_dji_meta_raw(
    file_path: Path,
    stream_index: int,
    *,
    start_s: float | None = None,
    duration_s: float | None = None,
) -> bytes:
    """Extract raw DJI meta stream bytes using ffmpeg.

    With no bounds this reads the whole stream, whose samples are interleaved
    across the entire file - so it costs a full seek through it. Rendering needs
    that; a single preview frame does not, which is what the bounds are for.

    Args:
        file_path: Path to the video file
        stream_index: Stream index of the DJI meta track
        start_s: Seek to this offset first. Passed before -i, which seeks by
            index rather than decoding forward from the start.
        duration_s: Stop after this much content.

    Returns:
        Raw protobuf bytes (concatenated samples)

    Raises:
        RuntimeError: If ffmpeg extraction fails
    """
    ffmpeg_bin = FFMPEG().binary
    cmd = [ffmpeg_bin]
    if start_s is not None:
        # Before -i: ffmpeg seeks using the container index instead of decoding
        # from the beginning, which is what makes this cheap on a large file.
        cmd += ["-ss", str(start_s)]
    cmd += ["-i", str(file_path), "-map", f"0:{stream_index}", "-c", "copy", "-f", "data"]
    if duration_s is not None:
        cmd += ["-t", str(duration_s)]
    cmd += ["pipe:1"]

    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        stderr_msg = result.stderr.decode(errors="replace").strip() if result.stderr else ""
        raise RuntimeError(f"ffmpeg failed to extract DJI meta stream from {file_path}: {stderr_msg}")
    return result.stdout


# --- Frozen GPS clock repair ---

# DJI Action records one GPS sample per video frame and the stream declares its
# own rate. A clip missing that field still needs some time axis; 29.97 is what
# every AC003 clip measured so far reports.
_DEFAULT_SAMPLE_RATE_HZ = 29.97


def read_declared_sample_rate(raw_data: bytes) -> float | None:
    """The sample rate the stream declares for itself, from the first sample's device info."""
    pos = 0
    while pos < len(raw_data):
        fn, wt, val, pos = _decode_field(raw_data, pos)
        if fn is None:
            break
        if fn == 3 and wt == 2 and isinstance(val, bytes):
            gps_msg = _get_submessage(val, 4)
            device_msg = _get_submessage(gps_msg, 1) if gps_msg is not None else None
            return _get_float(device_msg, 5) if device_msg is not None else None
    return None


# How long the clock may sit on one value before it counts as stalled. A healthy
# stream repeats each value for about one sample rate's worth of samples, since
# the timestamps carry one-second resolution; five times that is comfortably
# beyond granularity and well short of a real stall.
_MAX_STUCK_RUN_S = 5.0


def clock_unreliable(points: list[DjiMetaPoint], *, sample_rate_hz: float | None) -> bool:
    """Whether the GPS clock stalls for long enough to be untrustworthy.

    One clip stalled for 26483 of its 28344 samples and then resumed about four
    hours ahead, so its timestamps claimed a four-hour track for a 16-minute
    video. Timeseries is keyed by datetime, which left 30 usable points.

    Judged on the longest run of consecutive identical timestamps, which is a
    local signal and does not confuse a stall with a gap in coverage: GPS
    dropping out leaves samples missing, not repeated. Rebuilding a genuine gap
    would compress two real segments together, so that distinction matters.
    """
    if len(points) < 2:
        return False

    rate = sample_rate_hz if sample_rate_hz and sample_rate_hz > 0 else _DEFAULT_SAMPLE_RATE_HZ
    limit = _MAX_STUCK_RUN_S * rate

    longest = run = 1
    for previous, current in zip(points, points[1:], strict=False):
        run = run + 1 if current.timestamp == previous.timestamp else 1
        longest = max(longest, run)

    return longest > limit


def track_span_seconds(points: list[DjiMetaPoint]) -> float:
    """How much time the track covers, from its earliest point to its latest.

    Taken across all points rather than first-to-last, because parse_dji_meta
    returns samples in the order the stream carries them.
    """
    if len(points) < 2:
        return 0.0
    return (max(p.timestamp for p in points) - min(p.timestamp for p in points)).total_seconds()


def derive_sample_rate(points: list[DjiMetaPoint], *, target_hz: int) -> int:
    """Take every N-th point to land near `target_hz`, from the track's own timing.

    Returns 1 - keep everything - when there is no timing to derive from. That
    means a track spanning no time, which cannot be rendered at all; refusing
    such a track is a rendering precondition and belongs to the caller that
    cares, not here. Dividing by a duration floored at one second instead once
    returned a rate equal to the point count, leaving a single-point track.
    """
    from gpstitch.services.srt_parser import calc_sample_rate

    span_s = track_span_seconds(points)
    if span_s <= 0:
        return 1
    return calc_sample_rate(len(points) / span_s, target_hz)


def points_on_joined_timeline(
    segments: list[tuple[list[DjiMetaPoint], float]],
) -> list[DjiMetaPoint]:
    """Place several clips' GPS on the timeline of those clips joined end to end.

    Each segment is one clip's points and that clip's duration in seconds, in
    play order. A clip's points are re-based onto the cumulative duration of the
    clips before it, keeping their spacing within the clip.

    Clips recorded back to back - a camera splitting one recording at a file size
    limit - land where their own timestamps would put them anyway. Clips with a
    real gap between them have the gap removed, because the joined video has no
    gap either and the overlay has to match the picture rather than the clock.

    A segment with no points still advances the offset, so a clip whose GPS never
    locked leaves a hole rather than dragging everything after it earlier.
    """
    anchor: datetime | None = None
    for points, _ in segments:
        if points:
            anchor = min(p.timestamp for p in points)
            break
    if anchor is None:
        return []

    joined: list[DjiMetaPoint] = []
    offset_s = 0.0
    next_frame_idx = 0

    for points, duration_s in segments:
        if points:
            segment_start = min(p.timestamp for p in points)
            base = anchor + timedelta(seconds=offset_s)
            ordered = sorted(points, key=lambda p: p.timestamp)
            for index, point in enumerate(ordered):
                joined.append(
                    dc_replace(
                        point,
                        timestamp=base + (point.timestamp - segment_start),
                        frame_idx=next_frame_idx + index,
                    )
                )
            next_frame_idx += len(ordered)
        offset_s += duration_s

    return joined


# A fix that wobbles inside this much has not gone anywhere: 1e-5 degrees is
# about 1.1m of latitude, and less in longitude at these latitudes.
_STATIONARY_SPREAD_DEG = 1e-5


def position_frozen(points: list[DjiMetaPoint]) -> bool:
    """Whether the GPS fix never moves across the whole track.

    A stuck fix - the remote lost its link and kept re-reporting its last known
    position - leaves a map with nothing to pan and a place name with nothing to
    change. Judged over the entire track, so a clip that genuinely never moved
    more than a metre is treated the same way, which is the right answer for it
    too.
    """
    if len(points) < 2:
        return False

    lats = [p.lat for p in points]
    lons = [p.lon for p in points]
    return (max(lats) - min(lats)) < _STATIONARY_SPREAD_DEG and (max(lons) - min(lons)) < _STATIONARY_SPREAD_DEG


def rebuild_timestamps(
    points: list[DjiMetaPoint],
    *,
    sample_rate_hz: float | None,
    start_offset_s: float = 0.0,
    anchor_ts: datetime | None = None,
) -> list[DjiMetaPoint]:
    """Rebuild an untrustworthy clip's time axis from the sample index.

    Anchored on the first point's own frame index, not on frame zero: callers
    derive the video's start time by subtracting that index from the first
    timestamp, so moving the first point would count a GPS lock delay twice.

    `start_offset_s` places a window taken from part-way through the stream,
    where frame indices restart at zero. `anchor_ts` overrides the first point's
    timestamp as the axis origin, so windows from different offsets - one of
    which may sit in a stretch where the clock had jumped - still land on one
    coherent axis.
    """
    if not points:
        return points

    rate = sample_rate_hz if sample_rate_hz and sample_rate_hz > 0 else _DEFAULT_SAMPLE_RATE_HZ
    base_idx = points[0].frame_idx
    base_ts = (anchor_ts if anchor_ts is not None else points[0].timestamp) + timedelta(seconds=start_offset_s)
    return [dc_replace(p, timestamp=base_ts + timedelta(seconds=(p.frame_idx - base_idx) / rate)) for p in points]


def _repaired(
    raw: bytes,
    points: list[DjiMetaPoint],
    *,
    start_offset_s: float = 0.0,
    anchor_ts: datetime | None = None,
) -> list[DjiMetaPoint]:
    """Rebuild `points` off the rate `raw` declares for itself."""
    return rebuild_timestamps(
        points,
        sample_rate_hz=read_declared_sample_rate(raw),
        start_offset_s=start_offset_s,
        anchor_ts=anchor_ts,
    )


# The second probe sits at the very end of the clip, to catch a clock that runs
# cleanly at the start and stalls later. Sampling short of the end would miss it.
_PROBE_TAIL_WINDOW_S = 30.0


def dji_meta_clock_unreliable(
    file_path: Path,
    *,
    total_duration_s: float,
    stream_index: int | None = None,
) -> bool:
    """Whether this clip's GPS clock stalls, judged from windows at both ends.

    A stall is a long run of one timestamp, and that shows up inside a single
    window - unlike "every timestamp is identical", which a short window cannot
    tell apart from the clock's one-second granularity.

    Both ends are probed because the stall can sit at either: one clip stalled
    for its first 93% and resumed only in its last minute. A stall confined to
    the middle is missed here; the full parse behind a render sees the whole
    stream and catches it there.

    The threshold scales with the declared sample rate, which a window does not
    carry, so the default rate is assumed - it only sets how many repeated
    samples count as a stall.
    """
    if total_duration_s <= 0:
        return False

    at_start: list[DjiMetaPoint] = []
    for window_s in FIRST_POINT_WINDOWS_S:
        # Widen while GPS had not locked yet and the window came back empty.
        at_start = parse_dji_meta_window(file_path, start_s=0.0, duration_s=window_s, stream_index=stream_index)
        if len(at_start) >= 2:
            break

    if clock_unreliable(at_start, sample_rate_hz=None):
        return True

    at_tail = parse_dji_meta_window(
        file_path,
        start_s=max(0.0, total_duration_s - _PROBE_TAIL_WINDOW_S),
        duration_s=_PROBE_TAIL_WINDOW_S,
        stream_index=stream_index,
    )
    return clock_unreliable(at_tail, sample_rate_hz=None)


def parse_dji_meta_window(
    file_path: Path,
    *,
    start_s: float,
    duration_s: float,
    stream_index: int | None = None,
    rebuild_clock: bool = False,
    anchor_ts: datetime | None = None,
) -> list[DjiMetaPoint]:
    """GPS points from a slice of the stream around `start_s`.

    `rebuild_clock` has to be established by the caller - see
    `dji_meta_clock_unreliable` - because a window this short may not contain
    enough of a stall to recognise one. `anchor_ts` keeps the rebuilt window on
    the same axis as the rest of the track.
    """
    idx = stream_index if stream_index is not None else detect_dji_meta_stream(file_path)
    if idx is None:
        return []
    raw = extract_dji_meta_raw(file_path, idx, start_s=max(0.0, start_s), duration_s=duration_s)
    points = parse_dji_meta(raw)
    if rebuild_clock and points:
        logger.debug("Rebuilding %d timestamps for the window at %.1fs", len(points), start_s)
        return _repaired(raw, points, start_offset_s=max(0.0, start_s), anchor_ts=anchor_ts)
    return points


# Windows tried when hunting for the first GPS point. A camera can take a while
# to get a fix - one real clip had none in its first 10s and 490 points by 60s -
# so an empty first window means widen, not "no GPS".
FIRST_POINT_WINDOWS_S = (30.0, 120.0, 300.0)


def first_dji_meta_point(
    file_path: Path,
    *,
    stream_index: int | None = None,
) -> "DjiMetaPoint | None":
    """The first GPS point, read from the start of the stream rather than all of it.

    Callers want this for the recording's start time. Parsing the whole stream
    to reach its first record means seeking through the entire file.
    """
    idx = stream_index if stream_index is not None else detect_dji_meta_stream(file_path)
    if idx is None:
        return None

    for window_s in FIRST_POINT_WINDOWS_S:
        try:
            raw = extract_dji_meta_raw(file_path, idx, start_s=0.0, duration_s=window_s)
        except RuntimeError:
            return None
        points = parse_dji_meta(raw)
        if points:
            return points[0]

    return None


def sample_dji_meta_track(
    file_path: Path,
    *,
    total_duration_s: float,
    samples: int = 20,
    window_s: float = 2.0,
    stream_index: int | None = None,
) -> list[DjiMetaPoint]:
    """A coarse view of the whole track, from short windows spread across it.

    Enough for a preview's journey map and a rough odometer without reading the
    entire stream. One failed window is skipped rather than losing the rest.
    """
    idx = stream_index if stream_index is not None else detect_dji_meta_stream(file_path)
    if idx is None:
        return []

    count = max(1, samples) if total_duration_s > 0 else 1
    step = total_duration_s / count if count > 1 else 0.0

    windows: list[tuple[float, bytes, list[DjiMetaPoint]]] = []
    for i in range(count):
        start_s = i * step
        try:
            raw = extract_dji_meta_raw(file_path, idx, start_s=start_s, duration_s=window_s)
        except RuntimeError:
            logger.debug("DJI meta sample window at %.1fs failed for %s", start_s, file_path)
            continue
        window_points = parse_dji_meta(raw)
        if window_points:
            windows.append((start_s, raw, window_points))

    points = [p for _, _, window_points in windows for p in window_points]

    # A clock that stalls across windows taken from all over the clip is not
    # telling us where they belong; each window's own offset is. One window on
    # its own proves nothing - it can be shorter than the stall threshold.
    #
    # Every window is anchored on the clip's first timestamp rather than its own,
    # because a window landing in a stretch where the clock had jumped would
    # otherwise sit hours away from its neighbours.
    if len(windows) > 1 and clock_unreliable(points, sample_rate_hz=read_declared_sample_rate(windows[0][1])):
        anchor = windows[0][2][0].timestamp
        logger.info("DJI meta clock stalls in %s; rebuilding coarse windows from %s", file_path, anchor)
        points = [
            p
            for start_s, raw, window_points in windows
            for p in _repaired(raw, window_points, start_offset_s=start_s, anchor_ts=anchor)
        ]

    points.sort(key=lambda p: p.timestamp)
    return points


# --- Main parsing functions ---


def parse_dji_meta(raw_data: bytes) -> list[DjiMetaPoint]:
    """Parse raw DJI meta protobuf bytes into GPS points.

    The raw stream consists of concatenated protobuf messages, each wrapped
    in a top-level field 3 (length-delimited message).

    Args:
        raw_data: Raw bytes from ffmpeg extraction

    Returns:
        List of DjiMetaPoint with valid GPS data (samples without GPS are skipped)
    """
    points = []
    frame_idx = 0

    pos = 0
    while pos < len(raw_data):
        fn, wt, val, pos = _decode_field(raw_data, pos)
        if fn is None:
            break

        # Each sample is a top-level field 3, length-delimited message
        if fn == 3 and wt == 2 and isinstance(val, bytes):
            point = _parse_gps_from_sample(val, frame_idx)
            if point is not None:
                points.append(point)
            frame_idx += 1
        # Skip any non-field-3 data (shouldn't happen but be safe)

    return points


def parse_dji_meta_file(file_path: Path) -> list[DjiMetaPoint]:
    """Convenience: detect DJI meta stream, extract, and parse GPS points.

    Args:
        file_path: Path to the video file

    Returns:
        List of DjiMetaPoint with valid GPS data

    Raises:
        ValueError: If no DJI meta stream found
        RuntimeError: If ffmpeg extraction fails
    """
    stream_idx = detect_dji_meta_stream(file_path)
    if stream_idx is None:
        raise ValueError(f"No DJI meta stream found in {file_path}")

    raw_data = extract_dji_meta_raw(file_path, stream_idx)
    points = parse_dji_meta(raw_data)
    if clock_unreliable(points, sample_rate_hz=read_declared_sample_rate(raw_data)):
        logger.info(
            "DJI meta clock stalls in %s (%d points, first stamped %s); rebuilding from the sample rate",
            file_path.name,
            len(points),
            points[0].timestamp,
        )
        return _repaired(raw_data, points)
    return points


def get_dji_meta_metadata(file_path: Path, *, stream_index: int | None = None) -> dict:
    """Extract metadata about DJI meta GPS data in a video file.

    Args:
        file_path: Path to the video file
        stream_index: Optional pre-detected stream index to avoid redundant ffprobe call

    Returns:
        Dict with gps_point_count, duration_seconds, device_name, sample_rate_hz.
        Returns empty dict if no DJI meta stream found.
    """
    stream_idx = stream_index if stream_index is not None else detect_dji_meta_stream(file_path)
    if stream_idx is None:
        return {}

    try:
        raw_data = extract_dji_meta_raw(file_path, stream_idx)
    except RuntimeError:
        return {}

    points = parse_dji_meta(raw_data)

    # Extract device info from raw data (first sample's f4.f1)
    device_name = None
    sample_rate_hz = None
    pos = 0
    while pos < len(raw_data):
        fn, wt, val, pos = _decode_field(raw_data, pos)
        if fn is None:
            break
        if fn == 3 and wt == 2 and isinstance(val, bytes):
            gps_msg = _get_submessage(val, 4)
            if gps_msg is not None:
                device_msg = _get_submessage(gps_msg, 1)
                if device_msg is not None:
                    device_name = _get_string(device_msg, 4)
                    sr = _get_float(device_msg, 5)
                    if sr is not None:
                        sample_rate_hz = sr
            break  # Only need first sample for device info

    duration = None
    if len(points) > 1:
        duration = (points[-1].timestamp - points[0].timestamp).total_seconds()

    return {
        "gps_point_count": len(points),
        "duration_seconds": duration,
        "device_name": device_name,
        "sample_rate_hz": sample_rate_hz,
    }


# --- Timeseries conversion and GPX export ---


def dji_meta_to_timeseries(points: list[DjiMetaPoint], units, sample_rate: int = 1) -> Timeseries:
    """Convert parsed DJI meta points to a gopro_overlay Timeseries.

    Speed is calculated from the 2D velocity vector: speed = sqrt(vx^2 + vy^2).

    Args:
        points: Parsed DJI meta GPS points
        units: gopro_overlay units module
        sample_rate: Take every N-th point (1 = all, 25 = ~1/sec at 25fps)

    Returns:
        Timeseries compatible with gopro_overlay rendering pipeline
    """
    timeseries = Timeseries()

    sampled = points[::sample_rate] if sample_rate > 1 else points

    entries = []
    for index, point in enumerate(sampled):
        # Calculate speed from 2D velocity vector (m/s)
        vx, vy = point.velocity_2d
        speed_ms = math.sqrt(vx * vx + vy * vy)

        entries.append(
            Entry(
                point.timestamp,
                point=Point(point.lat, point.lon),
                alt=units.Quantity(point.alt_m, units.m),
                speed=units.Quantity(speed_ms, units.mps),
                gpsfix=GPSFix.LOCK_3D.value,
                gpslock=units.Quantity(GPSFix.LOCK_3D.value),
                packet=units.Quantity(index),
                packet_index=units.Quantity(0),
            )
        )

    timeseries.add(*entries)
    return timeseries


def load_dji_meta_timeseries(file_path: Path, units, sample_rate: int = 1) -> Timeseries:
    """Load DJI meta GPS from a video file and return a Timeseries.

    Convenience function: detect stream -> extract -> parse -> thin -> convert.

    Args:
        file_path: Path to the video file with DJI meta stream
        units: gopro_overlay units module
        sample_rate: Take every N-th point (1 = all, 25 = ~1/sec at 25fps)

    Returns:
        Timeseries compatible with gopro_overlay rendering pipeline

    Raises:
        ValueError: If no DJI meta stream or GPS data found
    """
    points = parse_dji_meta_file(file_path)
    if not points:
        raise ValueError(f"No valid GPS data found in DJI meta stream: {file_path}")
    return dji_meta_to_timeseries(points, units, sample_rate)


def dji_meta_to_gpx_file(
    file_path: Path,
    output_path: Path,
    sample_rate: int = 1,
    points: list[DjiMetaPoint] | None = None,
) -> Path:
    """Convert DJI meta GPS data to GPX format.

    Used for CLI rendering via gopro-dashboard.py which only accepts GPX/FIT.

    Args:
        file_path: Path to the video file with DJI meta stream
        output_path: Path for the output .gpx file
        sample_rate: Take every N-th point (1 = all, 25 = ~1/sec at 25fps)
        points: Pre-parsed DJI meta points (avoids re-parsing if already available)

    Returns:
        Path to the generated GPX file

    Raises:
        ValueError: If no DJI meta stream or GPS data found
    """
    if points is None:
        points = parse_dji_meta_file(file_path)
    if not points:
        raise ValueError(f"No valid GPS data found in DJI meta stream: {file_path}")

    sampled = points[::sample_rate] if sample_rate > 1 else points

    # Build GPX XML
    gpx = Element(
        "gpx",
        {
            "version": "1.1",
            "creator": "gpstitch",
            "xmlns": "http://www.topografix.com/GPX/1/1",
        },
    )

    trk = SubElement(gpx, "trk")
    SubElement(trk, "name").text = file_path.stem
    trkseg = SubElement(trk, "trkseg")

    for point in sampled:
        trkpt = SubElement(
            trkseg,
            "trkpt",
            {
                "lat": f"{point.lat:.6f}",
                "lon": f"{point.lon:.6f}",
            },
        )
        SubElement(trkpt, "ele").text = f"{point.alt_m:.1f}"
        SubElement(trkpt, "time").text = point.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")

    tree = ElementTree(gpx)
    tree.write(str(output_path), encoding="utf-8", xml_declaration=True)

    return output_path
