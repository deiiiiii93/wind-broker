import AppKit
import Foundation
import SwiftUI

enum SessionState: String {
    case daemonDown, disconnected, connecting, connected, error

    init(status: BrokerStatus?) {
        guard let status else { self = .daemonDown; return }
        self = SessionState(rawValue: status.state) ?? .error
    }

    var title: String {
        switch self {
        case .daemonDown: return "Daemon stopped"
        case .disconnected: return "Disconnected"
        case .connecting: return "Connecting…"
        case .connected: return "Connected"
        case .error: return "Error"
        }
    }

    var color: Color { Color(nsColor: nsColor) }

    var nsColor: NSColor {
        switch self {
        case .connected: return .systemGreen
        case .connecting: return .systemOrange
        case .error: return .systemRed
        case .disconnected, .daemonDown: return .systemGray
        }
    }
}

/// Paths shared with the Python side (~/.wind-broker, LaunchAgent).
enum BrokerPaths {
    static var home: URL {
        if let custom = ProcessInfo.processInfo.environment["WIND_BROKER_HOME"] {
            return URL(fileURLWithPath: NSString(string: custom).expandingTildeInPath)
        }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".wind-broker")
    }
    static var config: URL { home.appendingPathComponent("config.json") }
    static var log: URL { home.appendingPathComponent("windd.log") }
    static var launchAgent: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/com.windbroker.daemon.plist")
    }
    /// The Python package bundled inside WindBroker.app (Contents/Resources/python).
    static var bundledPython: String? {
        guard let resources = Bundle.main.resourcePath else { return nil }
        let path = (resources as NSString).appendingPathComponent("python")
        return FileManager.default.fileExists(atPath: (path as NSString).appendingPathComponent("wind_broker"))
            ? path : nil
    }
}

@MainActor
final class BrokerStore: ObservableObject {
    @Published private(set) var status: BrokerStatus?
    @Published private(set) var logs: [LogEntry] = []
    @Published private(set) var busyAction: String?
    @Published var banner: String?
    @Published var bannerIsError = false
    @Published private(set) var launchAgentInstalled = false
    @Published var config = BrokerConfig()
    @Published private(set) var savedConfig = BrokerConfig()
    @Published private(set) var restartRequired: [String] = []
    @Published var autoRefreshLogs = true
    @Published private(set) var testResult: TestResult?
    @Published private(set) var testError: String?
    @Published private(set) var testRunning = false

    @Published private(set) var routerInfo: RouterInfo?
    /// One-time workstation key; shown in a sheet and cleared when it is dismissed.
    @Published var newRouterKey: RouterClientCreated?

    private var api: BrokerAPI
    private var pollTask: Task<Void, Never>?

    var state: SessionState { SessionState(status: status) }
    var daemonRunning: Bool { status != nil }

    init() {
        let loaded = BrokerStore.readConfigFile() ?? BrokerConfig()
        config = loaded
        savedConfig = loaded
        api = BrokerAPI(host: loaded.host, port: loaded.port)
        launchAgentInstalled = FileManager.default.fileExists(atPath: BrokerPaths.launchAgent.path)
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
    }

    // MARK: polling

    func refresh() async {
        launchAgentInstalled = FileManager.default.fileExists(atPath: BrokerPaths.launchAgent.path)
        do {
            let latest = try await api.status()
            status = latest
            if autoRefreshLogs || logs.isEmpty {
                logs = try await api.logs(limit: 250)
            }
        } catch {
            status = nil
        }
    }

    func refreshLogs() async {
        if let entries = try? await api.logs(limit: 250) { logs = entries }
    }

    // MARK: actions

    private func perform(_ name: String, _ work: () async throws -> String?) async {
        busyAction = name
        defer { busyAction = nil }
        do {
            if let message = try await work() { show(message) }
        } catch {
            show(error.localizedDescription, isError: true)
        }
        await refresh()
    }

    func show(_ message: String, isError: Bool = false) {
        banner = message
        bannerIsError = isError
    }

    func login() async {
        await perform("Logging in") {
            // wait=false: the Wind QR window may stay open for minutes; we poll status.
            let _: BrokerStatus = try await api.post("v1/login", body: ["wait": false])
            return "Login started. Complete the Wind QR scan if the login window appears."
        }
    }

    func reauthenticate() async {
        await perform("Re-authenticating") {
            let _: BrokerStatus = try await api.post("v1/logout", timeout: 120)
            let _: BrokerStatus = try await api.post("v1/login", body: ["wait": false])
            return "Session reset. Complete the Wind QR scan if prompted."
        }
    }

    func logout() async {
        await perform("Disconnecting") {
            let _: BrokerStatus = try await api.post("v1/logout", timeout: 120)
            return "Wind session stopped (w.stop()). Every app on this Mac is now disconnected."
        }
    }

    func startDaemon() async {
        await perform("Starting daemon") {
            let result = try await runWindctl(["start"])
            return result.code == 0 ? "Daemon started." : "Daemon failed to start: \(result.output)"
        }
    }

    func stopDaemon() async {
        await perform("Stopping daemon") {
            let _: Ignored = try await api.post("v1/shutdown")
            for _ in 0..<40 {
                try? await Task.sleep(nanoseconds: 250_000_000)
                if (try? await api.status()) == nil { break }
            }
            return "Daemon stopped."
        }
    }

    func restartDaemon() async {
        await perform("Restarting daemon") {
            let result = try await runWindctl(["restart"])
            reloadAPI()
            return result.code == 0 ? "Daemon restarted." : "Restart failed: \(result.output)"
        }
    }

    func setLaunchAtLogin(_ enabled: Bool) async {
        await perform(enabled ? "Installing LaunchAgent" : "Removing LaunchAgent") {
            let result = try await runWindctl(["launchagent", enabled ? "install" : "uninstall"])
            guard result.code == 0 else { throw BrokerAPIError.server(0, result.output) }
            return enabled
                ? "windd will start automatically at login."
                : "Launch at login disabled. The running daemon keeps its session."
        }
    }

    func clearLogs() async {
        await perform("Clearing logs") {
            let _: Ignored = try await api.post("v1/logs/clear")
            logs = []
            return nil
        }
    }

    // MARK: test console

    func runTest(preset: String, method: String? = nil, args: [String] = []) async {
        testRunning = true
        testError = nil
        defer { testRunning = false }
        var body: [String: Any] = ["preset": preset, "client": "WindBroker.app"]
        if let method {
            body["method"] = method
            body["args"] = args
        }
        do {
            let timeout = config.startupSeconds + 2 * config.requestSeconds + 30
            testResult = try await api.post("v1/test", body: body, timeout: timeout)
        } catch {
            testResult = nil
            testError = error.localizedDescription
        }
        await refresh()
    }

    // MARK: configuration

    static func readConfigFile() -> BrokerConfig? {
        guard let data = try? Data(contentsOf: BrokerPaths.config) else { return nil }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try? decoder.decode(BrokerConfig.self, from: data)
    }

    func reloadConfig() {
        if let loaded = BrokerStore.readConfigFile() {
            config = loaded
            savedConfig = loaded
        }
    }

    func saveConfig() async {
        await perform("Saving configuration") {
            // Router settings may have changed elsewhere (windctl router enable, Router tab);
            // never send the stale in-memory copy. The daemon persists config.json on every change.
            if let fresh = BrokerStore.readConfigFile() {
                config.copyRouterFields(from: fresh)
                savedConfig.copyRouterFields(from: fresh)
            }
            if daemonRunning {
                let response: ConfigUpdateResponse = try await api.post("v1/config", body: config.jsonObject)
                restartRequired = response.restartRequired
                savedConfig = response.config
                config = response.config
                return restartRequired.isEmpty
                    ? "Configuration saved and applied."
                    : "Saved. Restart the daemon to apply: \(restartRequired.joined(separator: ", "))."
            }
            // Daemon down: write config.json directly; it is read on next start.
            try FileManager.default.createDirectory(at: BrokerPaths.home, withIntermediateDirectories: true)
            let data = try JSONSerialization.data(withJSONObject: config.jsonObject, options: [.prettyPrinted, .sortedKeys])
            try data.write(to: BrokerPaths.config, options: .atomic)
            savedConfig = config
            restartRequired = []
            reloadAPI()
            return "Configuration written to \(BrokerPaths.config.path)."
        }
    }

    // MARK: LAN router

    func refreshRouter() async {
        if let info: RouterInfo = try? await api.get("v1/router") { routerInfo = info }
    }

    /// Apply a router config response without marking the Configuration tab as edited.
    private func adopt(_ response: ConfigUpdateResponse) {
        routerInfo = response.router ?? routerInfo
        config.copyRouterFields(from: response.config)
        savedConfig.copyRouterFields(from: response.config)
    }

    /// Posts ONLY router_* keys so unrelated (unsaved) Configuration edits are never sent.
    func saveRouter(enabled: Bool, host: String, port: Int, maxInflight: Int) async {
        await perform("Saving router settings") {
            var update = BrokerConfig()
            update.routerEnabled = enabled
            update.routerHost = host
            update.routerPort = port
            update.routerMaxInflight = maxInflight
            let response: ConfigUpdateResponse = try await api.post("v1/config", body: update.routerJSONObject)
            adopt(response)
            return enabled ? "Router settings applied." : "Router disabled."
        }
    }

    func setRouterEnabled(_ enabled: Bool) async {
        await perform(enabled ? "Enabling router" : "Disabling router") {
            let response: ConfigUpdateResponse = try await api.post("v1/config", body: ["router_enabled": enabled])
            adopt(response)
            return enabled ? "Router enabled." : "Router disabled."
        }
    }

    func addRouterClient(_ name: String) async {
        await perform("Adding workstation") {
            let created: RouterClientCreated = try await api.post("v1/router/clients", body: ["name": name])
            newRouterKey = created
            await refreshRouter()
            return nil
        }
    }

    func revokeRouterClient(_ name: String) async {
        await perform("Revoking workstation") {
            let _: Ignored = try await api.post("v1/router/clients/revoke", body: ["name": name])
            await refreshRouter()
            return "Revoked \(name)."
        }
    }

    private func reloadAPI() {
        if let loaded = BrokerStore.readConfigFile() {
            api = BrokerAPI(host: loaded.host, port: loaded.port)
            if restartRequired.isEmpty { savedConfig = loaded }
        }
    }

    // MARK: windctl (Python) bridge

    /// Interpreter that runs windctl: config.json's python_executable, else common installs.
    func pythonExecutable() -> String? {
        let candidates = [savedConfig.pythonExecutable, "/opt/anaconda3/bin/python3", "/opt/homebrew/bin/python3",
                          "/usr/local/bin/python3", "/usr/bin/python3"]
        return candidates.first { !$0.isEmpty && FileManager.default.isExecutableFile(atPath: $0) }
    }

    struct CommandResult: Sendable { var code: Int32; var output: String }

    func runWindctl(_ arguments: [String]) async throws -> CommandResult {
        guard let python = pythonExecutable() else {
            throw BrokerAPIError.server(0, "No Python interpreter found. Set python_executable in Configuration.")
        }
        var environment = ProcessInfo.processInfo.environment
        if let bundled = BrokerPaths.bundledPython {
            environment["PYTHONPATH"] = [bundled, environment["PYTHONPATH"]].compactMap { $0 }.joined(separator: ":")
            // Writing __pycache__ inside the signed bundle would break its seal.
            environment["PYTHONPYCACHEPREFIX"] = BrokerPaths.home.appendingPathComponent("pycache").path
        }
        let argv = ["-m", "wind_broker.cli"] + arguments
        return try await Task.detached(priority: .userInitiated) {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: python)
            process.arguments = argv
            process.environment = environment
            let pipe = Pipe()
            process.standardOutput = pipe
            process.standardError = pipe
            try process.run()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            let text = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            return CommandResult(code: process.terminationStatus, output: String(text.suffix(600)))
        }.value
    }

    func openLogFile() {
        NSWorkspace.shared.open(BrokerPaths.log)
    }
}

// MARK: - formatting helpers

enum Format {
    static func uptime(_ seconds: Double?) -> String {
        guard let seconds else { return "—" }
        let total = Int(seconds)
        let days = total / 86_400, hours = (total % 86_400) / 3600, minutes = (total % 3600) / 60
        if days > 0 { return "\(days)d \(hours)h" }
        if hours > 0 { return "\(hours)h \(minutes)m" }
        return "\(minutes)m \(total % 60)s"
    }

    static func timestamp(_ iso: String?) -> String {
        guard let iso, !iso.isEmpty else { return "—" }
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime]
        guard let date = parser.date(from: iso) else { return iso }
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return formatter.string(from: date)
    }

    static func pid(_ value: Int?) -> String { value.map(String.init) ?? "—" }
}
