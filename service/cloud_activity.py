#!/usr/bin/env python3
"""Read existing whole-bucket S3 request metrics across all clients, as JSON."""
import argparse
from datetime import datetime, timezone
import json
import os
import subprocess
import time

import drive_metrics as metrics
import turtle_service as turtle

PERIOD = 60
STALE_SECONDS = 15 * 60
METRICS = {
    "uploadedBytes": ("BytesUploaded", "Sum"),
    "downloadedBytes": ("BytesDownloaded", "Sum"),
    "putRequests": ("PutRequests", "Sum"),
    "getRequests": ("GetRequests", "Sum"),
    "allRequests": ("AllRequests", "Sum"),
    "clientErrors": ("4xxErrors", "Sum"),
    "serverErrors": ("5xxErrors", "Sum"),
    "firstByteLatencyMs": ("FirstByteLatency", "Average"),
    "totalRequestLatencyMs": ("TotalRequestLatency", "Average"),
}
SUM_FIELDS = [key for key, (_, statistic) in METRICS.items() if statistic == "Sum"]
ASSUMPTIONS = [
    "These are remote S3 bucket metrics across all clients and computers, independent of this Mac's mount session.",
    "AWS publishes one-minute request metrics on a best-effort basis. Reports can be delayed or missing and are not complete billing records; missing observations are not zero traffic.",
    "Window totals sum reported observations only. Rates are reported bytes divided by the 60-second measurement interval; latency is the average for that interval.",
    "Uploaded bytes are traffic, not net storage growth: overwrites, copies, multipart parts, retries, versions and deletes can change the relationship. They are not used to forecast the bucket's stored size or bill.",
    "Only an existing whole-bucket request-metrics configuration is read. This command never enables paid monitoring or combines overlapping filters.",
]


class ActivityReader:
    def __init__(self, connection, paths):
        self.connection, self.paths = connection, paths

    def request(self, service, operation, payload):
        if (service, operation) not in (("s3api", "list-bucket-metrics-configurations"), ("cloudwatch", "get-metric-data")):
            raise metrics.MetricsError("Unsupported bucket activity request.")
        aws = turtle.executable("aws")
        if not aws:
            raise metrics.MetricsError("Install AWS CLI v2 to read bucket activity metrics.")
        command = [aws, service, operation, "--cli-input-json", json.dumps(payload),
                   "--profile=" + self.connection["profile"], "--region=" + self.connection["region"],
                   "--output", "json", "--no-paginate", "--cli-connect-timeout", "3", "--cli-read-timeout", "15"]
        env = turtle.environment(self.connection["profile"], self.paths)
        for key in list(env):
            if key.startswith("AWS_ENDPOINT_URL"):
                env.pop(key)
        env["AWS_MAX_ATTEMPTS"] = "1"
        try:
            result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=25)
        except subprocess.TimeoutExpired:
            raise metrics.MetricsError("AWS bucket activity took too long to respond. Refresh later.") from None
        except OSError:
            raise metrics.MetricsError("Could not start AWS CLI to read bucket activity.") from None
        if result.returncode:
            error = (result.stderr or "").lower()
            if turtle.AUTH_ERRORS.search(error):
                raise metrics.MetricsError("AWS sign-in expired. Sign in to this drive's AWS profile and refresh bucket activity.", "authenticationRequired")
            if any(value in error for value in ("accessdenied", "unauthorized", "not authorized")):
                permission = "s3:GetMetricsConfiguration" if service == "s3api" else "cloudwatch:GetMetricData"
                raise metrics.MetricsError(f"This AWS profile needs {permission} permission to read bucket activity.", "permissionDenied")
            raise metrics.MetricsError("Bucket activity is unavailable. Check the AWS profile, bucket region and network access.")
        try:
            if len(result.stdout) > metrics.MAX_RESPONSE_BYTES:
                raise ValueError()
            response = json.loads(result.stdout)
            if not isinstance(response, dict):
                raise ValueError()
            return response
        except (TypeError, ValueError):
            raise metrics.MetricsError("AWS returned invalid bucket activity data. Refresh later.") from None

    def configurations(self):
        return self.request("s3api", "list-bucket-metrics-configurations", {"Bucket": self.connection["bucket"]})

    def activity(self, filter_id, start, end):
        queries = [{"Id": "m" + str(index), "ReturnData": True,
                    "MetricStat": {"Metric": {"Namespace": "AWS/S3", "MetricName": name,
                                              "Dimensions": [{"Name": "BucketName", "Value": self.connection["bucket"]},
                                                             {"Name": "FilterId", "Value": filter_id}]},
                                   "Period": PERIOD, "Stat": statistic}}
                   for index, (name, statistic) in enumerate(METRICS.values())]
        return self.request("cloudwatch", "get-metric-data", {"MetricDataQueries": queries,
                            "StartTime": datetime.fromtimestamp(start, timezone.utc).isoformat(),
                            "EndTime": datetime.fromtimestamp(end, timezone.utc).isoformat(),
                            "ScanBy": "TimestampAscending", "MaxDatapoints": 20000})


def whole_bucket_configuration(response):
    configurations = response.get("MetricsConfigurationList", [])
    if not isinstance(configurations, list):
        raise metrics.MetricsError("AWS returned an invalid metrics-configuration list.")
    whole = []
    for configuration in configurations:
        if not isinstance(configuration, dict):
            continue
        filter_value = configuration.get("Filter")
        identity = configuration.get("Id")
        if isinstance(identity, str) and identity and (filter_value is None or filter_value == {} or filter_value == {"Prefix": ""}):
            whole.append(identity)
    if whole:
        # Any one whole-bucket filter covers all clients. Summing several would
        # double-count the same uploads and downloads.
        return sorted(set(whole))[0]
    if response.get("IsTruncated") or response.get("NextContinuationToken"):
        raise metrics.MetricsError("More metrics configurations exist than this bounded request returned. Check S3 for a whole-bucket request-metrics configuration.", "configurationIncomplete")
    raise metrics.MetricsError("Whole-bucket request metrics are not enabled for this S3 bucket. Enable a whole-bucket CloudWatch request-metrics configuration in S3 to see uploads and downloads from every computer; additional AWS monitoring charges apply.", "notConfigured")


def parse_activity(response, start, end, now):
    keys = list(METRICS)
    ids = {"m" + str(index): key for index, key in enumerate(keys)}
    series = {key: {} for key in keys}
    seen = set()
    incomplete = bool(response.get("NextToken") or response.get("Messages"))
    results = response.get("MetricDataResults", [])
    if not isinstance(results, list):
        raise metrics.MetricsError("CloudWatch returned an invalid activity response.")
    for row in results:
        if not isinstance(row, dict) or row.get("Id") not in ids:
            continue
        identity = row["Id"]
        key = ids[identity]
        if identity in seen:
            incomplete = True
        seen.add(identity)
        if row.get("StatusCode") == "Forbidden":
            raise metrics.MetricsError("This AWS profile needs cloudwatch:GetMetricData permission to read bucket activity.", "permissionDenied")
        if row.get("StatusCode") != "Complete":
            incomplete = True
        dates, values = row.get("Timestamps", []), row.get("Values", [])
        if not isinstance(dates, list) or not isinstance(values, list):
            incomplete = True
            continue
        if len(dates) != len(values):
            incomplete = True
        for observed, value in zip(dates, values):
            observed, value = metrics.timestamp(observed), metrics.number(value)
            if observed is None or value is None:
                incomplete = True
                continue
            if not start <= observed < end:
                continue
            minute = int(observed // PERIOD) * PERIOD
            if minute in series[key] and series[key][minute] != value:
                incomplete = True
            series[key][minute] = value
    incomplete = incomplete or seen != set(ids)
    dates = sorted({date for values in series.values() for date in values})
    history = []
    for date in dates:
        point = {"timestamp": date, **{key: series[key].get(date) for key in keys}}
        point["uploadBytesPerSecond"] = point["uploadedBytes"] / PERIOD if point["uploadedBytes"] is not None else None
        point["downloadBytesPerSecond"] = point["downloadedBytes"] / PERIOD if point["downloadedBytes"] is not None else None
        history.append(point)
    latest = history[-1] if history else None
    lag = max(0, now - latest["timestamp"] - PERIOD) if latest else None
    if not latest:
        status = "unavailable" if incomplete else "noData"
        message = ("CloudWatch returned an incomplete response. Refresh later." if incomplete else
                   "Request metrics are configured, but AWS has not returned observations in this window. New configurations can take time to publish; missing data is not zero traffic.")
    elif incomplete:
        status, message = "partial", "Some CloudWatch activity results are incomplete. Graphs and totals show reported observations only."
    elif lag > STALE_SECONDS:
        status, message = "stale", "The most recent reported bucket activity is more than 15 minutes old. Missing recent observations are not zero traffic."
    else:
        status, message = "available", "Remote bucket activity includes uploads and downloads from every computer."
    return {"status": status, "message": message, "sourceTimestamp": latest["timestamp"] if latest else None,
            "lagSeconds": lag, "current": latest if latest and lag <= STALE_SECONDS else None,
            "summary": {key: sum(series[key].values()) if series[key] else None for key in SUM_FIELDS},
            "coverage": {"expectedMinutes": int((end - start) / PERIOD), "observedMinutes": len(history),
                         "uploadedMinutes": len(series["uploadedBytes"]), "downloadedMinutes": len(series["downloadedBytes"])},
            "history": history}


def snapshot(connection, paths, hours=1, reader=None, now=None):
    if hours not in (1, 6, 24):
        raise metrics.MetricsError("Choose a bucket activity window of 1, 6, or 24 hours.")
    now = time.time() if now is None else now
    end = int(now // PERIOD) * PERIOD
    start = end - hours * 3600
    result = {"ok": True, "connectionID": connection["id"], "provider": turtle.connection_backend(connection),
              "source": "Amazon CloudWatch AWS/S3 request metrics", "scope": "bucketAllClients",
              "queriedAt": now, "windowHours": hours, "periodSeconds": PERIOD, "windowStart": start, "windowEnd": end,
              "filterID": None, "sourceTimestamp": None, "lagSeconds": None, "current": None,
              "summary": {key: None for key in SUM_FIELDS}, "history": [], "coverage": {"expectedMinutes": hours * 60, "observedMinutes": 0,
              "uploadedMinutes": 0, "downloadedMinutes": 0}, "assumptions": ASSUMPTIONS}
    if result["provider"] != "s3":
        return dict(result, status="unsupported", message="Bucket-wide CloudWatch activity is available for S3 drives.")
    reader = reader or ActivityReader(connection, paths)
    try:
        identity = whole_bucket_configuration(reader.configurations())
        result["filterID"] = identity
        result.update(parse_activity(reader.activity(identity, start, end), start, end, now))
    except metrics.MetricsError as error:
        result.update(status=error.status, message=str(error))
    return result


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--resource-dir")
    commands = result.add_subparsers(dest="command", required=True)
    command = commands.add_parser("snapshot")
    command.add_argument("id")
    command.add_argument("--hours", type=int, choices=(1, 6, 24), default=1)
    return result


def main():
    os.umask(0o077)
    try:
        args = parser().parse_args()
        paths = turtle.Paths(resources=args.resource_dir)
        connection = turtle.find_connection(turtle.Store(paths).read(), args.id)
        print(json.dumps(snapshot(connection, paths, args.hours), ensure_ascii=False, allow_nan=False))
    except Exception as error:
        message = str(error) if isinstance(error, (metrics.MetricsError, ValueError)) else "Could not retrieve bucket activity. Try again."
        print(json.dumps({"ok": False, "error": message}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
