# Architecture and boundaries

[PROTOCOL.md](../PROTOCOL.md) defines CLI commands and JSON fields.

```text
SwiftUI connection manager and insights dashboard
    | local JSON CLIs
    v
Python supervisor <---- per-user launchd
    | one rclone child per saved S3 or SFTP connection
    v
Loopback NFS server <---- macOS NFS client <---- Finder / applications
    |
    +---- Amazon S3 through the selected AWS profile
    +---- SFTP through a verified SSH server and selected credentials
```

## Ownership and authentication

The app owns presentation and user choices. The standard-library Python service
owns saved records, supervision, and safe mount lifecycle. Rclone owns remote
filesystem operations and its local cache. AWS tools own S3 profiles and SSO;
Mountain Turtle does not save AWS access keys in connection records.

SFTP records select an SSH agent, a local private key file, or a Keychain password.
The bundled native credentials helper stores passwords in the user's macOS
Keychain. Password input and helper results travel through pipes; plaintext
passwords do not appear in argv, saved JSON, logs, or rclone configuration. The
service supplies rclone's obscured password through the child environment when
needed. Obscuring is not encryption and the local process boundary still matters.

SFTP requires an existing readable known-hosts file and verifies the remote key.
Unknown or changed identities fail closed. Private-key and known-hosts paths are
validated locally. Remote shell/hash commands are disabled, supporting SFTP-only
accounts. Passphrase-protected keys can be unlocked in the SSH agent before use.

NFS, rclone's authenticated control endpoint, and the Finder bridge bind only to
loopback. This excludes other computers; it is not a complete security boundary
against every process or user on the same Mac. The general command interface is
local, not a remote administration API.

## Connection lifecycle

Stable UUIDs identify connections. Display names are unique directory components
under `~/Mountain Turtle`. Records without `backend` retain legacy S3 behavior.
Each record explicitly selects remote details, read mode, cache targets, and
login preferences. New GUI and CLI connections default to read-only with
auto-connect disabled.

Saving changes local settings. Connecting validates the selected destination
without scanning unrelated buckets, recursively walking remote files, or creating
a test object. Mount presence is distinct from successful remote access: a mount
can outlive working credentials or networking. The app polls status every three
seconds; neither a successful command nor a running process proves usable content.

Normal unmount preserves busy volumes and pending writes. Editing/removal require
a disconnected drive. Pending writes block connection edits. Changing an SFTP
server, username, or port requires fresh password input rather than reusing a
saved secret against the new endpoint. Changing the backend
or destination clears safe cached data so it cannot be reused against a different
remote; rename preserves the cache identity. Removing a record does not delete
remote files and attempts to remove its saved Keychain item.

The per-user LaunchAgent restores selected drives after macOS sign-in. Turning
off startup preserves current mounts. The installed app supplies its stable
service path. AWS SSO renewal, an unavailable SSH agent, or a locked Keychain can
prevent reconnect; startup does not bypass authentication. Graceful shutdown
retains caches and reports drives that could not eject.

## Finder integration and photos

The sandboxed Finder Sync extension requests status only for displayed items
within configured mount roots. Its discovery file is
`~/Library/Application Support/Mountain Turtle/Finder/bridge.json`. The extension
has read-only discovery access and uses the authenticated loopback bridge; it does
not read AWS or SSH credentials.

The bridge rejects browser-origin requests, wrong hosts, missing authentication,
oversized batches, and paths outside configured mounts. Badge lookup reads local
rclone metadata through directory descriptors without following symlinks. Complete
byte coverage means cached; partial coverage means partially cached; dirty data
means pending upload. Ambiguous metadata yields unknown. No recursive remote
listing is needed. A badge does not prove active transfer, permanent offline
retention, or remote freshness.

Finder menus expose drive controls and metrics. The bridge marks S3 roots as
supporting the separate photo browser; SFTP uses Finder for photos. The S3 photo
browser pages listings and fetches bounded previews only for visible tiles; see
[photo browser details](PHOTO_BROWSER.md). Local volume artwork does not require
uploading icons to either backend.

## Metrics and cost estimates

The independent `drive_metrics.py` service separates local observations from cloud
queries. Local snapshots call only authenticated `core/stats` and `vfs/stats`.
Counters describe the current mount process, combine reads and writes, and omit
credentials and filenames. Current speed comes from consecutive observed byte
counters; resets and gaps do not become invented zero readings. The dashboard
collects at five-second intervals while open and retains at most 720 observations
from the last 24 hours with private file permissions.

S3 storage uses one bounded CloudWatch request for 30 days of daily size and
object metrics. Components align by observation day; incomplete totals remain
unknown. Remote storage queries are independent of whether the drive is mounted and
refresh once on opening or on manual request. For SFTP, rclone's `about` request
uses the server filesystem-statistics extension to return total capacity, used,
and free bytes. These can describe a shared filesystem rather than only the
selected folder. Unsupported servers remain unknown, with no shell or recursive
scan fallback. SFTP does not provide a price or bill, so no cost is inferred.

Automatic cost estimates fetch and cache the public regional AWS price list,
without sending bucket identifiers or credentials. Explicit storage-class and
pricing-tier mappings determine coverage. Unpriced nonzero components produce a
partial subtotal, never a complete estimate. A per-drive blended-rate override
and growth slider recalculate the dashboard locally. The 12-month graph is a
scenario based on the selected measurement and assumptions, not historical spend
or an invoice. [Metrics details](METRICS.md) document query limits, cache expiry,
pricing coverage, excluded charges, and sources.

## Current limits and evidence

Supported connection types are Amazon S3 and SFTP. Custom S3-compatible endpoints,
other cloud providers, a searchable photo catalog, a complete offline mirror,
and credential administration are outside the current UI. Large flat directories
remain expensive to enumerate; see the [large-library design](LARGE_PHOTO_LIBRARIES.md).

Tests separate local mocks from live remote proof. The dedicated
[Harvester SFTP fixture](../deploy/sftp-test/README.md) records disposable server
read/write and persistence checks. Its success does not establish every server's
behavior, S3 write durability, or reconnect after an actual Mac reboot.
