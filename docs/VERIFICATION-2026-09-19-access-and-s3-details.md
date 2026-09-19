# Editable access and S3 connection details

Implemented from current main in the isolated `codex/edit-access-s3-details`
branch. The installed local app remains version 0.8.0.

## Behavior

- Access has a visible Change control and a read-only checkbox on saved S3 and
  SFTP drives. Connected drives use the established safe ejection and reconnection
  transaction; dirty caches, open files, and active mount processes prevent an
  unsafe mode change. Unrelated CLI edits retain the previous access mode.
- S3 type and Storage class appear immediately above AWS profile. Storage class
  reporting uses bounded read-only CloudWatch observations, with separate mixed,
  missing, stale, partial, and expired-login states. No object scan is performed.
- Drive-specific notices clear when selecting a different connection.

## Verification

- Existing suite plus service access and S3 details tests: 464 passed.
- Additional native access transaction suite: 8 passed. It compiles the actual
  Swift transaction and replaces the service transport and polling clock only.
  Coverage includes both access directions, safe-detach ordering, busy files,
  pending uploads, failed saves, lost responses, and connection intent recovery.
- Optimized native build and deep/strict signature verification passed, using
  the existing Apple Development identity. Installed service files match source.
- Visually inspected the installed connection card and connected-drive access
  sheet. Both S3 rows are above AWS profile and Access has the Change control.
- A temporary disconnected connection with no usable AWS profile was changed
  through the installed UI from read-only to read/write and back. Both saves
  succeeded and reopening the editor retained the selected mode. The test entry
  and its metrics cache were then removed. A digest comparison confirmed all
  four original saved connections were unchanged; Nikki Images stayed connected.

The existing AWS session was expired during the live check, so its storage class
correctly showed Unavailable with a sign-in explanation. Successful/mixed class
rendering is covered by fixtures; fetching this account's current classes requires
renewing AWS sign-in and refreshing the row. Live remounts with changed access
were not performed on the user's real drives.
