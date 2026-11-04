import AppKit
import SwiftUI

@main
struct WindBrokerApp: App {
    @StateObject private var store = BrokerStore()

    var body: some Scene {
        MenuBarExtra {
            MenuContentView()
                .environmentObject(store)
        } label: {
            StatusLabel()
                .environmentObject(store)
        }
        .menuBarExtraStyle(.window)

        Window("WindBroker Control Panel", id: "control") {
            ControlPanelView()
                .environmentObject(store)
                .frame(minWidth: 900, minHeight: 600)
        }
        .defaultSize(width: 1040, height: 680)
    }
}

/// Brings the accessory (LSUIElement) app forward when a window opens.
func activateApp() {
    NSApp.activate(ignoringOtherApps: true)
}
