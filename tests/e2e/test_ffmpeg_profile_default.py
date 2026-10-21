"""The FFmpeg profile starts at the machine's fastest, until the user picks one.

The page used to start every render on software x264, which on Apple Silicon
spends ten cores on work the media engine does 2.4x faster. The server says
what to recommend; the page uses it unless the user has chosen for themselves.

The profiles endpoint is stubbed so these behave the same on any machine.
"""

import json

import pytest
from playwright.sync_api import Page, expect

PROFILES = {
    "recommended": "mac",
    "profiles": [
        {"name": "", "display_name": "Software (CPU)", "description": "H.264 on the CPU", "is_builtin": True},
        {
            "name": "mac",
            "display_name": "Mac",
            "description": "macOS VideoToolbox H.264",
            "is_builtin": True,
            "recommended": True,
        },
        {"name": "mac_hevc", "display_name": "Mac Hevc", "description": "macOS VideoToolbox HEVC", "is_builtin": True},
    ],
}

QUICK_CONFIG_KEY = "gopro_unified_quick_config"


def _serve_profiles(page: Page):
    page.route(
        "**/api/options/ffmpeg-profiles",
        lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(PROFILES)),
    )


def _reload_with(page: Page, saved_config: dict | None):
    """Reload with this saved Quick-mode config in place (None = never used before)."""
    if saved_config is None:
        page.evaluate(f"localStorage.removeItem('{QUICK_CONFIG_KEY}')")
    else:
        page.evaluate(f"localStorage.setItem('{QUICK_CONFIG_KEY}', {json.dumps(json.dumps(saved_config))})")
    _serve_profiles(page)
    page.reload()
    page.wait_for_load_state("networkidle")


@pytest.mark.e2e
class TestFfmpegProfileStartsAtTheRecommendedOne:
    def test_a_first_visit_starts_at_the_recommended_profile(self, app_page: Page):
        _reload_with(app_page, None)

        expect(app_page.locator("#ffmpeg-profile")).to_have_value("mac")

    def test_the_recommended_profile_says_so(self, app_page: Page):
        _reload_with(app_page, None)

        expect(app_page.locator("#ffmpeg-profile option[value='mac']")).to_contain_text("recommended")

    def test_a_config_saved_before_this_existed_is_moved_onto_it(self, app_page: Page):
        """Its '' was only ever the initial value, not something anyone chose."""
        _reload_with(app_page, {"ffmpegProfile": ""})

        expect(app_page.locator("#ffmpeg-profile")).to_have_value("mac")

    def test_choosing_software_on_purpose_survives_a_reload(self, app_page: Page):
        _reload_with(app_page, None)
        app_page.locator("#ffmpeg-profile").select_option("")

        _serve_profiles(app_page)
        app_page.reload()
        app_page.wait_for_load_state("networkidle")

        expect(app_page.locator("#ffmpeg-profile")).to_have_value("")

    def test_choosing_another_profile_survives_a_reload(self, app_page: Page):
        _reload_with(app_page, None)
        app_page.locator("#ffmpeg-profile").select_option("mac_hevc")

        _serve_profiles(app_page)
        app_page.reload()
        app_page.wait_for_load_state("networkidle")

        expect(app_page.locator("#ffmpeg-profile")).to_have_value("mac_hevc")
