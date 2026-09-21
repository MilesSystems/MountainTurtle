Mountain Turtle 0.8.8 adds folder-level cache warming from Finder and includes the unpublished 0.8.7 fast browsing work.

- Adds a Finder right-click **Keep This Folder Downloaded** submenu for mounted folders, with indefinite, 24-hour, 7-day, 30-day, and stop options.
- Keeps folder download rules in the local service and warms one folder at a time in the background by reading regular files into the mount cache.
- Expires timed folder rules automatically and pauses folder warming while a drive is disconnected.
- Keeps filesystem paths out of public deep links; selected folder paths are accepted only through the authenticated local Finder bridge.
- Adds S3 **Fast folder browsing**, which can favor large-bucket Finder responsiveness by using S3 listing timestamps, recursive directory warming, `--fast-list`, a longer directory cache, and a short attribute cache.
- Documents the cache tradeoffs: fast browsing does not download file contents, folder keep rules warm the mount cache rather than create a permanent archive, and cache size/free-space safeguards can still evict data.
