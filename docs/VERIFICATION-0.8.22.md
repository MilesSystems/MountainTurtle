# 0.8.22 validation

- Full app/service suite: 591 tests passed. The final run uses unchanged source throughout; earlier runs interrupted by source edits were discarded.
- Focused cache badge tests verify that completed download records do not imply current child coverage, evicted files become online-only, empty caches are labelled explicitly, and dirty empty files remain pending uploads.
- Native AppKit regression verifies first expansion, loading placeholder, first-page replacement, collapse cancellation, completed empty folders, and side-effect-free expansion capability queries. A gated pipe writer also verifies the first small page is delivered before later pages or EOF, without a 64 KB buffering delay.
- Installed isolated browser: screenshots verified disclosure expansion and the visible loading row, followed by real RAW filenames and sizes without a second click. Unexpanded folders remain idle. The isolated test bundle was closed and retained under ignored build output afterward.
- Actual Finder displayed 6,517 RAW files with real sizes. Independent server and mount stats agreed for a populated file. A separate reported folder contained zero-byte files on the server; the inspected sidecar contained brok/MACS Finder metadata. No server files were modified.
- Upstream VFS, NFS, union and SFTP tests pass with the race detector. The mount engine is unchanged from 0.8.21.
- Signed universal app build passed; app and mount helper contain arm64 and x86_64. Deep/strict signature checks and both bundle-version checks passed at 0.8.22.
- GitHub validation, public asset delivery, and the actual 0.8.21-to-0.8.22 update/relaunch are checked after this source commit. Connection settings and 38 sampled cache files have private pre-update snapshots for comparison.
