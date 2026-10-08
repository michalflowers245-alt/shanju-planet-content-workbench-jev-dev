from __future__ import annotations

import hashlib
import io
import json
import subprocess
from pathlib import Path

import pytest

from backend.app import speech_transcription as speech


def test_confidence_quality_flags_actual_five_low_score_fragments_without_claiming_accuracy():
    segments = [{"text": text, "confidence": score} for text, score in zip("现在9一我", [0.32, 0.444, 0.452, 0.514, 0.286])]
    quality = speech.transcription_quality("现在9一我", segments)

    assert quality["status"] == "NEEDS_REVIEW"
    assert quality["mean_confidence"] == 0.4032
    assert quality["assessed_segment_count"] == quality["low_confidence_segment_count"] == 5
    assert quality["threshold"] == 0.65
    assert "accuracy" not in quality


@pytest.mark.parametrize("text,segments,confidence,truncated,status", [
    ("", [], {}, False, "NOT_AVAILABLE"),
    ("短句", [], {}, False, "UNKNOWN"),
    ("短句", [{"confidence": float("nan")}], {"mean": 5, "assessed_segment_count": 1}, False, "UNKNOWN"),
    ("短句", [], {"mean": 0.4, "assessed_segment_count": 3}, False, "NEEDS_REVIEW"),
    ("短句", [{"confidence": 0.65}], {}, False, "REVIEW_NOT_FLAGGED"),
    ("短句", [{"confidence": 0.99}, {"confidence": 0.6}], {}, False, "NEEDS_REVIEW"),
    ("截断正文", [{"confidence": 0.99}], {}, True, "INCOMPLETE"),
])
def test_transcription_review_flags_are_independent_of_text_length(text, segments, confidence, truncated, status):
    quality = speech.transcription_quality(text, segments, confidence, text_truncated=truncated)
    assert quality["status"] == status


def test_local_asr_status_reports_installed_chinese_model(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(speech, "_run_helper", lambda *_args, **_kwargs: {
        "supported": True,
        "installed": True,
        "language": "zh-CN",
        "engine": "Apple SpeechAnalyzer (offline, on-device)",
    })

    status = speech.local_asr_status()

    assert status["available"] is True
    assert status["installed"] is True
    assert status["language"] == "zh-CN"


def test_uploaded_media_is_transcoded_temporarily_and_only_transcript_hash_is_returned(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(speech.shutil, "which", lambda name: name)
    helper_paths: list[str] = []

    def fake_run(args, **kwargs):
        if args[0] == "ffprobe":
            return subprocess.CompletedProcess(args, 0, json.dumps({"streams": [{"codec_name": "aac"}], "format": {"duration": "3.25"}}), "")
        if args[0] == "ffmpeg":
            Path(args[-1]).write_bytes(b"RIFF" + b"0" * 64)
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected process: {args[0]}")

    def fake_helper(arguments, *, timeout_seconds):
        assert arguments[0] == "--transcribe"
        helper_paths.append(arguments[1])
        assert Path(arguments[1]).is_file()
        assert timeout_seconds >= 300
        return {
            "ok": True,
            "text": "本机识别出的中文口播。",
            "segments": [
                {"text": "本机识别出的", "start_seconds": 0.12, "end_seconds": 1.4, "confidence": 0.92},
                {"text": "中文口播。", "start_seconds": 1.4, "end_seconds": 2.8, "confidence": 0.51},
            ],
            "confidence": {"mean": 0.715, "minimum": 0.51, "maximum": 0.92, "assessed_segment_count": 2, "low_confidence_threshold": 0.65},
            "low_confidence": [
                {"text": "中文口播。", "start_seconds": 1.4, "end_seconds": 2.8, "confidence": 0.51},
            ],
            "language": "zh-CN",
            "engine": "Apple SpeechAnalyzer (offline, on-device)",
            "model": "SpeechTranscriber zh-CN",
        }

    monkeypatch.setattr(speech.subprocess, "run", fake_run)
    monkeypatch.setattr(speech, "_run_helper", fake_helper)
    content = b"user-selected-media-bytes"

    result = speech.transcribe_uploaded_media(io.BytesIO(content), "selected-video.mp4")

    assert result["text"] == "本机识别出的中文口播。"
    assert result["source"] == "USER_PROVIDED_LOCAL_MEDIA"
    assert result["media_sha256"] == hashlib.sha256(content).hexdigest()
    assert result["duration_seconds"] == 3.25
    assert result["segments"][1]["start_seconds"] == 1.4
    assert result["confidence"]["assessed_segment_count"] == 2
    assert len(result["low_confidence"]) == 1
    assert "低置信度" in result["message"]
    assert not Path(helper_paths[0]).exists()


def test_uploaded_media_rejects_non_media_extensions():
    with pytest.raises(speech.SpeechTranscriptionError) as error:
        speech.transcribe_uploaded_media(io.BytesIO(b"x"), "notes.txt")

    assert error.value.code == "unsupported_media_type"


def test_uploaded_media_rejects_oversize_before_decoding(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(speech, "MAX_MEDIA_BYTES", 3)
    with pytest.raises(speech.SpeechTranscriptionError) as error:
        speech.transcribe_uploaded_media(io.BytesIO(b"1234"), "clip.mp4")

    assert error.value.code == "media_too_large"


def test_transcribe_temporary_logged_in_media_path_keeps_only_transcript_and_hash(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(speech.shutil, "which", lambda name: name)
    helper_paths: list[str] = []

    def fake_run(args, **kwargs):
        if args[0] == "ffprobe":
            return subprocess.CompletedProcess(args, 0, json.dumps({"streams": [{"codec_name": "h264"}], "format": {"duration": "12.5"}}), "")
        if args[0] == "ffmpeg":
            Path(args[-1]).write_bytes(b"RIFF" + b"0" * 64)
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected process: {args[0]}")

    def fake_helper(arguments, *, timeout_seconds):
        helper_paths.append(arguments[1])
        assert Path(arguments[1]).is_file()
        return {
            "ok": True, "text": "这是登录态视频里的完整口播。", "language": "zh-CN",
            "segments": [{"text": "这是登录态视频里的完整口播。", "start_seconds": 0, "end_seconds": 4.5, "confidence": 0.91}],
            "confidence": {"mean": 0.91, "minimum": 0.91, "maximum": 0.91, "assessed_segment_count": 1, "low_confidence_threshold": 0.65},
            "low_confidence": [],
        }

    monkeypatch.setattr(speech.subprocess, "run", fake_run)
    monkeypatch.setattr(speech, "_run_helper", fake_helper)
    media = tmp_path / "video.mp4"
    media.write_bytes(b"temporary-browser-media")

    result = speech.transcribe_media_path(
        media,
        source="BROWSER_LOGGED_IN_MEDIA",
        message="登录态作品媒体只用于本机临时转写。",
    )

    assert result["text"] == "这是登录态视频里的完整口播。"
    assert result["source"] == "BROWSER_LOGGED_IN_MEDIA"
    assert result["media_sha256"] == hashlib.sha256(media.read_bytes()).hexdigest()
    assert result["duration_seconds"] == 12.5
    assert result["segments"][0]["end_seconds"] == 4.5
    assert result["confidence"]["mean"] == 0.91
    assert result["low_confidence"] == []
    assert media.exists()
    assert not Path(helper_paths[0]).exists()
