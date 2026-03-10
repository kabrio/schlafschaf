import SwiftUI
import UniformTypeIdentifiers

struct ContentView: View {
    @EnvironmentObject var healthManager: HealthKitManager
    @StateObject private var vm = ContentViewModel()

    var body: some View {
        NavigationStack {
            List {
                // Verbindungs-Sektion
                Section("Arduino Uno Q verbinden") {
                    HStack {
                        Image(systemName: "network")
                            .foregroundColor(.blue)
                        TextField("IP-Adresse (z.B. 192.168.1.100)", text: $vm.deviceIP)
                            .keyboardType(.decimalPad)
                            .autocorrectionDisabled()
                    }
                    Button {
                        Task { await vm.loadSessionsFromDevice() }
                    } label: {
                        Label("Sessions laden", systemImage: "arrow.down.circle")
                    }
                    .disabled(vm.deviceIP.isEmpty || vm.isLoading)
                }

                // Datei-Import
                Section("Oder JSON-Datei importieren") {
                    Button {
                        vm.showFilePicker = true
                    } label: {
                        Label("JSON-Datei öffnen", systemImage: "doc.badge.plus")
                    }
                }

                // Sessions-Liste
                if !vm.remoteSessions.isEmpty {
                    Section("Sessions auf dem Gerät (\(vm.remoteSessions.count))") {
                        ForEach(vm.remoteSessions) { session in
                            RemoteSessionRow(session: session) {
                                Task { await vm.importAndSync(session: session) }
                            }
                        }
                    }
                }

                // Importierte Exporte
                if !vm.importedExports.isEmpty {
                    Section("Importierte Sessions") {
                        ForEach(vm.importedExports) { export in
                            ImportedExportRow(export: export) {
                                Task { await vm.syncToHealth(export: export) }
                            }
                        }
                    }
                }

                // Status
                if let status = vm.statusMessage {
                    Section {
                        Label(status, systemImage: vm.statusIsError ? "xmark.circle" : "checkmark.circle")
                            .foregroundColor(vm.statusIsError ? .red : .green)
                    }
                }
            }
            .navigationTitle("Schlafschaf")
            .navigationBarTitleDisplayMode(.large)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    if vm.isLoading {
                        ProgressView()
                    }
                }
            }
            .fileImporter(
                isPresented: $vm.showFilePicker,
                allowedContentTypes: [.json],
                allowsMultipleSelection: false
            ) { result in
                Task { await vm.handleFileImport(result: result) }
            }
            .alert("HealthKit-Fehler", isPresented: $vm.showAuthAlert) {
                Button("OK") {}
            } message: {
                Text(healthManager.authError ?? "Unbekannter Fehler")
            }
        }
    }
}

// MARK: - Hilfs-Views

struct RemoteSessionRow: View {
    let session: SessionInfo
    let onSync: () -> Void

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 4) {
                if let date = session.startDate {
                    Text(date.formatted(date: .abbreviated, time: .shortened))
                        .font(.headline)
                }
                HStack(spacing: 8) {
                    if session.analyzed {
                        Label("\(session.epochCount) Epochen", systemImage: "chart.bar.fill")
                            .font(.caption)
                            .foregroundColor(.green)
                    } else {
                        Label("Nicht analysiert", systemImage: "exclamationmark.triangle")
                            .font(.caption)
                            .foregroundColor(.orange)
                    }
                }
            }
            Spacer()
            if session.analyzed {
                Button("Sync", action: onSync)
                    .buttonStyle(.bordered)
                    .tint(.blue)
            }
        }
        .padding(.vertical, 4)
    }
}

struct ImportedExportRow: View {
    let export: SleepExport
    let onSync: () -> Void

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 4) {
                if let date = export.session.startDate {
                    Text(date.formatted(date: .abbreviated, time: .shortened))
                        .font(.headline)
                }
                HStack(spacing: 12) {
                    SleepStatLabel(value: export.summary.sleepMinutes / 60,
                                   unit: "h Schlaf", color: .blue)
                    SleepStatLabel(value: export.summary.efficiency,
                                   unit: "% Effizienz", color: .green)
                }
                SleepPhaseBar(summary: export.summary)
            }
            Spacer()
            Button("→ Health", action: onSync)
                .buttonStyle(.bordered)
                .tint(.pink)
        }
        .padding(.vertical, 6)
    }
}

struct SleepStatLabel: View {
    let value: Double
    let unit: String
    let color: Color

    var body: some View {
        Text(String(format: "%.1f %@", value, unit))
            .font(.caption)
            .foregroundColor(color)
    }
}

struct SleepPhaseBar: View {
    let summary: SleepSummary

    var body: some View {
        let total = summary.awakeMinutes + summary.sleepMinutes
        GeometryReader { geo in
            HStack(spacing: 1) {
                PhaseBar(fraction: summary.awakeMinutes / total,
                         color: .red, width: geo.size.width)
                PhaseBar(fraction: summary.lightSleepMinutes / total,
                         color: .green, width: geo.size.width)
                PhaseBar(fraction: summary.deepSleepMinutes / total,
                         color: .blue, width: geo.size.width)
                PhaseBar(fraction: summary.remMinutes / total,
                         color: .yellow, width: geo.size.width)
            }
            .frame(height: 6)
            .clipShape(Capsule())
        }
        .frame(height: 6)
    }
}

struct PhaseBar: View {
    let fraction: Double
    let color: Color
    let width: CGFloat

    var body: some View {
        Rectangle()
            .fill(color)
            .frame(width: width * max(fraction, 0))
    }
}

// MARK: - ViewModel

@MainActor
class ContentViewModel: ObservableObject {
    @Published var deviceIP = ""
    @Published var remoteSessions: [SessionInfo] = []
    @Published var importedExports: [SleepExport] = []
    @Published var isLoading = false
    @Published var statusMessage: String?
    @Published var statusIsError = false
    @Published var showFilePicker = false
    @Published var showAuthAlert = false

    private let importer = SleepDataImporter()

    func loadSessionsFromDevice() async {
        isLoading = true
        statusMessage = nil
        defer { isLoading = false }

        do {
            remoteSessions = try await importer.loadSessionsFromHost(deviceIP)
            if remoteSessions.isEmpty {
                setStatus("Keine Sessions gefunden.", isError: false)
            } else {
                setStatus("\(remoteSessions.count) Sessions geladen.", isError: false)
            }
        } catch {
            setStatus(error.localizedDescription, isError: true)
        }
    }

    func importAndSync(session: SessionInfo) async {
        isLoading = true
        defer { isLoading = false }

        do {
            let export = try await importer.loadExportFromHost(deviceIP, sessionId: session.id)
            if !importedExports.contains(where: { $0.id == export.id }) {
                importedExports.insert(export, at: 0)
            }
            await syncToHealth(export: export)
        } catch {
            setStatus(error.localizedDescription, isError: true)
        }
    }

    func handleFileImport(result: Result<[URL], Error>) async {
        switch result {
        case .success(let urls):
            guard let url = urls.first else { return }
            isLoading = true
            defer { isLoading = false }
            do {
                // Security scope für Datei-Zugriff
                guard url.startAccessingSecurityScopedResource() else { return }
                defer { url.stopAccessingSecurityScopedResource() }

                let export = try await importer.loadFromURL(url)
                if !importedExports.contains(where: { $0.id == export.id }) {
                    importedExports.insert(export, at: 0)
                }
                setStatus("Datei importiert: \(export.epochs.count) Epochen", isError: false)
            } catch {
                setStatus(error.localizedDescription, isError: true)
            }
        case .failure(let error):
            setStatus(error.localizedDescription, isError: true)
        }
    }

    func syncToHealth(export: SleepExport) async {
        isLoading = true
        defer { isLoading = false }

        // Direkt über EnvironmentObject nicht möglich im ViewModel
        // Lösung: eigene HKHealthStore-Instanz im ViewModel verwenden
        let manager = HealthKitManager()
        do {
            let count = try await manager.writeSleepSession(export)
            setStatus("✓ \(count) Schlafphasen in Apple Health gespeichert!", isError: false)
        } catch {
            setStatus(error.localizedDescription, isError: true)
        }
    }

    private func setStatus(_ msg: String, isError: Bool) {
        statusMessage = msg
        statusIsError = isError
    }
}
