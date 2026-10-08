import json

import pytest

from backend.app.platform_browser import _read_douyin_detail_response


class Page:
    def __init__(self, body, *, status=200, mime="application/json"):
        self.body, self.status, self.mime = body, status, mime

    def take_network_responses(self):
        return [{"url": "https://www.douyin.com/aweme/v1/web/aweme/detail/?token=SECRET",
                 "request_id": "opaque", "status": self.status,
                 "headers": {"content-type": self.mime, "Set-Cookie": "SECRET"}}]

    def response_body(self, request_id):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


@pytest.mark.parametrize("body,status,mime,reason", [
    (b"", 200, "application/json", "empty_body"),
    (b"bad SECRET", 200, "application/json", "invalid_json"),
    (b'[]', 200, "application/json", "non_object_json"),
    (b'{"status_code": 1024}', 200, "application/json", "missing_detail_object"),
    (b'{"aweme_detail":{"aweme_id":"other","desc":"SECRET"}}', 200, "application/json", "identity_mismatch"),
    (b'{"aweme_detail":{"aweme_id":"123","desc":""}}', 200, "application/json", "empty_caption"),
    (RuntimeError("SECRET"), 200, "application/json", "body_unavailable"),
    (b"SECRET", 429, "text/html", "http_error"),
    (b"SECRET", 200, "text/html", "non_json_response"),
])
def test_failed_detail_has_safe_reason_without_response_or_credentials(body, status, mime, reason):
    result, diagnostics = _read_douyin_detail_response(Page(body, status=status, mime=mime), "123")
    assert result is None
    assert diagnostics["network_detail_outcomes"][0]["reason"] == reason
    assert "SECRET" not in json.dumps(diagnostics)
    assert "token" not in json.dumps(diagnostics)


def test_verified_caption_records_safe_outcome():
    raw = b'{"status_code":0,"aweme_detail":{"aweme_id":"123","desc":"  full\\n"}}'
    result, diagnostics = _read_douyin_detail_response(Page(raw), "123")
    assert result["published_body"] == "  full\n"
    assert diagnostics["network_detail_outcomes"][0] == {
        "http_status": 200, "body_bytes": len(raw), "platform_status_code": 0,
        "reason": "verified_caption",
    }


def filtered_response(*, video_id="123", reason="status_audit_self_see", **changes):
    return json.dumps({
        "status_code": 0, "aweme_detail": None,
        "filter_detail": {
            "aweme_id": video_id, "filter_reason": reason,
            "detail_msg": "SECRET", "notice": {"text": "SECRET"}, "icon": "https://example.test/?token=SECRET",
        }, **changes,
    }).encode()


def test_exact_observed_visibility_restriction_has_only_safe_diagnostics():
    result, diagnostics = _read_douyin_detail_response(Page(filtered_response()), "123")
    assert result is None
    outcome = diagnostics["network_detail_outcomes"][0]
    assert outcome["reason"] == "platform_visibility_restricted"
    assert outcome["filter_detail_video_id"] == "123"
    assert outcome["filter_reason"] == "status_audit_self_see"
    assert "SECRET" not in json.dumps(diagnostics)
    assert "token" not in json.dumps(diagnostics)
    assert "detail_msg" not in json.dumps(diagnostics)


@pytest.mark.parametrize("changes", [
    {"video_id": "1234"}, {"video_id": 123}, {"video_id": None},
    {"status_code": 1}, {"status_code": False}, {"status_code": None},
    {"reason": "status_unknown_future"}, {"reason": "STATUS_AUDIT_SELF_SEE"},
    {"reason": "status_audit_self_see "}, {"reason": "https://example.test/?token=SECRET"},
    {"aweme_detail": {}},
])
def test_unmatched_or_unrecognized_filters_never_imply_visibility_restriction(changes):
    result, diagnostics = _read_douyin_detail_response(Page(filtered_response(**changes)), "123")
    assert result is None
    outcome = diagnostics["network_detail_outcomes"][0]
    assert outcome["reason"] != "platform_visibility_restricted"
    assert "SECRET" not in json.dumps(diagnostics)
    if changes.get("reason") == "status_unknown_future":
        assert outcome["filter_reason"] == "status_unknown_future"
        assert outcome["reason"] == "missing_detail_object"


def test_actual_caption_is_not_downgraded_by_unrelated_filter_field():
    body = filtered_response(aweme_detail={"aweme_id": "123", "desc": "完整原文"})
    result, diagnostics = _read_douyin_detail_response(Page(body), "123")
    assert result["published_body"] == "完整原文"
    assert diagnostics["network_detail_outcomes"][0]["reason"] == "verified_caption"
