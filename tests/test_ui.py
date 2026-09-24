from pathlib import Path


INDEX_HTML = Path(__file__).parents[1] / "app" / "static" / "index.html"


def test_failed_step_uses_error_state_instead_of_completion_state() -> None:
    page = INDEX_HTML.read_text()

    assert '.step.failed::before' in page
    assert 'content:"×"' in page
    assert 'progressPanel.classList.add("failed")' in page
    assert 'currentStep.classList.add("failed")' in page
    assert 'if(scanEvent.event==="error"){failStep(scanEvent.message)' in page


def test_failed_scan_progress_uses_error_color_and_event_progress() -> None:
    page = INDEX_HTML.read_text()

    assert '.progress-panel.failed .progress-fill { background:var(--danger); }' in page
    assert 'updateProgress(scanEvent.progress)' in page
    assert 'progressPanel.classList.remove("failed")' in page


def test_blocked_site_category_has_distinct_title() -> None:
    page = INDEX_HTML.read_text()

    assert 'category==="blocked_by_site"' in page
    assert 'title="Blocked by the site"' in page
