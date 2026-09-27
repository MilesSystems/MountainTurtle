# Mountain Turtle mount engine

The app embeds a universal, Apple-signed `turtle-rclone` helper built from rclone
1.75.1. The CLI rclone dependency remains available for other service operations.
`go.mod` / `go.sum` pin dependencies; `paging.patch` is applied to private build
copies, never the Go module cache. `scripts/fetch-go.sh` verifies the Go 1.27.1
archive before extraction. No compiler is needed by app users.

The patch streams SFTP READDIR pages, preserves the small local icon overlay's
union policies, and merges VFS entries under short locks. NFS snapshots keep
stable inodes and append-only cookie positions; completed snapshots are retained
for concurrent enumeration. Absent entries are pruned only after successful EOF.
General unions and normalization-blocking configurations retain a complete-list
fallback. Exact Finder sidecar probes can stat independently of a pending list.

NFS reads each complete, size-limited RPC record before concurrent dispatch, with
64 in-flight requests per connection and serialized response writes. This keeps
a slow file read from serializing every unrelated directory request.

`tree-list` emits bounded JSON pages for the native outline browser, with no file
content or thumbnail requests. S3 uses server listing timestamps. Folder collapse
cancels its process; independent folder requests have four execution slots.

Build and run the upstream VFS, NFS, union and SFTP tests plus paging/concurrency
regressions with the race detector:

```sh
python3 scripts/build-mount-engine.py --test --output "$PWD/build/turtle-rclone"
python3 -m unittest discover -s tests -v
TARGET_ARCHS='arm64 x86_64' scripts/build.sh
```

The native rename test uses `build/turtle-rclone` when present. It creates an
isolated case-sensitive APFS/SFTP fixture and verifies writes, metadata and
renames without touching saved connections or remote user files. Dependency
license texts are generated into the app as `MountEngineNotices.txt`.
