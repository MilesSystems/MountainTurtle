# Verification record — 2026-09-14

This record covers the initial Mountain Turtle development build on the local
Mac. The live target was the Nikki photography bucket in read-only mode. It
records observed results separately from checks still outstanding.

## Completed

| Check | Result |
| --- | --- |
| Local service tests | All 20 tests passed, including normal non-force unmount and busy-volume retry checks. Mocked checks do not establish live write durability. |
| Native app build | The app built successfully. |
| Local app signature | The ad hoc code signature validated. This is local signature validation, not a notarization result. |
| Finder volume | The Nikki connection mounted as a native NFS volume. |
| Volume artwork | The local Mountain Turtle S3 drive icon was visible in Finder. |
| Show in Finder | The app selected the correct Mountain Turtle volume in Finder. Opening it displayed the real `app-data` and `Portfolio` root folders. |
| Eject and reconnect | On the updated installed app, the live UI round trip Eject → Disconnected → Connect → Connected completed successfully. |
| Real photo access | An existing JPEG was read through the live read-only mount, and a valid 4,096-byte JPEG read was repeated after reconnect. No cloud writes were performed. |
| Login startup configuration | Login restore was enabled and its LaunchAgent plist was verified to point into the installed app bundle. |
| Registered startup path | After cleanly shutting down the previous service, `launchctl kickstart` started the installed registered LaunchAgent. Auto-connect restored the drive and the UI showed Connected without a manual Connect action. |

The mount and photo-read checks establish actual filesystem and S3 read access;
they are more than an app status or a displayed drive icon. The LaunchAgent
restart exercised the registered startup path and automatic drive restoration.
It did not exercise an actual reboot or macOS logout/login.

## Morning sign-in recovery

The AWS session had expired while the network volume remained mounted. The
reported browser page was a localhost OAuth callback with no listener. The
screenshot alone could also describe a previously completed callback being
reopened; a separate AWS identity check confirmed expired authentication here.

The updated app uses AWS device authorization and exposes **Sign in to AWS**
while connected. A live request launched from that button completed on the AWS
portal, followed by **AWS sign-in completed** in Mountain Turtle. A fresh AWS
identity request and S3 photo metadata request succeeded. The existing mount
needed an Eject/Connect cycle to refresh stale folder information; afterward a
4,096-byte read through the mount had a valid JPEG signature. No cloud writes
were performed.

All 26 service tests passed after the sign-in fix, including failed/expired
sign-in state preservation and suppression of errors from previous mount
attempts. Current-attempt errors remain visible.

## Finder file badges

All 47 service and badge tests passed. The combined app and sandboxed Finder
extension built successfully and passed deep signature verification. The
installed extension was registered and enabled in macOS; the app displayed
**Finder badges enabled**.

The live badge bridge reported the existing photo as `cached` from local cache
metadata. Finder's icon view of Nikki's `Carmen&Blake` folder visibly showed
green check badges and online-only cloud badges. As Finder generated previews,
those cloud badges updated to green checks. The selected photo's context menu
showed **Mountain Turtle: Cached on this Mac**. This verifies the native badges
and labels on the actual NFS volume, not just a status API or mockup.

Partial-cache and pending-upload states are covered by local fixtures. No live
uploads were performed to exercise pending-upload badges. Cache checks inspect
only requested local metadata and backing-file attributes, not the remote
directory tree.

## Photo browser and drive controls

All 99 service, badge, and photo-browser tests passed. The updated native app,
Finder extension, and sidebar helper built successfully. The installed app is
now signed with the Mac's existing Apple Development identity; deep and strict
signature verification passed. This is still a local development build, not a
notarized release.

- The native **Browse photos** sheet opened the real Nikki portfolio. For a page
  containing 29 photos, the first viewport produced 12 local preview JPEGs totaling
  243,868 bytes; tiles outside that viewport did not request previews.
- A bounded three-object listing returned a continuation token without loading
  the rest of the directory. The app requests at most 100 entries per page.
- A cold-cache check against a 1,686,063-byte JPEG fetched only a 131,072-byte
  header range and produced a valid preview from its embedded JPEG thumbnail.
  The check did not fetch the full original or write to S3.
- **Download original** in the live UI saved the chosen IMG_8625 photo under
  Downloads/Mountain Turtle and revealed it in Finder. A later UI fix refreshes
  just that tile from local cache; typecheck and mock checks establish zero
  additional S3 reads for that refresh.
- The live cache-settings save safely ejected and reconnected Nikki. Defaults
  are a 2 GiB original cache and 24 hours without access; these are eviction
  targets rather than download quotas. Native and rclone read-ahead are disabled.
- The live rename flow changed **Nikki Images - Turtle** to **Nikki Images** and
  safely restored the connection. Bucket name and contents were unchanged.
- A native Locations entry with an eject control was observed using the actual
  NFS volume. The new sidebar helper replaces only its own entry and avoids
  resolving stale mount bookmarks. Direct helper checks completed in under half
  a second, preserving unrelated sidebar entries.

### macOS permissions and final Finder verification

The code-signing change initially invalidated the old ad hoc Network Volumes
grant. The Files and Folders UI still showed that old grant as enabled, so that
display alone did not establish access for the new signature. The helper waits
nonblockingly for up to 60 seconds and reports actionable permission guidance
while leaving the volume connected.

After the user approved the prompts, the Finder extension loaded normally.
Re-enabling it in macOS settings and adding its toolbar item produced the Turtle
dropdown in the actual Nikki volume window. Its **Cache settings** action opened
the correct Nikki settings sheet, with the existing 164.4 MB / 88-file cache
shown. Extension and app logs confirm the action was delivered end to end.

Finder's native **Eject** shortcut removed the real Nikki volume. The service
then reported `mounted: false`, `desiredConnected: false`, and `disconnected`;
it did not immediately remount an intentionally ejected drive. Nikki was then
reconnected from the app.
Status now also suppresses stale sidebar warnings as soon as the kernel mount
disappears; a regression test covers that ejection interval.

The user's Network Volumes approval at 10:09:52 answered an old ad hoc build's
queued request. TCC logs show the signed update issued its current request at
10:11:14. The user refreshed the permission in System Settings at 15:16:51–53.
After reconnect, TCC explicitly allowed the signed app and its supervisor-run
sidebar helper succeeded at 15:17:39, without a signature mismatch or further
prompt. No TCC database or sandbox permission was modified to bypass approval.

Final live checks on the installed signed build:

- Finder showed exactly one **Nikki Images** row directly under **Locations**,
  with its own eject button. Clicking the row opened the real bucket root.
- Clicking that sidebar eject button brought up macOS's native choice between
  **Eject** and **Eject All**. Choosing **Eject** removed Nikki's kernel mount;
  the service reported disconnected with `desiredConnected: false`, no stale
  sidebar warning, and no unwanted immediate reconnection.
- With the main app closed, the registered login service was cleanly stopped
  and restarted. It automatically reconnected Nikki and restored one direct
  sidebar entry, reporting `sidebarItemID: 3883753811` without `sidebarError`.
  The main app process was absent throughout that restoration.
- Clicking the restored sidebar row reopened `app-data` and `Portfolio` in
  Finder. Nikki was left connected in read-only mode, with login restore enabled.

These checks establish actual native sidebar ejection and background restoration,
not just successful helper output or a manually added sidebar shortcut. They do
not constitute an actual reboot or logout/login test.

## Pending or not tested

- Reconnect after an actual reboot or logout/login was not tested.
- Live upload, overwrite, rename, delete, and write-cache recovery were not
  tested. Validation against the photography bucket remained read-only.
- The full [manual acceptance checklist](ACCEPTANCE.md) has not been marked
  complete by this record; unlisted scenarios still require their own evidence.

The separate photo browser implements bounded paging. A million-photo dataset
has not been benchmarked. Virtual paging inside Finder's mounted folder tree
remains a [future design](LARGE_PHOTO_LIBRARIES.md).
The application's Python runtime requirement is Python 3.9 or later.
