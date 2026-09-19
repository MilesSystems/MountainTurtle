import Foundation
import ImageIO

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

// EXIF records camera wall time, which often has no timezone. Validate ImageIO's
// value without DateFormatter normalization or timezone conversion.
func cameraDate(_ value: String) -> String? {
    let bytes = Array(value.utf8)
    guard bytes.count == 19, bytes[4] == 58, bytes[7] == 58,
          bytes[10] == 32, bytes[13] == 58, bytes[16] == 58 else { return nil }
    let separators = Set([4, 7, 10, 13, 16])
    guard bytes.indices.allSatisfy({ separators.contains($0) || (48...57).contains(bytes[$0]) }) else {
        return nil
    }
    func number(_ range: Range<Int>) -> Int {
        range.reduce(0) { $0 * 10 + Int(bytes[$1] - 48) }
    }
    let year = number(0..<4), month = number(5..<7), day = number(8..<10)
    let hour = number(11..<13), minute = number(14..<16), second = number(17..<19)
    guard year > 0, (1...12).contains(month), hour < 24, minute < 60, second < 60 else {
        return nil
    }
    let leapYear = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)
    let days = [31, leapYear ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    guard (1...days[month - 1]).contains(day) else { return nil }
    var result = bytes
    result[4] = 45
    result[7] = 45
    result[10] = 84
    return String(bytes: result, encoding: .utf8)
}

// A JPEG prefix can end inside a large XMP segment before its image frame.
// ImageIO then exposes no EXIF properties, even when a complete EXIF APP1 is
// already present. Read only DateTimeOriginal from that bounded TIFF directory.
func boundedJPEGDate(_ data: Data) -> String? {
    let bytes = Array(data)
    guard bytes.count >= 2, bytes[0] == 0xff, bytes[1] == 0xd8 else { return nil }

    func exifDate(_ tiff: [UInt8]) -> String? {
        guard tiff.count >= 8 else { return nil }
        let littleEndian = tiff[0] == 0x49 && tiff[1] == 0x49
        guard littleEndian || (tiff[0] == 0x4d && tiff[1] == 0x4d) else { return nil }
        func number(_ offset: Int, _ count: Int) -> Int? {
            guard offset >= 0, offset <= tiff.count - count else { return nil }
            let indices = littleEndian ? Array((offset..<offset + count).reversed()) : Array(offset..<offset + count)
            return indices.reduce(0) { ($0 << 8) | Int(tiff[$1]) }
        }
        func entry(_ directory: Int, tag: Int) -> Int? {
            guard directory >= 8, let count = number(directory, 2), count <= 512,
                  directory + 2 + count * 12 + 4 <= tiff.count else { return nil }
            for index in 0..<count {
                let position = directory + 2 + index * 12
                if number(position, 2) == tag { return position }
            }
            return nil
        }
        guard number(2, 2) == 42, let first = number(4, 4),
              let exifEntry = entry(first, tag: 0x8769), number(exifEntry + 2, 2) == 4,
              number(exifEntry + 4, 4) == 1, let exif = number(exifEntry + 8, 4),
              let original = entry(exif, tag: 0x9003), number(original + 2, 2) == 2,
              number(original + 4, 4) == 20, let start = number(original + 8, 4),
              start >= 8, start <= tiff.count - 20, tiff[start + 19] == 0,
              let value = String(bytes: tiff[start..<start + 19], encoding: .ascii) else { return nil }
        return cameraDate(value)
    }

    var position = 2
    while position + 1 < bytes.count {
        guard bytes[position] == 0xff else { return nil }
        while position < bytes.count && bytes[position] == 0xff { position += 1 }
        guard position < bytes.count else { return nil }
        let marker = bytes[position]
        position += 1
        if marker == 0xda || marker == 0xd9 { return nil }
        if marker == 0x01 || (0xd0...0xd8).contains(marker) { continue }
        guard position + 2 <= bytes.count else { return nil }
        let length = (Int(bytes[position]) << 8) | Int(bytes[position + 1])
        guard length >= 2, length <= bytes.count - position else { return nil }
        let begin = position + 2, end = position + length
        if marker == 0xe1, end - begin >= 6,
           Array(bytes[begin..<begin + 6]) == [0x45, 0x78, 0x69, 0x66, 0, 0],
           let value = exifDate(Array(bytes[begin + 6..<end])) { return value }
        position = end
    }
    return nil
}

guard CommandLine.arguments.count == 2 else { fail("Usage: Mountain Turtle Photo Dates <local image path>") }
let url = URL(fileURLWithPath: CommandLine.arguments[1])
do {
    guard try url.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile == true else {
        fail("Photo metadata input must be a regular local file.")
    }
    let input = try FileHandle(forReadingFrom: url)
    defer { try? input.close() }
    _ = try input.read(upToCount: 1)
} catch {
    fail("Could not read the local photo metadata file.")
}

// ImageIO reads properties only. No image or thumbnail is decoded, and only
// DateTimeOriginal is trusted; file dates and digitization dates are unrelated.
let options = [kCGImageSourceShouldCache: false] as CFDictionary
var dateTaken: String?
if let source = CGImageSourceCreateWithURL(url as CFURL, options),
   let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, options) as? [String: Any],
   let exif = properties[kCGImagePropertyExifDictionary as String] as? [String: Any],
   let original = exif[kCGImagePropertyExifDateTimeOriginal as String] as? String {
    dateTaken = cameraDate(original)
}
if dateTaken == nil {
    do {
        let input = try FileHandle(forReadingFrom: url)
        defer { try? input.close() }
        dateTaken = boundedJPEGDate(try input.read(upToCount: 128 * 1024) ?? Data())
    } catch {
        fail("Could not read the local photo metadata file.")
    }
}
let result: [String: Any] = ["dateTaken": dateTaken as Any? ?? NSNull()]
guard let output = try? JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]) else {
    fail("Could not encode the photo metadata result.")
}
FileHandle.standardOutput.write(output)
FileHandle.standardOutput.write(Data("\n".utf8))
