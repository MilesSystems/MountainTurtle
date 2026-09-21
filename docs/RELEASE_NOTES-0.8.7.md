# Mountain Turtle 0.8.7 Preview

- Adds an S3-only Fast folder browsing setting for large buckets. It favors Finder responsiveness by using S3 listing timestamps, longer directory caching, recursive directory warming, and fast-list indexing.
- Refresh folder listings now warms the live rclone directory cache through Mountain Turtle's private loopback control channel without downloading file contents.
- Portable `.turtle` connection files preserve the Fast folder browsing preference while older files continue to import with precise-date browsing.

This is an Apple-signed Preview build. It is not notarized for general public distribution.
