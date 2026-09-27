# Mountain Turtle 0.8.22

- Mountain Turtle no longer prefetches originals when Finder opens a folder. Files remain available on demand, and explicit Keep Downloaded requests still work. Finder's own icon previews may still read file contents.
- A completed folder download now says “Folder download finished,” with a note that current offline availability is not verified. It no longer shows a green cached check based only on a past job. Individual file badges still inspect local cache coverage; empty cached files are identified as zero bytes.
- The file tree opens a visible Loading… row until its first page arrives, and shows each batch immediately without waiting for a 64 KB input buffer to fill. Merely checking whether a row can expand no longer starts a remote listing. Collapsing a folder cancels its pending listing.

Existing connections, credentials, and cached files are preserved. This release does not repair incomplete or empty files already stored on a server.

Runtime requirements: Python 3, rclone with nfsmount support, and AWS CLI v2 remain external dependencies.
