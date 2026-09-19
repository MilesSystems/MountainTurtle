import Foundation

struct PhotoIdentity: Hashable, Sendable {
    let key: String
    let etag: String
    let size: Int64
}

struct PhotoItem: Decodable, Identifiable, Sendable {
    var key: String
    var name: String
    var size: Int64
    var etag: String
    var id: String { key }
    var identity: PhotoIdentity { PhotoIdentity(key: key, etag: etag, size: size) }
    var arguments: [String] { ["--key=\(key)", "--etag=\(etag)", "--size=\(size)"] }
}

struct PhotoTakenDate: Sendable {
    let value: String?

    init(_ value: String?) {
        self.value = Self.recordedDate(value) == nil ? nil : value
    }

    var label: String { label(locale: .current) }

    func label(locale: Locale) -> String {
        guard let date = Self.recordedDate(value) else { return "Date taken: Unknown" }
        let formatter = DateFormatter()
        formatter.locale = locale
        // Camera timestamps do not include a timezone. Use the same fixed zone
        // for parsing and display so the recorded wall-clock time is preserved.
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateStyle = .medium
        formatter.timeStyle = .short
        return "Date taken: \(formatter.string(from: date))"
    }

    private static func recordedDate(_ value: String?) -> Date? {
        guard let value else { return nil }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        formatter.isLenient = false
        guard let date = formatter.date(from: value), formatter.string(from: date) == value else { return nil }
        return date
    }
}

enum PhotoPageSort: String, CaseIterable, Identifiable {
    case name = "Name"
    case newest = "Date taken: newest"
    case oldest = "Date taken: oldest"
    var id: Self { self }
}

enum PhotoDateOrdering {
    static func sorted(_ photos: [PhotoItem], by choice: PhotoPageSort, dates: [PhotoIdentity: PhotoTakenDate]) -> [PhotoItem] {
        photos.sorted { left, right in
            if choice != .name {
                let leftDate = dates[left.identity]?.value
                let rightDate = dates[right.identity]?.value
                switch (leftDate, rightDate) {
                case let (left?, right?) where left != right:
                    return choice == .newest ? left > right : left < right
                case (_?, nil): return true
                case (nil, _?): return false
                default: break
                }
            }
            let comparison = left.name.localizedStandardCompare(right.name)
            return comparison == .orderedSame ? left.key < right.key : comparison == .orderedAscending
        }
    }
}
