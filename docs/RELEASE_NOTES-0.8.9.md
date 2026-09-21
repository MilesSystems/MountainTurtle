Mountain Turtle 0.8.9 adds a local Activity queue and operation history for mounted-drive file work.

- Shows recent move/rename, delete, upload, folder download, and failure events on the selected drive, with action-specific icons.
- Groups bursty Finder operations such as bulk deletes into counted queue items instead of flooding the app.
- Captures Finder folder renames as move/rename activity when the mounted remote reports them that way.
- Includes folder keep-downloaded requests and background warming results in the same local queue.
- Adds a temporary Finder folder-loading badge for expanded disclosure rows while Finder is asking for the folder's visible children.
- Refreshes the opened folder's remote listing through rclone so deletes made elsewhere can appear without waiting for the normal directory cache expiry.
- Shows a pie-style Finder badge on folders while **Keep This Folder Downloaded** is queued or warming that folder into the local cache.
- Keeps completed operations as recent local history and documents that this history is not a provider-wide audit log.
