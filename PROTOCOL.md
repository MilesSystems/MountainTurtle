# Local service protocol (version 1)

CLI: python3 turtle_service.py COMMAND [args]. Output one JSON object. Commands never print credentials. Exit nonzero with {"ok":false,"error":"human explanation"}.

status: {"ok":true,"serviceRunning":bool,"launchAtLogin":bool,"dependencies":{"rclone":path-or-null,"aws":path-or-null,"python":path,"brew":path-or-null,"awsVersion":string,"awsCliV2":bool,"rcloneVersion":string,"rcloneNfsmount":bool,"appPath":string,"appInstalled":bool,"privacyState":"approved|needsApproval|checking|unknown","privacyMessage":string},"profiles":[string],"connections":[{"id":UUID,"name":string,"bucket":string,"profile":string,"region":string,"readOnly":bool,"autoConnect":bool,"desiredConnected":bool,"mounted":bool,"state":"disconnected|connecting|connected|disconnecting|needsLogin|error","message":string,"mountPath":string,"updatedAt":number}]}

add --name NAME --bucket BUCKET --profile PROFILE --region REGION [--read-only] [--auto-connect] -> {"ok":true,"id":UUID}
edit ID with same fields (only while disconnected)
remove ID (only disconnected; only local saved connection removed)
connect ID (starts service if needed)
disconnect ID (native non-forced unmount; pending uploads wait, busy volume remains connected with explanatory message)
login ID (start AWS SSO device authorization in the browser; five-minute timeout; no credentials printed or persisted by Turtle)
autostart on|off (login LaunchAgent registration, preserve running mounts)
serve (foreground supervisor for launchd)
shutdown (gracefully disconnect all, preserve cached data, report busy mounts)

## Drive controls (0.2)

Status connections also include `cacheMaxSizeMiB` (default 2048) and
`cacheMaxAgeHours` (default 24). `add` and `edit` accept optional
`--cache-max-size-mib` and `--cache-max-age-hours`; omitted fields on edit preserve
the connection's current settings.

- `settings ID [--cache-max-size-mib N] [--cache-max-age-hours N]`: disconnected
  only. Size range64..1048576 MiB, age1..8760 hours. Soft eviction targets.
- `rename ID --name NAME`: disconnected only, preserves bucket and cache identity.
- `refresh ID`: queues directory-cache invalidation for a mounted drive. This
  uses rclone's SIGHUP behavior and does not fetch object bodies.
- `reconnect ID`: queues safe ejection and reconnect. A subsequent disconnect
  cancels the reconnect intent. Pending writes and busy-volume protection apply.
- `cache-info ID`: bounded local scan returns `{ok, usedBytes, files, partial}`.
  Allocated bytes are measured; `partial=true` denotes a lower estimate.
- `clear-cache ID`: disconnected only, rejects pending or ambiguous write data
  even if the connection was subsequently made read-only. Deletes local rclone
  cache only, not explicit Downloads copies or the separate thumbnail cache.

The UI snapshots current connection intent, safely ejects, waits for the prior
process to finish, then applies rename/settings/clear-cache and restores the
previous connection intent. Failure cancels a pending ejection when appropriate.

Finder actions use `mountainturtle://connection/UUID?action=ACTION`, where ACTION
is `finder`, `browse`, `settings`, `rename`, `refresh`, `reconnect`, or `eject`.
The host accepts exactly one allowlisted query parameter and resolves UUIDs
against current saved connections. `mountainturtle://open` opens the app.
No filesystem paths, credentials, or arbitrary commands are accepted in URLs.

The supervisor runs the bundled native `Contents/Helpers/Mountain Turtle Sidebar`
helper with `ensure UUID MOUNT_PATH` once per confirmed mount generation. Work is
serialized, has an eight-second deadline, and is cancelled on ejection/shutdown.
Failure never prevents mounting. Status may include `sidebarItemID` or a human
`sidebarError`. The helper requires an exact kernel NFS mount beneath the user's
Turtle mount root and updates actual FavoriteVolumes through public SharedFileList
APIs. A private `sidebar-items.json` registry and ownership markers on the sidebar
item let it replace its old entry after rename/reconnect without resolving stale
NFS bookmarks, while leaving unrelated favorites alone. The API is deprecated;
Finder's manual Add to Sidebar command is the fallback.

The sibling `photo_browser.py` CLI is documented in [PHOTO_BROWSER.md](docs/PHOTO_BROWSER.md).

All action commands return {"ok":true,"message":optional}. GUI polls status every 3 seconds. Backend paths: ~/Library/Application Support/Mountain Turtle, ~/Library/Caches/MountainTurtle, ~/Library/Logs/MountainTurtle. Mount root ~/Mountain Turtle; each display name must be unique and safe as one directory component. No mounts of unrelated buckets. No recursive S3 scans or object mutation during connect/validation. Rclone mounts bind127.0.0.1 with unprivileged native macOS NFS options.

CLI accepts --resource-dir PATH before COMMAND to locate packaged icon overlay
assets. Backend can locate its parent Resources by default. Bundled app
resources include service/turtle_service.py and icon-overlay-assets; the service
materializes the dotfile-shaped Finder metadata into its private Application
Support directory at runtime. Dependencies discovered from explicit standard
paths + PATH. For app installation, LaunchAgent references the stable service
script inside the installed .app, not build checkout.

## First-run setup (0.3)

The app surfaces a Mac setup sheet from `status.dependencies`. It checks whether
the running bundle is installed at `~/Applications/Mountain Turtle.app` or
`/Applications/Mountain Turtle.app`, whether AWS CLI is version 2, whether
`rclone nfsmount --help` succeeds, and whether Finder sidebar registration has
already proven Network Volumes permission. Privacy remains `unknown` until macOS
has asked or a mounted drive has been added to Finder.

If Homebrew is already present, the sheet can run `brew install awscli rclone`
and stream output in the app. If Homebrew is missing, the sheet writes a bounded
installer script and opens it in Terminal so Homebrew's official installer can
use the normal shell and password prompt before installing `awscli` and `rclone`.
