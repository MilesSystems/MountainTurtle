# Drive metrics

`service/drive_metrics.py` returns JSON for two independent read-only commands:

```sh
python3 service/drive_metrics.py snapshot CONNECTION_ID
python3 service/drive_metrics.py storage CONNECTION_ID
python3 service/drive_metrics.py storage CONNECTION_ID --rate 0.023
python3 service/cloud_activity.py snapshot CONNECTION_ID --hours 1
python3 service/cloud_billing.py costs CONNECTION_ID
```

With no rate, the service estimates storage cost automatically from Amazon's public regional S3 on-demand price list. The optional rate overrides that with a user-provided blended USD per GiB-month assumption. Neither method returns an actual bill. `--resource-dir RESOURCES` may precede the command in the installed app.

## Transfer and cache charts

A snapshot reads only `core/stats` and `vfs/stats` from the mount's authenticated local rclone control endpoint. It never walks Finder, reads object contents, or refreshes remote folders. Control credentials and file names are excluded from results and history. Proxy use and redirects are disabled.

The current sample includes combined transferred bytes, completed transfer count, checks, errors, active transfers, elapsed time, disk cache bytes/files, cache errors, queued/in-progress uploads, and metadata cache counts. Unsupported fields are `null`. Read and write byte totals are not split because these counters do not identify direction. These are mount-process counters, not all traffic to an AWS account.

`speedBytesPerSecond` is the byte-counter difference divided by the time between observations. The first sample, a process/counter reset, or a gap greater than 90 seconds has no current-speed value. `averageSpeedBytesPerSecond` retains rclone's separate session-average measurement. Cache size is an evictable local cache; its configured size is a target, not a hard disk quota.

The dashboard's observations are saved locally with private permissions, retaining at most 720 samples from the last 24 hours. Collection is on demand; closing the dashboard does not create background history. Missing/disconnected intervals are unknown, not zero.

## S3 activity from every computer

`cloud_activity.py snapshot CONNECTION_ID --hours 1|6|24` reads remote, whole-bucket request metrics from CloudWatch. These measurements include other computers and applications uploading to or downloading from the bucket, whether this Mac's drive is mounted or not. They are separate from both local transfer counters and once-daily storage size.

The reader first checks `s3api list-bucket-metrics-configurations`, requiring `s3:GetMetricsConfiguration`. Only an existing whole-bucket configuration (no filter or an empty prefix) is selected; overlapping configurations are never added together and prefix/tag/access-point subsets are not presented as whole-bucket totals. If none exists, the response is `notConfigured`; no paid monitoring feature is enabled. A truncated configuration listing is an explicit `configurationIncomplete` result rather than proof that no configuration exists.

One `cloudwatch:GetMetricData` request retrieves nine metrics with `BucketName` and the selected `FilterId` dimensions. `BytesUploaded`, `BytesDownloaded`, `PutRequests`, `GetRequests`, `AllRequests`, `4xxErrors`, and `5xxErrors` use one-minute `Sum`; first-byte and total-request latency use one-minute `Average`. Queries cover 1, 6, or 24 hours with at most 20,000 points, one page, and a 25-second timeout per AWS call. The native dashboard refreshes at most once per minute while open.

Graphs retain unknown/missing minutes and report the newest observation time, delay and coverage. Window summaries sum only reported observations. Rates divide each reported minute's bytes by 60; old observations are not labeled current speed. AWS delivers these request metrics on a best-effort basis, so they are not a complete ledger of traffic or charges. Enabling a new configuration does not reconstruct earlier upload history.

**Upload traffic is not net storage growth.** Overwrites, versions, multipart operations, copies, retries and deletes affect stored size. The app does not turn uploaded bytes into a storage balance or a forecast bill.

To make these metrics available on an unconfigured bucket, an administrator can create one whole-bucket request-metrics configuration using `s3:PutMetricsConfiguration`; this is a separate paid AWS setting. Current public US East pricing can be read from the CloudWatch price-list endpoint: the first 10,000 metric-months cost $0.30 each, while GetMetricData costs $0.01 per 1,000 metrics requested (verified from the offer published September 15, 2026). A nine-metric refresh is $0.00009 at that rate, or about $0.0054 per hour when refreshing every minute. Emitted metric count, free tier, account tiers, taxes and other API requests affect actual charges. The monitoring setting remains unchanged by this application.

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

## Actual AWS spend

`cloud_billing.py costs CONNECTION_ID` retrieves daily `UnblendedCost` from AWS Cost Explorer for completed UTC days in the current month. It first verifies the configured profile's account through STS and filters by both `SERVICE=Amazon Simple Storage Service` and that `LINKED_ACCOUNT`. A management account therefore does not accidentally include every member account. These are account S3 costs across all buckets and regions, **not costs attributed to the selected bucket**.

The read requires `ce:GetCostAndUsage` and any organization-level Cost Explorer access. It does not enable billing features, modify IAM, activate tags, or opt into paid resource-level data. A successful response contains real reported daily costs and AWS's `Estimated` flags; current-month costs are provisional and can lag 24 hours or longer. Negative credits remain negative. Missing or malformed days, unexpected pagination or mixed currencies cannot become a complete monthly total. On the first UTC day of a month, no query is made until a completed day exists.

A private, locked cache shares one report among drives using the same profile/account/date range for six hours. This bounds paid reads: ordinary Cost Explorer primary-view requests cost $0.01 each. Refreshing the dashboard respects the cache, while every load verifies the current account identity. API errors are sanitized and independent of activity/storage metrics. Other-service charges, support and account-wide invoice adjustments are outside the S3 filter.

- [AWS Cost Explorer GetCostAndUsage API](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetCostAndUsage.html)
- [AWS Cost Explorer API pricing](https://aws.amazon.com/aws-cost-management/aws-cost-explorer/pricing/)

Run `python3 -m unittest discover -s tests -p test_cloud_billing.py -v` for account isolation, monetary parsing, incomplete reports, sign-in/permissions, cache expiry and concurrent paid-read prevention.

## Sources and checks

- [AWS S3 CloudWatch metric names, dimensions, and component definitions](https://docs.aws.amazon.com/AmazonS3/latest/userguide/metrics-dimensions.html)
- [AWS Price List Bulk API](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/using-the-aws-price-list-bulk-api.html)
- [S3 request-metrics monitoring](https://docs.aws.amazon.com/AmazonS3/latest/userguide/cloudwatch-monitoring.html)
- [Public regional CloudWatch monitoring and retrieval prices](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonCloudWatch/current/us-east-1/index.json)
- [Public regional S3 offer, including product attributes and tier dimensions](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonS3/current/us-east-1/index.json)
- [AWS S3 pricing and binary storage-unit definition](https://aws.amazon.com/s3/pricing/)
- [rclone remote-control statistics](https://rclone.org/rc/#core-stats)
- [rclone VFS statistics](https://rclone.org/rc/#vfs-stats)
- [rclone SFTP filesystem statistics and disabled shell access](https://rclone.org/sftp/#about-command)

Run `python3 -m unittest discover -s tests -p test_drive_metrics.py -v` for transport restrictions, counter resets, bounded history, partial/stale/absent daily observations, timestamp alignment, duplicate handling, pricing tiers, shared overhead allowances, unsupported costs, cache expiry, manual overrides, and sanitized cloud failures. These mocked tests do not establish that a user's AWS profile has CloudWatch access or that a real server is connected.
