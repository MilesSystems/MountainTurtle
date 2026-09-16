# Mountain Turtle artwork

The app and drive icons are original vector artwork drawn with AppKit in
`scripts/generate-icons.swift`. They use no Mountain Duck, AWS, or third-party
artwork. The artwork is covered by the repository's MIT license.

Regenerate from the repository root:

```sh
swift scripts/generate-icons.swift "$PWD/Resources"
```

`AppIcon.icns` is the app icon. `appIcon.png`, `s3Drive.png`, and `sftpDrive.png`
are previews. `S3Drive.icns` and `SFTPDrive.icns` label Finder volumes with their
connection protocol.

The read-only `icon-overlay` (S3) and `icon-overlay-sftp` (SFTP) each contain
`.VolumeIcon.icns` and two raw AppleDouble records. Separate directories keep
simultaneously mounted protocols from sharing artwork. The root record sets
Finder's custom-icon flag; the icon-file record marks that file invisible.
These are local overlay files, never uploads to remote storage.
Copy them as raw bytes. Do not use `ditto` or set a custom icon directly on a
cloud-backed volume: macOS can merge metadata or write it into remote storage.

The records use macOS's complete AppleDouble layout with a valid empty resource
fork, and contain no machine-specific provenance. The generated icon was
verified by reading its rendered image from a disposable local rclone NFS union
mount. Finder's URL `customIcon` flag may be false even when the icon renders
correctly; inspect the image rather than using that flag as a success check.
