# Drive metrics

`service/drive_metrics.py` returns JSON for two independent read-only commands:

```sh
python3 service/drive_metrics.py snapshot CONNECTION_ID
python3 service/drive_metrics.py storage CONNECTION_ID
python3 service/drive_metrics.py storage CONNECTION_ID --rate 0.023
```

With no rate, the service estimates storage cost automatically from Amazon's public regional S3 on-demand price list. The optional rate overrides that with a user-provided blended USD per GiB-month assumption. Neither method returns an actual bill. `--resource-dir RESOURCES` may precede the command in the installed app.

## Transfer and cache charts

A snapshot reads only `core/stats` and `vfs/stats` from the mount's authenticated local rclone control endpoint. It never walks Finder, reads object contents, or refreshes remote folders. Control credentials and file names are excluded from results and history. Proxy use and redirects are disabled.

The current sample includes combined transferred bytes, completed transfer count, checks, errors, active transfers, elapsed time, disk cache bytes/files, cache errors, queued/in-progress uploads, and metadata cache counts. Unsupported fields are `null`. Read and write byte totals are not split because these counters do not identify direction. These are mount-process counters, not all traffic to an AWS account.

`speedBytesPerSecond` is the byte-counter difference divided by the time between observations. The first sample, a process/counter reset, or a gap greater than 90 seconds has no current-speed value. `averageSpeedBytesPerSecond` retains rclone's separate session-average measurement. Cache size is an evictable local cache; its configured size is a target, not a hard disk quota.

The dashboard's observations are saved locally with private permissions, retaining at most 720 samples from the last 24 hours. Collection is on demand; closing the dashboard does not create background history. Missing/disconnected intervals are unknown, not zero.

## Daily S3 storage

For S3 drives, the storage command performs one bounded AWS CLI `cloudwatch get-metric-data` request, with daily `Average` statistics over 30 days. It requires `cloudwatch:GetMetricData` for the configured profile and region. It does not enumerate a bucket or enable paid request metrics.

`BucketSizeBytes` is requested separately for each documented storage component, including archive and minimum-size overhead. `NumberOfObjects` uses `StorageType=AllStorageTypes`. Components are aligned to the same UTC observation day before summing. Repeated timestamps are not added twice. The latest fully aligned day provides `totalBytes`; `sourceTimestamp` identifies that day. A newer incomplete day remains visible in history with a `null` total, its `reportedBytes` subtotal and missing component names. A class with no observations in the entire window is explicitly listed as unreported. Its size is not invented.

Responses distinguish available, partial, stale (older than three days), no data, sign-in required, permission denied, unavailable, and unsupported states. Missing measurements never imply an empty bucket. Cloud failures are separate from local transfer collection.

## SFTP server capacity

For SFTP drives, `storage` reads the server's filesystem-statistics extension using `rclone about --json`. It uses the saved authentication and trusted known-hosts file, a private temporary config, disabled hash checks, and `shell_type=none`. It does not scan directories or fall back to `df` or other remote shell commands. The direct SFTP remote is queried, excluding the local Finder icon overlay from totals.

The response has `scope=remoteFilesystem`, explicit `totalBytes` (capacity), `usedBytes`, and `freeBytes`, a source timestamp and bounded successful-refresh history. This filesystem may be shared with other folders and users; the used value is not the selected folder's recursive size. Missing fields stay unknown, and reserved space can make used plus free differ from total. Servers without the extension return an unsupported status. SFTP has no authoritative price feed, so no server-hosting bill or storage cost is invented.

## Automatic public pricing and manual overrides

Automatic estimates use `https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonS3/current/REGION/index.json` with the drive's validated region. This public HTTPS download sends no bucket name, profile, or credentials. It allows at most 12 MiB, uses a 20-second deadline with five-second socket timeouts, and caches successful price documents locally for 24 hours. Expired prices are not silently used when a refresh fails. Storage and local metrics remain available even if pricing fails.

Products must match the region, Amazon S3 service, Storage family, explicit volume type and usage type. Only unconditional USD `GB-Mo` on-demand dimensions with contiguous, non-overlapping tiers are accepted. AWS defines its storage GB as 2³⁰ bytes (GiB). Tier brackets apply incrementally to this bucket alone; account-wide or consolidated usage may change the effective rate. Components billed under the same product, such as Standard storage and certain archive metadata overhead, share one tier allowance.

The verified mappings cover Standard, Standard-IA, One Zone-IA, Reduced Redundancy, Express One Zone, Glacier Flexible/Instant Retrieval and staging, and the published Intelligent-Tiering storage tiers. Explicit AWS overhead charging rules map known metadata and minimum-size components. Deep Archive object storage/overhead and any other unmatched component remain unpriced; their absence from this S3 offer is not permission to substitute another class's rate.

`estimate.method` distinguishes `awsPublicPricing` from `manualBlended`. Automatic results include USD currency, region, price-list publication/fetch timestamps, the public source URL/version, per-component contributions, and assumptions. If any nonzero component is unpriced, `status` is `partial`, `monthlyUSD` and `annualUSD` are `null`, and `knownMonthlyUSD` is only the priced portion. No known components means no known subtotal. The dashboard offers the manual blended override for agreements or unsupported classes.

Manual estimates multiply the selected daily size in GiB by the user rate and bypass the price download. All estimates assume constant storage for a month; annual estimates multiply that by twelve. Neither is historical spend, a current-month bill, nor a forecast of future growth. Estimates omit requests, retrieval, transfer, minimum-duration charges, monitoring, replication charges, free-tier credits, taxes, and negotiated discounts.

## Sources and checks

- [AWS S3 CloudWatch metric names, dimensions, and component definitions](https://docs.aws.amazon.com/AmazonS3/latest/userguide/metrics-dimensions.html)
- [AWS Price List Bulk API](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/using-the-aws-price-list-bulk-api.html)
- [Public regional S3 offer, including product attributes and tier dimensions](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonS3/current/us-east-1/index.json)
- [AWS S3 pricing and binary storage-unit definition](https://aws.amazon.com/s3/pricing/)
- [rclone remote-control statistics](https://rclone.org/rc/#core-stats)
- [rclone VFS statistics](https://rclone.org/rc/#vfs-stats)
- [rclone SFTP filesystem statistics and disabled shell access](https://rclone.org/sftp/#about-command)

Run `python3 -m unittest discover -s tests -p test_drive_metrics.py -v` for transport restrictions, counter resets, bounded history, partial/stale/absent daily observations, timestamp alignment, duplicate handling, pricing tiers, shared overhead allowances, unsupported costs, cache expiry, manual overrides, and sanitized cloud failures. These mocked tests do not establish that a user's AWS profile has CloudWatch access or that a real server is connected.
