# Mountain Turtle

Mountain Turtle is a free, MIT-licensed macOS app that connects Amazon S3 buckets
and SFTP servers as real Finder network volumes. Its native SwiftUI window manages
saved connections, authentication, reconnecting at login, transfer and cache
charts, and S3 storage and cost estimates. A small local Python service supervises
rclone's NFS mounts.

This is an early macOS release. The interface and app artwork are original
project work; Mountain Duck's code and artwork are not used or bundled. There is
no subscription or license activation. Remote storage providers can still charge
for storage, requests, and data transfer.

## Requirements

- macOS 14 or later.
- Python 3.9 or later, installed separately; the service uses only its standard library.
- [rclone](https://rclone.org/install/) with the `nfsmount` command.
- **For S3 only:** [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
  and an AWS profile with access to the bucket. S3 storage metrics also need
  `cloudwatch:GetMetricData` permission. Actual spend needs Cost Explorer access
  (`ce:GetCostAndUsage`); these extra insights permissions are independent of mounting.
- **For SFTP:** a ready-to-connect `.turtle` file, or a server account, a verified
  server key in a local known-hosts file, and an SSH agent key, private key file,
  or password.
- Apple's Xcode Command Line Tools to build from source (`xcode-select --install`).

Check the tools before building:

```sh
python3 --version
rclone version
rclone nfsmount --help
aws --version # S3 only
xcrun swiftc --version
```

The app uses the macOS NFS client through rclone; a separate FUSE driver is not
required. The NFS listener binds to `127.0.0.1` and is for this Mac, not a network
file server for other computers. Upstream still labels `nfsmount` experimental.
See [rclone's macOS NFS documentation](https://rclone.org/commands/rclone_nfsmount/#nfs-mount).

Mounted drives use the remote's filename case rules rather than adding macOS
case-insensitive aliases. On S3 and case-sensitive SFTP servers, `Highschool` and
`highschool` are distinct names. This also allows native case-only renames without
mistaking the new name for an existing destination.

On first launch, Mountain Turtle shows a setup checklist for the installed app
location, rclone's `nfsmount` support, and macOS Network Volumes
permission, including the current macOS Privacy & Security toggle when readable.
AWS CLI is optional for SFTP-only use. If Homebrew is already
installed, the app can install `rclone` and optionally `awscli`. If Homebrew is missing, Mountain Turtle opens a visible
Terminal installer that runs Homebrew's official install script and then installs
the required packages.

## Backend support

| Connection type | Authentication | Finder and local metrics | Cloud storage and cost | Photo browser |
| --- | --- | --- | --- | --- |
| Amazon S3 | Saved AWS profile, including SSO | Yes | Bucket-wide activity, daily storage, AWS rates, and account S3 spend where authorized | Yes |
| SFTP | SSH agent, private key file, or Keychain password | Yes | Server filesystem capacity where supported; no inferred price | Browse files in Finder |

Other rclone backends and custom S3-compatible endpoints are not exposed by this
release's connection editor.

## Report a problem

Open a [GitHub issue](https://github.com/MilesSystems/MountainTurtle/issues/new)
with your Mountain Turtle and macOS versions, S3 or SFTP connection type, the
steps that triggered the problem, expected result, and exact error or screenshot.
For rename problems, include each previous and new name, preserving capitalization.
Remove private paths and account details from screenshots or log excerpts; do not
attach credentials or your saved connection files. Logs are in
`~/Library/Logs/MountainTurtle/`.

## Build and install

### App updates

Mountain Turtle checks the signed update feed on [GitHub Releases](https://github.com/MilesSystems/MountainTurtle/releases)
daily. Choose **Check for Updates…** in the app menu, menu-bar icon, or connection
sidebar to check immediately. When a newer version is available, Sparkle shows
its release notes, downloads and verifies the update, and offers **Install and
Relaunch**. You can turn automatic checks off in the sidebar. Updates do not
download or install until you choose to update.

Before installing, the app safely ejects connected drives and saves your
connection choices. A busy drive stops the update; cached files are preserved.
The relaunched app restores the prior connection choices. Ordinary app quits
continue to keep drives connected.

Versions before 0.7.0 need one manual installation to gain the updater. Release
builds support both Apple silicon and Intel Macs. See [Releasing](docs/RELEASING.md)
for version tags, signed release assets, and the distinction between development
previews and notarized distribution.

From this checkout:

```sh
./scripts/build.sh
./scripts/install.sh
open "$HOME/Applications/Mountain Turtle.app"
```

The build creates `build/Mountain Turtle.app`; the install script places it in
`~/Applications/Mountain Turtle.app`. Use the installed copy when enabling login
startup: the background service must keep a stable path to the app's resources.
Updates preserve the previous app as a verified ZIP archive under
`~/Library/Application Support/Mountain Turtle/Backups`, keeping backup apps out
of macOS app registration and permission identity lookup. The installer restores
the previous app if its final replacement fails. Use the archived app for rollback.
Python and rclone are external dependencies, not bundled executables. AWS CLI
is also external and is required only for S3. The build bundles the native
Keychain and Finder-sidebar helpers.

To build a drag-to-install disk image:

```sh
CODE_SIGN_IDENTITY="Apple Development: Your Name (TEAMID)" ./scripts/package-dmg.sh
```

The DMG uses the committed Mountain Turtle installer artwork in
`Resources/dmg-background.png` and includes the app plus a Finder Applications
alias.
A free Apple Development certificate can sign local builds, but clean
distribution outside your Mac requires Apple's Developer ID signing and
notarization flow.

For repeated development updates, set `CODE_SIGN_IDENTITY` to an existing Apple
Development or Developer ID signing identity when building. Using the same
identity lets macOS recognize subsequent updates. The default is ad hoc signing,
which can require granting Network Volumes permission again after each rebuild.
Local signing is not notarization or App Store distribution.

For a temporary development launch after building:

```sh
open "build/Mountain Turtle.app"
```

## Connect an S3 bucket

1. Configure your AWS profile if needed. For SSO, follow the
   [AWS IAM Identity Center setup](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html).
2. Add a connection with a display name, bucket name, AWS profile, and bucket
   region. Each connection names one bucket; adding a profile does not mount all
   of its buckets.
3. Keep **Read only** enabled for browsing. New connections default to read only
   and do not automatically connect until you choose that option.
4. Sign in when needed, then connect and open the volume in Finder.
5. To reconnect after restarting the Mac, enable login startup and the individual
   connection's automatic connection setting.

## Connect an SFTP server

1. Connect with SSH/SFTP first and verify the server fingerprint with its
   administrator. Save that verified host key in a local known-hosts file.
   Mountain Turtle rejects unknown or changed keys; it does not automatically
   trust a server.
2. Choose **Add connection → SFTP server**. Enter a drive name, server hostname
   or IP address, SSH username, and port (default `22`). Do not include `sftp://`
   or a port in the server field.
3. Leave **Remote folder** blank for the server home folder, or enter an absolute
   path or a path relative to that home folder. `~`, parent traversal (`..`), and
   control characters are rejected.
4. Choose **SSH agent**, **Private key file**, or **Password**. Load an encrypted
   key into the SSH agent first and use agent authentication. Passwords are saved
   in macOS Keychain; leaving the password blank keeps it when the server, username,
   and port are unchanged. Changing those fields requires a new password. Use the built app for password authentication.
5. Choose the verified **Known hosts** file (default `~/.ssh/known_hosts`) and,
   when applicable, the private key file. Keep **Read only** enabled initially,
   save, connect, and open the drive in Finder.

Key files and known-hosts files must already exist and be readable. Agent
connections require an available SSH agent containing the unlocked key when
connecting or restoring at login. SFTP mounts use SFTP operations without remote
shell/hash commands; an SFTP-only account is supported.

The dedicated [Harvester SFTP test drive](deploy/sftp-test/README.md) documents a
LAN fixture, its verified host key, disposable write checks, and recorded live
verification. It is not required for ordinary server connections.

## Move a connection to another Mac

Mountain Turtle 0.6 exports a single `.turtle` file. Send that file to the other
Mac, then double-click it in Finder, drag it into Mountain Turtle, or choose
**Import connection**. Older `.mountainturtle` files still open.

For an SFTP connection that uses a private key, **Export connection** offers
**Ready to connect on another Mac**. This includes the connection settings, its unencrypted SSH
private key, and the verified keys for that server. Password protection is on by
default; the exporter can turn it off. A protected file asks for its export
password when opened. The recipient sees the drive name, account, and server,
then chooses **Add connection**. **Connect now** is selected by default and can
be turned off. No separate key files or SSH configuration are needed.

The app installs these credentials in its own Application Support folder, with
private permissions, and saves the drive under a new identity. It does not edit
`~/.ssh`, SSH configuration, or other drives. Opening a file and canceling its
review saves nothing. A duplicate name gets a suggested unique name; importing
again never replaces an existing drive. Connect now does not enable connection
at login.

**Connection settings only** remains available for SFTP and S3. It includes the
name, destination, authentication method, read-only setting, and cache limits.
On the receiving Mac, review the settings and select local SSH credentials or
configure and sign in to the named AWS profile before connecting. For an SFTP
connection that uses a saved password, a password-protected export also includes
that password; an unprotected settings export never does. Imported saved
passwords are written to Keychain only when the reviewed connection is saved.
Settings-only imports start disconnected.

Exports never include AWS credentials, SSO sessions, cached files, the sender's
local file paths, or global SSH trust settings. Ready-to-connect exports require
an unencrypted private key file; encrypted SSH keys used through an agent remain
settings-only. All imports preserve the source read-only setting and cache
limits, and leave login startup preferences unchanged.

An unprotected ready-to-connect file contains a working private key, so anyone
with that file can use the account. Password-protected files use authenticated
AES-256-GCM encryption with a fresh salt and nonce and PBKDF2-HMAC-SHA256
passphrase derivation (600,000 iterations). Use at least 12 characters and share
the password separately; Mountain Turtle cannot recover a forgotten password.

## Finder volumes and login restore

Connected volumes live under `~/Mountain Turtle/<connection name>` and are
actual NFS mounts. In Finder Settings, enable **Connected servers** under General
for desktop icons and under Sidebar for the sidebar's Locations section.
Custom volume artwork is provided locally; it does not require uploading an icon
to your remote storage.

Mountain Turtle registers each mounted drive directly under Finder's Locations,
with its own eject button, and refreshes that entry after reconnecting or renaming.
This also runs from the login service while the app window is closed. macOS may
show a separate `localhost` server entry for the underlying transport connection.
Allow Mountain Turtle's normal Network Volumes permission request. If it was
denied, review **System Settings → Privacy & Security → Files and Folders →
Mountain Turtle → Network Volumes**, then reconnect the drive.
If sidebar registration fails, the drive remains connected; choose **Go →
Computer**, select the actual Turtle volume, then **File → Add to Sidebar**.
The isolated native helper uses Apple's public SharedFileList compatibility API,
which is deprecated and may require adaptation on future macOS versions.

Startup happens after you sign in to macOS. For S3, it cannot bypass an expired AWS SSO
session: use **Sign in to AWS** or
`aws sso login --use-device-code --profile PROFILE` when renewal is required.
The app uses AWS device authorization: approve the request on the AWS page and
wait for Mountain Turtle to confirm completion. This flow does not redirect to a
temporary localhost callback. If sign-in times out after five minutes, close the
previous sign-in tab and choose **Sign in to AWS** to start a fresh request.
Credentials remain under the AWS tools' management;
Mountain Turtle saves connection settings rather than AWS access keys.

For SFTP servers on your LAN, allow **Local Network** access when macOS asks.
If a connection works in Terminal but not in Mountain Turtle, check **System
Settings → Privacy & Security → Local Network → Mountain Turtle**, then retry.
The login service identifies Mountain Turtle as its responsible app so macOS can
associate this permission with the app.

## Photo dates

Mountain Turtle's **Browse photos** view shows **Date taken** from the camera's
embedded metadata. Use **Sort this page** to order the current page by name or
capture date. Missing dates explain what is known: **Not checked**,
**Needs original**, **No camera date**, or **Couldn't read—retry**. Dates use the camera's
recorded local time. Reading dates can fetch small JPEG headers, but never a
full large original or an entire folder in the background. Other formats can
show their date once the original is available locally. See the
[photo browser guide](docs/PHOTO_BROWSER.md#date-taken-and-sorting) for details.

## Keep photos offline

In **Browse photos**, select individual photos or **Select page**, then choose
**Keep offline**. The download queue shows progress and lets you pause, resume,
retry, and open saved photos. Closing the photo window does not cancel downloads.
Paused downloads remain paused until you resume them.

Offline originals live in a private folder under
`~/Library/Application Support/Mountain Turtle/offline-photos/`, separate from
the temporary drive and preview caches. Mountain Turtle never evicts these
originals during cache cleanup. Use the queue's **Reveal** action to find them.
They remain until you remove them yourself; protect this folder with your normal
Mac backups. Opening these originals does not depend on the mounted drive.

**Verified** means the downloaded bytes matched S3's supported full-object
checksum and the saved file passed a local SHA-256 reread. When S3 cannot supply
a supported checksum, the queue says **Downloaded**, explains that limitation,
and retains a local SHA-256 for later checks. Neither byte count nor a green
cache badge alone means a transfer is verified. A changed cloud version or
modified local copy stops that item's download; existing files are preserved.

## Finder file badges

Choose **Enable Finder badges…** in Mountain Turtle and enable its Finder
extension in macOS. Finder can then show a status badge and label on each item
you browse inside a connected Mountain Turtle volume.

| Badge | Meaning |
| --- | --- |
| Cloud | Online only; no local file cache is recorded. |
| Green check | Cached on this Mac; the file's entire contents are cached. |
| Partial cache | Only part of the file is cached. This does not prove an active transfer. |
| Blue arrows | Waiting to upload; local changes remain pending. |
| Spinning arrow | Folder contents are loading after opening a folder row. |
| Pie circle | A keep-downloaded folder is queued or warming into the local cache. |
| Unknown | Status is unavailable or cannot be established safely. |

Cached files can be evicted when the cache fills. A green check alone does not
pin a file for permanent offline access or establish that the remote copy has
not changed. The extension requests status for visible items using local cache
metadata; badge checks do not scan remote storage or download photos.
When a folder disclosure row is opened, Mountain Turtle can briefly show a
loading badge on that folder while Finder asks for its visible children.
Opening a folder also asks the mounted drive to refresh that folder's remote
listing, so deletes or renames made elsewhere can appear without waiting for the
longer directory cache to expire. While the folder remains open, Mountain Turtle
also prefetches that folder's direct files in the background so large folders
become responsive as their contents enter the local cache. This opportunistic
prefetch is shallow, cancellable, and bounded by the drive's cache settings.
Folders explicitly marked **Keep This Folder Downloaded** show a pie-style
progress badge while their cache-warming job is queued or running. If files are
deleted or renamed elsewhere while Mountain Turtle is warming a folder, it backs
off from that pass and retries later instead of grinding through the stale list.

The Turtle toolbar button and contextual menu provide **Browse photos** for S3,
**Keep This Folder Downloaded**, **Show drive in Finder**, **Refresh folder listings**,
**Reconnect**, **Drive insights**, **Download & cache settings**, **Rename drive**,
and **Eject**. If the toolbar button is hidden, right-click Finder's toolbar,
choose **Customize Toolbar**, and add Mountain Turtle. Rename and cache changes
safely eject and reconnect when needed; busy files or pending uploads can prevent
the change.

The selected drive's **Activity queue & history** shows recent local work with
icons for move/rename, delete, upload, folder download, and failures. Finder
folder renames can appear as move operations because remote storage handles them
that way, and large deletes are grouped into a single counted queue item.
Download/cache-read failures are tracked separately from upload failures so
remote deletes encountered during cache warming do not look like local upload
conflicts.
Completed operations remain as recent history. This history is local to this
Mac's mounted drive session; changes made from other computers or cloud consoles
are not a complete audit trail here.

## Files, caching, and disconnecting

File contents are fetched on demand and cached on this Mac. A connected volume
is not a complete offline copy, and Finder previews can trigger downloads.
Remote latency and valid server credentials still matter for files that are not cached.

Right-click a folder in a connected Mountain Turtle drive and choose **Keep This
Folder Downloaded** to recursively read its files into the local mount cache. You
can keep the folder refreshed indefinitely, keep it for 24 hours, 7 days, or 30
days, or stop keeping it downloaded. Disconnecting pauses folder warming until
the drive is connected again. Stopping a keep rule does not delete already cached
files; they age out under the drive's cache settings. This is mount-cache warming,
not a durable archive: the cache size and free-space safeguards can still evict
data, and Mountain Turtle will retry while the rule remains active.

Finder shows the remote file's modification time. For S3, rclone reads the saved
modification time from object metadata, falling back to the S3 last-modified
time when no saved time exists. This can add metadata requests while browsing;
it does not download file contents. These dates are not photo capture dates.
After upgrading from a version that showed a fixed December 31, 1999 or
January 1, 2000 date, reconnect the drive to refresh Finder's file attributes.

S3 drives can enable **Fast folder browsing** in **Download & cache settings**.
That mode favors Finder responsiveness for large buckets: it uses S3 listing
timestamps instead of per-object metadata timestamps, enables recursive directory
warming on connect and refresh, and keeps folder listings warm longer. It still
does not download file contents, but Finder may show upload/last-modified dates
instead of the original source file modification dates.

**Date Created is unavailable on the current NFSv3 mounts.** This protocol does
not carry a file creation time, so Finder may show December 31, 2000 or
January 1, 2001 as a placeholder. That value is not the file's original creation
date. Camera capture dates, when present, remain in the photo's embedded metadata.
See the [NFSv3 file attributes](https://www.rfc-editor.org/rfc/rfc1813.html#section-2.5).

S3 folder timestamps are also unavailable: these folders are inferred from
object-name prefixes, and rclone returns an unknown modification time for
them. Finder can therefore show the 1999/2000 placeholder or a date maintained
by the current mount session. These are not the original folder dates; the
file modification-date fix does not recover them. Recovering original folder
dates requires the source folders or separately preserved metadata. See
[how S3 folders work](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-folders.html).

The default original-file cache target is 2 GiB, with removal after 24 hours
without access. Both values and S3 fast folder browsing are configurable per
drive. Memory buffering, rclone read-ahead, parallel chunk prefetch, and native
NFS read-ahead are disabled. Sequential reads use chunks no larger than 1 MiB.
These settings reduce extra reads, but cannot stop Finder from explicitly
reading originals for previews.
Cache size and age are eviction targets, not a cap on total downloads; open or
dirty files may remain beyond the targets. **Clear cache** only removes safe
local cached copies after ejection, never remote files or pending uploads.

To reduce Finder downloads, turn off **Show icon preview** in View Options
(⌘J) and hide the Preview pane. Finder cannot distinguish a preview read from
an application opening the original through this NFS mount.

Read-only connections block remote edits. If you explicitly enable writing,
normal file actions can upload, overwrite, rename, or delete remote files using
the saved account's permissions. Mountain Turtle records those local mounted
drive operations in the selected drive's Activity queue & history when rclone
reports them. The isolated SFTP fixture has separate live write verification; its
results do not establish write durability for every server or S3 bucket.

To change a saved drive’s access, choose **Change…** beside **Access**, toggle
**Read only**, and choose **Save access**. A connected drive safely ejects and
reconnects to apply the setting. Open files or pending uploads prevent the change
until they are closed or finished.

S3 connection details show **S3 type** and **Storage class** above **AWS profile**.
Storage classes come from AWS daily storage metrics, so they can lag recent
changes. Buckets using several classes show a mixed result; missing metrics
remain unavailable. Use the refresh button beside the storage class to check
again, and hover over the value for the observation date and details.

The same card shows **Actual cost** for the completed days of this month and
**Projected** spending for the full month at the reported daily average. Both
amounts cover **all S3 buckets and regions in the AWS account**, using Cost
Explorer's reported currency and retaining credits. The projection assumes that
the month-to-date daily average continues; recent changes in storage or usage
can change that pace. Incomplete billing reports do not produce totals or
projections. Billing refreshes share the existing six-hour cache with Drive
insights, and the projection itself makes no additional AWS request.

Disconnect through the app or eject the volume in Finder. If a volume is busy,
close the files or applications using it and try again. Mountain Turtle does not
force-detach a busy volume or discard its cache. Removing a disconnected saved
connection removes its local configuration and attempts to remove its saved
Keychain password; it does not delete remote files.

## Drive insights: remote activity, storage, and AWS cost

Choose **View metrics** on a drive, or **Drive insights** in Finder's Turtle menu.
For S3, the dashboard leads with **Bucket activity · all computers**, using AWS
CloudWatch request metrics. It shows reported upload/download bytes and
throughput, PUT/GET counts, errors, and latency for 1-, 6-, or 24-hour windows.
Uploads from another computer are included without this Mac reading the files or
mounting the drive. AWS observation time, publication lag, and coverage are shown;
missing minutes remain unknown.

Whole-bucket S3 request metrics must already be configured. These are paid AWS
metrics; the dashboard explains missing configuration without enabling it.
Collection begins after enablement, without historical backfill. Configured
activity refreshes every minute while the dashboard is open.

**Bucket storage** separately shows daily CloudWatch totals, 30-day histories,
and storage-class breakdowns. Ongoing uploads can appear in later daily reports.
Upload traffic is not treated as net size growth: overwrites, deletions, multipart
uploads and versions affect the result. Remote storage refreshes once on opening
or through **Refresh storage**, without recursively scanning the bucket.

**AWS storage pricing** displays real products, SKUs, effective rates, region,
and source dates from Amazon's public Price List API. The monthly storage estimate
uses the dated daily size measurement. Unpriced nonzero classes keep the full
estimate unknown; any known subtotal is labeled. Actual AWS account S3 spend is
shown separately where Cost Explorer access is available, with explicit account
scope rather than presenting it as one bucket's bill. Billing can lag 24 hours
or more; results are cached for six hours. Each uncached Cost Explorer API
request costs $0.01.

**Explore a scenario** is collapsed initially. Its optional blended-rate override
and growth assumption are planning inputs, separate from the published rates and
actual spend. Scenarios exclude requests, transfer, retrieval, and other charges.
**This Mac: transfer & cache** separately contains this mount's local telemetry;
those counters do not describe uploads from other computers. Local observations
refresh every five seconds while open, retaining up to 720 samples in 24 hours.

For SFTP, **Remote server storage** requests total, used, and free filesystem
capacity from the server's SFTP statistics extension, with a usage breakdown and
observed history. Unsupported servers are labeled; no recursive scan or remote
shell fallback is used. These numbers can include other folders and users on the
same filesystem, so they are not the selected folder's size. SFTP does not expose
a provider price or bill, and Mountain Turtle does not invent one.

See [metrics details and pricing assumptions](docs/METRICS.md).

## Large photo libraries

The **S3-only** photo browser works with an ordinary bucket directory tree.
SFTP photos open through Finder. It does **not** make a
flat folder containing a million photos fast to open: directory enumeration and
Finder thumbnail requests can still be expensive. Use existing smaller folders
when available. There is no automatic bucket scan, thumbnail index, or photo-key
reorganization during connection setup.

**Browse photos** offers a separate paginated browser: at most 100 folder/file
entries are requested per page, without fetching subsequent pages automatically.
Only visible photo tiles request small previews, with at most two requests
running at once. Leaving the viewport cancels automatic requests.

Previews use an existing local original or an embedded JPEG thumbnail found
within a bounded 128 KiB header read. Missing previews remain placeholders;
the browser does not automatically fetch a large original just to fill a tile.
**Create preview from original** explicitly permits a temporary original fetch
of up to 32 MiB. **Open original** and **Download original** save a full copy to
`~/Downloads/Mountain Turtle`; these copies are retained separately from caches.
Small previews have a separate 256 MiB / 2,048-file cache, keyed by object version.
Nothing is uploaded to S3. See [photo browser details](docs/PHOTO_BROWSER.md).

A bounded virtual folder view *inside the network drive* is still a proposal in
[the large-library design](docs/LARGE_PHOTO_LIBRARIES.md). The new photo browser
does not change the bucket tree or replace Finder's built-in JPEG previews.

## Local state and troubleshooting

| Location | Purpose |
| --- | --- |
| `~/Library/Application Support/Mountain Turtle` | Saved connections and service state |
| `~/Library/Caches/MountainTurtle` | Local file cache |
| `~/Library/Logs/MountainTurtle` | Service and connection logs |
| `~/Mountain Turtle` | Volume mount points |

For an S3 sign-in error, renew the named AWS profile. For SFTP, check the saved
server identity, credentials, and SSH agent or Keychain availability. Never
remove a host-verification failure by automatically trusting an unknown key.
If a dependency is
missing, install it and reopen the app. If Finder waits on a very large folder,
return to a smaller existing prefix rather than repeatedly reopening it. If
ejection fails, close the apps holding files open; do not delete the mount
directory or cache to recover it.

For service diagnostics from the checkout:

```sh
python3 service/turtle_service.py status
```

The local CLI emits one JSON response and does not print credentials. The full
interface is in [PROTOCOL.md](PROTOCOL.md). The
[manual acceptance checklist](docs/ACCEPTANCE.md) separates mocked checks from
read-only S3 checks and deliberate writes to the isolated SFTP fixture. The
[2026-09-14 verification record](docs/VERIFICATION-2026-09-14.md) records the checks
completed on the initial build and the checks still outstanding. See [architecture](docs/ARCHITECTURE.md)
for the service boundaries.

## License

Original project code and artwork are available under the [MIT license](LICENSE).
External tools and system frameworks retain their own terms; see
[third-party notices](THIRD_PARTY_NOTICES.md).

## Compressing a remote folder

In Finder, select a folder on a connected Mountain Turtle drive and choose
**Compress with Mountain Turtle…** from its context menu. Choose a new `.zip`
filename on a local disk. The app downloads directly from the remote service,
then compresses the local copy, avoiding Finder's slow per-file metadata scan
through the mounted drive. The progress window shows completed files, bytes,
download speed, and the current phase. Cancel removes the temporary download.

Leave room for both the downloaded folder and the ZIP. Keep the source folder
unchanged until completion; remote folders are not point-in-time snapshots.
Pending local uploads must finish before starting. Existing ZIPs are never
replaced. Copy the finished ZIP to the remote drive if you want it stored there.
Finder's built-in **Compress** command still uses Finder's own archive workflow.
