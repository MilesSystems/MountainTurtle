import AppKit
import Foundation

// Original Mountain Turtle artwork. Coordinates are vector paths on a 1024-point canvas.
// This generator writes only the specified local Resources directory; it never sets an
// icon on a mounted volume. Finder's small metadata records are generated as raw bytes.

func color(_ hex: UInt32, alpha: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 255) / 255,
            green: CGFloat((hex >> 8) & 255) / 255,
            blue: CGFloat(hex & 255) / 255, alpha: alpha)
}

let pine = color(0x123F39)
let teal = color(0x246958)
let mint = color(0x8ECBA1)
let cream = color(0xFAF7E8)

func path(_ points: [(CGFloat, CGFloat)]) -> NSBezierPath {
    let result = NSBezierPath()
    guard let first = points.first else { return result }
    result.move(to: NSPoint(x: first.0, y: first.1))
    for point in points.dropFirst() { result.line(to: NSPoint(x: point.0, y: point.1)) }
    result.close()
    return result
}

func ellipse(_ rect: NSRect, _ fill: NSColor) {
    fill.setFill()
    NSBezierPath(ovalIn: rect).fill()
}

func shadow(_ blur: CGFloat, _ y: CGFloat, _ alpha: CGFloat) {
    let value = NSShadow()
    value.shadowColor = pine.withAlphaComponent(alpha)
    value.shadowBlurRadius = blur
    value.shadowOffset = NSSize(width: 0, height: y)
    value.set()
}

func drawTurtle() {
    // Back feet, neck, and tail establish a recognizable silhouette at small sizes.
    let rearFoot = NSBezierPath(roundedRect: NSRect(x: 301, y: 256, width: 103, height: 138), xRadius: 48, yRadius: 48)
    let frontFoot = NSBezierPath(roundedRect: NSRect(x: 588, y: 256, width: 103, height: 140), xRadius: 48, yRadius: 48)
    teal.setFill(); rearFoot.fill(); frontFoot.fill()
    let tail = path([(268, 368), (168, 383), (238, 424)])
    mint.setFill(); tail.fill()
    let neck = NSBezierPath(roundedRect: NSRect(x: 666, y: 365, width: 110, height: 87), xRadius: 40, yRadius: 40)
    mint.setFill(); neck.fill()
    let head = NSBezierPath(roundedRect: NSRect(x: 706, y: 375, width: 153, height: 132), xRadius: 62, yRadius: 62)
    NSGradient(starting: color(0xACDDB5), ending: color(0x75B98C))!.draw(in: head, angle: -90)
    ellipse(NSRect(x: 796, y: 450, width: 16, height: 18), pine)
    ellipse(NSRect(x: 800, y: 459, width: 4, height: 4), cream)
    let smile = NSBezierPath()
    smile.move(to: NSPoint(x: 813, y: 415))
    smile.curve(to: NSPoint(x: 847, y: 421), controlPoint1: NSPoint(x: 823, y: 409), controlPoint2: NSPoint(x: 839, y: 411))
    color(0x4B8965).setStroke(); smile.lineWidth = 5; smile.lineCapStyle = .round; smile.stroke()

    let belly = NSBezierPath(roundedRect: NSRect(x: 234, y: 327, width: 507, height: 127), xRadius: 60, yRadius: 60)
    NSGradient(starting: color(0xB7DBAE), ending: color(0x7AB68A))!.draw(in: belly, angle: -90)

    let shell = NSBezierPath()
    shell.move(to: NSPoint(x: 221, y: 403))
    shell.curve(to: NSPoint(x: 476, y: 709), controlPoint1: NSPoint(x: 220, y: 577), controlPoint2: NSPoint(x: 326, y: 709))
    shell.curve(to: NSPoint(x: 740, y: 403), controlPoint1: NSPoint(x: 630, y: 709), controlPoint2: NSPoint(x: 740, y: 577))
    shell.curve(to: NSPoint(x: 221, y: 403), controlPoint1: NSPoint(x: 610, y: 344), controlPoint2: NSPoint(x: 349, y: 344))
    shell.close()
    NSGraphicsContext.saveGraphicsState()
    shadow(16, -7, 0.18); pine.setFill(); shell.fill()
    NSGraphicsContext.restoreGraphicsState()
    NSGradient(starting: color(0x3E8B72), ending: color(0x144A40))!.draw(in: shell, angle: -90)

    NSGraphicsContext.saveGraphicsState()
    shell.addClip()
    // Two peaks and a single snowline form the mountain within the turtle's shell.
    let distantPeak = path([(418, 403), (604, 652), (799, 403)])
    color(0x75B490).setFill(); distantPeak.fill()
    let mountain = path([(235, 402), (457, 686), (693, 402)])
    color(0xE9EED4).setFill(); mountain.fill()
    let forestSlope = path([(235, 402), (387, 596), (435, 555), (470, 589), (541, 510), (588, 533), (693, 402)])
    NSGradient(starting: color(0x8DC8A0), ending: color(0x4E9879))!.draw(in: forestSlope, angle: -90)
    let lowerSlope = path([(184, 397), (364, 492), (478, 413), (605, 489), (783, 397), (783, 320), (184, 320)])
    color(0x215D4C).setFill(); lowerSlope.fill()
    let highlight = NSBezierPath()
    highlight.move(to: NSPoint(x: 252, y: 484))
    highlight.curve(to: NSPoint(x: 410, y: 679), controlPoint1: NSPoint(x: 267, y: 575), controlPoint2: NSPoint(x: 324, y: 654))
    color(0xC4E1C1, alpha: 0.45).setStroke(); highlight.lineWidth = 6; highlight.lineCapStyle = .round; highlight.stroke()
    NSGraphicsContext.restoreGraphicsState()

    // A broad polished rim keeps the shell readable at Dock and Finder sizes.
    let rim = NSBezierPath()
    rim.move(to: NSPoint(x: 230, y: 401))
    rim.curve(to: NSPoint(x: 732, y: 401), controlPoint1: NSPoint(x: 363, y: 348), controlPoint2: NSPoint(x: 600, y: 348))
    color(0x123F39).setStroke(); rim.lineWidth = 17; rim.lineCapStyle = .round; rim.stroke()
}

func drawAppIcon() {
    let tile = NSBezierPath(roundedRect: NSRect(x: 105, y: 105, width: 814, height: 814), xRadius: 184, yRadius: 184)
    NSGraphicsContext.saveGraphicsState()
    shadow(31, -15, 0.28); cream.setFill(); tile.fill()
    NSGraphicsContext.restoreGraphicsState()
    NSGradient(starting: color(0xF9F6E9), ending: color(0xD8E9D5))!.draw(in: tile, angle: -90)
    NSGraphicsContext.saveGraphicsState()
    tile.addClip()
    ellipse(NSRect(x: 654, y: 683, width: 117, height: 117), color(0xFDFBF2, alpha: 0.96))
    let horizon = NSBezierPath()
    horizon.move(to: NSPoint(x: 104, y: 269))
    horizon.curve(to: NSPoint(x: 919, y: 270), controlPoint1: NSPoint(x: 328, y: 220), controlPoint2: NSPoint(x: 722, y: 282))
    horizon.line(to: NSPoint(x: 919, y: 100)); horizon.line(to: NSPoint(x: 104, y: 100)); horizon.close()
    color(0xC1DBC3, alpha: 0.45).setFill(); horizon.fill()
    NSGraphicsContext.restoreGraphicsState()
    drawTurtle()
    color(0xFFFFFF, alpha: 0.55).setStroke(); tile.lineWidth = 3; tile.stroke()
}

func drawDriveIcon(label: String) {
    let front = NSBezierPath(roundedRect: NSRect(x: 130, y: 151, width: 764, height: 203), xRadius: 59, yRadius: 59)
    NSGraphicsContext.saveGraphicsState()
    shadow(26, -12, 0.27); cream.setFill(); front.fill()
    NSGraphicsContext.restoreGraphicsState()
    NSGradient(starting: color(0xE5ECE2), ending: color(0x97ADA2))!.draw(in: front, angle: -90)
    let top = NSBezierPath()
    top.move(to: NSPoint(x: 133, y: 322))
    top.line(to: NSPoint(x: 217, y: 824))
    top.curve(to: NSPoint(x: 260, y: 864), controlPoint1: NSPoint(x: 221, y: 849), controlPoint2: NSPoint(x: 232, y: 864))
    top.line(to: NSPoint(x: 764, y: 864))
    top.curve(to: NSPoint(x: 807, y: 824), controlPoint1: NSPoint(x: 792, y: 864), controlPoint2: NSPoint(x: 803, y: 849))
    top.line(to: NSPoint(x: 891, y: 322))
    top.curve(to: NSPoint(x: 863, y: 289), controlPoint1: NSPoint(x: 895, y: 302), controlPoint2: NSPoint(x: 884, y: 289))
    top.line(to: NSPoint(x: 161, y: 289))
    top.curve(to: NSPoint(x: 133, y: 322), controlPoint1: NSPoint(x: 140, y: 289), controlPoint2: NSPoint(x: 129, y: 302))
    top.close()
    NSGradient(starting: color(0x3B806A), ending: color(0x184A40))!.draw(in: top, angle: -90)
    color(0x77A78F).setStroke(); top.lineWidth = 4; top.stroke()
    NSGraphicsContext.saveGraphicsState()
    let placement = NSAffineTransform()
    placement.translateX(by: 214, yBy: 445)
    placement.scale(by: 0.58)
    placement.concat()
    drawTurtle()
    NSGraphicsContext.restoreGraphicsState()
    let text = label as NSString
    let attributes: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: label.count > 2 ? 145 : 170, weight: .bold), .foregroundColor: cream]
    let measure = text.size(withAttributes: attributes)
    text.draw(at: NSPoint(x: (1024 - measure.width) / 2, y: 359), withAttributes: attributes)
    let slot = NSBezierPath(roundedRect: NSRect(x: 200, y: 214, width: 251, height: 20), xRadius: 10, yRadius: 10)
    color(0x536E61).setFill(); slot.fill()
    ellipse(NSRect(x: 796, y: 203, width: 29, height: 29), color(0x4DD789))
    ellipse(NSRect(x: 803, y: 218, width: 7, height: 7), color(0xDBFFE9))
}

func png(size: Int, draw: () -> Void) -> Data {
    let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size,
                                  bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                  colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
    let scaling = NSAffineTransform(); scaling.scale(by: CGFloat(size) / 1024); scaling.concat()
    draw()
    NSGraphicsContext.restoreGraphicsState()
    return bitmap.representation(using: .png, properties: [:])!
}

func writeIcon(name: String, preview: String, at output: URL, draw: () -> Void) throws {
    let iconset = output.appendingPathComponent("\(name).iconset")
    try FileManager.default.createDirectory(at: iconset, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: iconset) }
    for size in [16, 32, 128, 256, 512] {
        for scale in [1, 2] {
            let suffix = scale == 2 ? "@2x" : ""
            try png(size: size * scale, draw: draw).write(to: iconset.appendingPathComponent("icon_\(size)x\(size)\(suffix).png"))
        }
    }
    try png(size: 1024, draw: draw).write(to: output.appendingPathComponent(preview))
    let task = Process()
    task.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
    task.arguments = ["--convert", "icns", "--output", output.appendingPathComponent("\(name).icns").path, iconset.path]
    try task.run(); task.waitUntilExit()
    guard task.terminationStatus == 0 else { throw NSError(domain: "Icon generation", code: Int(task.terminationStatus)) }
}

// AppleDouble entry 9 stores FinderInfo. kHasCustomIcon is 0x0400 on the
// volume root; kIsInvisible is 0x4000 on .VolumeIcon.icns. These sidecars are
// raw file contents because rclone NFS does not export the local xattrs.
func finderMetadata(flags: UInt16) -> Data {
    // Use macOS's 4096-byte layout. A minimal FinderInfo-only AppleDouble
    // record is ignored by the native NFS client on current macOS.
    var result = Data(repeating: 0, count: 4096)
    func put32(_ offset: Int, _ value: UInt32) {
        result.replaceSubrange(offset..<(offset + 4), with: [UInt8((value >> 24) & 255), UInt8((value >> 16) & 255), UInt8((value >> 8) & 255), UInt8(value & 255)])
    }
    put32(0, 0x00051607); put32(4, 0x00020000)
    result.replaceSubrange(8..<24, with: Data("Mac OS X        ".utf8))
    result[25] = 2
    put32(26, 9); put32(30, 50); put32(34, 3760)
    put32(38, 2); put32(42, 3810); put32(46, 286)
    result[58] = UInt8(flags >> 8); result[59] = UInt8(flags & 255)
    // Empty extended-attributes header; no machine provenance is included.
    put32(84, 0x41545452); put32(92, 3810); put32(96, 120)
    // Valid empty classic Resource Manager fork, as emitted by macOS.
    for offset in [3810, 4066] {
        put32(offset, 256); put32(offset + 4, 256); put32(offset + 8, 0); put32(offset + 12, 30)
    }
    let blank = Data("This resource fork intentionally left blank   ".utf8)
    result.replaceSubrange(3826..<(3826 + blank.count), with: blank)
    result[4091] = 28; result[4093] = 30; result[4094] = 255; result[4095] = 255
    return result
}

guard CommandLine.arguments.count == 2 else {
    fputs("Usage: swift generate-icons.swift /absolute/path/to/Resources\n", stderr)
    exit(2)
}
let output = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true).standardizedFileURL
try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
try writeIcon(name: "AppIcon", preview: "appIcon.png", at: output, draw: drawAppIcon)
for (label, name, preview, directory) in [
    ("S3", "S3Drive", "s3Drive.png", "icon-overlay-assets"),
    ("SFTP", "SFTPDrive", "sftpDrive.png", "icon-overlay-assets-sftp"),
] {
    try writeIcon(name: name, preview: preview, at: output) { drawDriveIcon(label: label) }
    let overlay = output.appendingPathComponent(directory, isDirectory: true)
    try FileManager.default.createDirectory(at: overlay, withIntermediateDirectories: true)
    try Data(contentsOf: output.appendingPathComponent("\(name).icns")).write(to: overlay.appendingPathComponent("VolumeIcon.icns"))
    try finderMetadata(flags: 0x0400).write(to: overlay.appendingPathComponent("root-finder-info.ad"))
    try finderMetadata(flags: 0x4000).write(to: overlay.appendingPathComponent("volume-icon-finder-info.ad"))
}
print("Created original app, S3, and SFTP drive icons in \(output.path)")
