import Foundation
import HealthKit

@MainActor
class HealthKitManager: ObservableObject {
    private let store = HKHealthStore()

    @Published var isAuthorized = false
    @Published var authError: String?

    private var sleepType: HKCategoryType {
        HKObjectType.categoryType(forIdentifier: .sleepAnalysis)!
    }

    // MARK: - Autorisierung

    func requestAuthorization() async {
        guard HKHealthStore.isHealthDataAvailable() else {
            authError = "HealthKit ist auf diesem Gerät nicht verfügbar."
            return
        }

        do {
            try await store.requestAuthorization(toShare: [sleepType], read: [sleepType])
            isAuthorized = true
        } catch {
            authError = error.localizedDescription
        }
    }

    // MARK: - Schlafphasen schreiben

    func writeSleepSession(_ export: SleepExport) async throws -> Int {
        var samples: [HKSample] = []

        for epoch in export.epochs {
            guard let start = epoch.startDate, let end = epoch.endDate else {
                continue
            }
            guard start < end else { continue }

            let value = healthKitValue(for: epoch.stage)
            let sample = HKCategorySample(
                type: sleepType,
                value: value,
                start: start,
                end: end,
                metadata: [
                    HKMetadataKeyWasUserEntered: false,
                    "SchlafschafMovementScore": epoch.movementScore,
                    "SchlafschafSoundScore": epoch.soundScore,
                ]
            )
            samples.append(sample)
        }

        guard !samples.isEmpty else {
            throw HealthKitError.noSamples
        }

        try await store.save(samples)
        return samples.count
    }

    // MARK: - Stage-Mapping

    private func healthKitValue(for stage: String) -> Int {
        if #available(iOS 16.0, *) {
            switch stage {
            case "AWAKE":       return HKCategoryValueSleepAnalysis.awake.rawValue
            case "LIGHT_SLEEP": return HKCategoryValueSleepAnalysis.asleepCore.rawValue
            case "DEEP_SLEEP":  return HKCategoryValueSleepAnalysis.asleepDeep.rawValue
            case "REM":         return HKCategoryValueSleepAnalysis.asleepREM.rawValue
            default:            return HKCategoryValueSleepAnalysis.asleepUnspecified.rawValue
            }
        } else {
            // iOS < 16: nur inBed / asleep / awake verfügbar
            switch stage {
            case "AWAKE": return HKCategoryValueSleepAnalysis.awake.rawValue
            default:      return HKCategoryValueSleepAnalysis.asleepUnspecified.rawValue
            }
        }
    }

    // MARK: - Vorhandene Sessions löschen

    func deleteSleepSession(startDate: Date, endDate: Date) async throws {
        let predicate = HKQuery.predicateForSamples(
            withStart: startDate,
            end: endDate,
            options: .strictStartDate
        )
        let samples = try await querySleepSamples(predicate: predicate)
        try await store.delete(samples)
    }

    private func querySleepSamples(predicate: NSPredicate) async throws -> [HKSample] {
        try await withCheckedThrowingContinuation { continuation in
            let query = HKSampleQuery(
                sampleType: sleepType,
                predicate: predicate,
                limit: HKObjectQueryNoLimit,
                sortDescriptors: nil
            ) { _, samples, error in
                if let error {
                    continuation.resume(throwing: error)
                } else {
                    continuation.resume(returning: samples ?? [])
                }
            }
            store.execute(query)
        }
    }
}

enum HealthKitError: LocalizedError {
    case noSamples
    case notAuthorized

    var errorDescription: String? {
        switch self {
        case .noSamples:     return "Keine gültigen Schlafphasen zum Speichern."
        case .notAuthorized: return "HealthKit-Zugriff verweigert."
        }
    }
}
