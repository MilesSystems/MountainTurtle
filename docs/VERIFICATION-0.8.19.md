# 0.8.19 verification

The affected mounted drive remained stalled on a remote SFTP directory listing.
An authenticated local goroutine snapshot showed `vfs/stats` walking the folder
tree while holding a parent lock; other Finder operations waited behind it.
A separate read-only SFTP listing also timed out. `vfs/queue` answered promptly
on the same stalled mount, without traversing that directory tree.

Upload status and drive insights now use the upload queue and transfer counters.
Local cache accounting is bounded and reports incomplete scans as unavailable.
Regression tests reject tree-statistics requests and cover malformed queues,
waiting, active and retried uploads, and incomplete cache accounting.

The signed universal build passed code-signature, architecture, Sparkle link and
bundle-version checks. A separate installed 0.8.19 build displayed No queued
uploads on the stalled drive, preserved its busy warning, and refreshed local
transfer/cache metrics. Screenshots and the metrics interaction were inspected.
Remote filesystem capacity remained unavailable, correctly shown as unavailable.

The local full suite ran 555 tests; the existing isolated native Finder rename
fixture failed on timed-out filesystem operations. Its retry then timed out
creating an APFS disk image before exercising application code. All other tests
passed. Clean GitHub validation must pass before publication; these local
filesystem failures are not treated as a passing local integration test.

The existing user mount could not be safely ejected because Finder and the
service prefetch worker held its Raw directory open. No forced unmount or cache
removal was attempted. Public update/relaunch remains a separate delivery check.
