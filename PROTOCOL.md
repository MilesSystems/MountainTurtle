# Local service protocol (version 1)

The saved-record format remains version 1. Records without `backend` are treated
as S3, preserving existing connections.

```text
python3 service/turtle_service.py [--resource-dir RESOURCES] COMMAND [args]
```

Each command emits one JSON object. Except for `export-connection` and
`export-setup`, success is
`{ "ok": true, ... }`; errors use
`{ "ok": false, "error": "human explanation" }` and a nonzero exit status.
Status, inspection, and error responses do not print credentials. `export-setup`
returns private key material to the native app's captured pipe and refuses a
terminal as stdout. Treat its document output as a credential, not diagnostic
output. `--resource-dir` selects the packaged resources and native helpers; it
precedes the command.

## Status

`status` returns `ok`, `serviceRunning`, `launchAtLogin`, `dependencies`,
`profiles`, and `connections`. The GUI refreshes this every three seconds.

Dependencies include discovered `rclone`, `aws`, `python`, and `brew` paths;
`awsVersion`, `awsCliV2`, `rcloneVersion`, `rcloneNfsmount`; `appPath`,
`appInstalled`; and `privacyState` / `privacyMessage`. Privacy is `approved`,
`needsApproval`, `checking`, or `unknown`. AWS is required only for S3.

Every connection includes:

- `id` (UUID), `name`, `backend` (`s3` or `sftp`).
- `bucket`, `profile`, `region` (empty strings for SFTP).
- `readOnly`, `autoConnect`, `desiredConnected`, `mounted`.
- `state`: `disconnected`, `connecting`, `connected`, `disconnecting`,
  `needsLogin`, or `error`; plus `message`, `mountPath`, and numeric `updatedAt`.
- `cacheMaxSizeMiB` (default 2048), `cacheMaxAgeHours` (default 24),
  `fastBrowsing` (default false).
- Optional `sidebarItemID` / `sidebarError` while the drive is mounted.

SFTP records additionally include `host`, `user`, `port` (integer, default 22),
`remotePath`, `authMode`, `keyFile`, `knownHostsFile`, and `passwordConfigured`
(boolean). Status never includes the password or private-key contents.

`mounted` comes from the kernel mount table and is separate from the service's
connection state. Neither a running process nor a successful action request
alone proves remote access.

## Add and edit

```text
add --name NAME [--backend s3] --bucket BUCKET --profile PROFILE --region REGION
add --name NAME --backend sftp --host HOST --user USER [--port 22]
    [--remote-path PATH] [--auth-mode agent|keyFile|password]
    [--key-file PATH] [--known-hosts-file PATH] [--password-stdin]
edit ID <the same complete connection fields>
```

Both commands accept these common flags:

```text
[--read-only | --read-write] [--auto-connect]
[--cache-max-size-mib N] [--cache-max-age-hours N]
```

New connections default to read-only with auto-connect disabled. `--backend` defaults to
`s3`, including on edit: callers editing SFTP must send `--backend sftp` and the
complete SFTP fields. Access and cache fields omitted on edit preserve their previous
values; other omitted flags use their parser defaults. Success returns the
saved connection's `id`.

Editing requires a disconnected drive and no pending cached writes. Changing
the backend or remote destination clears safe local cache before reuse; changing
a display name alone keeps its cache identity. Names must be unique and safe as
one directory component beneath `~/Mountain Turtle`.

SFTP constraints:

- `host` is a hostname or IP address, without a scheme or appended port.
- `port` is 1–65535; `user` is an SSH username without spaces/control characters.
- Empty `remotePath` selects the server home folder. Absolute and home-relative
  paths are accepted; `~`, `..` traversal, and control characters are rejected.
- `authMode` defaults to `agent`. `keyFile` requires a readable existing private
  key; encrypted keys should be loaded into the SSH agent instead.
- `knownHostsFile` defaults to `~/.ssh/known_hosts`. Key-file and known-hosts
  paths must be readable existing files expressed as absolute or `~/` paths.
  Unknown and changed host keys fail verification; there is no trust-on-first-use
  or insecure bypass mode.
- Password authentication requires `--password-stdin` for a new password. The
  input is at most 16,384 characters without CR, LF, or NUL; the password is never
  passed in argv. A password-mode edit without input keeps the saved password only when host,
  user, and port are unchanged; a changed endpoint requires fresh password input.
  Passwords are stored by the built app's macOS Keychain helper, not in saved
  JSON or rclone configuration. Switching away from password authentication or
  removing the record attempts to delete the obsolete Keychain item.

## Portable connection files

```text
export-connection ID
inspect-connection < connection.turtle
```

`export-connection` reads one saved record and emits the portable JSON document
itself, without an `ok` field. It is safe while the source drive is connected.
`inspect-connection` reads at most 65,537 bytes from binary stdin, rejects input
larger than 64 KiB, and returns `{ "ok": true, "connection": { ... } }` with
validated, normalized editor fields. These commands do not change saved settings,
credentials, caches, startup registration, or mounts. Inspection does not read
the connection store; duplicate names are resolved when the reviewed connection
is added through the existing `add` command.

The settings-only document has this envelope:

```json
{
  "format": "io.mountainturtle.connection",
  "version": 1,
  "connection": {
    "name": "Photo archive",
    "backend": "s3",
    "bucket": "photo-archive",
    "profile": "archive-reader",
    "region": "us-west-2",
    "readOnly": true,
    "autoConnect": false,
    "cacheMaxSizeMiB": 2048,
    "cacheMaxAgeHours": 24,
    "fastBrowsing": false
  }
}
```

SFTP replaces the S3 destination fields with `host`, `user`, `port`, `remotePath`,
and `authMode`. Only portable settings are exported. Passwords, private keys,
AWS credentials, local key/known-hosts paths, saved IDs, and runtime state are
absent. `autoConnect` is always exported and normalized as `false`; source login
restore preferences do not transfer. Import selects local authentication files.
Unknown formats/versions, invalid field types
or values, and malformed documents produce the normal JSON error response.

The native app additionally uses these commands for complete key setups:

```text
export-setup ID
inspect-setup < connection.turtle
import-setup --name NAME < connection.turtle
```

`export-setup` emits the setup document itself, without an `ok` field, through a
captured pipe. `inspect-setup` returns only `{ "ok": true, "connection": { ... } }`
after validating the entire document; private key material is never echoed by
inspection. `import-setup` accepts the reviewed name and setup document, installs
credentials, and returns `{ "ok": true, "id": "UUID" }`. It saves a disconnected
drive; the GUI sends `connect ID` separately if Connect now is selected. These
commands accept the unwrapped setup document. Protected files are decrypted in
the native app before inspection or import.

The complete SFTP key setup document contains exactly five fields:

| Field | Content |
| --- | --- |
| `format` | `"io.mountainturtle.setup"` |
| `version` | Integer `1` |
| `connection` | Validated portable SFTP settings, using `authMode: "keyFile"` |
| `privateKey` | Canonical base64 encoding of one unencrypted SSH private key |
| `knownHosts` | Canonical base64 encoding of pinned known-hosts entries for the exact destination host and port |

The document is JSON, not a ZIP archive. Its maximum size is 64 KiB; decoded
private keys are limited to 32 KiB and decoded known-hosts content to 16 KiB.
Export can filter a source known-hosts file up to 4 MiB. Hashed host entries that
match the endpoint are accepted and normalized to explicit endpoint entries.
Wildcard, negated, comma-separated alias, marker/CA, and unrelated-host entries
are not imported. Duplicate JSON fields and unsupported fields are rejected.
Key validation supports Ed25519, RSA, and NIST P-256/P-384/P-521 ECDSA keys and
runs bounded, noninteractive local checks; validation never connects to a
server. No public key file needs to be transferred separately.

The setup cannot install arbitrary archive members, local paths, global trust
entries, or SSH configuration. AWS credentials are not supported. The
settings-only CLI commands above retain their existing scope.

The key-file export UI defaults to **Ready to connect on another Mac** with password protection
enabled. The exporter can choose settings only or turn protection off for a
complete key setup. An unprotected complete setup contains a usable private key.
Saved SFTP passwords are included only in protected exports. Protected files are
created and opened in the native app. The wrapper is JSON with
`format: "io.mountainturtle.encrypted-connection"`, `version: 1`,
`cipher: "AES-256-GCM"`, `kdf: "PBKDF2-HMAC-SHA256"`, `iterations: 600000`, a
base64 `salt` (16 random bytes), and a base64 `sealedBox` (12-byte nonce,
ciphertext, and 16-byte authentication tag). The derived encryption key is 256
bits. AES-GCM also authenticates the UTF-8 bytes
`io.mountainturtle.encrypted-connection\n1\nAES-256-GCM\nPBKDF2-HMAC-SHA256\n600000`,
where each `\n` is one LF byte and there is no trailing LF. The encrypted payload
is JSON containing a base64 `document` with either settings-only or complete
setup bytes and, for SFTP password authentication, `sftpPassword`.
Wrapper input is limited to 256 KiB. Export passphrases require at least 12
characters and at most 1,024 UTF-8 bytes. Saved SFTP passwords are limited to
16,384 UTF-8 bytes without CR, LF, or NUL.

For a protected SFTP password export, the native app reads the selected drive's password from
the bundled Keychain helper through a private captured pipe and encrypts it in
memory. It does not pass the password in argv or write a plaintext temporary
file. Imported passwords reach the existing `add --password-stdin` path only
after the user chooses **Import connection** in the review. Wrong passphrases or damaged ciphertext do not
yield connection fields or save anything.

The app exports `.turtle` by default and accepts legacy `.mountainturtle` files.
Both extensions are registered as Mountain Turtle connection documents
with the document role `Viewer`. A `.turtle` file can hold a settings-only
document, a complete setup document, or the encrypted wrapper. Window drops,
**Import connection**, and Finder opens share the same validated import path.

Complete setups present a simple name/account/server review. **Add connection**
creates a new saved identity and installs the key and pinned known-hosts file
under an app-owned, per-UUID Application Support directory, with directory mode
`0700` and file mode `0600`. Imported paths never determine the installation
location. Failed saves roll back the new credential directory; a later connection
failure leaves the saved drive available to retry. **Connect now** defaults to
true but can be disabled. This does not set `autoConnect` or change login startup
preferences. Duplicate imports never overwrite saved connections or credentials.

Settings-only documents retain the full editor and start disconnected after
saving. The original file and existing connections remain unchanged by opening
or canceling either review. No import modifies global `~/.ssh` files or config.

## Lifecycle and drive controls

| Command | Behavior |
| --- | --- |
| `connect ID` | Requests connection and starts the service if needed. |
| `disconnect ID` | Requests native non-forced unmount; pending writes wait and busy mounts remain attached. |
| `reconnect ID` | Safely ejects and reconnects. A later disconnect cancels reconnect intent. |
| `remove ID` | Disconnected only; removes the saved record without deleting remote files. |
| `login ID` | S3 only: AWS SSO device authorization with a five-minute timeout. |
| `refresh ID` | Mounted only: refreshes rclone directory listings through the authenticated loopback control API, recursively and asynchronously where supported, without fetching file bodies. Falls back to invalidating listings if the live control request is unavailable. |
| `rename ID --name NAME` | Disconnected only; preserves remote destination and cache identity. |
| `access ID --read-only\|--read-write` | Disconnected only when changing mode; rejects pending/ambiguous writes and preserves destination, cache, authentication, and startup preferences. An unchanged mode is a no-op. |
| `settings ID [--cache-max-size-mib N] [--cache-max-age-hours N] [--fast-browsing\|--precise-browsing]` | Disconnected only; size 64–1,048,576 MiB, age 1–8,760 hours, soft eviction targets. Fast browsing is S3-only: it uses S3 server listing timestamps, recursive directory warming, `--fast-list`, a longer directory cache, and a short attribute cache. |
| `cache-info ID` | Bounded local scan returns `ok`, `usedBytes`, `files`, `partial`; partial usage is a lower estimate. |
| `clear-cache ID` | Disconnected only; rejects pending/ambiguous writes, including previously writable caches. |
| `autostart on\|off` | Updates the per-user LaunchAgent without stopping current mounts. |
| `serve [--at-login]` | Foreground supervisor used by launchd. |
| `shutdown` | Requests safe disconnection of all drives, retains cached data, reports pending mounts. |

Clear-cache affects the local rclone cache, not remote files, explicit Downloads
copies, or the photo thumbnail cache. The GUI saves connection intent, safely
ejects, waits, applies rename/access/settings/clear-cache, then restores that intent.
Access changes use the same safe ejection path for S3 and SFTP. The saved access
mode stays unchanged if a drive is busy, cached uploads are still pending, or the
save fails. The next mount uses the chosen read-only or read/write mode.

## Finder actions and helpers

```text
mountainturtle://connection/UUID?action=ACTION
mountainturtle://open
```

Allowed actions are `finder`, `browse`, `metrics`, `settings`, `rename`,
`refresh`, `reconnect`, and `eject`. The app accepts exactly one allowlisted query
parameter and resolves UUIDs against current saved records. URLs cannot contain
filesystem paths, credentials, or arbitrary commands. SFTP `browse` opens Finder
when mounted and explains that the separate photo browser is S3-only.

The authenticated Finder bridge's `/roots` result includes `supportsPhotoBrowser`
per root. The extension hides its photo action for SFTP; metrics remain available.
Bridge tokens and filesystem paths are not accepted through deep links.

The supervisor runs the bundled `Mountain Turtle Sidebar` helper with
`ensure UUID MOUNT_PATH` once per confirmed mount generation, with serialized
work, an eight-second deadline, and cancellation on ejection. Failure does not
prevent mounting. The helper verifies a kernel NFS mount under the Turtle mount
root and updates owned FavoriteVolumes entries through public SharedFileList
APIs. Its private registry preserves unrelated Finder favorites. That API is
deprecated; Finder's manual Add to Sidebar is the fallback.

## Photo, activity, and metrics services

The S3 connection card reads bucket type and observed storage classes separately
from the frequent status poll:

```text
python3 service/s3_details.py [--resource-dir RESOURCES] details ID [--refresh]
```

The JSON includes `bucketType`, `storageClass`, `storageClasses`, `status`,
`message`, `sourceTimestamp`, `queriedAt`, `cached`, and `isStale`. Bucket type
uses AWS's reserved naming rules. Storage classes use the existing bounded,
read-only CloudWatch storage reader without listing objects or fetching prices.
Multiple classes are labeled mixed; missing, partial, stale, and authentication
results remain explicit. Results are cached per connection and bucket/profile/
region for one hour (five minutes for failures); `--refresh` bypasses that cache.

The independent `photo_browser.py` CLI is **S3-only**; see
[photo browser protocol](docs/PHOTO_BROWSER.md).

```text
python3 service/drive_metrics.py [--resource-dir RESOURCES] snapshot ID
python3 service/drive_metrics.py [--resource-dir RESOURCES] storage ID [--rate NUMBER]
```

`snapshot` reads authenticated loopback rclone statistics for either backend,
returning current values and bounded local history. `storage` reads daily S3
CloudWatch metrics and automatic public regional storage pricing. For SFTP it
requests server filesystem statistics and returns `totalBytes` (filesystem
capacity), `usedBytes`, `freeBytes`, `scope: "remoteFilesystem"`, measurement time,
and bounded capacity history. Unlike S3's `totalBytes` (stored object bytes),
SFTP `totalBytes` is capacity: clients must branch by provider. Missing extension
support returns an unsupported result without a shell or recursive-scan fallback.
SFTP pricing remains unavailable. `--rate` supplies a blended USD/GiB/month
assumption instead of fetching prices. Missing measurements and incomplete total
estimates are `null`, not zero. See [metrics schema, limits, and pricing
assumptions](docs/METRICS.md).

The S3 dashboard also calls an independent request-metrics API:

```text
python3 service/cloud_activity.py [--resource-dir RESOURCES] snapshot ID --hours 1|6|24
```

This reads an existing whole-bucket S3 request-metrics configuration and returns
`scope: "bucketAllClients"`, source/observation timestamps, publication lag,
selected-window byte/request/error summaries, coverage, and minute-level history.
Missing configuration returns `notConfigured` or `configurationIncomplete`;
no command enables paid metrics. Upload traffic is never converted to net bucket
growth. Configured activity refreshes every minute while the dashboard is open.

The metrics dashboard polls local snapshots about every five seconds only while
open. Daily storage and pricing load on opening or manual refresh. The collapsed
scenario's persisted optional rate override and growth assumption recalculate
locally without cloud calls. Published AWS rates remain separate from those
planning inputs.

## Actual AWS spend

The S3 connection card also uses this account-wide report. Alongside `total`,
`currency`, and the completed-day billing period, the response includes
`projectedTotal`, `projectionMethod`, `projectionEstimated`,
`projectedPeriodEnd` (exclusive UTC boundary), `observedDays`, and `daysInMonth`.
For a complete report, `completedDaysRunRate` projects the full calendar month
from the month-to-date daily average, preserving the report's currency and
credits. Missing or partial reports have no projected total. This local estimate
does not call the AWS forecast API and reuses cached raw billing responses.

```text
python3 service/cloud_billing.py [--resource-dir RESOURCES] costs ID
```

This separate read-only Cost Explorer request returns account-wide S3 spend,
covering all buckets and regions in the stated account scope. It does not
attribute the account total to the selected bucket. The response includes
`source`, `scope`, `accountID`, `currency`, `monthStart`, exclusive `periodEnd`,
`queriedAt`, `cached`, `total`, `estimated`, `history`, `message`, and `assumptions`.
Daily history contains `date`, Unix `timestamp`, signed `amount`, and `estimated`;
negative credits must remain negative. Missing amounts are `null`, not zero.

The period ends before today's incomplete UTC day. AWS reporting can lag by
24 hours or more, and provisional amounts can be revised. Results are cached
for six hours; normal refresh respects that cache. Each uncached Cost Explorer
request costs $0.01. The command does not enable billing features. Permission,
authentication, no-data, partial, unavailable, and unsupported outcomes remain
separate from public-price estimates and bucket storage.

## Paths and setup

State lives under `~/Library/Application Support/Mountain Turtle`, cache under
`~/Library/Caches/MountainTurtle`, logs under `~/Library/Logs/MountainTurtle`, and
mounts under `~/Mountain Turtle`. Rclone's NFS and authenticated control listeners
bind loopback. Connecting does not scan unrelated storage or create a test file.
SFTP uses no remote shell/hash commands.

Packaged resources include the service scripts and icon-overlay assets. Finder
metadata is materialized locally, never uploaded to the remote. The LaunchAgent
references the stable service script inside the installed app.

The setup sheet checks the installed app location, rclone NFS support, AWS CLI
v2 when needed for S3, and Network Volumes permission. It can install rclone alone
or include AWS CLI. With no Homebrew, it opens the official Homebrew installer in
Terminal. Privacy is read from macOS's current Network Volumes record when
available, then falls back to sidebar registration evidence; setup does not
bypass that permission.
