# Connection transfer verification — 2026-09-16

App version: **0.5.0**, macOS arm64.

## Automated verification

- `python3 -m unittest discover -s tests -q`: **262 tests passed**.
- Native app build, generated Info.plist validation, and deep/strict code-signature
  verification passed.
- Portable S3 and SFTP settings round-trip without runtime identifiers, local
  file paths, AWS credentials, or passwords. Malformed, unsupported, ambiguous,
  and oversized settings documents are rejected.
- Native AES-GCM encryption tests cover randomized ciphertext, Unicode,
  incorrect passwords, tampered nonce/ciphertext/tag, metadata and size limits,
  and an independently derived PBKDF2 reference.
- Native file-handling tests compile the actual application sources without
  starting the application or service. They cover early rejection before pipe
  writes, local-file validation, size boundaries, duplicate names, and Unicode
  filename limits.
- A synthetic transfer between separate temporary user directories exercised
  export, native encryption/decryption, inspection, and actual service add.
  Keychain was mocked. The receiving record received a new identifier, remained
  disconnected, and preserved the intended destination/cache settings.

## Installed app verification

Installed to `~/Applications/Mountain Turtle.app` using the same existing Apple
Development identity as the previous installed version. The installer retained
a backup of the previous app. This is a local development build.

Using synthetic files and account settings:

- The export dialog displayed **Connection settings only** and
  **Password-protected file** choices. Protected export required an export
  password and confirmation; its Export button stayed disabled with empty input.
- A protected SFTP file opened through the native import panel. An incorrect
  password displayed a recoverable error. The correct password displayed the
  expected endpoint, masked SFTP password, access mode, cache values, and local
  known-hosts selection. Cancel did not add that SFTP connection.
- Double-clicking an S3 `.mountainturtle` file in Finder opened the populated
  review screen in Mountain Turtle.
- A synthetic S3 record was saved disconnected, then exported through the native
  save panel. The resulting file passed the portable decoder and had mode `0600`.
  The synthetic record was removed after verification; the four existing saved
  connections remained.

## Remaining manual checks

- An actual Finder-to-window drag gesture was not verified. The window drop
  handler shares the same file-reading/import path as the verified open flow.
- No physical second Mac was used. AWS profile setup, SSH key/trusted-host
  selection, and real imported-password Keychain storage on a receiving Mac still
  require the acceptance checks in `ACCEPTANCE.md`.
- SSH private-key contents and trusted-host files are deliberately not included
  in this version's exports, including password-protected exports.
