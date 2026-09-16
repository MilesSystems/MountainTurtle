# SFTP Finder icon verification — 2026-09-16

Mountain Turtle 0.6.0 now gives S3 and SFTP mounts separate local icon overlays.
The original S3 artwork is unchanged. SFTP artwork displays **SFTP** using the
same original vector design. No icon files are written to remote storage.

- All **305 automated tests passed**, including protocol isolation, missing
  artwork, and cache badges for recovered mounts using the old shared overlay.
- The final native app build and deep/strict signature verification passed with
  the existing local signing identity. Bundled service files and artwork were
  compared byte-for-byte with the final source before installation.
- Installed locally at `~/Applications/Mountain Turtle.app`, preserving the
  current connection-export/setup features and a verified backup archive.
- The guarded service restart safely ejected the two connected drives and
  restored their prior connection intent. Four saved connections were retained;
  the two previously disconnected drives remained disconnected.
- Finder's Computer view visually showed **S3** on **Nikki Images** and **SFTP**
  on **Nikki's Home**. Relaunching Finder removed a stale duplicate icon retained
  after reconnect. The kernel mount table confirmed one mount for each drive.

These checks cover the installed local development build. No repository push,
release publication, or notarization was performed for this fix.
