import AppKit
import SwiftUI

struct ControlPanelView: View {
    @EnvironmentObject private var store: BrokerStore

    var body: some View {
        TabView {
            OverviewTab().tabItem { Label("Overview & Session", systemImage: "gauge.with.dots.needle.50percent") }
            LogsTab().tabItem { Label("Live Requests", systemImage: "list.bullet.rectangle") }
            TestConsoleTab().tabItem { Label("Test Console", systemImage: "play.rectangle") }
            ConfigTab().tabItem { Label("Configuration", systemImage: "gearshape") }
            RouterTab().tabItem { Label("Router", systemImage: "network") }
        }
        .padding()
        .onAppear(perform: activateApp)
    }
}

// MARK: - Overview

private struct StatusCard: View {
    let title: String
    let value: String
    var tint: Color = .primary

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            Text(value).font(.title3.weight(.semibold)).foregroundStyle(tint).lineLimit(1).minimumScaleFactor(0.6)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 10))
    }
}

struct OverviewTab: View {
    @EnvironmentObject private var store: BrokerStore
    @State private var confirmReauth = false
    @State private var confirmLogout = false

    private let columns = [GridItem(.adaptive(minimum: 190), spacing: 12)]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                HStack {
                    Text("Wind session").font(.title2.bold())
                    StateBadge(state: store.state)
                    Spacer()
                    if let action = store.busyAction {
                        ProgressView().controlSize(.small)
                        Text(action).foregroundStyle(.secondary)
                    }
                }
                if let banner = store.banner {
                    Text(banner).foregroundStyle(store.bannerIsError ? .red : .secondary)
                }

                let status = store.status
                LazyVGrid(columns: columns, spacing: 12) {
                    StatusCard(title: "Daemon PID", value: Format.pid(status?.daemonPid))
                    StatusCard(title: "Worker PID", value: Format.pid(status?.workerPid))
                    StatusCard(title: "Session state", value: store.state.title, tint: store.state.color)
                    StatusCard(title: "Connected since", value: Format.timestamp(status?.connectedSince))
                    StatusCard(title: "Uptime", value: Format.uptime(status?.uptimeSeconds))
                    StatusCard(title: "Last active", value: Format.timestamp(status?.lastActiveAt))
                    StatusCard(title: "Total requests", value: status.map { String($0.totalRequests) } ?? "—")
                    StatusCard(title: "Failed requests", value: status.map { String($0.failedRequests) } ?? "—",
                               tint: (status?.failedRequests ?? 0) > 0 ? .orange : .primary)
                    StatusCard(title: "Wind logins", value: status?.loginCount.map(String.init) ?? "—")
                    StatusCard(title: "Worker restarts", value: status?.workerRestarts.map(String.init) ?? "—")
                    StatusCard(title: "WindPy SDK", value: status?.sdkInstalled == true ? "Installed" : (status == nil ? "—" : "Not found"),
                               tint: status?.sdkInstalled == false ? .red : .primary)
                    StatusCard(title: "Version", value: status?.version ?? "—")
                }

                if let error = store.status?.lastError {
                    GroupBox("Last error") {
                        Text(error).font(.callout.monospaced()).textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }

                GroupBox("Session") {
                    HStack {
                        Button {
                            if store.state == .connected { confirmReauth = true } else { Task { await store.login() } }
                        } label: {
                            Label(store.state == .connected ? "Re-authenticate" : "Login (Wind QR)", systemImage: "qrcode.viewfinder")
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(!store.daemonRunning || store.state == .connecting || store.busyAction != nil)
                        Button("Disconnect Session") { confirmLogout = true }
                            .disabled(!store.daemonRunning || store.state == .disconnected || store.busyAction != nil)
                        Spacer()
                        Text("The QR window appears once; afterwards every app reuses this login.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    .padding(6)
                }

                GroupBox("Daemon (windd)") {
                    VStack(alignment: .leading, spacing: 10) {
                        HStack {
                            Button("Start Daemon") { Task { await store.startDaemon() } }
                                .disabled(store.daemonRunning || store.busyAction != nil)
                            Button("Stop Daemon") { Task { await store.stopDaemon() } }
                                .disabled(!store.daemonRunning || store.busyAction != nil)
                            Button("Restart Daemon") { Task { await store.restartDaemon() } }
                                .disabled(store.busyAction != nil)
                            Spacer()
                            Button("Open windd.log") { store.openLogFile() }
                        }
                        Toggle(isOn: Binding(
                            get: { store.launchAgentInstalled },
                            set: { enabled in Task { await store.setLaunchAtLogin(enabled) } }
                        )) {
                            VStack(alignment: .leading) {
                                Text("Launch windd at login")
                                Text(BrokerPaths.launchAgent.path).font(.caption.monospaced()).foregroundStyle(.secondary)
                            }
                        }
                        .toggleStyle(.switch)
                        .disabled(store.busyAction != nil)
                    }
                    .padding(6)
                }
            }
            .padding()
        }
        .confirmationDialog("Re-authenticate Wind?", isPresented: $confirmReauth) {
            Button("Log out and log in again", role: .destructive) { Task { await store.reauthenticate() } }
        } message: {
            Text("This calls w.stop() and starts a fresh login. All apps using WindBroker lose the session until you finish the QR scan.")
        }
        .confirmationDialog("Disconnect the Wind session?", isPresented: $confirmLogout) {
            Button("Disconnect", role: .destructive) { Task { await store.logout() } }
        } message: {
            Text("Every app on this Mac that uses WindBroker will be disconnected. The next request logs in again.")
        }
    }
}

// MARK: - Logs

struct LogsTab: View {
    @EnvironmentObject private var store: BrokerStore
    @State private var search = ""
    @State private var filter = "all"
    @State private var selection: LogEntry.ID?

    private var filtered: [LogEntry] {
        store.logs.filter { entry in
            let statusMatch: Bool
            switch filter {
            case "ok": statusMatch = !entry.isFailure && entry.status != "event"
            case "failed": statusMatch = entry.isFailure
            case "events": statusMatch = entry.status == "event"
            default: statusMatch = true
            }
            guard statusMatch else { return false }
            guard !search.isEmpty else { return true }
            let needle = search.lowercased()
            return [entry.client, entry.method, entry.argsSummary, entry.status, entry.detail]
                .contains { $0.lowercased().contains(needle) }
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                TextField("Search client, method, args, detail…", text: $search)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 360)
                Picker("", selection: $filter) {
                    Text("All").tag("all")
                    Text("OK").tag("ok")
                    Text("Failed").tag("failed")
                    Text("Events").tag("events")
                }
                .pickerStyle(.segmented)
                .frame(width: 280)
                Spacer()
                Toggle("Auto-refresh", isOn: $store.autoRefreshLogs)
                Button { Task { await store.refreshLogs() } } label: { Label("Refresh", systemImage: "arrow.clockwise") }
                Button(role: .destructive) { Task { await store.clearLogs() } } label: { Label("Clear", systemImage: "trash") }
                    .disabled(!store.daemonRunning)
            }
            Table(filtered, selection: $selection) {
                TableColumn("Time") { Text($0.time).monospacedDigit() }.width(min: 90, ideal: 95)
                TableColumn("Client", value: \.client).width(min: 90, ideal: 140)
                TableColumn("Method") { Text($0.method).bold() }.width(min: 50, ideal: 60)
                TableColumn("Args", value: \.argsSummary).width(min: 160, ideal: 300)
                TableColumn("Latency") { Text($0.latencyText).monospacedDigit() }.width(min: 60, ideal: 70)
                TableColumn("Rows") { Text($0.rowsText).monospacedDigit() }.width(min: 40, ideal: 45)
                TableColumn("Status") { entry in
                    Text(entry.status)
                        .foregroundStyle(entry.isFailure ? .red : (entry.status == "event" ? .blue : .green))
                }
                .width(min: 70, ideal: 90)
                TableColumn("Detail", value: \.detail).width(min: 120, ideal: 260)
            }
            if let selected = filtered.first(where: { $0.id == selection }) {
                GroupBox {
                    Text("\(selected.timestamp)  \(selected.client)  \(selected.method)(\(selected.argsSummary))\n\(selected.detail)")
                        .font(.caption.monospaced())
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            Text("\(filtered.count) of \(store.logs.count) entries · ring buffer keeps the latest 250")
                .font(.caption).foregroundStyle(.secondary)
        }
        .padding()
    }
}

// MARK: - Test console

struct TestConsoleTab: View {
    @EnvironmentObject private var store: BrokerStore
    @State private var method = "wsd"
    @State private var argsText = "000300.SH\nclose\n2024-01-02\n2024-01-10\nFill=Blank"

    private let methods = ["wsd", "wss", "wsq", "wsi", "wset", "tdays", "tdaysoffset", "tdayscount", "edb", "wst", "wses", "wsee"]

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            GroupBox("Presets") {
                HStack {
                    Button("Live Index Quotes (WSQ)") { Task { await store.runTest(preset: "spot_live") } }
                    Button("Daily Close (WSD)") { Task { await store.runTest(preset: "spot_daily") } }
                    Button("Trading Days (TDAYS)") { Task { await store.runTest(preset: "tdays") } }
                    Spacer()
                    Text("000300.SH · 000016.SH · 000905.SH · 000852.SH").font(.caption).foregroundStyle(.secondary)
                }
                .disabled(store.testRunning || !store.daemonRunning)
                .padding(6)
            }
            GroupBox("Custom query") {
                HStack(alignment: .top, spacing: 12) {
                    VStack(alignment: .leading) {
                        Picker("Method", selection: $method) {
                            ForEach(methods, id: \.self) { Text($0).tag($0) }
                        }
                        .frame(width: 180)
                        Button {
                            let args = argsText.split(separator: "\n", omittingEmptySubsequences: false)
                                .map { $0.trimmingCharacters(in: .whitespaces) }
                            let trimmed = Array(args.reversed().drop(while: { $0.isEmpty }).reversed())
                            Task { await store.runTest(preset: "custom", method: method, args: trimmed) }
                        } label: {
                            Label("Run", systemImage: "play.fill")
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(store.testRunning || !store.daemonRunning)
                    }
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Arguments: one positional argument per line, passed verbatim to w.\(method)(…)")
                            .font(.caption).foregroundStyle(.secondary)
                        TextEditor(text: $argsText)
                            .font(.body.monospaced())
                            .frame(height: 96)
                            .overlay(RoundedRectangle(cornerRadius: 6).stroke(.quaternary))
                    }
                }
                .padding(6)
            }
            resultView
            Spacer(minLength: 0)
        }
        .padding()
    }

    @ViewBuilder private var resultView: some View {
        if store.testRunning {
            HStack { ProgressView().controlSize(.small); Text("Running… (first request may wait for the Wind QR login)") }
        } else if let error = store.testError {
            Label(error, systemImage: "xmark.octagon").foregroundStyle(.red).textSelection(.enabled)
        } else if let result = store.testResult {
            VStack(alignment: .leading, spacing: 8) {
                Text("\(result.method) · \(result.preview.rowCount) rows × \(result.preview.colCount) cols · \(String(format: "%.0f", result.latencyMs)) ms · source: \(result.source)\(result.preview.truncated ? " · first 50 rows" : "")")
                    .font(.callout).foregroundStyle(.secondary)
                ScrollView([.horizontal, .vertical]) {
                    Grid(alignment: .leading, horizontalSpacing: 18, verticalSpacing: 6) {
                        GridRow {
                            ForEach(Array(result.preview.columns.enumerated()), id: \.offset) { _, column in
                                Text(column.isEmpty ? "index" : column).bold()
                            }
                        }
                        Divider()
                        ForEach(Array(result.preview.rows.enumerated()), id: \.offset) { _, row in
                            GridRow {
                                ForEach(Array(row.enumerated()), id: \.offset) { index, cell in
                                    Text(cell).foregroundStyle(index == 0 ? .secondary : .primary)
                                }
                            }
                        }
                    }
                    .font(.callout.monospaced())
                    .textSelection(.enabled)
                    .padding(10)
                }
                .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 8))
            }
        } else {
            Text("Run a preset to verify entitlements and connectivity.").foregroundStyle(.secondary)
        }
    }
}

// MARK: - Configuration

struct ConfigTab: View {
    @EnvironmentObject private var store: BrokerStore

    var body: some View {
        Form {
            Section("Network") {
                TextField("Host", text: $store.config.host)
                TextField("Port", value: $store.config.port, format: .number.grouping(.never))
                TextField("Unix socket path", text: $store.config.unixSocketPath)
            }
            Section("Wind SDK") {
                pathField("WindPy module", text: $store.config.windModulePath)
                pathField("Python executable (with pandas)", text: $store.config.pythonExecutable)
                TextField("Login wait (startup_seconds)", value: $store.config.startupSeconds, format: .number)
                TextField("Request deadline (request_seconds)", value: $store.config.requestSeconds, format: .number)
                Toggle("Connect to Wind automatically when windd starts", isOn: $store.config.autoConnectOnStart)
                TextField("History cache directory (optional)", text: Binding(
                    get: { store.config.historyCacheDir ?? "" },
                    set: { store.config.historyCacheDir = $0.isEmpty ? nil : $0 }
                ))
            }
            Section {
                HStack {
                    Button("Save") { Task { await store.saveConfig() } }
                        .buttonStyle(.borderedProminent)
                        .disabled(store.config == store.savedConfig || store.busyAction != nil)
                    Button("Revert") { store.reloadConfig() }
                    if !store.restartRequired.isEmpty {
                        Button("Restart Daemon to Apply") { Task { await store.restartDaemon() } }
                    }
                    Spacer()
                    Text(BrokerPaths.config.path).font(.caption.monospaced()).foregroundStyle(.secondary)
                }
                if let banner = store.banner {
                    Text(banner).font(.caption).foregroundStyle(store.bannerIsError ? .red : .secondary)
                }
                Text("Timeouts and the history cache apply immediately. Host, port, socket, SDK path and Python changes apply after a daemon restart, which re-runs the Wind login.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .onAppear { if store.config == store.savedConfig { store.reloadConfig() } }
    }

    private func pathField(_ title: String, text: Binding<String>) -> some View {
        HStack {
            TextField(title, text: text)
            Button("Choose…") {
                let panel = NSOpenPanel()
                panel.canChooseFiles = true
                panel.canChooseDirectories = false
                panel.treatsFilePackagesAsDirectories = true
                panel.showsHiddenFiles = true
                if !text.wrappedValue.isEmpty {
                    panel.directoryURL = URL(fileURLWithPath: text.wrappedValue).deletingLastPathComponent()
                }
                if panel.runModal() == .OK, let url = panel.url { text.wrappedValue = url.path }
            }
        }
    }
}


// MARK: - Router (LAN sharing)

struct RouterTab: View {
    @EnvironmentObject private var store: BrokerStore
    @State private var host = ""
    @State private var port = 18882
    @State private var maxInflight = 2
    @State private var loaded = false
    @State private var newName = ""
    @State private var revoking: RouterClient?

    private var info: RouterInfo? { store.routerInfo }
    private var dirty: Bool {
        guard let info else { return false }
        return host != info.host || port != store.config.routerPort || maxInflight != info.maxInflight
    }
    /// info.port is the bound port while listening, else the configured one.
    private var shownPort: Int { info?.port ?? port }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                HStack {
                    Text("LAN router").font(.title2.bold())
                    Spacer()
                    if let action = store.busyAction {
                        ProgressView().controlSize(.small)
                        Text(action).foregroundStyle(.secondary)
                    }
                }
                if let banner = store.banner {
                    Text(banner).foregroundStyle(store.bannerIsError ? .red : .secondary)
                }

                GroupBox("Sharing") {
                    VStack(alignment: .leading, spacing: 10) {
                        Toggle("Share this Wind session with workstations on the LAN", isOn: Binding(
                            get: { info?.enabled ?? false },
                            set: { enabled in Task { await store.setRouterEnabled(enabled) } }
                        ))
                        .toggleStyle(.switch)
                        .disabled(!store.daemonRunning || store.busyAction != nil)
                        Label("Sharing Wind data with other computers must be within your Wind agreement and entitlement. The router speaks plain HTTP and is for a trusted LAN only; do not expose it to the internet.",
                              systemImage: "exclamationmark.triangle.fill")
                            .font(.callout.weight(.medium))
                            .foregroundStyle(.orange)
                            .fixedSize(horizontal: false, vertical: true)
                        if let info {
                            HStack(spacing: 6) {
                                Circle().fill(info.listening ? Color.green : Color.gray).frame(width: 9, height: 9)
                                Text(info.listening ? "Listening on \(info.host):\(info.port)" : (info.enabled ? "Enabled, not listening" : "Disabled"))
                            }
                            if let error = info.error, !error.isEmpty {
                                Label(error, systemImage: "xmark.octagon").foregroundStyle(.red).textSelection(.enabled)
                            }
                        } else {
                            Text(store.daemonRunning ? "Loading…" : "Start the daemon to manage the router.")
                                .foregroundStyle(.secondary)
                        }
                    }
                    .padding(6)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }

                GroupBox("Listener") {
                    VStack(alignment: .leading, spacing: 10) {
                        HStack {
                            TextField("Host", text: $host).frame(maxWidth: 200)
                            TextField("Port", value: $port, format: .number.grouping(.never)).frame(width: 90)
                            TextField("Max in-flight", value: $maxInflight, format: .number.grouping(.never)).frame(width: 110)
                            Button("Apply") {
                                Task { await store.saveRouter(enabled: info?.enabled ?? false, host: host, port: port, maxInflight: maxInflight) }
                            }
                            .buttonStyle(.borderedProminent)
                            .disabled(!dirty || !store.daemonRunning || store.busyAction != nil)
                            Spacer()
                        }
                        Text("Host 0.0.0.0 listens on every interface. Max in-flight is the number of simultaneous Wind queries allowed per workstation. Changes apply live.")
                            .font(.caption).foregroundStyle(.secondary)
                        if let info, !info.addresses.isEmpty {
                            Text("Workstations connect to").font(.caption).foregroundStyle(.secondary)
                            Text(info.addresses.map { routerURL($0, port: shownPort) }.joined(separator: "\n"))
                                .font(.callout.monospaced())
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                    .padding(6)
                }

                GroupBox("Workstations") {
                    VStack(alignment: .leading, spacing: 10) {
                        Table(info?.clients ?? []) {
                            TableColumn("Name", value: \.name).width(min: 90, ideal: 130)
                            TableColumn("Created") { Text(Format.timestamp($0.createdAt)).monospacedDigit() }.width(min: 110, ideal: 140)
                            TableColumn("Last seen") { Text(Format.timestamp($0.lastSeenAt)).monospacedDigit() }.width(min: 110, ideal: 140)
                            TableColumn("Last address") { Text($0.lastAddr ?? "—") }.width(min: 90, ideal: 120)
                            TableColumn("Requests") { Text(String($0.requests)).monospacedDigit() }.width(min: 60, ideal: 70)
                            TableColumn("In-flight") { Text(String($0.inflight)).monospacedDigit() }.width(min: 60, ideal: 70)
                            TableColumn("") { client in
                                Button("Revoke", role: .destructive) { revoking = client }
                                    .disabled(store.busyAction != nil)
                            }
                            .width(min: 70, ideal: 80)
                        }
                        .frame(minHeight: 140, idealHeight: 180)
                        HStack {
                            TextField("New workstation name (e.g. alice-pc)", text: $newName)
                                .textFieldStyle(.roundedBorder)
                                .frame(maxWidth: 300)
                                .onSubmit(addClient)
                            Button { addClient() } label: { Label("Add workstation", systemImage: "plus") }
                                .disabled(newName.trimmingCharacters(in: .whitespaces).isEmpty || !store.daemonRunning || store.busyAction != nil)
                            Spacer()
                        }
                        Text("Names: letters, digits, dot, dash, underscore (max 40). Each workstation gets its own key; revoke it here to cut it off immediately.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    .padding(6)
                }
            }
            .padding()
        }
        .task {
            // Refresh while the tab is on screen; the task is cancelled when it disappears.
            while !Task.isCancelled {
                await store.refreshRouter()
                if let info = store.routerInfo, !loaded {
                    host = info.host
                    port = store.config.routerPort
                    maxInflight = info.maxInflight
                    loaded = true
                }
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
        .confirmationDialog("Revoke \(revoking?.name ?? "")?", isPresented: Binding(
            get: { revoking != nil }, set: { if !$0 { revoking = nil } }
        )) {
            Button("Revoke", role: .destructive) {
                if let client = revoking { Task { await store.revokeRouterClient(client.name) } }
                revoking = nil
            }
        } message: {
            Text("The workstation's key stops working immediately and its open connections are closed.")
        }
        .sheet(item: $store.newRouterKey) { created in
            NewKeySheet(created: created, url: routerURL(info?.addresses.first ?? info?.hostname ?? "<hub-address>", port: shownPort))
        }
    }

    private func addClient() {
        let name = newName.trimmingCharacters(in: .whitespaces)
        guard !name.isEmpty else { return }
        newName = ""
        Task { await store.addRouterClient(name) }
    }

    private func routerURL(_ address: String, port: Int) -> String {
        let shown = address.contains(":") ? "[\(address)]" : address
        return "http://\(shown):\(port)"
    }
}

extension RouterClientCreated: Identifiable {
    var id: String { name }
}

private struct NewKeySheet: View {
    @Environment(\.dismiss) private var dismiss
    let created: RouterClientCreated
    let url: String

    private var snippet: String { "WIND_BROKER_URL=\(url)\nWIND_BROKER_KEY=\(created.key)" }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Key for \(created.name)").font(.title3.bold())
            Label("This key is shown only once and will not be shown again. Copy it now; if it is lost, revoke the workstation and add it again.",
                  systemImage: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
                .fixedSize(horizontal: false, vertical: true)
            Text("Set these on the workstation:").font(.caption).foregroundStyle(.secondary)
            Text(snippet)
                .font(.callout.monospaced())
                .textSelection(.enabled)
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 8))
            HStack {
                Button {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(snippet, forType: .string)
                } label: { Label("Copy", systemImage: "doc.on.doc") }
                Spacer()
                Button("Done") { dismiss() }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(20)
        .frame(width: 520)
    }
}
