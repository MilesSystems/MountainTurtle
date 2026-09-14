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

## Pending or not tested

- Reconnect after an actual reboot or logout/login was not tested.
- Live upload, overwrite, rename, delete, and write-cache recovery were not
  tested. Validation against the photography bucket remained read-only.
- The full [manual acceptance checklist](ACCEPTANCE.md) has not been marked
  complete by this record; unlisted scenarios still require their own evidence.

Million-photo flat-folder paging remains a
[future design](LARGE_PHOTO_LIBRARIES.md), not an implemented or verified feature.
The application's Python runtime requirement is Python 3.9 or later.
