from types import SimpleNamespace

import pytest

from backend.app import main as main_module


@pytest.mark.parametrize("failing", ["xiaohongshu", "douyin"])
def test_platform_intake_failure_does_not_skip_the_other_platform(monkeypatch, failing):
    calls = []

    def callback(platform):
        def enqueue(workspace_id, payload, **kwargs):
            calls.append((platform, workspace_id, kwargs))
            if platform == failing:
                raise ValueError("intake failed")
            return 2
        return enqueue

    monkeypatch.setattr(main_module, "xhs_extraction_service", SimpleNamespace(enqueue_results=callback("xiaohongshu")))
    monkeypatch.setattr(main_module, "douyin_extraction_service", SimpleNamespace(enqueue_results=callback("douyin")))
    with pytest.raises(ValueError, match="intake failed"):
        main_module._enqueue_radar_detail_reads("ws-test", {
            "items": [{"platform": "xiaohongshu"}, {"platform": "douyin"}],
            "resume_after_user_action": True,
        })
    assert [call[0] for call in calls] == ["xiaohongshu", "douyin"]
    assert calls[0][2]["resume_after_user_action"] is True
    assert calls[1][2]["force_retry"] is True
