# Bounded photo browser service

The native app can call `service/photo_browser.py` to browse S3 photos without
asking Finder to enumerate a huge flat directory. The script uses Python 3.9+
standard-library code, the installed AWS CLI v2, macOS `sips`, and the bundled
ImageIO photo-date helper. It emits one
JSON response and never writes to S3. It reads the same saved connection records
as the mount service, so a request can access only its selected bucket/profile.

## CLI and response contract

`--resource-dir PATH` is optional and appears before the command. Each request
uses a saved connection ID. Keys and ETags are separate process arguments, never
shell command text. The native client should launch the script directly using
its bundled path, just as it launches the main service.

The AWS adapter passes GET bucket, key, version condition, and range as explicit
named arguments. This supports the installed AWS CLI's streaming `get-object`
argument validation; listing continues to use a structured JSON request.

```text
photo_browser.py list ID --prefix PREFIX [--cursor TOKEN] [--limit 100]
photo_browser.py thumbnail ID --key KEY --etag ETAG --size BYTES [--pixels 256]
photo_browser.py thumbnail ID ... --cache-only
photo_browser.py thumbnail ID ... --allow-original
photo_browser.py date-taken ID --key KEY --etag ETAG --size BYTES [--cache-only]
photo_browser.py open-original ID --key KEY --etag ETAG --size BYTES
```

Successful listing:

```json
{
  "ok": true,
  "prefix": "Portfolio/",
  "folders": [{"key": "Portfolio/Portraits/", "name": "Portraits"}],
  "photos": [{"key": "Portfolio/photo.jpg", "name": "photo.jpg", "size": 1234567, "etag": "\"version\""}],
  "nextCursor": null,
  "hiddenFileCount": 0
}
```

Listing makes one ListObjectsV2 request using the exact prefix, a `/` delimiter,
and a default page limit of 100 (maximum 200). Automatic AWS CLI pagination is
disabled. `nextCursor` is the opaque S3 continuation token; pass it only when the
user asks for the next page. Non-photo files are omitted, with the count omitted
from this page reported as `hiddenFileCount`. A page can therefore contain no
photos while still having a next page. The service does not automatically scan
past such a page. See [AWS's listing API documentation](https://docs.aws.amazon.com/cli/latest/reference/s3api/list-objects-v2.html).

Successful thumbnail:

```json
{
  "ok": true,
  "thumbnailPath": "/absolute/local/cache/path.jpg",
  "source": "embedded-jpeg",
  "downloadedBytes": 131072,
  "needsOriginal": false,
  "dateTaken": "2026-09-11T13:34:50",
  "dateState": "known"
}
```

`source` is `thumbnail-cache`, `original-cache`, `embedded-jpeg`, `bounded-jpeg`,
`downloaded-original`, or `unavailable`. An unavailable preview returns
`thumbnailPath: null`, `needsOriginal: true`, and an explanation; it is a usable
placeholder result rather than a reason to download the full original silently.
Original retrieval returns `originalPath`, `source`, `downloadedBytes`, and
nullable `dateTaken`, and `dateState`.
Errors use `{"ok":false,"error":"explanation"}` and a nonzero exit status.
Cancellation exits with status 130.

## Date taken and sorting

Photo tiles show **Date taken** from EXIF `DateTimeOriginal`. Missing dates use
these explicit `dateState` values:

| Value | Label | Meaning |
| --- | --- | --- |
| `notChecked` | Not checked | No metadata read has completed yet. |
| `needsOriginal` | Needs original | The bounded header did not contain a date, or this format needs its complete original. |
| `noCameraDate` | No camera date | A readable complete original contains no valid camera capture date. |
| `error` | Couldn't read—retry | Metadata could not be read; retry can try again. |
| `known` | The camera date | A valid capture date was found. |

The native helper reads ImageIO properties without
decoding image pixels. If a bounded JPEG header ends before its image frame,
it also checks complete EXIF APP1 segments within those first 128 KiB for
`DateTimeOriginal`, validating TIFF byte order, directory counts, and offsets.
This handles large editing metadata that prevents ImageIO from exposing the
already available EXIF date. It validates the recorded calendar date and keeps the
camera's local wall-clock time; it does not guess a timezone or replace missing
capture dates with file creation, modification, or S3 upload dates.

Thumbnail requests extract dates from the same original or bounded JPEG header
they already read. Date results are cached separately from thumbnail size and
scoped to the connection, bucket, profile, region, key, ETag, and size. Opening an
original can resolve a date that was unavailable in a header or unsupported
online format. A cached thumbnail alone is not a source for the original's date.

The explicit `date-taken` operation returns `ok`, nullable `dateTaken`, `dateState`, and
`downloadedBytes`. It reuses a verified complete local original when available;
otherwise it can request only the first 128 KiB of a JPEG, conditional on the
listed ETag. It never fetches a full large original for a date. Other formats
show **Needs original** until a complete original is locally available. `--cache-only`
forbids remote reads and returns **Not checked** when nothing has been inspected.
Missing metadata is cached only after a successful read. Helper failures remain
retryable, and a newly available complete original can replace an earlier
**Needs original** result. Legacy negative cache entries are rechecked before
claiming that a complete photo has no camera date.

**Sort this page** offers name, newest date taken, and oldest date taken. Date
sorting reads dates for the current page with at most two photo requests in
flight, then orders known dates by recorded camera time with missing dates last.
Name provides a deterministic tie-break. This does not sort the entire bucket or
scan additional pages. Choosing date sorting can read up to 128 KiB per uncached
JPEG on the page even when small previews are disabled. Navigation and dismissal
cancel outstanding page requests.

## Thumbnail retrieval and honest download behavior

Request thumbnails only for visible cells, and stop requesting them after the
cell or folder disappears. The service tries, in order:

1. An existing local thumbnail tied to connection, bucket, profile, region, key,
   ETag, size, and requested pixel size.
2. A previously downloaded original, or a complete clean rclone-cached original
   whose metadata ranges cover the whole file and whose fingerprint matches the
   requested ETag. Sparse or dirty cache files are not treated as full originals.
3. For JPEGs, at most the first 128 KiB, requested with an S3 byte range and
   `If-Match`. An EXIF IFD1 embedded JPEG thumbnail can then be extracted locally.
   For a JPEG smaller than that limit, the probe covers the complete small image,
   which can be rendered directly.

When none of those paths works, the default response asks for an explicit
original action. No full large original is fetched automatically. This means
some photos initially have placeholders. `--cache-only` disables even the
bounded JPEG probe. The optional `--allow-original` explicitly permits a full
fetch for rendering when the listed size is at most 32 MiB. The UI must not use
that flag while promising that original bytes are downloaded only on Open.

There is no generic S3 thumbnail endpoint, and no derivative prefix is guessed
or scanned. If a bucket already has derivatives, their documented key mapping
would need an explicit future connection setting. The implemented cheap path
uses embedded [EXIF JPEG thumbnail tags](https://exiv2.org/tags.html), not a claim
that every JPEG contains one. Byte reads are conditional on the listed ETag;
changed objects require a refreshed listing. See [AWS GetObject](https://docs.aws.amazon.com/cli/latest/reference/s3api/get-object.html).

## Original files

`open-original` is an explicit full-file action. It copies a verified complete
local original when possible, otherwise fetches the original from S3. The 32 MiB
thumbnail fallback limit does not apply. It checks available disk space and uses
a bounded network timeout, then verifies the downloaded length before publishing
the result. The native app can open or reveal the returned local file.

Downloads go under `~/Downloads/Mountain Turtle/<bucket>` with a hash-prefixed,
safe basename. They are retained as user files, separate from the bounded
thumbnail cache. A locally modified download is preserved; a fresh remote copy
gets a different filename. Editing that local file does not upload it to S3.
Opening an original also makes it available for later thumbnail generation.

## Durable offline queue

**Keep offline** adds selected photos to `service/offline_photos.py`. Unlike the
single-photo `open-original` convenience action, this queue persists progress,
checksums, and pause intent outside the evictable cache. It runs one detached
worker per connection and uses conditional 8 MiB S3 range reads. Pause stops the
active request and retains the last committed, hashed prefix; resume validates
that prefix before requesting the remaining ranges.

```text
offline_photos.py status ID
offline_photos.py enqueue ID --items-json '[{"key":"photo.jpg","etag":"version","size":1234}]'
offline_photos.py pause ID
offline_photos.py resume ID
offline_photos.py retry ID --item-id ITEM
offline_photos.py verify ID --item-id ITEM
offline_photos.py open ID --item-id ITEM
```

As with the photo browser, optional `--resource-dir PATH` precedes the command.
Status reads local records only. Responses include `paused`, `workerRunning`,
`items`, and aggregate `totals`. Each item includes its version identity, state,
byte progress, any error, and a local path after completion. The native UI must
not treat all completed byte counts as verified.

Originals are retained under
`~/Library/Application Support/Mountain Turtle/offline-photos/`, in folders scoped
to the connection and bucket. Cache cleanup does not remove them. Completed
files are checked against S3's supported `FULL_OBJECT` checksum and reread with
local SHA-256. State `verified` means both checks passed; `downloaded` means the
conditional transfer and local check passed but a supported cloud checksum was
unavailable. Composite/multipart checksums and ETags are not assumed to be
full-object content hashes. Later **Check copy** and **Open** validate the local
SHA-256 without a network request.

The queue never overwrites a changed local original, does not upload edits, and
preserves an older retained version when a new cloud version is selected.
Metadata reads and thumbnails can reuse retained originals. App updates pause
workers before replacing their code and restore prior queue intent afterward.

## Cache, concurrency, and cancellation

Thumbnail images and small bookkeeping records live under
`~/Library/Caches/MountainTurtle/photo-browser/artifacts`. Eviction keeps this
artifact cache at most 256 MiB and 2,048 files after a completed cache update.
The temporary working set is separate: at most two thumbnail/original jobs hold
download slots, and explicit original files may be much larger. Original
downloads are not evicted as part of thumbnail cleanup.

Cross-process file locks limit photo work to two jobs and coalesce requests for
the same thumbnail. Temporary files use private directories. Symbolic links in
owned cache/download paths are rejected to avoid redirecting file operations.
SIGTERM or SIGINT stops and reaps the active AWS/sips process group and cleans up
temporary downloads. The UI should terminate cancelled CLI requests instead of
merely discarding their results. A queued job also stops on cancellation.

This service creates local previews. It is not a global photo index, a thumbnail
upload job, or a reorganization of S3 keys. Its bounded pages make flat folders
browsable through the app; they do not change how a plain Finder directory is
enumerated.

## Focused validation

```sh
python3 -m unittest discover -s tests -p test_photo_browser.py -v
```

Tests use fake S3 responses and temporary local files, with a real local `sips`
render where available. They cover one-page listing, conditional range reads,
EXIF extraction, complete-cache validation, explicit original fetches, cache
eviction, local-edit preservation, path boundaries, and cancellation. No test
requires live AWS access or makes a cloud mutation.

## Live bounded-header verification — 2026-09-14

A three-item listing of `Portfolio/Engagements/Sam&Sky/` succeeded. All three
listed JPEGs already had complete matching originals in the normal rclone cache,
so this verification used a separate, empty temporary cache to exercise the cold
thumbnail path. It was a controlled cold-cache test, not a claim that the object
had never been downloaded on this Mac.

For `Portfolio/Engagements/Sam&Sky/_Z2A0040.jpg` (listed size **1,686,063 bytes**),
the default thumbnail request made one successful `bytes=0-131071` range GET:

- Downloaded **131,072 bytes**; no full-original GET and no S3 writes.
- Returned `source: "embedded-jpeg"` and `needsOriginal: false`.
- Rendered a local JPEG thumbnail from the embedded preview.
- Received header bytes identical to the corresponding bytes in the complete,
  version-matched existing rclone cache. Header SHA-256:
  `ad71d37dfcf03248dad6d7e09b4f61bf33cf34199f2370fbe3c6ad57734372d8`.
- Left the normal cache unchanged and removed the temporary fixture and preview
  after verification.

The focused photo-browser suite passed **27 tests** after the live AWS CLI
argument compatibility fix, including explicit GET arguments and literal handling
of object keys containing leading dashes or shell characters.
