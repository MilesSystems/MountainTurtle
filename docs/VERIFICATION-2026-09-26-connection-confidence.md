# Connection confidence — 0.8.18

- Full suite: 552 tests passed on macOS. The first run had one native-compilation
  timeout at 180 seconds under heavy system load; a full rerun allowed that
  compilation 900 seconds without changing any test assertions. GitHub runs the
  ordinary suite with its original timeout.
- Focused cases cover corrupt, partial, dirty, symlinked and unavailable cache
  metadata; active/open files; failed control requests; queue-only upload retries;
  wake retry delays; ejected-drive intent; live mounts; and update handoff safety.
- Native tests cover setup availability when Python is absent, optional AWS CLI
  for SFTP, and discovering a runtime after installation without relaunching.
- Universal Apple Development signed build passed deep/strict codesign checks,
  arm64/x86_64 verification, Sparkle symlink and bundle-version checks.
- Installed a separate signed validation copy in the user's Applications folder
  to preserve the running app and busy mounted drive. Inspected screenshots of
  Mac setup and the connected-drive upload summary, refreshed setup, dismissed
  the sheet, and switched drives. The busy drive correctly showed unconfirmed
  completion, and disconnected drives showed unavailable status.
- Recovered a pre-existing updater handoff through the installed service's
  resume-update command. The installed 0.8.15 app's subsequent updater attempt
  correctly refused to eject a busy drive. Connection settings, credential/trust
  file hashes, and cache directories matched the private pre-update baseline.

The live public update/relaunch check remains blocked by open files on the
existing drive. No force-ejection was used. Wake/network recovery and queued
upload retry behavior were verified in automated tests; a live sleep or network
interruption was not imposed on the user's active drive. Developer ID signing
and notarization remain unavailable; distribution uses the signed Preview channel.
