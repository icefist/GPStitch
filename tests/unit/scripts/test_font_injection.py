"""Tests for automatic --font injection in the gpstitch-dashboard wrapper.

gopro-dashboard.py defaults --font to "Roboto-Medium.ttf", which Linux
distributions package but macOS does not ship. renderer.generate_cli_command()
already works around this for the UI; these tests cover the same fallback for
the bare gpstitch-dashboard CLI, so the README's own example command works on a
stock macOS install.
"""

from gpstitch.scripts.gopro_dashboard_wrapper import _maybe_inject_font

HELVETICA = "/System/Library/Fonts/Helvetica.ttc"
GOPRO_DEFAULT_FONT = "Roboto-Medium.ttf"


def _finder(result):
    """Build a stub font finder that always reports the given result."""
    return lambda: result


class TestMaybeInjectFont:
    """Tests for _maybe_inject_font()."""

    def test_injects_font_when_caller_specified_none(self):
        argv = ["gpstitch-dashboard", "in.mp4", "out.mp4"]
        assert _maybe_inject_font(argv, finder=_finder(HELVETICA)) == [
            "gpstitch-dashboard",
            "in.mp4",
            "out.mp4",
            "--font",
            HELVETICA,
        ]

    def test_leaves_explicit_font_flag_untouched(self):
        argv = ["gpstitch-dashboard", "in.mp4", "out.mp4", "--font", "Custom.ttf"]
        assert _maybe_inject_font(argv, finder=_finder(HELVETICA)) == argv

    def test_leaves_explicit_font_equals_form_untouched(self):
        """argparse accepts --font=X as well as --font X."""
        argv = ["gpstitch-dashboard", "in.mp4", "out.mp4", "--font=Custom.ttf"]
        assert _maybe_inject_font(argv, finder=_finder(HELVETICA)) == argv

    def test_no_injection_when_no_font_found(self):
        """With nothing usable found, let gopro-dashboard report its own error."""
        argv = ["gpstitch-dashboard", "in.mp4", "out.mp4"]
        assert _maybe_inject_font(argv, finder=_finder(None)) == argv

    def test_no_injection_when_finder_returns_gopro_default(self):
        """Roboto-Medium.ttf is already argparse's default - passing it is noise."""
        argv = ["gpstitch-dashboard", "in.mp4", "out.mp4"]
        assert _maybe_inject_font(argv, finder=_finder(GOPRO_DEFAULT_FONT)) == argv

    def test_does_not_mutate_caller_list(self):
        argv = ["gpstitch-dashboard", "in.mp4", "out.mp4"]
        _maybe_inject_font(argv, finder=_finder(HELVETICA))
        assert argv == ["gpstitch-dashboard", "in.mp4", "out.mp4"]


class TestSharedFontModule:
    """The font list must have exactly one home, shared by CLI and UI."""

    def test_exposes_shared_helpers(self):
        from gpstitch import fonts

        assert fonts.FONTS_TO_TRY[0] == GOPRO_DEFAULT_FONT
        assert callable(fonts.find_available_font)
        assert callable(fonts.load_font_with_fallback)

    def test_renderer_reuses_shared_finder(self):
        """renderer must not keep a private copy of the list that can drift."""
        from gpstitch import fonts
        from gpstitch.services import renderer

        assert renderer.find_available_font is fonts.find_available_font
