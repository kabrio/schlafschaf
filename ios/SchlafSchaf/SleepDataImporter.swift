import Foundation

// MARK: - JSON-Datenmodelle (entsprechen dem Python-Export-Format)

struct SleepExport: Codable, Identifiable {
    let version: String
    let device: String
    let exportedAt: String
    let session: SleepSession
    let summary: SleepSummary
    let epochs: [SleepEpoch]

    var id: String { session.id }

    enum CodingKeys: String, CodingKey {
        case version, device, session, summary, epochs
        case exportedAt = "exported_at"
    }
}

struct SleepSession: Codable {
    let id: String
    let startTime: String
    let endTime: String
    let durationMinutes: Double

    enum CodingKeys: String, CodingKey {
        case id
        case startTime = "start_time"
        case endTime = "end_time"
        case durationMinutes = "duration_minutes"
    }

    var startDate: Date? { ISO8601DateFormatter().date(from: startTime) }
    var endDate: Date? { ISO8601DateFormatter().date(from: endTime) }
}

struct SleepSummary: Codable {
    let awakeMinutes: Double
    let lightSleepMinutes: Double
    let deepSleepMinutes: Double
    let remMinutes: Double

    enum CodingKeys: String, CodingKey {
        case awakeMinutes = "awake_minutes"
        case lightSleepMinutes = "light_sleep_minutes"
        case deepSleepMinutes = "deep_sleep_minutes"
        case remMinutes = "rem_minutes"
    }

    var sleepMinutes: Double { lightSleepMinutes + deepSleepMinutes + remMinutes }
    var efficiency: Double {
        let total = awakeMinutes + sleepMinutes
        return total > 0 ? sleepMinutes / total * 100 : 0
    }
}

struct SleepEpoch: Codable {
    let start: String
    let end: String
    let stage: String
    let movementScore: Double
    let soundScore: Double

    enum CodingKeys: String, CodingKey {
        case start, end, stage
        case movementScore = "movement_score"
        case soundScore = "sound_score"
    }

    var startDate: Date? { ISO8601DateFormatter().date(from: start) }
    var endDate: Date? { ISO8601DateFormatter().date(from: end) }
}

// MARK: - Importer

enum ImportError: LocalizedError {
    case invalidJSON(String)
    case networkError(String)
    case noEpochs

    var errorDescription: String? {
        switch self {
        case .invalidJSON(let msg): return "Ungültiges JSON: \(msg)"
        case .networkError(let msg): return "Netzwerkfehler: \(msg)"
        case .noEpochs: return "Keine Schlafphasen in der Datei."
        }
    }
}

class SleepDataImporter {
    private let decoder: JSONDecoder = {
        let d = JSONDecoder()
        return d
    }()

    func loadFromURL(_ url: URL) async throws -> SleepExport {
        let data: Data
        if url.isFileURL {
            data = try Data(contentsOf: url)
        } else {
            let (responseData, _) = try await URLSession.shared.data(from: url)
            data = responseData
        }

        do {
            let export = try decoder.decode(SleepExport.self, from: data)
            if export.epochs.isEmpty {
                throw ImportError.noEpochs
            }
            return export
        } catch let err as ImportError {
            throw err
        } catch {
            throw ImportError.invalidJSON(error.localizedDescription)
        }
    }

    func loadSessionsFromHost(_ host: String, port: Int = 8080) async throws -> [SessionInfo] {
        guard let url = URL(string: "http://\(host):\(port)/sessions") else {
            throw ImportError.networkError("Ungültige IP-Adresse")
        }
        let (data, _) = try await URLSession.shared.data(from: url)
        return try decoder.decode([SessionInfo].self, from: data)
    }

    func loadExportFromHost(_ host: String, port: Int = 8080, sessionId: String) async throws -> SleepExport {
        guard let url = URL(string: "http://\(host):\(port)/sessions/\(sessionId)/export") else {
            throw ImportError.networkError("Ungültige URL")
        }
        return try await loadFromURL(url)
    }
}

struct SessionInfo: Codable, Identifiable {
    let id: String
    let startTime: String
    let endTime: String?
    let analyzed: Bool
    let epochCount: Int

    enum CodingKeys: String, CodingKey {
        case id, analyzed
        case startTime = "start_time"
        case endTime = "end_time"
        case epochCount = "epoch_count"
    }

    var startDate: Date? { ISO8601DateFormatter().date(from: startTime) }
}
