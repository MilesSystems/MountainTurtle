# GitHub updater validation — 2026-09-18

Version 0.7.0 integrates the previously separate connection-transfer, SFTP
artwork, Finder-date, and photo-date changes with Sparkle 2.10.0.

- `python3 -m unittest discover -s tests`: 365 tests passed, including native
  Swift helper tests and 21 update lifecycle tests.
- Built the app, Finder extension, and native helpers for arm64 and x86_64.
- `codesign --verify --deep --strict` passed for the Apple Development-signed
  universal app and its embedded Sparkle framework.
- Bundled service Python files matched current source byte for byte.
- Verified Sparkle's relative framework linkage, committed Ed25519 public key,
  signed-feed requirement, and disabled automatic download/install settings.
- Reviewed Sparkle's installer ordering against the pinned 2.10.0 source.
  The app's quit gate is armed before extraction, covering both Install and
  Relaunch and ordinary Quit while an update is staged.
- Lifecycle tests cover safe ejection, busy/timeout refusal, cache preservation,
  rollback, interrupted installation recovery, concurrent-operation rejection,
  and recovery of a legacy supervisor that predates this updater.

The release signing key is stored in the local login Keychain. Release tools
verify archive/feed signatures, stage uploads as a draft, compare the downloaded
assets byte for byte, and only then publish the release.

The Mac was locked during this run. Native visual review and an actual installed
update/relaunch were not performed; compilation and signatures do not substitute
for that check. The installed 0.6.0 app was left unchanged. It needs one manual
installation of 0.7.0 to gain the updater. This build is a development preview;
Developer ID signing and Apple notarization are not yet configured on this Mac.
