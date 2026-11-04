import Foundation

// MARK: - Wire models (snake_case JSON from windd)

struct BrokerConfig: Codable, Equatable {
    var host: String = "127.0.0.1"
    var port: Int = 18881
    var unixSocketPath: String = NSString(string: "~/.wind-broker/wind.sock").expandingTildeInPath
    var windModulePath: String = "/Applications/Wind API.app/Contents/python/WindPy.py"
    var pythonExecutable: String = ""
    var startupSeconds: Double = 300
    var requestSeconds: Double = 90
    var autoConnectOnStart: Bool = false
    var historyCacheDir: String? = nil

    init() {}

    init(from decoder: Decoder) throws {
        // Tolerate missing keys so a partial config.json still loads.
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let d = BrokerConfig()
        host = try c.decodeIfPresent(String.self, forKey: .host) ?? d.host
        port = try c.decodeIfPresent(Int.self, forKey: .port) ?? d.port
        unixSocketPath = try c.decodeIfPresent(String.self, forKey: .unixSocketPath) ?? d.unixSocketPath
        windModulePath = try c.decodeIfPresent(String.self, forKey: .windModulePath) ?? d.windModulePath
        pythonExecutable = try c.decodeIfPresent(String.self, forKey: .pythonExecutable) ?? d.pythonExecutable
        startupSeconds = try c.decodeIfPresent(Double.self, forKey: .startupSeconds) ?? d.startupSeconds
        requestSeconds = try c.decodeIfPresent(Double.self, forKey: .requestSeconds) ?? d.requestSeconds
        autoConnectOnStart = try c.decodeIfPresent(Bool.self, forKey: .autoConnectOnStart) ?? d.autoConnectOnStart
        historyCacheDir = try c.decodeIfPresent(String.self, forKey: .historyCacheDir)
    }

    /// JSON body for POST /v1/config and for writing config.json directly.
    var jsonObject: [String: Any] {
        [
            "host": host, "port": port, "unix_socket_path": unixSocketPath,
            "wind_module_path": windModulePath, "python_executable": pythonExecutable,
            "startup_seconds": Int(startupSeconds), "request_seconds": requestSeconds,
            "auto_connect_on_start": autoConnectOnStart,
            "history_cache_dir": (historyCacheDir?.isEmpty ?? true) ? NSNull() : historyCacheDir as Any,
        ]
    }
}

struct BrokerStatus: Codable {
    var version: String?
    var daemonPid: Int?
    var uptimeSeconds: Double?
    var workerPid: Int?
    var workerRestarts: Int?
    var state: String
    var connected: Bool
    var connectedSince: String?
    var lastActiveAt: String?
    var lastError: String?
    var totalRequests: Int
    var failedRequests: Int
    var loginCount: Int?
    var sdkInstalled: Bool?
    var config: BrokerConfig?
}

struct LogEntry: Codable, Identifiable, Hashable {
    var id: Int
    var timestamp: String
    var client: String
    var method: String
    var argsSummary: String
    var rows: Int?
    var cols: Int?
    var latencyMs: Double
    var status: String
    var detail: String

    var time: String {
        // "2026-11-04T10:29:56.123+08:00" -> "10:29:56.123"
        let chars = Array(timestamp)
        return chars.count >= 23 ? String(chars[11..<23]) : timestamp
    }
    var rowsText: String { rows.map(String.init) ?? "" }
    var latencyText: String { String(format: "%.0f ms", latencyMs) }
    var isFailure: Bool { !["ok", "cache", "event"].contains(status) }
}

struct LogsResponse: Codable { var logs: [LogEntry] }

struct PreviewTable: Codable {
    var columns: [String]
    var rows: [[String]]
    var rowCount: Int
    var colCount: Int
    var truncated: Bool
}

struct TestResult: Codable {
    var preset: String
    var method: String
    var source: String
    var latencyMs: Double
    var preview: PreviewTable
}

struct ConfigUpdateResponse: Codable {
    var config: BrokerConfig
    var restartRequired: [String]
}

struct ErrorEnvelope: Codable {
    struct Detail: Codable { var kind: String?; var code: Int?; var message: String }
    var error: Detail?
    var lastError: String?
}

enum BrokerAPIError: LocalizedError {
    case unreachable(String)
    case server(Int, String)

    var errorDescription: String? {
        switch self {
        case .unreachable(let message): return "Daemon unreachable: \(message)"
        case .server(let status, let message): return "\(message) (HTTP \(status))"
        }
    }
}

// MARK: - HTTP client

final class BrokerAPI: @unchecked Sendable {
    let baseURL: URL
    private let session: URLSession
    private let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }()

    init(host: String, port: Int) {
        let shownHost = host.contains(":") ? "[\(host)]" : host
        baseURL = URL(string: "http://\(shownHost):\(port)")!
        let configuration = URLSessionConfiguration.ephemeral
        // windd is loopback-only; never route it through an HTTP(S) proxy.
        configuration.connectionProxyDictionary = [:]
        configuration.timeoutIntervalForRequest = 15
        configuration.timeoutIntervalForResource = 900
        session = URLSession(configuration: configuration)
    }

    private func send<T: Decodable>(_ request: URLRequest) async throws -> T {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw BrokerAPIError.unreachable(error.localizedDescription)
        }
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        if status >= 400 {
            let envelope = try? decoder.decode(ErrorEnvelope.self, from: data)
            let message = envelope?.error?.message ?? envelope?.lastError ?? HTTPURLResponse.localizedString(forStatusCode: status)
            throw BrokerAPIError.server(status, message)
        }
        return try decoder.decode(T.self, from: data)
    }

    func get<T: Decodable>(_ path: String, timeout: TimeInterval = 5) async throws -> T {
        var request = URLRequest(url: baseURL.appendingPathComponent(path))
        request.timeoutInterval = timeout
        request.setValue("WindBroker.app", forHTTPHeaderField: "X-Wind-Client")
        return try await send(request)
    }

    func post<T: Decodable>(_ path: String, body: [String: Any] = [:], timeout: TimeInterval = 30) async throws -> T {
        var request = URLRequest(url: baseURL.appendingPathComponent(path))
        request.httpMethod = "POST"
        request.timeoutInterval = timeout
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("WindBroker.app", forHTTPHeaderField: "X-Wind-Client")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        return try await send(request)
    }

    // Convenience endpoints
    func status() async throws -> BrokerStatus { try await get("v1/status", timeout: 3) }
    func logs(limit: Int = 250) async throws -> [LogEntry] {
        var components = URLComponents(url: baseURL.appendingPathComponent("v1/logs"), resolvingAgainstBaseURL: false)!
        components.queryItems = [URLQueryItem(name: "limit", value: String(limit))]
        var request = URLRequest(url: components.url!)
        request.timeoutInterval = 3
        let response: LogsResponse = try await send(request)
        return response.logs
    }
}

struct Ignored: Codable {}
