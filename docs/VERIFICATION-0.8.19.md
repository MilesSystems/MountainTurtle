# 0.8.19 verification

## Connection and status polling

The affected mounted drive stalled on a remote SFTP directory listing. An
authenticated local goroutine snapshot showed `vfs/stats` walking the folder tree
while holding a parent lock; other Finder operations waited behind it. A separate
read-only SFTP listing timed out. `vfs/queue` answered promptly on that same mount.
Upload status and drive insights now use queue and transfer counters, with bounded
local cache accounting. The first installed validation build showed the corrected
upload status and live local metrics on that stalled mount.

## Failure reports and measured activity

Regression coverage includes delete/rename/listing/transfer classification,
persistent log offsets, partial lines and rotation, bounded retention, symlink
exclusion, owner-only history permissions, and allowlisted reports that omit
private item names, settings, credentials and arbitrary provider output.
Measured app operations cover preview retrieval, folder listings, original reads,
photo metadata and folder prefetch. Tests cover completion, cancellation, dead
owners, failed async listing results, unavailable transfer counters and uncertain
outcomes. Counters and listing job status never request directory-tree statistics.

The local full suite passed all 569 tests, including the native Finder rename
integration. The initial attempt before the reporting work hit local NFS and APFS
fixture timeouts; the subsequent full run passed without disabling those tests.
The signed universal build, installed UI screenshots and report controls must be
verified on the final source, with successful GitHub validation before publication.
Public delivery and an actual older-version update/relaunch are separate checks;
a busy mounted drive is not bypassed to claim completion.

Existing credentials and cache roots are preserved. No forced unmount or cache
removal was used during diagnosis. The report's share action copies the reviewed
text and opens a GitHub draft; it does not submit a report automatically.
