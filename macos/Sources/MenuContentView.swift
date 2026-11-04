import SwiftUI

struct StateBadge: View {
    let state: SessionState

    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(state.color).frame(width: 8, height: 8)
            Text(state.title).font(.caption.weight(.semibold))
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 3)
        .background(state.color.opacity(0.15), in: Capsule())
    }
}

struct MenuContentView: View {
    @EnvironmentObject private var store: BrokerStore
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            Divider()
            sessionControls
            daemonControls
            Divider()
            recent
            Divider()
            footer
        }
        .padding(14)
        .frame(width: 360)
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("WindBroker").font(.headline)
                Spacer()
                StateBadge(state: store.state)
            }
            if let status = store.status {
                HStack(spacing: 14) {
                    Label(Format.uptime(status.uptimeSeconds), systemImage: "clock")
                    Label("\(status.totalRequests) req", systemImage: "arrow.left.arrow.right")
                    Label("\(status.failedRequests) failed", systemImage: "exclamationmark.triangle")
                        .foregroundStyle(status.failedRequests > 0 ? .orange : .secondary)
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                if let error = status.lastError, store.state != .connected {
                    Text(error).font(.caption).foregroundStyle(.red).lineLimit(3)
                }
            } else {
                Text("windd is not running. Start it to share one Wind login across apps.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let action = store.busyAction {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.small)
                    Text(action).font(.caption)
                }
            } else if let banner = store.banner {
                Text(banner).font(.caption).foregroundStyle(store.bannerIsError ? .red : .secondary).lineLimit(3)
            }
        }
    }

    private var sessionControls: some View {
        HStack {
            Button {
                Task { await store.login() }
            } label: {
                Label(store.state == .connected ? "Connected" : "Connect / Authenticate Wind (QR)",
                      systemImage: "qrcode.viewfinder")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .disabled(store.state == .connected || store.state == .connecting || store.busyAction != nil)

            Button("Disconnect") { Task { await store.logout() } }
                .disabled(!(store.state == .connected || store.state == .error) || store.busyAction != nil)
        }
    }

    private var daemonControls: some View {
        HStack {
            Text("Daemon").font(.caption).foregroundStyle(.secondary)
            Spacer()
            Button("Start") { Task { await store.startDaemon() } }
                .disabled(store.daemonRunning || store.busyAction != nil)
            Button("Stop") { Task { await store.stopDaemon() } }
                .disabled(!store.daemonRunning || store.busyAction != nil)
            Button("Restart") { Task { await store.restartDaemon() } }
                .disabled(store.busyAction != nil)
        }
        .controlSize(.small)
    }

    private var recent: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Recent requests").font(.caption).foregroundStyle(.secondary)
            let entries = Array(store.logs.filter { $0.status != "event" }.prefix(5))
            if entries.isEmpty {
                Text("No requests yet").font(.caption).foregroundStyle(.tertiary)
            }
            ForEach(entries) { entry in
                HStack(spacing: 6) {
                    Circle().fill(entry.isFailure ? Color.red : Color.green).frame(width: 6, height: 6)
                    Text(entry.time).monospacedDigit()
                    Text(entry.client).lineLimit(1).frame(width: 90, alignment: .leading)
                    Text(entry.method).bold()
                    Spacer()
                    Text(entry.latencyText).monospacedDigit().foregroundStyle(.secondary)
                }
                .font(.caption)
                .help(entry.detail.isEmpty ? entry.argsSummary : entry.detail)
            }
        }
    }

    private var footer: some View {
        HStack {
            Button("Open Control Panel…") {
                openWindow(id: "control")
                activateApp()
            }
            .keyboardShortcut(",")
            Spacer()
            Button("Quit") { NSApp.terminate(nil) }
                .keyboardShortcut("q")
                .help("Quits the app. windd keeps running so other apps stay connected.")
        }
    }
}
