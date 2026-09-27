# 0.8.21 validation

- Full app/service suite: 588 tests passed, including native case-sensitive SFTP/NFS Finder rename and metadata preservation using the bundled engine.
- Upstream VFS, NFS, union and SFTP tests plus new paging, error, cookie continuity and concurrent-RPC regressions run with Go's race detector. A first upstream NFS run encountered a local client-port collision; the unchanged rerun passed.
- Read-only real-server NFS probe: first 63 filenames in 0.78 seconds, EOF correctly false; 6,517 unique names in 131 responses, complete in 49.48 seconds. No remote files changed.
- Installed browser test app: inspected the native four-column tree, watched a real large folder populate, then verified keyboard collapse/re-expansion with 6,517 loaded items retained. No previews or originals were requested by the browser.
- Signed universal macOS build passed: app and mount helper contain arm64 and x86_64; deep/strict code-signature verification passed; bundle version is 0.8.21. Public delivery and the actual older-version update/relaunch are verified separately after publication.
