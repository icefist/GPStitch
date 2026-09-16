"""The merge toggle has to reach the server, in both requests.

Pre-check uses it to describe one output instead of several; the batch request
uses it to create one job instead of one per clip. A toggle that reaches only one
of them warns about files that will never be written.
"""

import json

import pytest
from playwright.sync_api import Page, expect

PRE_CHECK_BODY = json.dumps(
    {
        "total_files": 2,
        "overwrite_conflicts": [],
        "duplicate_outputs": [],
        "gps_files": [],
        "gps_issues_count": 0,
    }
)


def _open_batch_modal(page: Page):
    page.locator("#btn-batch-render").click()
    expect(page.locator("#batch-render-modal")).to_be_visible()


def _queue_two_files(page: Page):
    page.locator("#batch-files-input").fill("/tmp/a.mp4\n/tmp/b.mp4")
    page.locator("#batch-files-input").dispatch_event("input")


@pytest.mark.e2e
class TestMergeToggle:
    def test_the_toggle_is_offered(self, app_page: Page):
        _open_batch_modal(app_page)

        expect(app_page.locator("#batch-merge-toggle")).to_be_attached()

    def test_it_is_off_by_default(self, app_page: Page):
        """Merging changes what the batch produces, so it should be asked for."""
        _open_batch_modal(app_page)

        expect(app_page.locator("#batch-merge-toggle")).not_to_be_checked()

    def test_the_flag_reaches_the_pre_check_request(self, app_page: Page):
        sent = []

        def capture(route):
            sent.append(json.loads(route.request.post_data))
            route.fulfill(status=200, content_type="application/json", body=PRE_CHECK_BODY)

        app_page.route("**/api/render/pre-check", capture)
        # Left unanswered: the pre-check assertion is all this test needs.
        app_page.route("**/api/render/batch", lambda route: None)

        _open_batch_modal(app_page)
        _queue_two_files(app_page)
        app_page.locator("#batch-merge-toggle").check()
        app_page.locator("#batch-start-btn").click()

        expect(app_page.locator("#batch-merge-toggle")).to_be_checked()
        app_page.wait_for_timeout(500)
        assert sent, "pre-check was never called"
        assert sent[0]["merge"] is True, sent[0]

    def test_the_flag_reaches_the_batch_request(self, app_page: Page):
        sent = []

        app_page.route(
            "**/api/render/pre-check",
            lambda route: route.fulfill(status=200, content_type="application/json", body=PRE_CHECK_BODY),
        )

        def capture(route):
            sent.append(json.loads(route.request.post_data))
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"batch_id": "b1", "job_ids": ["j1"], "total_jobs": 1, "skipped_files": []}),
            )

        app_page.route("**/api/render/batch", capture)

        _open_batch_modal(app_page)
        _queue_two_files(app_page)
        app_page.locator("#batch-merge-toggle").check()
        app_page.locator("#batch-start-btn").click()

        app_page.wait_for_timeout(800)
        assert sent, "the batch request was never made"
        assert sent[0]["merge"] is True, sent[0]

    def test_leaving_it_off_sends_false(self, app_page: Page):
        sent = []

        def capture(route):
            sent.append(json.loads(route.request.post_data))
            route.fulfill(status=200, content_type="application/json", body=PRE_CHECK_BODY)

        app_page.route("**/api/render/pre-check", capture)
        app_page.route("**/api/render/batch", lambda route: None)

        _open_batch_modal(app_page)
        _queue_two_files(app_page)
        app_page.locator("#batch-start-btn").click()

        app_page.wait_for_timeout(500)
        assert sent, "pre-check was never called"
        assert sent[0]["merge"] is False, sent[0]
