"""Output folder selection and batch multi-select.

The native dialog cannot be driven by Playwright, so /api/browse is
intercepted; these cover the wiring, persistence and append behaviour.
"""

import json

import pytest
from playwright.sync_api import Page, expect


def _stub_browse(page: Page, paths):
    """Answer /api/browse as if the user picked `paths` (empty = cancelled)."""
    page.route(
        "**/api/browse",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"path": paths[0] if paths else None, "paths": paths}),
        ),
    )


@pytest.mark.e2e
class TestOutputFolderControl:
    def test_defaults_to_alongside_the_source(self, app_page: Page):
        expect(app_page.locator("#output-folder-value")).to_contain_text("Alongside source")

    def test_choosing_a_folder_shows_it(self, app_page: Page):
        _stub_browse(app_page, ["/Users/me/Exports"])
        app_page.locator("#output-folder-btn").click()
        expect(app_page.locator("#output-folder-value")).to_contain_text("/Users/me/Exports")

    def test_cancelling_leaves_the_current_choice(self, app_page: Page):
        _stub_browse(app_page, ["/Users/me/Exports"])
        app_page.locator("#output-folder-btn").click()
        expect(app_page.locator("#output-folder-value")).to_contain_text("/Users/me/Exports")

        _stub_browse(app_page, [])
        app_page.locator("#output-folder-btn").click()
        app_page.wait_for_timeout(300)
        expect(app_page.locator("#output-folder-value")).to_contain_text("/Users/me/Exports")

    def test_choice_survives_a_reload(self, app_page: Page):
        """Pick your exports folder once, not every render."""
        _stub_browse(app_page, ["/Users/me/Exports"])
        app_page.locator("#output-folder-btn").click()
        expect(app_page.locator("#output-folder-value")).to_contain_text("/Users/me/Exports")

        app_page.reload()
        app_page.wait_for_load_state("networkidle")
        expect(app_page.locator("#output-folder-value")).to_contain_text("/Users/me/Exports")

    def test_clearing_returns_to_the_default(self, app_page: Page):
        _stub_browse(app_page, ["/Users/me/Exports"])
        app_page.locator("#output-folder-btn").click()
        expect(app_page.locator("#output-folder-value")).to_contain_text("/Users/me/Exports")

        app_page.locator("#output-folder-clear").click()
        expect(app_page.locator("#output-folder-value")).to_contain_text("Alongside source")


@pytest.mark.e2e
class TestBatchMultiSelect:
    def _open_batch(self, page: Page):
        page.locator("#btn-batch-render").click()
        expect(page.locator("#batch-files-input")).to_be_visible()

    def test_add_videos_button_is_present(self, app_page: Page):
        self._open_batch(app_page)
        expect(app_page.locator("#batch-add-videos-btn")).to_be_visible()

    def test_picked_videos_are_appended_one_per_line(self, app_page: Page):
        self._open_batch(app_page)
        _stub_browse(app_page, ["/Users/me/a.mp4", "/Users/me/b.mp4"])

        app_page.locator("#batch-add-videos-btn").click()
        expect(app_page.locator("#batch-files-input")).to_have_value("/Users/me/a.mp4\n/Users/me/b.mp4")

    def test_appends_rather_than_replaces(self, app_page: Page):
        """So videos can be gathered from several folders."""
        self._open_batch(app_page)
        app_page.locator("#batch-files-input").fill("/Users/me/existing.mp4")
        _stub_browse(app_page, ["/Users/me/new.mp4"])

        app_page.locator("#batch-add-videos-btn").click()
        expect(app_page.locator("#batch-files-input")).to_have_value("/Users/me/existing.mp4\n/Users/me/new.mp4")

    def test_paths_already_listed_are_not_added_twice(self, app_page: Page):
        self._open_batch(app_page)
        app_page.locator("#batch-files-input").fill("/Users/me/a.mp4")
        _stub_browse(app_page, ["/Users/me/a.mp4", "/Users/me/b.mp4"])

        app_page.locator("#batch-add-videos-btn").click()
        expect(app_page.locator("#batch-files-input")).to_have_value("/Users/me/a.mp4\n/Users/me/b.mp4")

    def test_file_count_updates_after_adding(self, app_page: Page):
        self._open_batch(app_page)
        _stub_browse(app_page, ["/Users/me/a.mp4", "/Users/me/b.mp4"])

        app_page.locator("#batch-add-videos-btn").click()
        expect(app_page.locator("#batch-file-count")).to_have_text("2")

    def test_batch_shows_the_same_output_folder(self, app_page: Page):
        """One setting, not two that can disagree."""
        _stub_browse(app_page, ["/Users/me/Exports"])
        app_page.locator("#output-folder-btn").click()
        expect(app_page.locator("#output-folder-value")).to_contain_text("/Users/me/Exports")

        self._open_batch(app_page)
        expect(app_page.locator("#batch-output-folder-value")).to_contain_text("/Users/me/Exports")


@pytest.mark.e2e
class TestOutputFolderIsSent:
    """Choosing a folder is pointless unless the requests carry it."""

    def test_batch_request_carries_the_output_folder(self, app_page: Page):
        sent = {}

        def capture(route):
            sent["output_dir"] = route.request.post_data_json.get("output_dir")
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"batch_id": "b1", "job_ids": ["j1"], "total_jobs": 1}),
            )

        _stub_browse(app_page, ["/Users/me/Exports"])
        app_page.locator("#output-folder-btn").click()
        expect(app_page.locator("#output-folder-value")).to_contain_text("/Users/me/Exports")

        app_page.locator("#btn-batch-render").click()
        app_page.locator("#batch-files-input").fill("/Users/me/a.mp4")
        app_page.locator("#batch-pre-checks").uncheck()
        app_page.route("**/api/render/batch", capture)
        app_page.locator("#batch-start-btn").click()
        app_page.wait_for_timeout(800)

        assert sent.get("output_dir") == "/Users/me/Exports"

    def test_pre_check_carries_the_output_folder(self, app_page: Page):
        """Otherwise the overwrite warning inspects the wrong folder."""
        sent = {}

        def capture(route):
            sent["output_dir"] = route.request.post_data_json.get("output_dir")
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "total_files": 1,
                        "overwrite_conflicts": [],
                        "duplicate_outputs": [],
                        "gps_files": [],
                        "gps_issues_count": 0,
                    }
                ),
            )

        _stub_browse(app_page, ["/Users/me/Exports"])
        app_page.locator("#output-folder-btn").click()
        expect(app_page.locator("#output-folder-value")).to_contain_text("/Users/me/Exports")

        app_page.locator("#btn-batch-render").click()
        app_page.locator("#batch-files-input").fill("/Users/me/a.mp4")
        app_page.route("**/api/render/pre-check", capture)
        app_page.route("**/api/render/batch", lambda r: r.abort())
        app_page.locator("#batch-start-btn").click()
        app_page.wait_for_timeout(800)

        assert sent.get("output_dir") == "/Users/me/Exports"


@pytest.mark.e2e
class TestDuplicateOutputWarning:
    """Two clips claiming one output name must not silently overwrite."""

    def _pre_check_returning_duplicates(self, page: Page):
        page.route(
            "**/api/render/pre-check",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "total_files": 2,
                        "overwrite_conflicts": [],
                        "duplicate_outputs": [
                            {
                                "output_path": "/Users/me/Exports/DJI_0001_overlay.mp4",
                                "video_paths": [
                                    "/Users/me/shoot-a/DJI_0001.mp4",
                                    "/Users/me/shoot-b/DJI_0001.mp4",
                                ],
                            }
                        ],
                        "gps_files": [],
                        "gps_issues_count": 0,
                    }
                ),
            ),
        )

    def test_duplicate_outputs_stop_the_batch_for_confirmation(self, app_page: Page):
        app_page.once("dialog", lambda d: (seen.update(text=d.message), d.dismiss()))
        seen = {}

        app_page.locator("#btn-batch-render").click()
        app_page.locator("#batch-files-input").fill("/Users/me/shoot-a/DJI_0001.mp4\n/Users/me/shoot-b/DJI_0001.mp4")
        self._pre_check_returning_duplicates(app_page)
        started = {"called": False}
        app_page.route(
            "**/api/render/batch",
            lambda route: (started.update(called=True), route.abort()),
        )

        app_page.locator("#batch-start-btn").click()
        app_page.wait_for_timeout(1000)

        assert "DJI_0001_overlay.mp4" in seen.get("text", ""), seen
        assert started["called"] is False, "batch started despite a collision"


@pytest.mark.e2e
class TestOutputFolderAppearance:
    """Text assertions cannot see these: textContent is complete either way."""

    def test_default_label_is_not_truncated_from_the_left(self, app_page: Page):
        """RTL truncation suits long paths, but mangles 'Alongside source video'."""
        direction = app_page.locator("#output-folder-value").evaluate("el => getComputedStyle(el).direction")
        assert direction == "ltr"

    def test_a_chosen_path_truncates_from_the_left(self, app_page: Page):
        """A path's tail is the useful part, so long paths clip at the start."""
        _stub_browse(app_page, ["/Users/me/Movies/2026/Exports"])
        app_page.locator("#output-folder-btn").click()
        expect(app_page.locator("#output-folder-value")).to_contain_text("Exports")

        direction = app_page.locator("#output-folder-value").evaluate("el => getComputedStyle(el).direction")
        assert direction == "rtl"

    def test_reset_is_hidden_until_a_folder_is_chosen(self, app_page: Page):
        expect(app_page.locator("#output-folder-clear")).to_be_hidden()

    def test_reset_appears_once_a_folder_is_chosen(self, app_page: Page):
        _stub_browse(app_page, ["/Users/me/Exports"])
        app_page.locator("#output-folder-btn").click()
        expect(app_page.locator("#output-folder-clear")).to_be_visible()
