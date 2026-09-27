import AppKit
import SwiftUI
import Combine
import UniformTypeIdentifiers
import Darwin

private struct FileTreeEntry: Decodable {
    let name: String
    let directory: Bool
    let size: Int64
    let modified: Double
}
private struct FileTreePage: Decodable {
    var entries: [FileTreeEntry]?
    var complete: Bool?
    var error: String?
}

private final class FileTreeRequest: @unchecked Sendable {
    private let lock = NSLock()
    private var process: Process?
    private var cancelled = false
    static func readChunk(_ handle: FileHandle) throws -> Data {
        // FileHandle.read(upToCount:) fills its requested buffer on a pipe.
        // One POSIX read returns the first available bytes of a listing page.
        var bytes = [UInt8](repeating: 0, count: 64 * 1024)
        while true {
            let count = bytes.withUnsafeMutableBytes { Darwin.read(handle.fileDescriptor, $0.baseAddress, $0.count) }
            if count >= 0 { return Data(bytes.prefix(count)) }
            if errno != EINTR { throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno)) }
        }
    }
    func start(_ value: Process) throws {
        lock.lock(); defer { lock.unlock() }
        if cancelled { throw CancellationError() }
        try value.run()
        process = value
    }
    func cancel() {
        lock.lock(); defer { lock.unlock() }
        cancelled = true
        if let process, process.isRunning { process.terminate() }
    }
}

@MainActor private final class FileTreeNode: NSObject {
    let key: String
    let name: String
    let directory: Bool
    let placeholder: Bool
    lazy var loadingRow = FileTreeNode(key: key + "/\0loading", name: "Loading…", directory: false, parent: self, placeholder: true)
    var size: Int64
    var modified: Double
    weak var parent: FileTreeNode?
    var children: [FileTreeNode] = []
    var loaded = false
    var loading = false
    var error: String?
    var request: FileTreeRequest?
    var generation = UUID()
    var received = Set<String>()
    init(key: String, name: String, directory: Bool, size: Int64 = 0, modified: Double = 0, parent: FileTreeNode? = nil, placeholder: Bool = false) {
        self.key = key; self.name = name; self.directory = directory; self.placeholder = placeholder
        self.size = size; self.modified = modified; self.parent = parent
    }
}

@MainActor private final class FileTreeModel: ObservableObject {
    let connection: Connection
    let root: FileTreeNode
    @Published var revision = 0
    @Published var selected: FileTreeNode?
    private let queue: OperationQueue = {
        let queue = OperationQueue()
        queue.maxConcurrentOperationCount = 4
        queue.qualityOfService = .userInitiated
        return queue
    }()
    private var notificationPending = false
    init(connection: Connection) {
        self.connection = connection
        root = FileTreeNode(key: "", name: connection.name, directory: true)
    }
    var folder: FileTreeNode { selected.flatMap { $0.directory ? $0 : $0.parent } ?? root }
    var status: String {
        let node = folder
        if let error = node.error { return "\(node.children.count.formatted()) items shown · \(error)" }
        if node.loading { return "\(node.children.count.formatted()) items shown · Loading…" }
        return "\(node.children.count.formatted()) items"
    }
    func changed() {
        guard !notificationPending else { return }
        notificationPending = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.06) { [weak self] in
            guard let self else { return }
            self.notificationPending = false
            self.revision += 1
        }
    }
    func load(_ node: FileTreeNode) {
        guard node.directory, !node.loaded, !node.loading else { return }
        let request = FileTreeRequest(), generation = UUID()
        node.request = request; node.generation = generation
        node.loading = true; node.error = nil; node.received = []
        changed()
        let connectionID = connection.id, key = node.key
        let resources = ServiceClient.resources.path, python = ServiceClient.pythonPath
        queue.addOperation { [weak self, weak node] in
            do {
                guard let python else { throw TurtleError(message: "Python is unavailable.") }
                let process = Process(), output = Pipe()
                process.executableURL = URL(fileURLWithPath: python)
                process.arguments = ["-B", resources + "/service/file_browser.py", "--resource-dir", resources, connectionID, key]
                process.standardOutput = output
                process.standardError = FileHandle.nullDevice
                try request.start(process)
                var buffer = Data(), completed = false, receivedError = false
                while true {
                    let chunk = try FileTreeRequest.readChunk(output.fileHandleForReading)
                    if chunk.isEmpty { break }
                    buffer.append(chunk)
                    // A page has at most 256 names. Reject an invalid/unbounded response.
                    if buffer.count > 4 * 1024 * 1024 { process.terminate(); throw TurtleError(message: "The folder response was too large.") }
                    while let newline = buffer.firstIndex(of: 10) {
                        let line = buffer.subdata(in: buffer.startIndex..<newline)
                        buffer.removeSubrange(buffer.startIndex...newline)
                        guard !line.isEmpty else { continue }
                        let page = try JSONDecoder().decode(FileTreePage.self, from: line)
                        completed = completed || page.complete == true
                        receivedError = receivedError || page.error != nil
                        DispatchQueue.main.async { [weak self, weak node] in
                            guard let self, let node, node.generation == generation else { return }
                            self.receive(page, node: node)
                        }
                    }
                }
                process.waitUntilExit()
                if !completed && !receivedError {
                    throw TurtleError(message: "The folder listing stopped. Retry to continue.")
                }
            } catch {
                DispatchQueue.main.async { [weak self, weak node] in
                    guard let self, let node, node.generation == generation else { return }
                    node.loading = false; node.request = nil
                    node.error = (error as? TurtleError)?.message ?? "Couldn’t read this folder. Retry to continue."
                    self.changed()
                }
            }
        }
    }
    private func receive(_ page: FileTreePage, node: FileTreeNode) {
        var existing = Dictionary(node.children.map { ($0.name, $0) }, uniquingKeysWith: { first, _ in first })
        for entry in page.entries ?? [] {
            guard !entry.name.isEmpty, entry.name != ".", entry.name != "..", !entry.name.contains("/"), !entry.name.contains("\0") else { continue }
            node.received.insert(entry.name)
            if let child = existing[entry.name], child.directory == entry.directory {
                child.size = entry.size; child.modified = entry.modified
            } else {
                let child = FileTreeNode(key: node.key.isEmpty ? entry.name : node.key + "/" + entry.name,
                                         name: entry.name, directory: entry.directory, size: entry.size,
                                         modified: entry.modified, parent: node)
                if let old = existing[entry.name] { cancel(old); node.children.removeAll { $0 === old } }
                node.children.append(child); existing[entry.name] = child
            }
        }
        node.children.sort { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
        if page.complete == true {
            for child in node.children where !node.received.contains(child.name) { cancel(child) }
            node.children.removeAll { !node.received.contains($0.name) }
            node.loaded = true; node.loading = false; node.request = nil
        }
        if let error = page.error { node.error = error; node.loading = false; node.request = nil }
        changed()
    }
    func cancel(_ node: FileTreeNode) {
        node.generation = UUID()
        node.request?.cancel(); node.request = nil; node.loading = false
        for child in node.children { cancel(child) }
        changed()
    }
    func refresh() {
        let node = folder
        cancel(node); node.loaded = false; load(node)
    }
    func openSelected() {
        guard let selected, !selected.directory else { return }
        let url = URL(fileURLWithPath: connection.mountPath, isDirectory: true).appendingPathComponent(selected.key)
        NSWorkspace.shared.open(url)
    }
    func showInFinder() {
        let url = URL(fileURLWithPath: connection.mountPath, isDirectory: true).appendingPathComponent(selected?.key ?? "")
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }
}

private struct FileTreeTable: NSViewRepresentable {
    @ObservedObject var model: FileTreeModel
    func makeCoordinator() -> Coordinator { Coordinator(model) }
    func makeNSView(context: Context) -> NSScrollView {
        let table = NSOutlineView()
        table.headerView = NSTableHeaderView()
        table.rowSizeStyle = .small
        table.rowHeight = 23
        table.intercellSpacing = NSSize(width: 10, height: 2)
        table.usesAlternatingRowBackgroundColors = true
        table.style = .plain
        table.indentationPerLevel = 16
        table.autoresizesOutlineColumn = false
        table.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle
        for (id, title, width) in [("name", "Name", 410.0), ("modified", "Date Modified", 165.0), ("size", "Size", 90.0), ("kind", "Kind", 130.0)] {
            let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(id))
            column.title = title; column.width = width; column.minWidth = id == "name" ? 180 : 70
            if id == "name" { column.sortDescriptorPrototype = NSSortDescriptor(key: "name", ascending: true) }
            table.addTableColumn(column)
        }
        table.outlineTableColumn = table.tableColumns.first
        table.setAccessibilityLabel("Files and folders")
        table.dataSource = context.coordinator; table.delegate = context.coordinator
        table.target = context.coordinator; table.doubleAction = #selector(Coordinator.doubleClick(_:))
        context.coordinator.table = table
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true; scroll.hasHorizontalScroller = true
        scroll.autohidesScrollers = true; scroll.borderType = .bezelBorder
        scroll.documentView = table
        return scroll
    }
    func updateNSView(_ scroll: NSScrollView, context: Context) { context.coordinator.reload() }

    @MainActor final class Coordinator: NSObject, NSOutlineViewDataSource, NSOutlineViewDelegate {
        let model: FileTreeModel
        weak var table: NSOutlineView?
        var revision = -1
        var restoring = false
        var ascending = true
        static let dateFormatter: DateFormatter = { let f = DateFormatter(); f.dateStyle = .medium; f.timeStyle = .short; return f }()
        init(_ model: FileTreeModel) { self.model = model }
        func children(_ item: Any?) -> [FileTreeNode] {
            let parent = item as? FileTreeNode ?? model.root
            let nodes = parent.children
            // Keep the expanded state visible while the folder has no real
            // children yet, until the first page or successful EOF arrives.
            if nodes.isEmpty && !parent.loaded && parent.directory { return [parent.loadingRow] }
            return ascending ? nodes : nodes.reversed()
        }
        func outlineView(_ outlineView: NSOutlineView, numberOfChildrenOfItem item: Any?) -> Int { children(item).count }
        func outlineView(_ outlineView: NSOutlineView, child index: Int, ofItem item: Any?) -> Any { children(item)[index] }
        func outlineView(_ outlineView: NSOutlineView, isItemExpandable item: Any) -> Bool { (item as? FileTreeNode)?.directory == true }
        func outlineView(_ outlineView: NSOutlineView, shouldExpandItem item: Any) -> Bool {
            // AppKit/accessibility can ask whether expansion is allowed without
            // actually expanding. Start remote work only after a real expansion.
            (item as? FileTreeNode)?.directory == true
        }
        func outlineViewItemDidExpand(_ notification: Notification) {
            if !restoring, let node = notification.userInfo?["NSObject"] as? FileTreeNode { model.load(node) }
        }
        func outlineViewItemDidCollapse(_ notification: Notification) {
            if !restoring, let node = notification.userInfo?["NSObject"] as? FileTreeNode { model.cancel(node) }
        }
        func outlineView(_ outlineView: NSOutlineView, shouldSelectItem item: Any) -> Bool {
            (item as? FileTreeNode)?.placeholder == false
        }
        func outlineViewSelectionDidChange(_ notification: Notification) {
            guard !restoring, let table else { return }
            model.selected = table.item(atRow: table.selectedRow) as? FileTreeNode
        }
        func outlineView(_ outlineView: NSOutlineView, sortDescriptorsDidChange oldDescriptors: [NSSortDescriptor]) {
            ascending = outlineView.sortDescriptors.first?.ascending ?? true
            revision = -1; reload()
        }
        @objc func doubleClick(_ sender: NSOutlineView) {
            guard let node = sender.item(atRow: sender.clickedRow) as? FileTreeNode else { return }
            if node.directory {
                if sender.isItemExpanded(node) { sender.collapseItem(node) } else { sender.expandItem(node) }
            } else { model.openSelected() }
        }
        func reload() {
            guard let table, revision != model.revision else { return }
            revision = model.revision
            var expanded = Set<String>()
            for row in 0..<table.numberOfRows {
                if let node = table.item(atRow: row) as? FileTreeNode, table.isItemExpanded(node) { expanded.insert(node.key) }
            }
            let selection = model.selected?.key
            let origin = table.enclosingScrollView?.contentView.bounds.origin
            restoring = true
            table.reloadData()
            func restore(_ node: FileTreeNode) {
                for child in node.children where expanded.contains(child.key) {
                    table.expandItem(child); restore(child)
                }
            }
            restore(model.root)
            if let selection {
                for row in 0..<table.numberOfRows where (table.item(atRow: row) as? FileTreeNode)?.key == selection {
                    table.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false); break
                }
            }
            if let origin { table.enclosingScrollView?.contentView.scroll(to: origin) }
            restoring = false
        }
        func outlineView(_ outlineView: NSOutlineView, viewFor tableColumn: NSTableColumn?, item: Any) -> NSView? {
            guard let node = item as? FileTreeNode, let id = tableColumn?.identifier.rawValue else { return nil }
            let identifier = NSUserInterfaceItemIdentifier(id)
            let cell: NSTableCellView
            if let reused = outlineView.makeView(withIdentifier: identifier, owner: self) as? NSTableCellView { cell = reused }
            else {
                cell = NSTableCellView(); cell.identifier = identifier
                let text = NSTextField(labelWithString: "")
                text.font = NSFont.systemFont(ofSize: 12)
                text.lineBreakMode = .byTruncatingMiddle
                text.translatesAutoresizingMaskIntoConstraints = false
                cell.addSubview(text); cell.textField = text
                var leading = cell.leadingAnchor
                if id == "name" {
                    let image = NSImageView(); image.translatesAutoresizingMaskIntoConstraints = false
                    cell.addSubview(image); cell.imageView = image
                    NSLayoutConstraint.activate([image.leadingAnchor.constraint(equalTo: cell.leadingAnchor), image.centerYAnchor.constraint(equalTo: cell.centerYAnchor), image.widthAnchor.constraint(equalToConstant: 16), image.heightAnchor.constraint(equalToConstant: 16)])
                    leading = image.trailingAnchor
                }
                NSLayoutConstraint.activate([text.leadingAnchor.constraint(equalTo: leading, constant: id == "name" ? 5 : 0), text.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4), text.centerYAnchor.constraint(equalTo: cell.centerYAnchor)])
            }
            var value: String
            switch id {
            case "name": value = node.name + (node.loading ? "  · Loading…" : node.error == nil ? "" : "  · Retry")
            case "modified": value = node.modified > 0 ? Self.dateFormatter.string(from: Date(timeIntervalSince1970: node.modified)) : "—"
            case "size": value = node.directory || node.size < 0 ? "—" : ByteCountFormatter.string(fromByteCount: node.size, countStyle: .file)
            default:
                let ext = (node.name as NSString).pathExtension
                value = node.directory ? "Folder" : UTType(filenameExtension: ext)?.localizedDescription ?? "Document"
            }
            if node.placeholder { value = id == "name" ? (node.parent?.error ?? "Loading…") : "" }
            cell.textField?.stringValue = value
            cell.textField?.textColor = id == "name" && !node.placeholder ? .labelColor : .secondaryLabelColor
            cell.imageView?.image = node.placeholder ? nil : NSImage(named: node.directory ? NSImage.folderName : NSImage.multipleDocumentsName)
            cell.toolTip = node.error ?? node.name
            return cell
        }
    }
}

struct FileBrowserView: View {
    let connection: Connection
    @StateObject private var model: FileTreeModel
    @Environment(\.dismiss) private var dismiss
    init(connection: Connection) { self.connection = connection; _model = StateObject(wrappedValue: FileTreeModel(connection: connection)) }
    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                Image(systemName: "externaldrive.fill").foregroundStyle(moss)
                Text(connection.name).font(.title3.weight(.semibold))
                Spacer()
                Button { model.refresh() } label: { Label("Refresh folder", systemImage: "arrow.clockwise") }
                Button("Show in Finder") { model.showInFinder() }.disabled(!connection.isConnected)
                Button("Open") { model.openSelected() }.disabled(model.selected == nil || model.selected?.directory == true || !connection.isConnected)
                Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
            }.padding(16)
            FileTreeTable(model: model).padding(.horizontal, 16)
            HStack {
                Text(model.folder.key.isEmpty ? connection.name : model.folder.key).lineLimit(1).truncationMode(.middle)
                Spacer()
                if model.folder.loading { ProgressView().controlSize(.small) }
                Text(model.status).lineLimit(2)
                if model.folder.error != nil { Button("Retry") { model.refresh() } }
            }.font(.system(size: 11)).foregroundStyle(.secondary).padding(16)
        }.frame(minWidth: 920, minHeight: 560)
            .onAppear { model.load(model.root) }
            .onDisappear { model.cancel(model.root) }
    }
}
