from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, BinaryIO

from .config import STATE_DIR


MAX_MEDIA_BYTES = 500 * 1024 * 1024
MAX_MEDIA_SECONDS = 15 * 60
ALLOWED_MEDIA_SUFFIXES = {".aac", ".aif", ".aiff", ".caf", ".m4a", ".m4v", ".mkv", ".mov", ".mp3", ".mp4", ".wav", ".webm", ".wma"}
_BUILD_LOCK = threading.Lock()
ASR_REVIEW_THRESHOLD = 0.65


def transcription_quality(
    text: str, segments: Any = None, confidence: Any = None, *, text_truncated: bool = False,
) -> dict[str, Any]:
    """Flag machine output for review; model confidence is not an accuracy rate."""
    scores: list[float] = []
    for segment in segments if isinstance(segments, list) else []:
        raw = segment.get("confidence") if isinstance(segment, dict) else None
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(raw) and 0 <= raw <= 1:
            scores.append(float(raw))
    summary = confidence if isinstance(confidence, dict) else {}
    raw_mean = summary.get("mean")
    raw_count = summary.get("assessed_segment_count")
    summary_valid = (
        isinstance(raw_mean, (int, float)) and not isinstance(raw_mean, bool)
        and math.isfinite(raw_mean) and 0 <= raw_mean <= 1
        and isinstance(raw_count, (int, float)) and not isinstance(raw_count, bool)
        and math.isfinite(raw_count) and raw_count > 0
    )
    mean = sum(scores) / len(scores) if scores else float(raw_mean) if summary_valid else None
    assessed = len(scores) if scores else int(raw_count) if summary_valid else 0
    low_count = sum(score < ASR_REVIEW_THRESHOLD for score in scores)
    reasons: list[str] = []
    if not str(text or "").strip():
        status = "NOT_AVAILABLE"
        reasons.append("未取得可评估的识别文本。")
    elif text_truncated:
        status = "INCOMPLETE"
        reasons.append("识别文本达到保存上限，已截断，不能作为完整口播。")
    elif mean is None:
        status = "UNKNOWN"
        reasons.append("缺少有效的识别置信度，尚未评估，请人工核对。")
    elif mean < ASR_REVIEW_THRESHOLD or low_count:
        status = "NEEDS_REVIEW"
        if mean < ASR_REVIEW_THRESHOLD:
            reasons.append("模型置信度均值低于 0.65，识别文本需要人工核对。")
        if low_count:
            reasons.append(f"{low_count}/{assessed} 个已评分分段低于 0.65。")
    else:
        status = "REVIEW_NOT_FLAGGED"
        reasons.append("未触发低置信度规则；这不代表准确率或完整口播已获核验。")
    return {
        "status": status, "reasons": reasons, "threshold": ASR_REVIEW_THRESHOLD,
        "mean_confidence": round(mean, 6) if mean is not None else None,
        "assessed_segment_count": assessed, "low_confidence_segment_count": low_count,
    }


class SpeechTranscriptionError(RuntimeError):
    def __init__(self, message: str, *, code: str = "transcription_failed") -> None:
        super().__init__(message)
        self.code = code


def _helper_source() -> Path:
    return Path(__file__).with_name("macos_speech_transcribe.swift")


def _helper_binary() -> Path:
    if sys.platform != "darwin":
        raise SpeechTranscriptionError("本机中文 ASR 目前需要 macOS 26 或更新版本", code="unsupported_platform")
    swiftc = shutil.which("swiftc")
    if not swiftc:
        raise SpeechTranscriptionError("没有找到 Swift 编译器；请安装 Xcode Command Line Tools", code="swift_unavailable")
    source = _helper_source()
    if not source.is_file():
        raise SpeechTranscriptionError("本机语音识别组件文件缺失", code="helper_missing")
    binary = STATE_DIR / "runtime" / "speech-transcribe"
    with _BUILD_LOCK:
        if binary.is_file() and binary.stat().st_mtime >= source.stat().st_mtime:
            return binary
        binary.parent.mkdir(parents=True, exist_ok=True)
        try:
            result = subprocess.run(
                [swiftc, "-parse-as-library", "-O", "-framework", "Speech", "-framework", "AVFoundation", str(source), "-o", str(binary)],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SpeechTranscriptionError("编译本机语音识别组件超时", code="helper_build_timeout") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-1200:]
            raise SpeechTranscriptionError(f"本机语音识别组件编译失败：{detail}", code="helper_build_failed")
        binary.chmod(0o700)
    return binary


def _run_helper(arguments: list[str], *, timeout_seconds: float) -> dict[str, Any]:
    binary = _helper_binary()
    try:
        result = subprocess.run(
            [str(binary), *arguments], capture_output=True, text=True,
            timeout=timeout_seconds, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SpeechTranscriptionError("本机语音识别超时；原文和文件均未被删除", code="transcription_timeout") from exc
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        detail = (result.stderr or result.stdout or "").strip()[-800:]
        raise SpeechTranscriptionError(f"本机语音识别没有返回有效结果：{detail}", code="invalid_helper_response") from exc
    if result.returncode != 0 or not payload.get("ok"):
        raise SpeechTranscriptionError(str(payload.get("error") or "本机语音识别失败"), code="asr_unavailable")
    return payload


def local_asr_status() -> dict[str, Any]:
    payload = _run_helper(["--check"], timeout_seconds=30)
    return {
        "available": bool(payload.get("supported")),
        "installed": bool(payload.get("installed")),
        "language": str(payload.get("language") or "zh-CN"),
        "engine": str(payload.get("engine") or ""),
        "message": "本机中文模型已就绪" if payload.get("installed") else "首次转写时将下载 Apple 本机中文语音资源",
    }


def transcribe_media_path(
    media_path: str | Path,
    *,
    source: str = "USER_PROVIDED_LOCAL_MEDIA",
    message: str = "仅转写本机临时媒体；原媒体不会上传或长期保存。识别结果建议人工核对。",
) -> dict[str, Any]:
    input_path = Path(media_path)
    suffix = input_path.suffix.lower()
    if suffix not in ALLOWED_MEDIA_SUFFIXES:
        raise SpeechTranscriptionError("请选择常见的音频或视频文件（如 mp4、mov、m4a、mp3、wav）", code="unsupported_media_type")
    try:
        byte_size = input_path.stat().st_size
    except OSError as exc:
        raise SpeechTranscriptionError("本机媒体文件不可读", code="media_read_failed") from exc
    if byte_size == 0:
        raise SpeechTranscriptionError("所选媒体文件为空", code="empty_media")
    if byte_size > MAX_MEDIA_BYTES:
        raise SpeechTranscriptionError("单个媒体文件不能超过 500MB", code="media_too_large")
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise SpeechTranscriptionError("本机转写需要 FFmpeg；请先安装 ffmpeg 和 ffprobe", code="ffmpeg_unavailable")

    digest = hashlib.sha256()
    try:
        with input_path.open("rb") as media_stream:
            while chunk := media_stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise SpeechTranscriptionError("无法读取本机媒体文件", code="media_read_failed") from exc

    with tempfile.TemporaryDirectory(prefix="douyin-local-asr-") as temporary_directory:
        root = Path(temporary_directory)
        wav_path = root / "audio.wav"
        try:
            probe = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name", "-show_entries", "format=duration", "-of", "json", str(input_path)],
                capture_output=True, text=True, timeout=60, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SpeechTranscriptionError("检查媒体文件超时", code="media_probe_timeout") from exc
        if probe.returncode != 0:
            raise SpeechTranscriptionError("媒体文件无法解码；请确认文件完整且格式受支持", code="media_decode_failed")
        probe_payload: dict[str, Any] = {}
        try:
            probe_payload = json.loads(probe.stdout or "{}")
            duration = float((probe_payload.get("format") or {}).get("duration") or 0)
        except (ValueError, TypeError, json.JSONDecodeError):
            duration = 0
        if not probe_payload.get("streams"):
            raise SpeechTranscriptionError("媒体文件中没有音轨", code="audio_track_missing")
        if duration <= 0:
            raise SpeechTranscriptionError("无法确认音频时长，拒绝把可能截断的结果标成完整", code="duration_unavailable")
        if duration > MAX_MEDIA_SECONDS:
            raise SpeechTranscriptionError("单条本机转写最多支持 15 分钟音频", code="media_too_long")

        try:
            converted = subprocess.run(
                [ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(input_path), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-t", str(MAX_MEDIA_SECONDS), str(wav_path)],
                capture_output=True, text=True, timeout=180, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SpeechTranscriptionError("音轨转换超时", code="audio_conversion_timeout") from exc
        if converted.returncode != 0 or not wav_path.is_file() or wav_path.stat().st_size < 44:
            raise SpeechTranscriptionError("无法把音轨转换为本机识别格式", code="audio_conversion_failed")

        payload = _run_helper(["--transcribe", str(wav_path)], timeout_seconds=max(300, duration * 4 + 120))
        raw_text = str(payload.get("text") or "").strip()
        text = raw_text[:60000]
        if not text:
            raise SpeechTranscriptionError("音频中没有识别到可保存的语音", code="no_speech")
        raw_segments = payload.get("segments") if isinstance(payload.get("segments"), list) else []

        def normalize_segment(value: Any) -> dict[str, Any] | None:
            if not isinstance(value, dict):
                return None
            segment_text = str(value.get("text") or "").strip()
            if not segment_text:
                return None
            segment: dict[str, Any] = {"text": segment_text}
            for key in ("start_seconds", "end_seconds", "confidence"):
                raw_number = value.get(key)
                if raw_number is None:
                    segment[key] = None
                    continue
                try:
                    number = float(raw_number)
                except (TypeError, ValueError):
                    segment[key] = None
                    continue
                segment[key] = number if math.isfinite(number) else None
            return segment

        segments = [segment for item in raw_segments if (segment := normalize_segment(item))]
        raw_low_confidence = payload.get("low_confidence") if isinstance(payload.get("low_confidence"), list) else []
        low_confidence = [segment for item in raw_low_confidence if (segment := normalize_segment(item))]
        confidence = payload.get("confidence") if isinstance(payload.get("confidence"), dict) else {}
        safe_confidence: dict[str, int | float] = {}
        for key in ("mean", "minimum", "maximum", "assessed_segment_count", "low_confidence_threshold"):
            value = confidence.get(key)
            if value is None:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number):
                safe_confidence[key] = int(number) if key == "assessed_segment_count" else round(number, 6)

        truncation_message = " 识别文本达到保存上限，末尾已截断。" if len(raw_text) > 60000 else ""
        review_message = f" 有 {len(low_confidence)} 个低置信度分段，建议重点人工核对。" if low_confidence else ""
        return {
            "text": text[:60000],
            "text_truncated": len(raw_text) > 60000,
            "segments": segments,
            "confidence": safe_confidence,
            "low_confidence": low_confidence,
            "quality": transcription_quality(text, segments, safe_confidence, text_truncated=len(raw_text) > 60000),
            "language": str(payload.get("language") or "zh-CN"),
            "engine": str(payload.get("engine") or "Apple SpeechAnalyzer (offline, on-device)"),
            "model": str(payload.get("model") or "SpeechTranscriber zh-CN"),
            "media_sha256": digest.hexdigest(),
            "media_bytes": byte_size,
            "duration_seconds": round(duration, 3),
            "source": source[:80],
            "message": (message + truncation_message + review_message)[:1000],
        }


def transcribe_uploaded_media(file_obj: BinaryIO, filename: str) -> dict[str, Any]:
    suffix = Path(str(filename or "").replace("\\", "/")).suffix.lower()
    if suffix not in ALLOWED_MEDIA_SUFFIXES:
        raise SpeechTranscriptionError("请选择常见的音频或视频文件（如 mp4、mov、m4a、mp3、wav）", code="unsupported_media_type")
    with tempfile.TemporaryDirectory(prefix="douyin-upload-asr-") as temporary_directory:
        input_path = Path(temporary_directory) / f"media{suffix}"
        byte_size = 0
        try:
            with input_path.open("wb") as output:
                while True:
                    chunk = file_obj.read(1024 * 1024)
                    if not chunk:
                        break
                    byte_size += len(chunk)
                    if byte_size > MAX_MEDIA_BYTES:
                        raise SpeechTranscriptionError("单个媒体文件不能超过 500MB", code="media_too_large")
                    output.write(chunk)
        except SpeechTranscriptionError:
            raise
        except OSError as exc:
            raise SpeechTranscriptionError("无法读取上传的本机媒体文件", code="media_read_failed") from exc
        result = transcribe_media_path(
            input_path,
            source="USER_PROVIDED_LOCAL_MEDIA",
            message="仅转写用户选定的本机文件；工作台未下载或长期保存音视频，文件与平台作品是否一致由用户确认。识别结果建议人工核对。",
        )
        result["media_bytes"] = byte_size
        return result
