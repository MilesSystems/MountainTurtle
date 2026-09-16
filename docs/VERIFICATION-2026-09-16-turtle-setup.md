# Single-file setup verification — 2026-09-16

App version: **0.6.0**, macOS arm64. This extends the 0.5 settings-transfer feature.

## Automated checks

- Full suite: `python3 -m unittest discover -s tests -q` — **297 tests passed**.
- Real generated keys exercise setup encoding/decoding and imports between
  isolated home directories. Coverage includes trusted-host filtering, malformed
  and encrypted keys, unsupported fields, oversized input, duplicate names,
  symlink destinations, UUID collisions, partial writes, and rollback failures.
- Native file handling accepts `.turtle` and legacy `.mountainturtle` files.
- Native encryption/decryption of a complete synthetic setup document preserves
  the entire document without a plaintext key in the encrypted file.
- Optimized app build, Info.plist validation, and deep/strict signature
  verification passed. Installed using the same Apple Development identity as
  the previous local installation, with the installer preserving its backup.

## Installed app checks

- Double-clicking a synthetic `.turtle` file in Finder while the app was closed
  launched 0.6 and opened the simplified review: drive name, account, server,
  access level, Connect now, and Add connection. No credential-path fields.
- Turning off Connect now and choosing Add connection created a disconnected
  record, with its matching key and host pins in a new private directory.
- The directory had mode `0700`; both credential files had mode `0600`.
- Export showed the complete-setup/settings-only choice and enabled password
  protection by default for complete setups. An unprotected native Save-panel
  export decoded to the original complete setup and had mode `0600`.
- A protected setup opened from Finder, unlocked, and displayed the same small
  review. An existing name received the next available suffix. Cancel created
  no duplicate.
- The synthetic saved connection and its test-only managed credentials were
  removed. All four original saved connections were preserved.

## Live transfer checks

Five individually scoped family setup files were generated privately, outside
the repository. Each file was imported into a fresh temporary home, and its
installed key and exact server pin successfully authenticated to the live
server and listed the expected private and shared folders. Temporary imported
credentials were removed after each check. An additional check used the app's
actual generated rclone configuration and environment; it listed both expected
directories successfully. No family files were sent and no server settings or
existing client keys were changed by these checks.

The generated family files are unprotected complete setup documents, equivalent
to distributing each account's existing private key. The app supports choosing
password protection when exporting a complete setup.

## Remaining release checks

- No physical second Mac was available. Finder-to-window drag was not exercised
  as a physical gesture; it uses the same validated file-import path.
- The current local build still relies on external Python/rclone (and AWS CLI
  for S3), and the main app is not sandboxed. This feature does not complete
  App Store packaging or clean-Mac acceptance.
