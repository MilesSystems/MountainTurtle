# Mountain Turtle artwork

The app and drive icons are original vector artwork drawn with AppKit in
`scripts/generate-icons.swift`. They use no Mountain Duck, AWS, or third-party
artwork. The artwork is covered by the repository's MIT license.

Regenerate from the repository root:

```sh
swift scripts/generate-icons.swift "$PWD/Resources"
```

`AppIcon.icns` is the app icon. `appIcon.png` and `s3Drive.png` are previews.
`S3Drive.icns` is the matching Finder network volume icon.

The read-only `icon-overlay` contains `.VolumeIcon.icns` and two raw AppleDouble
records. The root record sets Finder's custom-icon flag; the icon-file record
marks that file invisible. These are local overlay files, never S3 uploads.
Copy them as raw bytes. Do not use `ditto` or set a custom icon directly on a
cloud-backed volume: macOS can merge metadata or write it into remote storage.

The records use macOS's complete AppleDouble layout with a valid empty resource
fork, and contain no machine-specific provenance. The generated icon was
verified by reading its rendered image from a disposable local rclone NFS union
mount. Finder's URL `customIcon` flag may be false even when the icon renders
correctly; inspect the image rather than using that flag as a success check.
