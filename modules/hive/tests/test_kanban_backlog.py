from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def test_backlog_hierarchy_crud_status_and_safe_titles():
    text=(ROOT/'src/templates/kanban.html').read_text()
    for token in ("state.epics", "state.features", "state.stories", "s.epic_id===epic.id", "feature_id:record.id", "epic_id:record.id"):
        assert token in text
    assert "Add feature" in text and "Add story" in text and "Rename" in text and "Delete" in text
    assert "[['backlog','Backlog'], ...statuses]" in text and "status:'backlog'" not in text # server default
    assert "select.addEventListener('change'" in text and "textContent=text" in text
    assert "JSON.stringify(body)" in text and "innerHTML" not in text
