# Architecture and v1 boundaries

The protocol between the SwiftUI app and local Python service is defined in
[PROTOCOL.md](../PROTOCOL.md). That file is the command and JSON field contract.
This document describes the intended boundaries and failure behavior.

```text
SwiftUI connection manager
    |
    | local Python CLI commands / JSON status
    v
Python standard-library supervisor <---- per-user launchd registration
    |
    | rclone child process for one saved bucket connection
    v
Loopback NFS server <---- macOS NFS client <---- Finder / applications
    |
    | rclone S3 access with the selected AWS profile
    v
Amazon S3
```

## Ownership

The app owns connection presentation and user choices. The Python service owns
saved connection records, process supervision, and safe mount lifecycle. Rclone
owns remote filesystem operations and its local file cache. AWS tools own
profile configuration and SSO authentication. Mountain Turtle does not save
static access keys in connection records.

The Python service is a local command interface, not a remotely accessible web
API. The only filesystem listener binds to loopback. Loopback prevents access
from other computers; it is not a security boundary against every other process
or user on the same Mac.

## Connection lifecycle

Connection identifiers are stable UUIDs. Display names are unique, safe single
directory components beneath `~/Mountain Turtle`. Bucket, profile, region, read
mode, and automatic connection preferences are explicit per-connection fields.
New GUI connections begin read-only with automatic connection disabled.

Saving a connection changes local settings. Connecting validates the selected
bucket without enumerating unrelated buckets, recursively scanning its objects,
or creating a test object. A live mount's presence is distinct from successful
remote access: an existing mount can outlive working credentials or networking.

The service reports disconnected, connecting, connected, disconnecting,
needs-login, or error states through the protocol's exact state names. The GUI
polls status every three seconds. Neither a successful CLI invocation nor a
running process alone proves a completed, usable connection.

Disconnect attempts a normal unmount. A busy volume remains mounted with a
useful explanation; the service must remain available to serve it. An edit or
removal is allowed only when disconnected. Removing a saved record does not
delete remote objects or erase potentially useful cached data.

## Reconnecting and shutdown

Login startup controls the per-user LaunchAgent. A connection's auto-connect
setting determines whether it should be connected automatically. Turning off
login startup preserves running mounts. Login startup applies after the user
signs into macOS, not before FileVault or the macOS login screen.

Installed startup configuration references the service inside the installed
app. Moving or replacing an installed app while it has active mounts needs
careful handling; a build checkout is not a durable launch path.

AWS SSO can require interactive renewal. Reconnect logic should not repeatedly
open browser windows or treat failed credentials as an empty bucket. A graceful
shutdown retains cached data and reports any volumes it could not eject.

## Current limits

This release is an S3 connection manager with native Finder mounts. It does not
provide a searchable photo catalog, a complete offline mirror, a general cloud
provider UI, credential administration, or a migration of existing objects.
Large flat folders remain a separate problem described in
[the future photo-library design](LARGE_PHOTO_LIBRARIES.md).
