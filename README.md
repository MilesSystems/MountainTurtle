# Mountain Turtle

Mountain Turtle is a free, MIT-licensed macOS app that connects Amazon S3 buckets
as real Finder network volumes. Its native SwiftUI window manages saved
connections, AWS SSO sign-in, connection status, and reconnecting at login. A
small local Python service supervises rclone's NFS mounts.

This is an early release focused on macOS and Amazon S3. The interface and app
artwork are original project work. Mountain Duck's code and artwork are not used
or bundled. There is no subscription or license activation; AWS storage,
requests, and data transfer remain subject to your AWS account's charges.

## Requirements

- macOS 14 or later.
- Python 3.9 or later, installed separately; the service uses only its standard library.
- [rclone](https://rclone.org/install/) with the `nfsmount` command.
- [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
  and an AWS profile with access to the bucket you want to connect.
- Apple's Xcode Command Line Tools to build from source (`xcode-select --install`).

Check the tools before building:

```sh
python3 --version
rclone version
rclone nfsmount --help
aws --version
xcrun swiftc --version
```

The app uses the macOS NFS client through rclone; a separate FUSE driver is not
required. The NFS listener binds to `127.0.0.1` and is for this Mac, not a network
file server for other computers. Upstream still labels `nfsmount` experimental.
See [rclone's macOS NFS documentation](https://rclone.org/commands/rclone_nfsmount/#nfs-mount).

## Build and install

From this checkout:

```sh
./scripts/build.sh
./scripts/install.sh
open "$HOME/Applications/Mountain Turtle.app"
```

The build creates `build/Mountain Turtle.app`; the install script places it in
`~/Applications/Mountain Turtle.app`. Use the installed copy when enabling login
startup: the background service must keep a stable path to the app's resources.
Python, rclone, and AWS CLI are external dependencies, not bundled executables.

For a temporary development launch after building:

```sh
open "build/Mountain Turtle.app"
```

## Connect a bucket

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

Connected volumes live under `~/Mountain Turtle/<connection name>` and are
actual NFS mounts. In Finder Settings, enable **Connected servers** under General
for desktop icons and under Sidebar for the sidebar's Locations section.
Custom volume artwork is provided locally; it does not require uploading an icon
to your bucket.

Startup happens after you sign in to macOS. It cannot bypass an expired AWS SSO
session: use **Sign in to AWS** or
`aws sso login --use-device-code --profile PROFILE` when renewal is required.
The app uses AWS device authorization: approve the request on the AWS page and
wait for Mountain Turtle to confirm completion. This flow does not redirect to a
temporary localhost callback. If sign-in times out after five minutes, close the
previous sign-in tab and choose **Sign in to AWS** to start a fresh request.
Credentials remain under the AWS tools' management;
Mountain Turtle saves connection settings rather than AWS access keys.

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
| Unknown | Status is unavailable or cannot be established safely. |

Cached files can be evicted when the cache fills. A green check does not pin a
file for permanent offline access or establish that the cloud copy has not
changed. The extension requests status for visible items using local cache
metadata; it does not scan the bucket or download photos to generate badges.

## Files, caching, and disconnecting

File contents are fetched on demand and cached on this Mac. A connected volume
is not a complete offline copy, and Finder previews can trigger downloads.
Cloud latency and the AWS session still matter for files that are not cached.

Read-only connections block cloud edits. If you explicitly enable writing,
normal file actions can upload, overwrite, rename, or delete S3 objects using
your profile's permissions. Cached writes and error handling are covered with
local mocks during initial validation; that is not proof of successful live
upload or recovery behavior.

Disconnect through the app or eject the volume in Finder. If a volume is busy,
close the files or applications using it and try again. Mountain Turtle does not
force-detach a busy volume or discard its cache. Removing a disconnected saved
connection removes its local configuration, not its bucket or objects.

## Large photo libraries

This version connects an ordinary bucket directory tree. It does **not** make a
flat folder containing a million photos fast to open: directory enumeration and
Finder thumbnail requests can still be expensive. Use existing smaller folders
when available. There is no automatic bucket scan, thumbnail index, or photo-key
reorganization during connection setup.

A bounded virtual folder view is described in
[the future large-library design](docs/LARGE_PHOTO_LIBRARIES.md). It is a proposal,
not a feature of this release.

## Local state and troubleshooting

| Location | Purpose |
| --- | --- |
| `~/Library/Application Support/Mountain Turtle` | Saved connections and service state |
| `~/Library/Caches/MountainTurtle` | Local file cache |
| `~/Library/Logs/MountainTurtle` | Service and connection logs |
| `~/Mountain Turtle` | Volume mount points |

If a connection needs sign-in, renew the named AWS profile. If a dependency is
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
read-only verification against a real bucket. The
[2026-09-14 verification record](docs/VERIFICATION-2026-09-14.md) records the checks
completed on the initial build and the checks still outstanding. See [architecture](docs/ARCHITECTURE.md)
for the service boundaries.

## License

Original project code and artwork are available under the [MIT license](LICENSE).
External tools and system frameworks retain their own terms; see
[third-party notices](THIRD_PARTY_NOTICES.md).
