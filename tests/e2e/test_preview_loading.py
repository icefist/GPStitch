"""The preview must show that it is working.

Reading GPS out of a large clip takes seconds, and the panel that says so
already existed in the markup - it was simply never displayed, so a slow
preview looked identical to a dead one.
"""

import json

import pytest
from playwright.sync_api import Page, expect


def _hanging_preview(page: Page):
    """Leave the preview request unanswered so the in-flight state is visible."""
    page.route("**/api/preview", lambda route: None)
    page.route("**/api/editor/preview", lambda route: None)


def _answered_preview(page: Page):
    page.route(
        "**/api/preview",
        lambda route: route.fulfill(
            status=500,
            content_type="application/json",
            body=json.dumps({"detail": "stubbed"}),
        ),
    )


@pytest.mark.e2e
class TestPreviewLoadingIndicator:
    def test_loading_panel_is_hidden_before_any_preview(self, app_page: Page):
        expect(app_page.locator("#preview-loading")).to_be_hidden()

    def test_loading_panel_appears_while_generating(self, app_page: Page):
        _hanging_preview(app_page)
        app_page.evaluate("window.app._showPreviewLoading()")
        expect(app_page.locator("#preview-loading")).to_be_visible()

    def test_loading_panel_says_what_is_happening(self, app_page: Page):
        _hanging_preview(app_page)
        app_page.evaluate("window.app._showPreviewLoading()")
        expect(app_page.locator("#preview-loading")).to_contain_text("preview")

    def test_loading_panel_is_hidden_again_afterwards(self, app_page: Page):
        app_page.evaluate("window.app._showPreviewLoading()")
        expect(app_page.locator("#preview-loading")).to_be_visible()
        app_page.evaluate("window.app._hidePreviewLoading()")
        expect(app_page.locator("#preview-loading")).to_be_hidden()

    def test_refresh_button_still_shows_its_own_spinner(self, app_page: Page):
        """The existing button affordance must not be lost."""
        app_page.evaluate("window.app._showPreviewLoading()")
        assert app_page.locator("#btn-refresh-preview").evaluate("el => el.classList.contains('loading')")
