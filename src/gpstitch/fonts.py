"""Font discovery shared by the web UI renderer and the gpstitch-dashboard CLI.

gopro-dashboard.py defaults --font to "Roboto-Medium.ttf", which Linux
distributions package but macOS and Windows do not ship. Both the UI render
path and the CLI wrapper walk this list so overlays render without the user
having to install a font first.

PIL and gopro_overlay are imported inside the functions rather than at module
scope: the CLI wrapper imports this module on every invocation and should not
pay for them up front.
"""

from pathlib import Path

# gopro-dashboard.py's own argparse default for --font. Passing it explicitly
# would be a no-op, so callers skip injection when detection lands back here.
GOPRO_DEFAULT_FONT = "Roboto-Medium.ttf"

# Shared font list for consistency between preview and CLI render
FONTS_TO_TRY = [
    # Standard Roboto font (may be installed)
    GOPRO_DEFAULT_FONT,
    # macOS system fonts
    "/Library/Fonts/SF-Pro.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Geneva.ttf",
    "/System/Library/Fonts/Monaco.ttf",
    "/Library/Fonts/Arial.ttf",
    # Linux common fonts
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    # Windows fonts
    "C:/Windows/Fonts/arial.ttf",
    # Generic names (PIL will search system paths)
    "Arial",
    "Helvetica",
]


def find_available_font() -> str | None:
    """Find an available font file. Used by both preview and CLI render."""
    for font in FONTS_TO_TRY:
        path = Path(font)
        if path.is_absolute() and path.exists():
            return str(path)
        # For non-absolute paths, try to find via font loader
        try:
            from gopro_overlay.font import load_font

            load_font(font)
            return font  # Font name is valid
        except (OSError, ImportError):
            continue

    return None


def load_font_with_fallback():
    """Load font with fallback to system fonts. Uses same list as CLI."""
    from gopro_overlay.font import load_font
    from PIL import ImageFont

    for font_name in FONTS_TO_TRY:
        try:
            return load_font(font_name)
        except OSError:
            continue

    # Last resort - use default PIL font
    return ImageFont.load_default()
