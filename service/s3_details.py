#!/usr/bin/env python3
"""Read S3 bucket type and observed storage classes without scanning objects."""

import argparse
import fcntl
import hashlib
import json
import os
import time

import drive_metrics as metrics
import turtle_service as turtle

CACHE_SECONDS = 3600
FAILURE_CACHE_SECONDS = 300

# These are object storage classes, not billing categories: for example,
# Glacier metadata billed at Standard rates still belongs to Glacier objects.
# https://docs.aws.amazon.com/AmazonS3/latest/userguide/metrics-dimensions.html
STORAGE_CLASSES = {
    "S3 Standard": ("StandardStorage",),
    "S3 Intelligent-Tiering": (
        "IntelligentTieringFAStorage", "IntelligentTieringIAStorage", "IntelligentTieringAAStorage",
        "IntelligentTieringAIAStorage", "IntelligentTieringDAAStorage", "IntAAObjectOverhead",
        "IntAAS3ObjectOverhead", "IntDAAObjectOverhead", "IntDAAS3ObjectOverhead",
    ),
    "S3 Standard-IA": ("StandardIAStorage", "StandardIAObjectOverhead", "StandardIASizeOverhead"),
    "S3 One Zone-IA": ("OneZoneIAStorage", "OneZoneIASizeOverhead"),
    "S3 Glacier Instant Retrieval": ("GlacierInstantRetrievalStorage", "GlacierIRSizeOverhead"),
    "S3 Glacier Flexible Retrieval": (
        "GlacierStorage", "GlacierObjectOverhead", "GlacierS3ObjectOverhead", "GlacierStagingStorage",
    ),
    "S3 Glacier Deep Archive": (
        "DeepArchiveStorage", "DeepArchiveObjectOverhead", "DeepArchiveS3ObjectOverhead", "DeepArchiveStagingStorage",
    ),
    "S3 Express One Zone": ("ExpressOneZoneStorage",),
    "S3 Reduced Redundancy": ("ReducedRedundancyStorage",),
}


def bucket_type(connection):
    # AWS reserves this suffix for directory buckets; general purpose buckets
    # cannot use it. Directory buckets also support Local Zones, so do not
    # assume that the suffix's zone identifier contains "az".
    # https://docs.aws.amazon.com/AmazonS3/latest/userguide/directory-bucket-naming-rules.html
    bucket = str(connection.get("bucket", ""))
    if bucket.endswith("--x-s3"):
        return "Directory"
    if bucket.endswith(("-s3alias", "--ol-s3", ".mrap", "--table-s3")):
        return "Unavailable"
    return "General purpose" if bucket else "Unavailable"


def storage_labels(storage):
    components = {item.get("name") for item in storage.get("storageClasses", [])
                  if isinstance(item, dict) and metrics.number(item.get("bytes")) is not None
                  and item["bytes"] > 0}
    return [label for label, names in STORAGE_CLASSES.items() if components.intersection(names)]


def cache_identity(connection):
    fields = [turtle.connection_backend(connection), connection.get("bucket"),
              connection.get("profile"), connection.get("region")]
    return hashlib.sha256(json.dumps(fields).encode()).hexdigest()


def base_result(connection, now):
    return {"ok": True, "connectionID": connection["id"], "provider": turtle.connection_backend(connection),
            "bucketType": bucket_type(connection), "storageClass": "Unavailable", "storageClasses": [],
            "status": "unavailable", "message": "Storage classes are unavailable.",
            "source": "Amazon CloudWatch AWS/S3 daily storage metrics", "sourceTimestamp": None,
            "queriedAt": now, "cached": False, "isStale": False}


def details(connection, paths, refresh=False, reader=None, now=None):
    """Fetch on demand, caching independently of the frequent service status poll.

    CloudReader bounds the CLI request to 25 seconds and makes no object-list or
    pricing requests. Store raw measurements so cached timestamps and partial
    results continue to be interpreted using the existing metrics parser.
    """
    now = time.time() if now is None else now
    result = base_result(connection, now)
    if turtle.connection_backend(connection) != "s3":
        result.update(bucketType="Unavailable", status="unsupported",
                      message="S3 details are only available for S3 drives.")
        return result
    directory = metrics.metric_directory(paths, connection)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    identity = cache_identity(connection)
    with (directory / "s3-details.lock").open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        path = directory / "s3-details.json"
        cache = metrics.safe_read(path, {})
        queried = metrics.number(cache.get("queriedAt")) if isinstance(cache, dict) else None
        valid = (isinstance(cache, dict) and cache.get("schemaVersion") == 1
                 and cache.get("identity") == identity and queried is not None
                 and queried <= now and isinstance(cache.get("response", cache.get("failure")), dict))
        lifetime = FAILURE_CACHE_SECONDS if valid and "failure" in cache else CACHE_SECONDS
        if valid and not refresh and now - queried < lifetime:
            result.update(cached=True, queriedAt=queried)
        else:
            cache = {"schemaVersion": 1, "identity": identity, "queriedAt": now}
            try:
                response = (reader or metrics.CloudReader(connection, paths)).read(now)
                # Validate before writing so permission and malformed responses
                # use the short failure cache instead of hiding behind an hour.
                metrics.parse_storage(response, now)
                cache["response"] = response
            except metrics.MetricsError as error:
                cache["failure"] = {"status": error.status, "message": str(error)}
            turtle.write_json(path, cache)

    if "failure" in cache:
        result.update(cache["failure"])
        return result
    storage = metrics.parse_storage(cache["response"], now)
    labels = storage_labels(storage)
    label = labels[0] if len(labels) == 1 else "Mixed: " + ", ".join(labels) if labels else "Unavailable"
    if labels and storage["status"] == "partial":
        label += " (partial)"
    if labels and storage["isStale"]:
        label += " (stale)"
    result.update(storageClass=label, storageClasses=labels, status=storage["status"],
                  sourceTimestamp=storage["sourceTimestamp"], isStale=storage["isStale"],
                  message=storage["message"] + " Storage classes describe reported bucket contents, including versions and multipart parts; a bucket can use multiple classes.")
    if not labels and storage["status"] in ("available", "stale"):
        result.update(status="noData", message="No nonzero storage-class measurements were reported. An empty bucket does not have one storage class.")
    return result


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--resource-dir")
    commands = result.add_subparsers(dest="command", required=True)
    command = commands.add_parser("details")
    command.add_argument("id")
    command.add_argument("--refresh", action="store_true")
    return result


def main():
    os.umask(0o077)
    connection = None
    try:
        args = parser().parse_args()
        paths = turtle.Paths(resources=args.resource_dir)
        connection = turtle.find_connection(turtle.Store(paths).read(), args.id)
        result = details(connection, paths, refresh=args.refresh)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    except Exception as error:
        message = str(error) if isinstance(error, (metrics.MetricsError, ValueError)) else "Could not retrieve S3 details. Try again."
        result = base_result(connection or {"id": ""}, time.time())
        result.update(ok=False, error=message, message=message)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
