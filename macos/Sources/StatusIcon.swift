import AppKit
import Combine
import SwiftUI

/// Menu-bar wombat. The head follows the menu bar's appearance (drawn in
/// `labelColor` at draw time, like a template image), the face shows the
/// session state, and a colored dot in the corner repeats it at a glance.
enum StatusIcon {
    static let size = NSSize(width: 18, height: 18)

    private enum Face { case awake, glancing, meh, asleep, knockedOut }

    private static func face(for state: SessionState) -> Face {
        switch state {
        case .connected: return .awake
        case .connecting: return .glancing
        case .disconnected: return .meh
        case .daemonDown: return .asleep
        case .error: return .knockedOut
        }
    }

    /// - Parameter dotDimmed: second phase of the "Connecting…" pulse.
    static func image(for state: SessionState, dotDimmed: Bool = false) -> NSImage {
        let image = NSImage(size: size, flipped: true) { _ in
            guard let cg = NSGraphicsContext.current?.cgContext else { return false }
            draw(state: state, dotDimmed: dotDimmed, in: cg)
            return true
        }
        image.isTemplate = false  // the dot keeps its color
        image.accessibilityDescription = "WindBroker: \(state.title)"
        return image
    }

    // Geometry is in an 18×18, y-down space (matches the design mockups).
    private static let dotCenter = CGPoint(x: 15, y: 14.6)

    private static func draw(state: SessionState, dotDimmed: Bool, in cg: CGContext) {
        if state == .daemonDown { cg.setAlpha(0.45) }

        // Head with the face and the dot's gap cut out. The transparency layer
        // keeps the .clear blend mode from punching through the menu bar itself.
        cg.beginTransparencyLayer(auxiliaryInfo: nil)
        // One path, one fill: labelColor is translucent, so separate fills
        // would darken where the ears overlap the head.
        cg.setFillColor(NSColor.labelColor.cgColor)
        cg.addEllipse(in: circle(3.5, 5.6, r: 1.9))
        cg.addEllipse(in: circle(14.5, 5.6, r: 1.9))
        cg.addEllipse(in: CGRect(x: 9 - 7.8, y: 10.6 - 5.9, width: 15.6, height: 11.8))
        cg.fillPath()

        cg.setBlendMode(.clear)
        cg.fillEllipse(in: CGRect(x: 9 - 2.9, y: 12.4 - 1.8, width: 5.8, height: 3.6))  // nose
        drawEyes(face(for: state), in: cg)
        cg.fillEllipse(in: circle(dotCenter.x, dotCenter.y, r: 3.9))  // gap around the dot
        cg.endTransparencyLayer()

        // Status dot (hollow ring when the daemon is stopped).
        let color = state == .daemonDown ? NSColor.systemGray : state.nsColor
        let dot = color.withAlphaComponent(dotDimmed ? 0.35 : 1).cgColor
        if state == .daemonDown {
            cg.setStrokeColor(dot)
            cg.setLineWidth(1.1)
            cg.strokeEllipse(in: circle(dotCenter.x, dotCenter.y, r: 2.3))
        } else {
            cg.setFillColor(dot)
            cg.fillEllipse(in: circle(dotCenter.x, dotCenter.y, r: 2.7))
        }
    }

    /// Called with blend mode .clear: everything drawn here becomes a hole.
    private static func drawEyes(_ face: Face, in cg: CGContext) {
        cg.setLineCap(.round)
        switch face {
        case .awake:
            cg.fillEllipse(in: circle(6.1, 9.4, r: 1.05))
            cg.fillEllipse(in: circle(11.9, 9.4, r: 1.05))
        case .glancing:
            cg.fillEllipse(in: circle(6.8, 8.9, r: 1.05))
            cg.fillEllipse(in: circle(12.6, 8.9, r: 1.05))
        case .meh:
            for x in [5.0, 10.8] {
                cg.addPath(CGPath(roundedRect: CGRect(x: x, y: 9.0, width: 2.2, height: 0.9),
                                  cornerWidth: 0.45, cornerHeight: 0.45, transform: nil))
            }
            cg.fillPath()
        case .asleep:
            cg.setLineWidth(0.95)
            for x in [5.0, 10.8] {
                cg.move(to: CGPoint(x: x, y: 9.1))
                cg.addQuadCurve(to: CGPoint(x: x + 2.2, y: 9.1), control: CGPoint(x: x + 1.1, y: 10.3))
            }
            cg.strokePath()
        case .knockedOut:
            cg.setLineWidth(0.9)
            for x in [5.2, 11.0] {
                cg.move(to: CGPoint(x: x, y: 8.4)); cg.addLine(to: CGPoint(x: x + 1.8, y: 10.2))
                cg.move(to: CGPoint(x: x + 1.8, y: 8.4)); cg.addLine(to: CGPoint(x: x, y: 10.2))
            }
            cg.strokePath()
        }
    }

    private static func circle(_ x: CGFloat, _ y: CGFloat, r: CGFloat) -> CGRect {
        CGRect(x: x - r, y: y - r, width: 2 * r, height: 2 * r)
    }
}

/// Menu-bar label: the wombat, with the dot pulsing while connecting.
struct StatusLabel: View {
    @EnvironmentObject private var store: BrokerStore
    @State private var dotDimmed = false
    private let pulse = Timer.publish(every: 0.7, on: .main, in: .common).autoconnect()

    var body: some View {
        Image(nsImage: StatusIcon.image(for: store.state, dotDimmed: store.state == .connecting && dotDimmed))
            .accessibilityLabel("WindBroker: \(store.state.title)")
            .onReceive(pulse) { _ in
                if store.state == .connecting { dotDimmed.toggle() } else if dotDimmed { dotDimmed = false }
            }
    }
}
