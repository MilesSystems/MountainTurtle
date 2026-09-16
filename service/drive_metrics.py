#!/usr/bin/env python3
"""Read-only local drive telemetry and daily S3 storage estimates as JSON."""

import argparse
import base64
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

import turtle_service as turtle

GIB = 1024 ** 3
HISTORY_LIMIT = 720
HISTORY_SECONDS = 24 * 3600
MAX_SPEED_INTERVAL = 90
STORAGE_DAYS = 30
STALE_SECONDS = 3 * 86400
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PRICE_BYTES = 12 * 1024 * 1024
PRICE_CACHE_SECONDS = 24 * 3600
PRICE_SOURCE = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonS3/current/{region}/index.json"
# Match both published product attributes; never infer a price from a similar
# label. Unmapped components remain explicitly unpriced.
PRICE_PRODUCTS = {
    "standard": ("Standard", "TimedStorage-ByteHrs"),
    "standardIA": ("Standard - Infrequent Access", "TimedStorage-SIA-ByteHrs"),
    "oneZoneIA": ("One Zone - Infrequent Access", "TimedStorage-ZIA-ByteHrs"),
    "reducedRedundancy": ("Reduced Redundancy", "TimedStorage-RRS-ByteHrs"),
    "express": ("Express One Zone", "TimedStorage-XZ-ByteHrs"),
    "glacier": ("Amazon Glacier", "TimedStorage-GlacierByteHrs"),
    "glacierInstant": ("Glacier Instant Retrieval", "TimedStorage-GIR-ByteHrs"),
    "glacierStaging": ("Amazon Glacier Staging", "TimedStorage-GlacierStaging"),
    "deepArchiveStaging": ("Glacier Deep Archive", "TimedStorage-GDA-Staging"),
    "intFA": ("Intelligent-Tiering Frequent Access", "TimedStorage-INT-FA-ByteHrs"),
    "intIA": ("Intelligent-Tiering Infrequent Access", "TimedStorage-INT-IA-ByteHrs"),
    "intAIA": ("Intelligent-Tiering Archive Instant Access", "TimedStorage-INT-AIA-ByteHrs"),
    "intAA": ("IntelligentTieringArchiveAccess", "TimedStorage-INT-AA-ByteHrs"),
    "intDAA": ("IntelligentTieringDeepArchiveAccess", "TimedStorage-INT-DAA-ByteHrs"),
}
PRICE_COMPONENTS = {
    "StandardStorage": "standard", "GlacierS3ObjectOverhead": "standard",
    "DeepArchiveS3ObjectOverhead": "standard", "IntAAS3ObjectOverhead": "standard", "IntDAAS3ObjectOverhead": "standard",
    "StandardIAStorage": "standardIA", "StandardIASizeOverhead": "standardIA",
    "OneZoneIAStorage": "oneZoneIA", "OneZoneIASizeOverhead": "oneZoneIA",
    "ReducedRedundancyStorage": "reducedRedundancy", "ExpressOneZoneStorage": "express",
    "GlacierStorage": "glacier", "GlacierObjectOverhead": "glacier", "IntAAObjectOverhead": "glacier",
    "GlacierInstantRetrievalStorage": "glacierInstant", "GlacierIRSizeOverhead": "glacierInstant",
    "GlacierStagingStorage": "glacierStaging", "DeepArchiveStagingStorage": "deepArchiveStaging",
    "IntelligentTieringFAStorage": "intFA", "IntelligentTieringIAStorage": "intIA",
    "IntelligentTieringAIAStorage": "intAIA", "IntelligentTieringAAStorage": "intAA", "IntelligentTieringDAAStorage": "intDAA",
}
PUBLIC_COST_ASSUMPTIONS = [
    "Storage-only estimate from public regional AWS on-demand prices, not an AWS bill. AWS storage GB means GiB (2^30 bytes).",
    "Applies each published storage product's volume tiers to this bucket alone. Account-wide or consolidated-billing tier aggregation may change the effective rate.",
    "Assumes the selected daily storage amount remains constant for a month; annual estimate is twelve such months.",
    "Excludes requests, retrieval, data transfer, minimum-duration charges, monitoring, replication charges, free-tier credits, taxes and negotiated discounts.",
    "Only explicitly matched storage components are priced. If any nonzero component is unpriced, the known subtotal is shown separately and no complete monthly estimate is claimed.",
]
# AWS documents these independent byte components. AllStorageTypes applies ONLY
# to NumberOfObjects, never BucketSizeBytes. Include archive metadata and billed
# minimum-size overhead rather than silently dropping part of the storage total.
STORAGE_TYPES = (
    "StandardStorage", "StandardIAStorage", "StandardIAObjectOverhead", "StandardIASizeOverhead",
    "OneZoneIAStorage", "OneZoneIASizeOverhead", "ReducedRedundancyStorage", "ExpressOneZoneStorage",
    "GlacierStorage", "GlacierObjectOverhead", "GlacierS3ObjectOverhead", "GlacierStagingStorage",
    "GlacierInstantRetrievalStorage", "GlacierIRSizeOverhead", "DeepArchiveStorage",
    "DeepArchiveObjectOverhead", "DeepArchiveS3ObjectOverhead", "DeepArchiveStagingStorage",
    "IntelligentTieringFAStorage", "IntelligentTieringIAStorage", "IntelligentTieringAAStorage",
    "IntelligentTieringAIAStorage", "IntelligentTieringDAAStorage", "IntAAObjectOverhead",
    "IntAAS3ObjectOverhead", "IntDAAObjectOverhead", "IntDAAS3ObjectOverhead",
)
STORAGE_ASSUMPTIONS = [
    "Daily AWS/S3 CloudWatch observations; not a live bucket scan. Delivery can be delayed or missing.",
    "Storage includes reported object versions, multipart parts, metadata and minimum-size overhead; object count includes versions, delete markers and multipart parts.",
    "Totals use storage components from the same UTC observation day. Unreported components are not invented; classes with no observations in this 30-day window are excluded.",
]
COST_ASSUMPTIONS = [
    "Estimate only, not an AWS bill: reported GiB multiplied by your blended USD per GiB-month rate.",
    "Assumes the selected daily storage amount stays constant for a month; annual estimate is twelve such months.",
    "Excludes requests, retrieval, transfer, minimum-duration charges, monitoring, taxes and discounts. Storage classes and regions may have different prices.",
]


class MetricsError(Exception):
    def __init__(self, message, status="unavailable"):
        super().__init__(message)
        self.status = status


def number(value):
    """Accept actual nonnegative finite telemetry; booleans are not numbers."""
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def timestamp(value):
    if number(value) is not None:
        return float(value)
    if not isinstance(value, str):
        return None
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            return None
        return date.timestamp()
    except (ValueError, OverflowError):
        return None


def provider(connection):
    return turtle.connection_backend(connection)


def metric_directory(paths, connection):
    # A digest keeps saved connection IDs from becoming filesystem paths.
    identity = hashlib.sha256(str(connection["id"]).encode()).hexdigest()[:32]
    return paths.base / "metrics" / identity


def safe_read(path, default, max_bytes=MAX_RESPONSE_BYTES):
    try:
        if path.stat().st_size > max_bytes:
            return default
        return turtle.read_json(path, default)
    except (OSError, ValueError, TypeError):
        return default


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LocalReader:
    """Only allow read-only methods on the supervisor's authenticated loopback port."""
    def __init__(self, live):
        self.live = live

    def read(self, method):
        if method not in ("core/stats", "vfs/stats"):
            raise MetricsError("Unsupported local metrics request.")
        port = self.live.get("rcPort")
        user, password = self.live.get("rcUser"), self.live.get("rcPass")
        if (type(port) is not int or not 1 <= port <= 65535 or not isinstance(user, str)
                or not user or not isinstance(password, str) or not password):
            raise MetricsError("Reconnect this drive to enable local metrics.", "disconnected")
        credentials = base64.b64encode(f"{user}:{password}".encode()).decode()
        request = urllib.request.Request(f"http://127.0.0.1:{port}/{method}", data=b"{}",
                                         headers={"Authorization": "Basic " + credentials,
                                                  "Content-Type": "application/json"}, method="POST")
        # Do not use proxy environment variables or follow a redirect carrying
        # privileged local-control credentials to another endpoint.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            with opener.open(request, timeout=2) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError()
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (OSError, ValueError, urllib.error.URLError):
            raise MetricsError("Local metrics are unavailable. Connect or reconnect this drive and try again.") from None


def history_point(core, vfs, connection, live, now):
    disk = vfs.get("diskCache", {})
    if not isinstance(disk, dict):
        disk = {}
    metadata = vfs.get("metadataCache", {})
    if not isinstance(metadata, dict):
        metadata = {}
    in_progress = core.get("transferring")
    point = {
        "timestamp": now,
        "sessionID": str(live.get("sessionID") or f'{live.get("pid", "unknown")}:{connection.get("lastMountAt", "unknown")}'),
        "transferredBytes": number(core.get("bytes")),
        "speedBytesPerSecond": None,
        "averageSpeedBytesPerSecond": number(core.get("speed")),
        "transferCount": number(core.get("transfers")),
        "checks": number(core.get("checks")), "errors": number(core.get("errors")),
        "activeTransfers": len(in_progress) if isinstance(in_progress, list) else (0 if core else None),
        "elapsedSeconds": number(core.get("elapsedTime")),
        "cacheBytes": number(disk.get("bytesUsed")), "cacheFiles": number(disk.get("files")),
        "cacheLimitBytes": (number(connection.get("cacheMaxSizeMiB", 2048)) or 2048) * 1024 ** 2,
        "cacheInUse": number(vfs.get("inUse")),
        "cacheOutOfSpace": disk.get("outOfSpace") if isinstance(disk.get("outOfSpace"), bool) else None,
        "cacheErrors": number(disk.get("erroredFiles")),
        "uploadsQueued": number(disk.get("uploadsQueued")),
        "uploadsInProgress": number(disk.get("uploadsInProgress")),
        "metadataFiles": number(metadata.get("files")), "metadataDirectories": number(metadata.get("dirs")),
        "intervalBytes": None, "counterReset": False,
    }
    return point


def append_history(directory, point):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "history.lock").open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        old = safe_read(directory / "history.json", [])
        if not isinstance(old, list):
            old = []
        now = point["timestamp"]
        history = [item for item in old if isinstance(item, dict) and number(item.get("timestamp")) is not None
                   and now - HISTORY_SECONDS <= item["timestamp"] < now]
        history.sort(key=lambda item: item["timestamp"])
        if history:
            previous = history[-1]
            new_bytes, old_bytes = point["transferredBytes"], number(previous.get("transferredBytes"))
            same_session = point["sessionID"] == previous.get("sessionID")
            reset = same_session and new_bytes is not None and old_bytes is not None and new_bytes < old_bytes
            point["counterReset"] = reset or not same_session
            interval = now - previous["timestamp"]
            if same_session and not reset and new_bytes is not None and old_bytes is not None:
                point["intervalBytes"] = new_bytes - old_bytes
                if 0 < interval <= MAX_SPEED_INTERVAL:
                    point["speedBytesPerSecond"] = (new_bytes - old_bytes) / interval
        history.append(dict(point))
        history = history[-HISTORY_LIMIT:]
        turtle.write_json(directory / "history.json", history)
        return history


def snapshot(connection, paths, live=None, reader=None, now=None):
    now = time.time() if now is None else now
    if live is None:
        live = turtle.Store(paths).runtime().get("connections", {}).get(connection["id"], {})
    reader = reader or LocalReader(live)
    core, vfs, failures = {}, {}, []
    for method in ("core/stats", "vfs/stats"):
        try:
            result = reader.read(method)
            if not isinstance(result, dict):
                raise MetricsError("Local metrics returned an invalid response.")
            if method == "core/stats":
                core = result
            else:
                vfs = result
        except MetricsError as error:
            failures.append(error)
    point = history_point(core, vfs, connection, live, now)
    status = "available" if not failures else ("partial" if core or vfs else failures[0].status)
    point["status"] = status
    history = append_history(metric_directory(paths, connection), point)
    return {"ok": True, "connectionID": connection["id"], "provider": provider(connection),
            "timestamp": now, "status": status, "message": str(failures[0]) if failures else "Local drive metrics are live.",
            "source": "rclone local control API", "current": point, "history": history,
            "capabilities": {"directionalTransferBytes": False, "cloudStorage": provider(connection) == "s3"},
            "assumptions": ["Transfer bytes are combined reads and writes reported by this mount process, not account-wide traffic.",
                            "Speed uses consecutive observed byte counters; it is unavailable after a reset or a sampling gap longer than 90 seconds.",
                            "History records dashboard observations only, retaining up to 720 samples from the last 24 hours. Cache is evictable and its configured limit is a target, not a hard capacity."]}


def storage_queries(connection):
    queries = []
    for index, name in enumerate((*STORAGE_TYPES, "AllStorageTypes")):
        metric = "NumberOfObjects" if name == "AllStorageTypes" else "BucketSizeBytes"
        queries.append({"Id": "objects" if metric == "NumberOfObjects" else f"s{index}", "ReturnData": True,
                        "MetricStat": {"Metric": {"Namespace": "AWS/S3", "MetricName": metric,
                                                  "Dimensions": [{"Name": "BucketName", "Value": connection["bucket"]},
                                                                 {"Name": "StorageType", "Value": name}]},
                                       "Period": 86400, "Stat": "Average"}})
    return queries


class CloudReader:
    def __init__(self, connection, paths):
        self.connection, self.paths = connection, paths

    def read(self, now):
        aws = turtle.executable("aws")
        if not aws:
            raise MetricsError("Install AWS CLI v2 to retrieve daily S3 storage metrics.")
        end = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        payload = {"MetricDataQueries": storage_queries(self.connection),
                   "StartTime": (end - timedelta(days=STORAGE_DAYS)).isoformat(),
                   "EndTime": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                   "ScanBy": "TimestampAscending", "MaxDatapoints": 5000}
        command = [aws, "cloudwatch", "get-metric-data", "--cli-input-json", json.dumps(payload),
                   "--profile=" + self.connection["profile"], "--region=" + self.connection["region"],
                   "--output", "json", "--no-paginate", "--cli-connect-timeout", "3", "--cli-read-timeout", "15"]
        env = turtle.environment(self.connection["profile"], self.paths)
        for key in list(env):
            if key.startswith("AWS_ENDPOINT_URL"):
                env.pop(key)
        env["AWS_MAX_ATTEMPTS"] = "1"
        try:
            result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=25)
        except subprocess.TimeoutExpired:
            raise MetricsError("CloudWatch timed out. Local metrics remain available; retry storage later.") from None
        except OSError:
            raise MetricsError("Could not start AWS CLI. Check its installation and try again.") from None
        if result.returncode:
            error = (result.stderr or "").lower()
            if turtle.AUTH_ERRORS.search(error):
                raise MetricsError("AWS sign-in expired. Sign in to this drive's AWS profile and refresh storage.", "authenticationRequired")
            if any(value in error for value in ("accessdenied", "unauthorized", "not authorized")):
                raise MetricsError("This AWS profile needs cloudwatch:GetMetricData permission for daily storage metrics.", "permissionDenied")
            raise MetricsError("Daily storage metrics are unavailable. Check this drive's AWS profile, region and network access.")
        try:
            if len(result.stdout) > MAX_RESPONSE_BYTES:
                raise ValueError()
            response = json.loads(result.stdout)
            if not isinstance(response, dict):
                raise ValueError()
            return response
        except (ValueError, TypeError):
            raise MetricsError("CloudWatch returned an invalid metrics response. Try again later.") from None


def price_number(value):
    try:
        return number(float(value)) if not isinstance(value, bool) else None
    except (TypeError, ValueError, OverflowError):
        return None


def parse_price_tiers(term):
    dimensions = term.get("priceDimensions", {})
    if not isinstance(dimensions, dict) or not dimensions or term.get("termAttributes"):
        return None
    tiers = []
    for row in dimensions.values():
        if not isinstance(row, dict) or row.get("unit") != "GB-Mo" or row.get("appliesTo"):
            return None
        begin = price_number(row.get("beginRange"))
        end = None if row.get("endRange") == "Inf" else price_number(row.get("endRange"))
        price = row.get("pricePerUnit", {})
        rate = price_number(price.get("USD")) if isinstance(price, dict) else None
        if begin is None or rate is None or (end is None and row.get("endRange") != "Inf") or (end is not None and end <= begin):
            return None
        tiers.append({"beginGiB": begin, "endGiB": end, "rateUSDPerGiBMonth": rate})
    tiers.sort(key=lambda row: row["beginGiB"])
    expected = 0
    for tier in tiers:
        if expected is None or tier["beginGiB"] != expected:
            return None
        expected = tier["endGiB"]
    return tiers if expected is None else None


def parse_price_list(response, region, now):
    if not isinstance(response, dict) or response.get("offerCode") != "AmazonS3":
        raise MetricsError("AWS returned an unexpected storage price list.")
    published = timestamp(response.get("publicationDate"))
    products = response.get("products", {})
    terms = response.get("terms", {}).get("OnDemand", {}) if isinstance(response.get("terms"), dict) else {}
    if published is None or published > now or not isinstance(products, dict) or not isinstance(terms, dict):
        raise MetricsError("AWS returned an invalid storage price list.")
    matches = {key: [] for key in PRICE_PRODUCTS}
    for sku, product in products.items():
        if not isinstance(product, dict):
            continue
        attributes = product.get("attributes", {})
        if (product.get("productFamily") != "Storage" or not isinstance(attributes, dict)
                or attributes.get("servicecode") != "AmazonS3" or attributes.get("regionCode") != region
                or attributes.get("locationType") != "AWS Region"):
            continue
        usage = attributes.get("usagetype", "")
        keys = [key for key, (volume, suffix) in PRICE_PRODUCTS.items()
                if attributes.get("volumeType") == volume and isinstance(usage, str)
                and (usage == suffix or usage.endswith("-" + suffix))]
        if not keys:
            continue
        offers = terms.get(sku, {})
        if not isinstance(offers, dict):
            continue
        active = [(timestamp(term.get("effectiveDate")), term) for term in offers.values() if isinstance(term, dict)
                  and timestamp(term.get("effectiveDate")) is not None and timestamp(term.get("effectiveDate")) <= now]
        if not active:
            continue
        latest = max(date for date, _ in active)
        latest_terms = [term for date, term in active if date == latest]
        if len(latest_terms) != 1:
            continue
        tiers = parse_price_tiers(latest_terms[0])
        if tiers:
            for key in keys:
                matches[key].append({"sku": sku, "tiers": tiers, "effectiveAt": latest})
    return {"region": region, "currency": "USD", "publishedAt": published,
            "version": str(response.get("version", "")),
            "products": {key: values[0] for key, values in matches.items() if len(values) == 1}}


class PublicPriceReader:
    """A public regional price document; no AWS identity or bucket data is sent."""
    def __init__(self, paths):
        self.paths = paths

    def fetch(self, url):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        deadline = time.monotonic() + 20
        try:
            request = urllib.request.Request(url, headers={"Accept": "application/json"})
            with opener.open(request, timeout=5) as response:
                chunks, size = [], 0
                while True:
                    if time.monotonic() > deadline:
                        raise TimeoutError()
                    chunk = response.read(min(65536, MAX_PRICE_BYTES + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_PRICE_BYTES:
                        raise ValueError()
            return json.loads(b"".join(chunks))
        except (OSError, ValueError, urllib.error.URLError):
            raise MetricsError("Public AWS storage prices are unavailable. Refresh later or enter your own blended rate.") from None

    def read(self, region, now):
        if not isinstance(region, str) or len(region) > 64 or not re.fullmatch(r"[a-z]{2}(?:-[a-z0-9]+)+-\d", region):
            raise MetricsError("A valid AWS region is required for automatic storage pricing.")
        url = PRICE_SOURCE.format(region=region)
        directory = self.paths.base / "metrics" / "pricing"
        cache = safe_read(directory / (region + ".json"), {}, max_bytes=MAX_PRICE_BYTES * 2)
        if isinstance(cache, dict) and cache.get("schemaVersion") == 1 and cache.get("sourceURL") == url:
            fetched = number(cache.get("fetchedAt"))
            if fetched is not None and 0 <= now - fetched < PRICE_CACHE_SECONDS:
                try:
                    catalog = parse_price_list(cache.get("document"), region, now)
                    return dict(catalog, fetchedAt=fetched, sourceURL=url)
                except MetricsError:
                    pass
        document = self.fetch(url)
        catalog = parse_price_list(document, region, now)
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            turtle.write_json(directory / (region + ".json"), {"schemaVersion": 1, "fetchedAt": now, "sourceURL": url, "document": document})
        except OSError:
            pass  # A failed local cache write must not discard verified prices.
        return dict(catalog, fetchedAt=now, sourceURL=url)


def tiered_monthly_cost(gib, tiers):
    return sum(max(0, (gib if tier["endGiB"] is None else min(gib, tier["endGiB"])) - tier["beginGiB"])
               * tier["rateUSDPerGiBMonth"] for tier in tiers)


def automatic_cost(storage_result, region, paths, now, reader=None):
    estimate = {"method": "awsPublicPricing", "status": "unavailable", "currency": "USD", "region": region,
                "monthlyUSD": None, "knownMonthlyUSD": None, "annualUSD": None,
                "pricingFetchedAt": None, "pricingPublishedAt": None, "sourceURL": None,
                "components": [], "unsupportedStorageTypes": [], "assumptions": PUBLIC_COST_ASSUMPTIONS,
                "message": "A complete storage measurement is needed for automatic cost estimates."}
    if storage_result.get("totalBytes") is None:
        return estimate
    try:
        catalog = (reader or PublicPriceReader(paths)).read(region, now)
    except MetricsError as error:
        estimate["message"] = str(error)
        return estimate
    estimate.update(pricingFetchedAt=catalog["fetchedAt"], pricingPublishedAt=catalog["publishedAt"],
                    sourceURL=catalog["sourceURL"], pricingVersion=catalog.get("version"))
    groups, unsupported = {}, []
    for component in storage_result.get("storageClasses", []):
        if component["bytes"] <= 0:
            continue
        key = PRICE_COMPONENTS.get(component["name"])
        if key is None or key not in catalog["products"]:
            unsupported.append(component["name"])
            continue
        groups.setdefault(key, []).append(component)
    known = 0
    for key, components in groups.items():
        product = catalog["products"][key]
        size = sum(component["bytes"] for component in components)
        cost = tiered_monthly_cost(size / GIB, product["tiers"])
        known += cost
        for component in components:
            estimate["components"].append({"storageType": component["name"], "bytes": component["bytes"],
                                            "monthlyUSD": cost * component["bytes"] / size, "sku": product["sku"],
                                            "effectiveRateUSDPerGiBMonth": cost / (size / GIB)})
    estimate.update(status="partial" if unsupported else "available", knownMonthlyUSD=known if groups or not unsupported else None,
                    monthlyUSD=None if unsupported else known, annualUSD=None if unsupported else known * 12,
                    unsupportedStorageTypes=unsupported,
                    message="Some storage components have no verified public price mapping; the known subtotal is not the full estimate." if unsupported else "Estimated storage cost uses public regional AWS prices.")
    return estimate


def estimate_cost(total_bytes, rate):
    if rate is None:
        return None
    if number(rate) is None or rate > 1000000:
        raise MetricsError("Enter a finite storage rate between 0 and 1000000 USD per GiB-month.", "invalidRate")
    monthly = total_bytes / GIB * rate if total_bytes is not None else None
    return {"method": "manualBlended", "status": "available" if monthly is not None else "unavailable", "currency": "USD",
            "rateUSDPerGiBMonth": rate, "monthlyUSD": monthly, "knownMonthlyUSD": monthly,
            "annualUSD": monthly * 12 if monthly is not None else None, "assumptions": COST_ASSUMPTIONS}


def parse_storage(response, now):
    """Align observations by UTC day, preserving absent/partial data as unknown."""
    query_ids = {f"s{index}": name for index, name in enumerate(STORAGE_TYPES)}
    query_ids["objects"] = "objects"
    series = {name: {} for name in query_ids.values()}
    present, incomplete = set(), bool(response.get("NextToken") or response.get("Messages"))
    results = response.get("MetricDataResults", [])
    if not isinstance(results, list):
        raise MetricsError("CloudWatch returned an invalid metrics response. Try again later.")
    for item in results:
        if not isinstance(item, dict) or item.get("Id") not in query_ids:
            continue
        identity = item["Id"]
        if identity in present:
            incomplete = True
        present.add(identity)
        if item.get("StatusCode") == "Forbidden":
            raise MetricsError("This AWS profile needs cloudwatch:GetMetricData permission for daily storage metrics.", "permissionDenied")
        if item.get("StatusCode") != "Complete":
            incomplete = True
        dates, values = item.get("Timestamps", []), item.get("Values", [])
        if not isinstance(dates, list) or not isinstance(values, list):
            incomplete = True
            continue
        if len(dates) != len(values):
            incomplete = True
        for date, value in zip(dates, values):
            observed, value = timestamp(date), number(value)
            if observed is None or value is None or observed > now or observed < now - (STORAGE_DAYS + 1) * 86400:
                incomplete = True
                continue
            day = math.floor(observed / 86400) * 86400
            previous = series[query_ids[identity]].get(day)
            # A duplicate day's value is not another storage component. Keep the
            # latest observation, never sum duplicate or repeated query data.
            if previous and previous[0] == observed and previous[1] != value:
                incomplete = True
            if previous is None or observed >= previous[0]:
                series[query_ids[identity]][day] = (observed, value)
    incomplete = incomplete or present != set(query_ids)
    observed_types = [name for name in STORAGE_TYPES if series[name]]
    days = sorted({day for values in series.values() for day in values})[-STORAGE_DAYS:]
    history = []
    for day in days:
        available = {name: series[name][day][1] for name in observed_types if day in series[name]}
        complete = bool(observed_types) and len(available) == len(observed_types) and not incomplete
        history.append({"timestamp": day, "totalBytes": sum(available.values()) if complete else None,
                        "reportedBytes": sum(available.values()) if available else None,
                        "objectCount": series["objects"].get(day, (None, None))[1],
                        "complete": complete, "storageClasses": [{"name": name, "bytes": value} for name, value in available.items()]})
    complete_points = [point for point in history if point["complete"]]
    latest = history[-1] if history else None
    selected = complete_points[-1] if complete_points else latest
    source_time = selected["timestamp"] if selected else None
    total = selected["totalBytes"] if selected else None
    if (not latest or not observed_types) and incomplete:
        status, message = "unavailable", "CloudWatch did not return a complete storage response. Retry storage later; missing data is not zero storage."
    elif not latest or not observed_types:
        status, message = "noData", "CloudWatch has no daily storage size observations for this bucket in the last 30 days. Missing data is not zero storage."
    elif incomplete or not latest["complete"]:
        status, message = "partial", "Some daily storage components are missing or incomplete. Any total shown uses the latest complete, aligned observation."
    elif now - source_time > STALE_SECONDS:
        status, message = "stale", "The latest aligned storage observation is more than three days old. Refresh later or check CloudWatch."
    else:
        status, message = "available", "Daily storage metrics retrieved from CloudWatch."
    return {"status": status, "message": message, "sourceTimestamp": source_time,
            "latestObservedTimestamp": latest["timestamp"] if latest else None,
            "isStale": source_time is not None and now - source_time > STALE_SECONDS,
            "totalBytes": total, "reportedBytes": latest["reportedBytes"] if latest else None,
            "objectCount": selected["objectCount"] if selected else None,
            "storageClasses": selected["storageClasses"] if selected else [], "history": history,
            "unreportedStorageTypes": [name for name in STORAGE_TYPES if not series[name]],
            "missingStorageTypes": [name for name in observed_types if latest and latest["timestamp"] not in series[name]]}


def sftp_failure(stderr):
    """Classify failures without exposing server output, paths or credentials."""
    error = (stderr or "").lower()
    connecting = any(value in error for value in ("dial tcp", "connect:", "network connection"))
    if ((connecting and any(value in error for value in ("operation not permitted", "permission denied")))
            or any(value in error for value in ("local network prohibited", "local network access denied", "local network permission", "necp policy denied"))):
        return MetricsError("macOS denied the network connection. In System Settings → Privacy & Security → Local Network, allow Mountain Turtle, then refresh.", "localNetworkPermissionRequired")
    if any(value in error for value in ("no route to host", "network is unreachable", "network unreachable", "host is down")):
        return MetricsError("The SFTP server cannot be reached. Check its address and your network connection. For a local server, also check Mountain Turtle under macOS Privacy & Security → Local Network.", "networkUnavailable")
    if "connection refused" in error:
        return MetricsError("The server refused the SFTP connection. Check the SSH port and confirm that the server's SFTP service is running.", "connectionRefused")
    if any(value in error for value in ("i/o timeout", "connection timed out", "context deadline exceeded", "timeout awaiting", "operation timed out")):
        return MetricsError("The SFTP connection timed out. Check the server and network connection, then refresh.", "timeout")
    if "permission denied" in error and any(value in error for value in ("open ", "failed to read", "unable to read")):
        return MetricsError("macOS could not read an SSH key, known-hosts file or configuration file. Check local file permissions and select accessible SSH files.", "localFilePermissionDenied")
    if any(value in error for value in ("knownhosts", "host key", "key mismatch")):
        return MetricsError("The SFTP host key could not be verified. Check this server's trusted known-hosts entry before reconnecting.", "hostKeyMismatch")
    if any(value in error for value in ("unable to authenticate", "authentication failed", "permission denied (", "no supported methods remain", "ssh agent", "private key")):
        return MetricsError("SFTP authentication failed. Check this drive's saved credentials or unlock its SSH agent.", "authenticationRequired")
    if "permission denied" in error or "operation not permitted" in error:
        return MetricsError("Access to the remote folder or its filesystem statistics was denied. Check the SSH user's permissions for the selected path.", "permissionDenied")
    if any(value in error for value in ("not supported", "not support", "doesn't support", "shell type", "not implemented", "unsupported")):
        return MetricsError("This SFTP server does not provide filesystem statistics without shell access. Transfer and cache metrics remain available.", "unsupported")
    return MetricsError("The SFTP server could not report filesystem capacity. Check connection access and refresh.")


class SFTPReader:
    """Read server statvfs data with verified hosts and remote shell access off."""
    def __init__(self, connection, paths):
        self.connection, self.paths = connection, paths

    def read(self):
        rclone = turtle.executable("rclone")
        if not rclone:
            raise MetricsError("Install rclone to read storage capacity from this SFTP server.")
        try:
            fields = turtle.validate_sftp(self.connection, self.paths)
            config, _ = turtle.connection_config(self.connection, self.paths)
            env = turtle.mount_environment(self.connection, self.paths, rclone)
        except ValueError as error:
            raise MetricsError(str(error), "authenticationRequired") from None
        # Query the SFTP filesystem directly; including the local icon overlay
        # in a union's about response could add this Mac's capacity to the total.
        remote = "sftp:" + fields["remotePath"]
        try:
            with tempfile.TemporaryDirectory(prefix="mountainturtle-capacity-") as temporary:
                config_path = Path(temporary) / "sftp.conf"
                config_path.write_text(config)
                config_path.chmod(0o600)
                command = [rclone, "about", remote, "--json", "--config", str(config_path),
                           "--sftp-shell-type", "none", "--sftp-disable-hashcheck",
                           "--retries", "1", "--low-level-retries", "1", "--contimeout", "5s", "--timeout", "10s"]
                result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL,
                                        capture_output=True, text=True, timeout=25)
        except subprocess.TimeoutExpired:
            raise MetricsError("The SFTP server took too long to report capacity. Check the connection and refresh.", "timeout") from None
        except PermissionError:
            raise MetricsError("macOS denied access to the local SFTP configuration or executable. Check local file permissions and refresh.", "localFilePermissionDenied") from None
        except OSError:
            raise MetricsError("Could not read SFTP filesystem capacity. Check the connection and refresh.") from None
        if result.returncode:
            raise sftp_failure(result.stderr)
        try:
            if len(result.stdout) > MAX_RESPONSE_BYTES:
                raise ValueError()
            response = json.loads(result.stdout)
            if not isinstance(response, dict):
                raise ValueError()
            return response
        except (TypeError, ValueError):
            raise MetricsError("The SFTP server returned invalid filesystem statistics. Refresh later.") from None


def sftp_storage(connection, paths, now, reader=None):
    result = {"ok": True, "connectionID": connection["id"], "provider": "sftp", "queriedAt": now,
              "source": "SFTP server filesystem statistics", "scope": "remoteFilesystem", "sourceTimestamp": None,
              "totalBytes": None, "usedBytes": None, "freeBytes": None, "objectCount": None,
              "storageClasses": [], "history": [], "estimate": None,
              "assumptions": ["Capacity is reported by the server's filesystem-statistics extension for the selected path. The filesystem may be shared with other folders or users; this is not the selected folder's size.",
                              "No recursive scan or remote shell command is used. Missing statistics remain unknown; used and free may not add to total because servers can reserve space.",
                              "SFTP does not publish a provider price through this protocol; no hosting-cost estimate is inferred.",
                              "Capacity history records successful refreshes only, retaining up to 720 observations from the last 24 hours."]}
    directory = metric_directory(paths, connection)
    saved = safe_read(directory / "capacity.json", [])
    history = [point for point in saved if isinstance(point, dict) and number(point.get("timestamp")) is not None
               and now - HISTORY_SECONDS <= point["timestamp"] < now] if isinstance(saved, list) else []
    try:
        raw = (reader or SFTPReader(connection, paths)).read()
        values = {"totalBytes": number(raw.get("total")), "usedBytes": number(raw.get("used")), "freeBytes": number(raw.get("free"))}
        if all(value is None for value in values.values()):
            raise MetricsError("This SFTP server did not report filesystem capacity. Transfer and cache metrics remain available.", "unsupported")
        result.update(values)
        result.update(sourceTimestamp=now, status="available" if all(value is not None for value in values.values()) else "partial",
                      message="Capacity reported by the SFTP server for the remote filesystem.")
        point = dict(values, timestamp=now)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (directory / "capacity.lock").open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            saved = safe_read(directory / "capacity.json", [])
            history = [item for item in saved if isinstance(item, dict) and number(item.get("timestamp")) is not None
                       and now - HISTORY_SECONDS <= item["timestamp"] < now] if isinstance(saved, list) else []
            history = sorted(history + [point], key=lambda item: item["timestamp"])[-HISTORY_LIMIT:]
            turtle.write_json(directory / "capacity.json", history)
    except MetricsError as error:
        result.update(status=error.status, message=str(error))
    result["history"] = history[-HISTORY_LIMIT:]
    return result


def storage(connection, paths, rate=None, reader=None, now=None, pricing_reader=None):
    now = time.time() if now is None else now
    estimate_cost(None, rate)  # Validate before performing any remote request.
    if provider(connection) == "sftp":
        return sftp_storage(connection, paths, now, reader)
    response = {"ok": True, "connectionID": connection["id"], "provider": provider(connection), "queriedAt": now,
                "source": "Amazon CloudWatch AWS/S3 daily storage metrics", "sourceTimestamp": None,
                "totalBytes": None, "objectCount": None, "storageClasses": [], "history": [],
                "estimate": None, "assumptions": STORAGE_ASSUMPTIONS}
    try:
        response.update(parse_storage((reader or CloudReader(connection, paths)).read(now), now))
        response["estimate"] = (estimate_cost(response["totalBytes"], rate) if rate is not None else
                                automatic_cost(response, connection["region"], paths, now, pricing_reader))
    except MetricsError as error:
        response.update(status=error.status, message=str(error))
    return response


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--resource-dir")
    commands = result.add_subparsers(dest="command", required=True)
    for operation in ("snapshot", "storage"):
        command = commands.add_parser(operation)
        command.add_argument("id")
        if operation == "storage":
            command.add_argument("--rate", type=float)
    return result


def main():
    os.umask(0o077)
    try:
        args = parser().parse_args()
        paths = turtle.Paths(resources=args.resource_dir)
        connection = turtle.find_connection(turtle.Store(paths).read(), args.id)
        result = snapshot(connection, paths) if args.command == "snapshot" else storage(connection, paths, rate=args.rate)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    except Exception as error:
        message = str(error) if isinstance(error, (MetricsError, ValueError)) else "Could not retrieve drive metrics. Try again."
        print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
