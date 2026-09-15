import AppKit

// Reproducible branded installer background for the Mountain Turtle DMG.
// Finder places the real app icon and Applications alias over the two soft cards.

guard CommandLine.arguments.count == 3 else {
    fputs("Usage: swift generate-dmg-background.swift /path/to/project /path/to/background.png\n", stderr)
    exit(2)
}

let output = URL(fileURLWithPath: CommandLine.arguments[2])
let width: CGFloat = 900
let height: CGFloat = 520
let scale = NSScreen.main?.backingScaleFactor ?? 2
let pixelsWide = Int(width * scale)
let pixelsHigh = Int(height * scale)

guard let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixelsWide, pixelsHigh: pixelsHigh,
                                    bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                    isPlanar: false, colorSpaceName: .deviceRGB,
                                    bytesPerRow: 0, bitsPerPixel: 0) else {
    fputs("Could not create bitmap\n", stderr)
    exit(1)
}
bitmap.size = NSSize(width: width, height: height)

func color(_ red: CGFloat, _ green: CGFloat, _ blue: CGFloat, _ alpha: CGFloat = 1) -> NSColor {
    NSColor(calibratedRed: red, green: green, blue: blue, alpha: alpha)
}

let cream = color(0.96, 0.97, 0.91)
let mist = color(0.78, 0.88, 0.79)
let sky = color(0.87, 0.93, 0.82)
let teal = color(0.12, 0.33, 0.29)
let moss = color(0.22, 0.48, 0.37)
let pine = color(0.09, 0.27, 0.25)
let clay = color(0.66, 0.48, 0.34)
let water = color(0.70, 0.88, 0.82)
let snow = color(0.98, 0.98, 0.91)
let appBlue = color(0.17, 0.58, 0.93)

func drawText(_ text: String, in rect: NSRect, size: CGFloat, weight: NSFont.Weight,
              color: NSColor, alignment: NSTextAlignment = .center, tracking: CGFloat = 0) {
    let paragraph = NSMutableParagraphStyle()
    paragraph.alignment = alignment
    let attributes: [NSAttributedString.Key: Any] = [
        .font: NSFont.systemFont(ofSize: size, weight: weight),
        .foregroundColor: color,
        .paragraphStyle: paragraph,
        .kern: tracking,
    ]
    text.draw(with: rect, options: [.usesLineFragmentOrigin, .usesFontLeading], attributes: attributes)
}

func roundedRect(_ rect: NSRect, radius: CGFloat, fill: NSColor, stroke: NSColor? = nil, lineWidth: CGFloat = 1) {
    let path = NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius)
    fill.setFill()
    path.fill()
    if let stroke {
        stroke.setStroke()
        path.lineWidth = lineWidth
        path.stroke()
    }
}

func polygon(_ points: [NSPoint], fill: NSColor) {
    guard let first = points.first else { return }
    let path = NSBezierPath()
    path.move(to: first)
    for point in points.dropFirst() {
        path.line(to: point)
    }
    path.close()
    fill.setFill()
    path.fill()
}

func line(_ points: [NSPoint], color: NSColor, width: CGFloat, cap: NSBezierPath.LineCapStyle = .round) {
    guard let first = points.first else { return }
    let path = NSBezierPath()
    path.move(to: first)
    for point in points.dropFirst() {
        path.line(to: point)
    }
    color.setStroke()
    path.lineWidth = width
    path.lineCapStyle = cap
    path.lineJoinStyle = .round
    path.stroke()
}

func tree(x: CGFloat, y: CGFloat, scale: CGFloat, color: NSColor) {
    polygon([
        NSPoint(x: x, y: y + 78 * scale),
        NSPoint(x: x - 31 * scale, y: y + 22 * scale),
        NSPoint(x: x - 14 * scale, y: y + 24 * scale),
        NSPoint(x: x - 42 * scale, y: y - 30 * scale),
        NSPoint(x: x + 42 * scale, y: y - 30 * scale),
        NSPoint(x: x + 14 * scale, y: y + 24 * scale),
        NSPoint(x: x + 31 * scale, y: y + 22 * scale),
    ], fill: color)
    roundedRect(NSRect(x: x - 7 * scale, y: y - 44 * scale, width: 14 * scale, height: 28 * scale),
                radius: 3 * scale, fill: color)
}

func cloud(x: CGFloat, y: CGFloat, scale: CGFloat, alpha: CGFloat) {
    let cloudColor = NSColor.white.withAlphaComponent(alpha)
    NSBezierPath(ovalIn: NSRect(x: x, y: y, width: 94 * scale, height: 42 * scale)).fill(with: cloudColor)
    NSBezierPath(ovalIn: NSRect(x: x + 34 * scale, y: y + 16 * scale, width: 70 * scale, height: 52 * scale)).fill(with: cloudColor)
    NSBezierPath(ovalIn: NSRect(x: x + 84 * scale, y: y + 6 * scale, width: 86 * scale, height: 42 * scale)).fill(with: cloudColor)
}

extension NSBezierPath {
    func fill(with color: NSColor) {
        color.setFill()
        fill()
    }
}

NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
NSGraphicsContext.current?.cgContext.scaleBy(x: scale, y: scale)

let bg = NSGradient(starting: sky, ending: cream)
bg?.draw(in: NSRect(x: 0, y: 0, width: width, height: height), angle: 90)

// Soft sun and clouds.
NSBezierPath(ovalIn: NSRect(x: 706, y: 332, width: 88, height: 88)).fill(with: NSColor.white.withAlphaComponent(0.48))
cloud(x: 20, y: 290, scale: 0.78, alpha: 0.28)
cloud(x: 96, y: 270, scale: 0.78, alpha: 0.25)
cloud(x: 720, y: 285, scale: 0.82, alpha: 0.28)
cloud(x: 810, y: 392, scale: 0.55, alpha: 0.27)
cloud(x: 245, y: 376, scale: 0.45, alpha: 0.23)

// Mountain layers.
polygon([
    NSPoint(x: -20, y: 236), NSPoint(x: 48, y: 280), NSPoint(x: 86, y: 242),
    NSPoint(x: 124, y: 258), NSPoint(x: 180, y: 226), NSPoint(x: 242, y: 276),
    NSPoint(x: 286, y: 236), NSPoint(x: 342, y: 300), NSPoint(x: 392, y: 256),
    NSPoint(x: 448, y: 322), NSPoint(x: 500, y: 270), NSPoint(x: 552, y: 300),
    NSPoint(x: 610, y: 246), NSPoint(x: 688, y: 282), NSPoint(x: 762, y: 236),
    NSPoint(x: 842, y: 258), NSPoint(x: 920, y: 234), NSPoint(x: 920, y: 0),
    NSPoint(x: -20, y: 0)
], fill: color(0.40, 0.67, 0.61, 0.34))

polygon([
    NSPoint(x: 180, y: 198), NSPoint(x: 272, y: 278), NSPoint(x: 330, y: 248),
    NSPoint(x: 430, y: 352), NSPoint(x: 500, y: 230), NSPoint(x: 572, y: 282),
    NSPoint(x: 660, y: 204), NSPoint(x: 920, y: 204), NSPoint(x: 920, y: 0),
    NSPoint(x: 180, y: 0)
], fill: color(0.33, 0.61, 0.55, 0.44))

polygon([
    NSPoint(x: 302, y: 217), NSPoint(x: 430, y: 374), NSPoint(x: 526, y: 214)
], fill: color(0.31, 0.57, 0.52, 0.62))
polygon([
    NSPoint(x: 430, y: 374), NSPoint(x: 400, y: 273), NSPoint(x: 446, y: 304), NSPoint(x: 476, y: 249),
    NSPoint(x: 526, y: 214)
], fill: color(0.18, 0.40, 0.36, 0.24))
polygon([
    NSPoint(x: 398, y: 336), NSPoint(x: 430, y: 374), NSPoint(x: 463, y: 320),
    NSPoint(x: 444, y: 331), NSPoint(x: 424, y: 304)
], fill: snow.withAlphaComponent(0.86))
polygon([
    NSPoint(x: 472, y: 296), NSPoint(x: 502, y: 238), NSPoint(x: 468, y: 270)
], fill: snow.withAlphaComponent(0.72))

// Lake and reflections.
let lake = NSBezierPath()
lake.move(to: NSPoint(x: 0, y: 154))
lake.curve(to: NSPoint(x: 900, y: 162), controlPoint1: NSPoint(x: 250, y: 178), controlPoint2: NSPoint(x: 610, y: 130))
lake.line(to: NSPoint(x: 900, y: 0))
lake.line(to: NSPoint(x: 0, y: 0))
lake.close()
water.withAlphaComponent(0.55).setFill()
lake.fill()
line([NSPoint(x: 0, y: 162), NSPoint(x: 900, y: 164)], color: NSColor.white.withAlphaComponent(0.70), width: 2)

for (x, w, a) in [(104.0, 166.0, 0.28), (330.0, 188.0, 0.20), (568.0, 168.0, 0.18)] {
    roundedRect(NSRect(x: x, y: 42 + CGFloat(Int(x) % 38), width: w, height: 8), radius: 4,
                fill: NSColor.white.withAlphaComponent(CGFloat(a)))
}

// Forest silhouettes.
for item in [(26.0, 142.0, 1.0), (74.0, 112.0, 0.78), (850.0, 108.0, 0.92), (808.0, 66.0, 0.62), (126.0, 48.0, 0.62)] {
    tree(x: item.0, y: item.1, scale: item.2, color: pine.withAlphaComponent(0.88))
}
for index in 0..<46 {
    let x = CGFloat(index) * 18 + 2
    let s = CGFloat(0.28 + Double((index * 37) % 19) / 100.0)
    tree(x: x, y: 182 - CGFloat((index * 11) % 26), scale: s, color: moss.withAlphaComponent(0.30))
}

// Title treatment.
drawText("Mountain Turtle", in: NSRect(x: 60, y: 420, width: 780, height: 56), size: 43, weight: .heavy,
         color: teal.withAlphaComponent(0.97))
drawText("SLOWER DAYS.  HIGHER PLACES.", in: NSRect(x: 60, y: 388, width: 780, height: 26), size: 16,
         weight: .medium, color: moss.withAlphaComponent(0.75), tracking: 5)

// Icon landing cards; Finder's real icons sit over these.
roundedRect(NSRect(x: 129, y: 182, width: 152, height: 152), radius: 30,
            fill: NSColor.white.withAlphaComponent(0.70), stroke: NSColor.white.withAlphaComponent(0.80), lineWidth: 2)
roundedRect(NSRect(x: 619, y: 182, width: 152, height: 152), radius: 30,
            fill: NSColor.white.withAlphaComponent(0.38), stroke: NSColor.white.withAlphaComponent(0.28), lineWidth: 1)

for (letter, centerX) in [("A", CGFloat(148)), ("B", CGFloat(638))] {
    NSBezierPath(ovalIn: NSRect(x: centerX - 13, y: 314, width: 26, height: 26)).fill(with: clay.withAlphaComponent(0.88))
    drawText(letter, in: NSRect(x: centerX - 13, y: 318, width: 26, height: 18), size: 13,
             weight: .bold, color: NSColor.white)
}

// Extra folder glow on the Applications side so the target reads clearly under Finder's alias icon.
roundedRect(NSRect(x: 619, y: 202, width: 152, height: 100), radius: 15,
            fill: appBlue.withAlphaComponent(0.10))
roundedRect(NSRect(x: 638, y: 297, width: 58, height: 22), radius: 11,
            fill: appBlue.withAlphaComponent(0.12))

// Main arrow.
line([NSPoint(x: 354, y: 257), NSPoint(x: 492, y: 257)], color: moss.withAlphaComponent(0.78), width: 12)
line([NSPoint(x: 492, y: 257), NSPoint(x: 462, y: 286)], color: moss.withAlphaComponent(0.78), width: 12)
line([NSPoint(x: 492, y: 257), NSPoint(x: 462, y: 228)], color: moss.withAlphaComponent(0.78), width: 12)

// Bottom instruction and sign-off.
drawText("DRAG FROM A TO B: MOUNTAIN TURTLE TO YOUR APPLICATIONS FOLDER", in: NSRect(x: 96, y: 74, width: 708, height: 30),
         size: 13, weight: .semibold, color: moss.withAlphaComponent(0.72), tracking: 4)
drawText("Keep Exploring", in: NSRect(x: 668, y: 38, width: 164, height: 38), size: 23, weight: .light,
         color: moss.withAlphaComponent(0.80), alignment: .center)
line([NSPoint(x: 694, y: 42), NSPoint(x: 806, y: 56)], color: moss.withAlphaComponent(0.62), width: 1.4)

NSGraphicsContext.restoreGraphicsState()

try FileManager.default.createDirectory(at: output.deletingLastPathComponent(), withIntermediateDirectories: true)
guard let data = bitmap.representation(using: .png, properties: [:]) else {
    fputs("Could not encode PNG\n", stderr)
    exit(1)
}
try data.write(to: output)
