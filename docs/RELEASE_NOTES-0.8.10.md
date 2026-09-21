Mountain Turtle 0.8.10 improves Finder responsiveness in large mounted folders.

- Automatically prefetches direct files inside folders that Finder opens or expands, so large file lists warm into the local cache while you browse.
- Keeps automatic open-folder prefetch shallow, cancellable, and bounded by the drive's cache settings rather than recursively pinning whole trees.
- Gives explicit **Keep This Folder Downloaded** jobs priority over automatic prefetch, preserving the manual keep-downloaded behavior and progress badge.
- Cancels automatic prefetch during shutdown, eject, reconnect, or update handoff so background warming does not block safe drive lifecycle work.
- Documents the new authenticated Finder bridge route used for open-folder prefetch.
