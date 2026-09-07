"""A clip whose GPS fix never moves gets its position widgets dropped.

Three clips off one card reported the same coordinates for the whole recording -
the Bluetooth remote lost its link and kept re-reporting its last known fix, so
clock, position, altitude and velocity were all constant. Rebuilding the time
axis makes such a clip render, but its map, place name and compass have nothing
to show, so they are removed from the layout rather than drawn dead.

If that leaves nothing at all - and the template in use held only position
widgets - the clip is refused instead: re-encoding 17GB to add an empty overlay
is nobody's intent.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gpstitch.services.renderer import generate_cli_command


@pytest.fixture
def dji_session(monkeypatch, temp_dir):
    """A session whose primary is a DJI clip with embedded GPS."""
    video_path = temp_dir / "DJI_stuck.mp4"
    video_path.write_bytes(b"fake")

    primary = MagicMock()
    primary.file_path = str(video_path)
    primary.file_type = "video"
    primary.video_metadata.has_dji_meta = True

    manager = MagicMock()
    manager.get_files.return_value = [primary]
    manager.get_primary_file.return_value = primary
    manager.get_secondary_file.return_value = None
    monkeypatch.setattr("gpstitch.services.file_manager.file_manager", manager)
    return video_path


@pytest.fixture
def stuck_track(monkeypatch, temp_dir):
    """Make the DJI→GPX conversion report a stuck fix without touching a file."""
    gpx = temp_dir / "track.gpx"
    gpx.write_text("<gpx/>", encoding="utf-8")

    def fake_convert(video_path, notify=None):
        from gpstitch.services.renderer import DjiTrackSummary

        return str(gpx), DjiTrackSummary(point_count=43917, duration_s=1465.3, position_frozen=True)

    monkeypatch.setattr("gpstitch.services.renderer._convert_dji_meta_to_gpx", fake_convert)


@pytest.fixture
def moving_track(monkeypatch, temp_dir):
    gpx = temp_dir / "track.gpx"
    gpx.write_text("<gpx/>", encoding="utf-8")

    def fake_convert(video_path, notify=None):
        from gpstitch.services.renderer import DjiTrackSummary

        return str(gpx), DjiTrackSummary(point_count=43917, duration_s=1465.3, position_frozen=False)

    monkeypatch.setattr("gpstitch.services.renderer._convert_dji_meta_to_gpx", fake_convert)


def _template(temp_dir, body: str):
    path = temp_dir / "template.xml"
    path.write_text(f"<layout>{body}</layout>", encoding="utf-8")
    return path


class TestStuckFixDropsPositionWidgets:
    def test_position_widgets_are_removed_from_the_layout(self, dji_session, stuck_track, temp_dir):
        template = _template(
            temp_dir,
            '<component type="journey_map" /><component type="place" /><component type="big_mph" />',
        )

        cmd, temp_files = generate_cli_command(
            session_id="s1",
            output_file="/tmp/out.mp4",
            layout="xml",
            layout_xml_path=str(template),
        )

        used = [part for part in cmd.split() if part.endswith(".xml")]
        assert used, cmd
        filtered = Path(used[-1]).read_text(encoding="utf-8")
        assert "journey_map" not in filtered
        assert "place" not in filtered
        assert "big_mph" in filtered
        assert used[-1] in temp_files, "the filtered layout is a temp file and must be cleaned up"

    def test_the_original_template_is_left_alone(self, dji_session, stuck_track, temp_dir):
        template = _template(temp_dir, '<component type="journey_map" /><component type="big_mph" />')
        before = template.read_text(encoding="utf-8")

        generate_cli_command(
            session_id="s1",
            output_file="/tmp/out.mp4",
            layout="xml",
            layout_xml_path=str(template),
        )

        assert template.read_text(encoding="utf-8") == before

    def test_a_moving_track_keeps_its_template(self, dji_session, moving_track, temp_dir):
        template = _template(temp_dir, '<component type="journey_map" /><component type="big_mph" />')

        cmd, _ = generate_cli_command(
            session_id="s1",
            output_file="/tmp/out.mp4",
            layout="xml",
            layout_xml_path=str(template),
        )

        assert str(template) in cmd, "an unaffected clip must render its own template"

    def test_a_layout_of_only_position_widgets_is_refused(self, dji_session, stuck_track, temp_dir):
        """This is the user's moto-map-gta.xml: journey_map, place, gps_lock_icon, compass_arrow."""
        template = _template(
            temp_dir,
            '<component type="journey_map" /><component type="place" />'
            '<component type="gps_lock_icon" /><component type="compass_arrow" />',
        )

        with pytest.raises(ValueError, match="nothing"):
            generate_cli_command(
                session_id="s1",
                output_file="/tmp/out.mp4",
                layout="xml",
                layout_xml_path=str(template),
            )

    def test_the_reason_names_the_stuck_position(self, dji_session, stuck_track, temp_dir):
        template = _template(temp_dir, '<component type="place" />')

        with pytest.raises(ValueError, match="GPS position"):
            generate_cli_command(
                session_id="s1",
                output_file="/tmp/out.mp4",
                layout="xml",
                layout_xml_path=str(template),
            )

    def test_dropped_widgets_are_announced(self, dji_session, stuck_track, temp_dir):
        template = _template(temp_dir, '<component type="place" /><component type="big_mph" />')
        messages = []

        generate_cli_command(
            session_id="s1",
            output_file="/tmp/out.mp4",
            layout="xml",
            layout_xml_path=str(template),
            on_progress=messages.append,
        )

        assert any("place" in m for m in messages), messages
        assert any("GPS position" in m for m in messages), messages
