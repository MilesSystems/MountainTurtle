"""Exercise the real outline before and after its first asynchronous page."""
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('xcrun'), 'requires macOS AppKit')
class FileBrowserNativeTests(unittest.TestCase):
    def test_first_expansion_stays_open_and_collapse_cancels_loading(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='turtle-outline-') as temporary:
            source = Path(temporary) / 'main.swift'
            source.write_text((root / 'Sources/FileBrowser.swift').read_text() + r'''
let moss = Color.green
struct TurtleError: Error { let message: String }
struct Connection { let id: String; let name: String; let mountPath: String; let isConnected: Bool }
enum ServiceClient { static let resources = URL(fileURLWithPath: "/nonexistent"); static let pythonPath: String? = nil }
@main struct OutlineTest {
    @MainActor static func main() {
        let model = FileTreeModel(connection: Connection(id: "fixture", name: "Fixture", mountPath: "/nonexistent", isConnected: false))
        let folder = FileTreeNode(key: "large", name: "Large folder", directory: true, parent: model.root)
        model.root.children = [folder]; model.root.loaded = true
        // Model a running remote request without starting a subprocess.
        folder.loading = true
        let coordinator = FileTreeTable.Coordinator(model)
        let table = NSOutlineView()
        let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("name"))
        table.addTableColumn(column); table.outlineTableColumn = column
        table.dataSource = coordinator; table.delegate = coordinator; coordinator.table = table
        let unopened = FileTreeNode(key: "unopened", name: "Unopened", directory: true)
        precondition(coordinator.outlineView(table, shouldExpandItem: unopened))
        precondition(!unopened.loading && unopened.request == nil, "Expansion capability query started a remote listing")
        table.reloadData(); table.expandItem(folder)
        model.revision += 1; coordinator.reload()
        precondition(table.isItemExpanded(folder), "First click lost expansion before the first page")
        precondition(table.numberOfRows == 2, "Missing loading row")
        precondition((table.item(atRow: 1) as? FileTreeNode)?.placeholder == true)
        precondition(!coordinator.outlineView(table, shouldSelectItem: folder.loadingRow))
        folder.children = [FileTreeNode(key: "large/one", name: "one", directory: false, parent: folder)]
        model.revision += 1; coordinator.reload()
        precondition(table.isItemExpanded(folder), "First page lost the expansion")
        precondition((table.item(atRow: 1) as? FileTreeNode)?.name == "one", "First page needed a second click")
        let generation = folder.generation
        table.collapseItem(folder)
        precondition(!folder.loading && folder.generation != generation, "Collapse did not cancel the pending request")
        folder.children = []; folder.loaded = true
        model.revision += 1; coordinator.reload()
        precondition(coordinator.children(folder).isEmpty, "Empty completed folder retained its loading row")
        print("PASS outline expansion, first page, cancellation and empty EOF")
    }
}
''')
            executable = Path(temporary) / 'outline-test'
            compiled = subprocess.run(['xcrun', 'swiftc', '-Onone', '-swift-version', '5', '-parse-as-library',
                '-target', platform.machine() + '-apple-macosx14.0', '-framework', 'AppKit', '-framework', 'SwiftUI',
                str(source), '-o', str(executable)], capture_output=True, text=True, timeout=90)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('PASS outline expansion', result.stdout)
