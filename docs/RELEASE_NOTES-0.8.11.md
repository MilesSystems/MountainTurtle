# Mountain Turtle 0.8.11

- Distinguishes cache-read/download failures from upload failures in the Activity queue, so files deleted remotely while a folder is warming no longer appear as local upload conflicts.
- Clears queued/running folder-download rows when a folder warming job is cancelled or superseded.
- Backs off from folder warming when a listed file can no longer be read, letting the next Finder refresh retry from fresher remote metadata instead of churning through stale paths.
