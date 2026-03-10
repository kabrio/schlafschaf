import SwiftUI
import HealthKit

@main
struct SchlafSchafApp: App {
    @StateObject private var healthManager = HealthKitManager()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(healthManager)
                .task {
                    await healthManager.requestAuthorization()
                }
        }
    }
}
