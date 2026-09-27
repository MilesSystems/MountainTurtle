# Mountain Turtle 0.8.21 Preview

- Browse files inside Mountain Turtle using a native, expandable Mac list with Name, Date Modified, Size and Kind columns. Large folders populate in batches. Expanded folders remain in the same tree, and collapsing a loading folder cancels its request.
- Finder now uses a bundled mount engine that streams directory entries progressively and handles unrelated requests concurrently. A slow file read or a metadata-file probe no longer holds up every directory response.
- Browsing the internal tree does not fetch thumbnails or file contents. Open a selected file explicitly to download it through its connected Finder drive.
- Existing connections, authentication settings and file caches are retained.

This is an Apple-signed Preview. It is not Developer ID notarized for general distribution.
