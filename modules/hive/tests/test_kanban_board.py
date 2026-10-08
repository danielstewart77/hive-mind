from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def test_board_lanes_and_api_drag_drop():
    text=(ROOT/'src/templates/kanban.html').read_text()
    assert "['committed','Committed']" in text and "['in_progress','Work In Progress']" in text and "['done','Done']" in text
    assert "s.status===status" in text and "dataTransfer.getData('text/plain')" in text
    assert "addEventListener('drop',onDrop)" in text and "dataTransfer.setData('text/plain',s.id)" in text
    assert "PATCH" in text and "Could not move story" in text and "Drop a story here" in text
    assert "parentOf(s)" in text
