"""Buttons that trigger slow work must show they are working.

Get Command is the sharpest case: for a DJI clip it reads the whole GPS stream
and writes a GPX, minutes of work, and the button previously did nothing at all
- no spinner, no disable, so a second click queued a second read.

Requests are left unanswered rather than delayed, because sleeping inside a sync
Playwright route handler blocks Playwright's own loop and the assertion then
only runs after the request has finished.
"""

import json
import re

import pytest
from playwright.sync_api import Page, expect


def _give_the_page_a_session(page: Page):
    """The command and export buttons refuse to act without a loaded video."""
    page.evaluate(
        """() => {
            window.app.state.setSession('test-session', {
                session_id: 'test-session',
                files: [{ file_type: 'video', role: 'primary', filename: 'clip.mp4',
                          file_path: '/tmp/clip.mp4' }],
            });
        }"""
    )


@pytest.mark.e2e
class TestGetCommandShowsProgress:
    def test_the_button_shows_a_spinner_while_working(self, app_page: Page):
        _give_the_page_a_session(app_page)
        app_page.route("**/api/command", lambda route: None)

        app_page.locator("#btn-generate-cmd").click()

        expect(app_page.locator("#btn-generate-cmd")).to_have_class(re.compile(r"\bloading\b"))

    def test_the_button_is_disabled_while_working(self, app_page: Page):
        """A DJI clip takes minutes; a second click would queue a second read."""
        _give_the_page_a_session(app_page)
        app_page.route("**/api/command", lambda route: None)

        app_page.locator("#btn-generate-cmd").click()

        expect(app_page.locator("#btn-generate-cmd")).to_be_disabled()

    def test_the_button_recovers_after_the_work_finishes(self, app_page: Page):
        _give_the_page_a_session(app_page)
        app_page.route(
            "**/api/command",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"command": "gpstitch-dashboard out.mp4", "input_file": "/tmp/clip.mp4"}),
            ),
        )

        app_page.locator("#btn-generate-cmd").click()

        expect(app_page.locator("#btn-generate-cmd")).to_be_enabled()
        expect(app_page.locator("#command-modal")).to_have_class(re.compile(r"\bvisible\b"))

    def test_the_button_recovers_after_a_failure(self, app_page: Page):
        """A stuck disabled button is worse than no indicator at all."""
        _give_the_page_a_session(app_page)
        app_page.route(
            "**/api/command",
            lambda route: route.fulfill(
                status=500,
                content_type="application/json",
                body=json.dumps({"detail": "boom"}),
            ),
        )

        app_page.locator("#btn-generate-cmd").click()

        expect(app_page.locator("#btn-generate-cmd")).to_be_enabled()
