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
            Image(nsImage: StatusIcon.image(for: store.state))
                .accessibilityLabel("WindBroker: \(store.state.title)")
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

/// Colored (non-template) menu-bar glyph so the session state is visible at a glance.
enum StatusIcon {
    static func image(for state: SessionState) -> NSImage {
        let symbol = state == .daemonDown ? "w.circle" : "w.circle.fill"
        let configuration = NSImage.SymbolConfiguration(pointSize: 15, weight: .semibold)
            .applying(NSImage.SymbolConfiguration(paletteColors: [state.nsColor]))
        let image = NSImage(systemSymbolName: symbol, accessibilityDescription: "WindBroker")?
            .withSymbolConfiguration(configuration) ?? NSImage()
        image.isTemplate = false
        return image
    }
}

/// Brings the accessory (LSUIElement) app forward when a window opens.
func activateApp() {
    NSApp.activate(ignoringOtherApps: true)
}
