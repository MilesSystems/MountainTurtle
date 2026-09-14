# Future design: bounded browsing for flat photo libraries

**Status: proposal only. This is not implemented in Mountain Turtle v1.** The
initial app work connects the Nikki bucket using its existing directory layout.

## Problem

A bucket can contain a very large number of photos under one prefix. A normal
filesystem directory listing asks the remote for that directory's entries;
Finder may then request metadata and previews. On-demand file caching does not
remove the cost of enumerating a huge flat directory. A displayed drive icon or
a successful connection does not demonstrate that a million-photo folder is
usable.

The next version should make the amount of work needed to open a folder bounded
without renaming, copying, tagging, or rewriting the original objects. It should
not scan the entire bucket during login or connection setup.

## Proposed optional virtual view

For a verified collection whose original basenames begin with a fixed lowercase
hexadecimal identifier, expose virtual folders for successive identifier digits.
For example, the existing S3 key `images/000abc….webp` could be displayed as
`0/0/0/000abc….webp`. This changes only the browsing view; the original key stays
`images/000abc….webp`.

The first three levels would each show 16 synthetic children without calling S3.
A leaf would issue one ListObjectsV2 request for its exact original prefix, with
a limit such as 501 objects. Up to 500 entries would be shown as files. A full or
truncated page would instead show another level of 16 children, without following
continuation tokens. This can subdivide further until a practical listing is
possible, with an explicit maximum depth and error when subdivision cannot help.

The service would retain a bounded, expiring cache of prefix pages and object
metadata. Repeated and concurrent requests for the same page would reuse a
result. Directory HEAD probes must not trigger child listings. Cached listing
metadata should answer file HEAD probes without a separate S3 request per photo.
Actual reads would stream only the selected object or requested byte range.

One implementation option is a read-only HTTP filesystem on loopback consumed
by rclone's HTTP backend, then mounted through the existing NFS layer. An
unguessable local token, constant-time token checking, omitted token access logs,
and private configuration would limit accidental access. The token must not be
placed in process arguments or public diagnostics. The mount and gateway would
both reject writes.

## Decisions before implementation

- Confirm the real key naming convention, supported image types, prefix, region,
  and AWS profile with a small, bounded sample. Never assume every photo has a
  32-character hash just because a few filenames do.
- Define how unmatched filenames and real subfolders appear. Do not silently
  hide them while claiming this is a complete bucket view.
- Make the optional virtual layout clear in the connection editor and Finder
  volume name. Preserve the original view as an explicit alternative.
- Distinguish local health from current S3 access. An expired SSO session must
  produce a sign-in state, not an empty directory.
- Set cache size, expiry, cancellation, timeouts, retry limits, and a refresh
  action. Avoid prefetching all branches or building a global thumbnail index.
- Decide how an exact original key can be found without manually traversing
  many virtual folders. Browsing and search are separate capabilities.

## Verification gate

Use a fake S3 client to prove fixed child counts, bounded calls, no pagination,
cache reuse, safe traversal rejection, byte-range reads, and zero mutations.
Test uneven prefixes as well as uniform identifiers. Verify the HTTP/rclone
contract locally before connecting to an authorized real bucket.

For live evaluation, measure opening a known leaf and reading one known image.
Report cold and cached timings with observed object counts; do not turn a sample
into an unverified claim about the entire library. Finder responsiveness,
network loss, credential renewal, and safe ejection still need separate checks.
