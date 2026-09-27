# Folder refresh verification

The reported SFTP folder contains 6,517 entries. A direct, read-only listing
succeeded in 39.4 seconds; the server's directory-name enumeration took 0.017
seconds while samples of 100 file metadata requests took 0.425–0.584 seconds.
This separates the remote metadata cost from Mountain Turtle's extra work.

An authenticated local rclone goroutine snapshot showed a pending SFTP ReadDir
and multiple refresh jobs holding parent-directory locks while waiting on child
locks. Finder requests waited on that same tree. Background refreshes previously
started even when all eight monitoring slots were occupied. Finder badge requests
and periodic observation polling also triggered repeated ancestor refreshes.

The service now admits one refresh per mount generation and reserves monitor
capacity before submission. Confirmed completion admits subsequent work, including
after rclone expires the finished job record. Lost monitoring or an uncertain
submission does not authorize another overlapping job. Reconnection starts a new
generation. Routine badge processing does not launch ancestor refresh/prefetch.

Nine focused regression tests cover concurrent callbacks, slow-child and manual
refresh coalescing, independent mounts, expired completed jobs, uncertain HTTP
outcomes, monitor saturation, and capacity recovery. The full local suite passed
584 tests, including the native NFS/SFTP rename integration. The universal macOS
build passed deep signature, architecture, framework-link and version checks.

An isolated read-only rclone verification instance using the affected remote
coalesced seven duplicate/ancestor requests into one listing. It uses separate
temporary state and no Finder mount, file downloads or remote mutations.

The storage server's cold metadata-listing cost remains. No server configuration,
credentials, photo contents or existing caches were changed during diagnosis.
Signed asset preparation, GitHub validation, public delivery and the installed
update/relaunch check are separate release gates and must be verified before
claiming delivery complete.
