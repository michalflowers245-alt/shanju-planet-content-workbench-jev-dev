import AVFoundation
import Foundation
import Speech

struct TranscriptionResponse: Codable {
    var ok: Bool
    var engine: String
    var model: String
    var language: String
    var text: String
    var error: String
    var segments: [TranscriptSegment] = []
    var confidence: ConfidenceSummary? = nil
    var low_confidence: [TranscriptSegment] = []
}

struct TranscriptSegment: Codable {
    var text: String
    var start_seconds: Double?
    var end_seconds: Double?
    var confidence: Double?
}

struct ConfidenceSummary: Codable {
    var mean: Double
    var minimum: Double
    var maximum: Double
    var assessed_segment_count: Int
    var low_confidence_threshold: Double
}

struct TranscriptionCollection {
    var text = ""
    var segments: [TranscriptSegment] = []
}

struct CapabilityResponse: Codable {
    var ok: Bool
    var engine: String
    var language: String
    var supported: Bool
    var installed: Bool
    var error: String
}

enum LocalSpeechError: LocalizedError {
    case unsupportedOS
    case unsupportedLanguage
    case audioUnreadable
    case noSpeech

    var errorDescription: String? {
        switch self {
        case .unsupportedOS:
            return "本机离线长音频识别需要 macOS 26 或更新版本"
        case .unsupportedLanguage:
            return "Apple 本机语音模型不支持简体中文（zh-CN）"
        case .audioUnreadable:
            return "无法读取音频文件或文件中没有可识别的音轨"
        case .noSpeech:
            return "音频中没有识别到语音"
        }
    }
}

@main
struct LocalSpeechTranscribe {
    static let locale = Locale(identifier: "zh-CN")
    static let engine = "Apple SpeechAnalyzer (offline, on-device)"
    static let model = "SpeechTranscriber zh-CN"
    static let lowConfidenceThreshold = 0.65

    static func finiteSeconds(_ value: CMTime) -> Double? {
        let seconds = value.seconds
        return seconds.isFinite && seconds >= 0 ? seconds : nil
    }

    static func encode<T: Encodable>(_ value: T) {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        if let data = try? encoder.encode(value), let json = String(data: data, encoding: .utf8) {
            print(json)
        } else {
            print("{\"ok\":false,\"error\":\"无法序列化本机语音识别结果\"}")
        }
    }

    static func main() async {
        let arguments = Array(CommandLine.arguments.dropFirst())
        guard !arguments.isEmpty else {
            encode(TranscriptionResponse(ok: false, engine: engine, model: model, language: "zh-CN", text: "", error: "缺少命令参数"))
            Foundation.exit(2)
        }

        do {
            if arguments[0] == "--check" {
                let result = try await capabilities()
                encode(result)
                Foundation.exit(result.ok ? 0 : 2)
            }
            guard arguments.count == 2, arguments[0] == "--transcribe" else {
                throw NSError(domain: "LocalSpeechTranscribe", code: 2, userInfo: [NSLocalizedDescriptionKey: "用法：--check 或 --transcribe <audio-file>"])
            }
            let output = try await transcribe(URL(fileURLWithPath: arguments[1]))
            let confidenceValues = output.segments.compactMap(\.confidence)
            let confidence: ConfidenceSummary? = confidenceValues.isEmpty ? nil : ConfidenceSummary(
                mean: confidenceValues.reduce(0, +) / Double(confidenceValues.count),
                minimum: confidenceValues.min() ?? 0,
                maximum: confidenceValues.max() ?? 0,
                assessed_segment_count: confidenceValues.count,
                low_confidence_threshold: lowConfidenceThreshold
            )
            let lowConfidence = output.segments.filter {
                guard let value = $0.confidence else { return false }
                return value < lowConfidenceThreshold
            }
            encode(TranscriptionResponse(
                ok: true, engine: engine, model: model, language: "zh-CN",
                text: output.text, error: "", segments: output.segments,
                confidence: confidence, low_confidence: lowConfidence
            ))
            Foundation.exit(0)
        } catch {
            encode(TranscriptionResponse(ok: false, engine: engine, model: model, language: "zh-CN", text: "", error: error.localizedDescription))
            Foundation.exit(2)
        }
    }

    static func capabilities() async throws -> CapabilityResponse {
        guard #available(macOS 26.0, *) else { throw LocalSpeechError.unsupportedOS }
        let supportedLocales = await SpeechTranscriber.supportedLocales
        let installedLocales = await SpeechTranscriber.installedLocales
        let language = locale.identifier(.bcp47)
        let supported = supportedLocales.contains { $0.identifier(.bcp47).caseInsensitiveCompare(language) == .orderedSame }
        let installed = installedLocales.contains { $0.identifier(.bcp47).caseInsensitiveCompare(language) == .orderedSame }
        return CapabilityResponse(
            ok: supported,
            engine: engine,
            language: language,
            supported: supported,
            installed: installed,
            error: supported ? "" : LocalSpeechError.unsupportedLanguage.localizedDescription
        )
    }

    static func transcribe(_ url: URL) async throws -> TranscriptionCollection {
        guard #available(macOS 26.0, *) else { throw LocalSpeechError.unsupportedOS }
        guard FileManager.default.isReadableFile(atPath: url.path) else { throw LocalSpeechError.audioUnreadable }
        let supportedLocales = await SpeechTranscriber.supportedLocales
        guard supportedLocales.contains(where: { $0.identifier(.bcp47).caseInsensitiveCompare("zh-CN") == .orderedSame }) else {
            throw LocalSpeechError.unsupportedLanguage
        }

        let preset = SpeechTranscriber.Preset.transcription
        let transcriber = SpeechTranscriber(
            locale: locale,
            transcriptionOptions: preset.transcriptionOptions,
            reportingOptions: preset.reportingOptions,
            attributeOptions: [.audioTimeRange, .transcriptionConfidence]
        )
        let installedLocales = await SpeechTranscriber.installedLocales
        let installed = installedLocales.contains { $0.identifier(.bcp47).caseInsensitiveCompare("zh-CN") == .orderedSame }
        if !installed {
            if let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
                try await request.downloadAndInstall()
            }
            let installedAfterRequest = await SpeechTranscriber.installedLocales.contains {
                $0.identifier(.bcp47).caseInsensitiveCompare("zh-CN") == .orderedSame
            }
            guard installedAfterRequest else { throw LocalSpeechError.unsupportedLanguage }
        }

        let audioFile: AVAudioFile
        do {
            audioFile = try AVAudioFile(forReading: url)
        } catch {
            throw LocalSpeechError.audioUnreadable
        }

        let analyzer = SpeechAnalyzer(modules: [transcriber])
        async let transcription = try transcriber.results.reduce(into: TranscriptionCollection()) { collected, result in
            let attributedText = result.text
            let phrase = String(attributedText.characters)
            collected.text += phrase
            let segmentCountBefore = collected.segments.count
            for run in attributedText.runs {
                let value = String(attributedText[run.range].characters)
                guard !value.isEmpty else { continue }
                let timeRange = run[AttributeScopes.SpeechAttributes.TimeRangeAttribute.self]
                let confidence: Double? = run[AttributeScopes.SpeechAttributes.ConfidenceAttribute.self]
                let start = timeRange.flatMap { finiteSeconds($0.start) }
                let end: Double? = timeRange.flatMap { range -> Double? in
                    guard let start = finiteSeconds(range.start), let duration = finiteSeconds(range.duration) else { return nil }
                    return start + duration
                }
                collected.segments.append(TranscriptSegment(
                    text: value, start_seconds: start, end_seconds: end,
                    confidence: confidence
                ))
            }
            // Keep a phrase-level time range if this OS/model omitted the
            // optional per-text attributes; do not invent a confidence score.
            if collected.segments.count == segmentCountBefore && !phrase.isEmpty {
                let start = finiteSeconds(result.range.start)
                let end = start.flatMap { value in
                    finiteSeconds(result.range.duration).map { value + $0 }
                }
                collected.segments.append(TranscriptSegment(
                    text: phrase, start_seconds: start, end_seconds: end, confidence: nil
                ))
            }
        }

        if let lastSample = try await analyzer.analyzeSequence(from: audioFile) {
            try await analyzer.finalizeAndFinish(through: lastSample)
        } else {
            await analyzer.cancelAndFinishNow()
        }

        var output = try await transcription
        output.text = output.text.trimmingCharacters(in: CharacterSet.whitespacesAndNewlines)
        guard !output.text.isEmpty else { throw LocalSpeechError.noSpeech }
        return output
    }
}
