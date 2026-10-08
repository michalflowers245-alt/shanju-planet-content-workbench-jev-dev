from backend.app.platform_browser import _wait_for_xhs_recovery_search


TARGET = "6a9859ea00000000290199cf"
OTHER = "6a9859ea00000000290199ca"


class Page:
    def __init__(self, snapshots):
        self.snapshots = snapshots
        self.calls = 0

    def evaluate(self, expression):
        assert "section.note-item" in expression
        value = self.snapshots[min(self.calls, len(self.snapshots) - 1)]
        self.calls += 1
        return value

    def wait_for_timeout(self, ms):
        assert ms == 250


def row(post_id):
    return {"href": f"https://www.xiaohongshu.com/explore/{post_id}", "title": "相同标题"}


def test_waits_for_exact_post_after_first_card_arrives():
    page = Page([{"rows": [row(OTHER)]}, {"rows": [row(OTHER), row(TARGET)]}])
    result = _wait_for_xhs_recovery_search(page, TARGET)
    assert page.calls == 2
    assert result["rows"][-1] == row(TARGET)


def test_stable_other_id_is_never_treated_as_requested_post():
    page = Page([{"rows": [row(OTHER)]}])
    result = _wait_for_xhs_recovery_search(page, TARGET)
    assert page.calls == 5
    assert result["rows"] == [row(OTHER)]


def test_verification_stops_immediately_even_if_cards_exist():
    page = Page([{"rows": [row(OTHER)], "accessHints": "安全验证"}])
    _wait_for_xhs_recovery_search(page, TARGET)
    assert page.calls == 1
