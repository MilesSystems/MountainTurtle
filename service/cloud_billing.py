#!/usr/bin/env python3
"""Read reported account S3 spend from AWS Cost Explorer without changing billing."""
import argparse
import calendar
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import math
import os
import re
import subprocess
import time

import drive_metrics as metrics
import turtle_service as turtle

CACHE_SECONDS = 6 * 3600
ASSUMPTIONS = [
    "Reported UnblendedCost from AWS Cost Explorer, filtered to the signed-in AWS account and Amazon S3 across all regions and buckets. It is not this bucket's bill.",
    "Includes the completed UTC days of this month returned by AWS. Cost Explorer can lag by 24 hours or longer; AWS marks current-month costs as estimated until finalized.",
    "Negative amounts such as reported credits are preserved. Charges recorded under other AWS services, support and account-wide adjustments are outside this S3 service filter; this is not the total AWS invoice.",
    "Public API storage prices are separate from reported spend. They do not replace negotiated billing rates or establish per-bucket cost attribution.",
    "Projected month-end S3 spend is a local run-rate estimate: complete reported month-to-date spend divided by completed UTC days, multiplied by this month's calendar days. It uses the same account, service and currency as actual spend, preserves credits, and assumes the daily average continues. It is not an AWS forecast or a final bill; incomplete reports have no projection.",
    "One Cost Explorer request is normally $0.01. Results are cached for six hours per profile and account; refreshing this view reuses that cache. This command never enables billing features or changes permissions.",
]


class BillingReader:
    def __init__(self, connection, paths):
        self.connection, self.paths = connection, paths

    def request(self, service, operation, payload):
        if (service, operation) not in (("sts", "get-caller-identity"), ("ce", "get-cost-and-usage")):
            raise metrics.MetricsError("Unsupported billing request.")
        aws = turtle.executable("aws")
        if not aws:
            raise metrics.MetricsError("Install AWS CLI v2 to read actual AWS spend.")
        command = [aws, service, operation, "--cli-input-json", json.dumps(payload),
                   "--profile=" + self.connection["profile"], "--region=us-east-1", "--output", "json",
                   "--no-paginate", "--cli-connect-timeout", "3", "--cli-read-timeout", "20"]
        env = turtle.environment(self.connection["profile"], self.paths)
        for key in list(env):
            if key.startswith("AWS_ENDPOINT_URL"):
                env.pop(key)
        env.update(AWS_MAX_ATTEMPTS="1", AWS_IGNORE_CONFIGURED_ENDPOINT_URLS="true")
        try:
            result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL, text=True,
                                    capture_output=True, timeout=30)
        except subprocess.TimeoutExpired:
            raise metrics.MetricsError("AWS billing took too long to respond. Refresh later.") from None
        except OSError:
            raise metrics.MetricsError("Could not start AWS CLI to read actual spend.") from None
        if result.returncode:
            error = (result.stderr or "").lower()
            if turtle.AUTH_ERRORS.search(error):
                raise metrics.MetricsError("AWS sign-in expired. Sign in to this drive's AWS profile and refresh actual spend.", "authenticationRequired")
            if any(value in error for value in ("accessdenied", "unauthorized", "not authorized")):
                if service == "sts":
                    raise metrics.MetricsError("AWS could not verify this profile's account identity. Check its sign-in and access before reading spend.", "permissionDenied")
                raise metrics.MetricsError("This profile cannot read AWS billing. Allow ce:GetCostAndUsage for its account; AWS Organizations may also restrict Cost Explorer access.", "permissionDenied")
            if "dataunavailable" in error or "not enabled" in error:
                raise metrics.MetricsError("AWS has no Cost Explorer data available for this account and period. Check Cost Explorer in AWS Billing; this app has not enabled or changed billing settings.", "noData")
            raise metrics.MetricsError("Actual AWS spend is unavailable. Check the profile's Cost Explorer access and network connection.")
        try:
            if len(result.stdout) > metrics.MAX_RESPONSE_BYTES:
                raise ValueError()
            data = json.loads(result.stdout)
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (ValueError, TypeError):
            raise metrics.MetricsError("AWS returned invalid billing data. Refresh later.") from None

    def account(self):
        account = self.request("sts", "get-caller-identity", {}).get("Account")
        if not isinstance(account, str) or not re.fullmatch(r"[0-9]{12}", account):
            raise metrics.MetricsError("AWS did not return an account identity. Spend cannot be safely scoped.")
        return account

    def costs(self, account, start, end):
        return self.request("ce", "get-cost-and-usage", {
            "TimePeriod": {"Start": start, "End": end}, "Granularity": "DAILY", "Metrics": ["UnblendedCost"],
            "Filter": {"And": [
                {"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Simple Storage Service"]}},
                {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [account]}},
            ]},
        })


def projection_details(first, observed_days=0, total=None):
    days = calendar.monthrange(first.year, first.month)[1]
    # Like Cost Explorer's periodEnd, this boundary is exclusive, in UTC.
    month_end = first.replace(day=1) + timedelta(days=days)
    result = {"projectedTotal": None, "projectionMethod": None, "projectionEstimated": False,
              "projectedPeriodEnd": month_end.date().isoformat(), "observedDays": observed_days,
              "daysInMonth": days}
    if total is not None and observed_days > 0:
        # Keep the existing Decimal sum through the extrapolation. Do not
        # round daily averages, drop credits, or change the report's currency.
        projected = float(total / Decimal(observed_days) * Decimal(days))
        if math.isfinite(projected):
            result.update(projectedTotal=projected, projectionMethod="completedDaysRunRate", projectionEstimated=True)
    return result


def parse_costs(response, start, end):
    if not isinstance(response, dict):
        raise metrics.MetricsError("AWS returned an invalid billing response.")
    rows = response.get("ResultsByTime", [])
    if not isinstance(rows, list):
        raise metrics.MetricsError("AWS returned an invalid daily billing report.")
    first, last = datetime.fromisoformat(start), datetime.fromisoformat(end)
    expected = {(first + timedelta(days=day)).date().isoformat() for day in range((last - first).days)}
    history, amounts, seen, currencies = [], [], set(), set()
    incomplete = bool(response.get("NextPageToken") or response.get("GroupDefinitions"))
    for row in rows:
        try:
            period = row["TimePeriod"]
            date = period["Start"]
            observed = datetime.fromisoformat(date).replace(tzinfo=timezone.utc)
            if date not in expected or period["End"] != (observed + timedelta(days=1)).date().isoformat() or date in seen:
                raise ValueError()
            seen.add(date)
            entry = row["Total"]["UnblendedCost"]
            if not isinstance(entry["Amount"], str) or not isinstance(entry["Unit"], str):
                raise ValueError()
            amount = Decimal(entry["Amount"])
            if not amount.is_finite() or not math.isfinite(float(amount)) or not re.fullmatch(r"[A-Z]{3}", entry["Unit"]):
                raise ValueError()
            if type(row.get("Estimated")) is not bool or row.get("Groups"):
                raise ValueError()
            currencies.add(entry["Unit"])
            amounts.append(amount)
            history.append({"date": date, "timestamp": observed.timestamp(), "amount": float(amount), "estimated": row["Estimated"]})
        except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
            incomplete = True
    if len(currencies) > 1:
        raise metrics.MetricsError("AWS returned multiple currencies. They cannot be combined into one spend total.")
    history.sort(key=lambda point: point["date"])
    incomplete = incomplete or len(history) != len(expected)
    if not history:
        return {"status": "noData", "message": "AWS has not returned daily S3 spend for this month. Missing billing data is not zero spend.",
                "total": None, "currency": None, "estimated": False, "history": [],
                **projection_details(first)}
    decimal_total = sum(amounts, Decimal(0))
    total = float(decimal_total)
    if not math.isfinite(total):
        raise metrics.MetricsError("AWS returned an invalid spend total.")
    projection = projection_details(first, len(history))
    if not incomplete and first.day == 1 and end <= projection["projectedPeriodEnd"]:
        projection = projection_details(first, len(history), decimal_total)
    return {"status": "partial" if incomplete else "available",
            "message": ("AWS returned an incomplete daily report. Available days are shown; the month-to-date total is withheld." if incomplete else
                        "Reported S3 spend for this AWS account, across all buckets and regions. AWS may still revise these costs."),
            "total": None if incomplete else total, "currency": next(iter(currencies)),
            "estimated": any(point["estimated"] for point in history), "history": history, **projection}


def snapshot(connection, paths, reader=None, now=None):
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now, timezone.utc).date()
    start, end = today.replace(day=1).isoformat(), today.isoformat()
    result = {"ok": True, "connectionID": connection["id"], "provider": turtle.connection_backend(connection),
              "source": "AWS Cost Explorer API", "scope": "accountS3AllRegions", "accountID": None,
              "monthStart": start, "periodEnd": end, "queriedAt": now, "cached": False, "total": None,
              "currency": None, "estimated": False, "history": [], "assumptions": ASSUMPTIONS,
              **projection_details(datetime.fromisoformat(start))}
    if result["provider"] != "s3":
        return dict(result, status="unsupported", message="AWS actual spend is available for S3 connections.")
    if start == end:
        return dict(result, status="noData", message="This month has no completed UTC day yet. Actual spend will appear after AWS publishes daily billing data.")
    reader = reader or BillingReader(connection, paths)
    try:
        account = reader.account()
        if not isinstance(account, str) or not re.fullmatch(r"[0-9]{12}", account):
            raise metrics.MetricsError("AWS did not return a valid billing account identity.")
        result["accountID"] = account
        identity = hashlib.sha256(json.dumps([connection["profile"], account, start, end]).encode()).hexdigest()
        directory = paths.base / "billing"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Share the cache across drives with the same profile/account. Locking
        # prevents simultaneous dashboards from issuing duplicate paid reads.
        with (directory / (identity + ".lock")).open("a") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = directory / (identity + ".json")
            cache = metrics.safe_read(path, {})
            queried = metrics.number(cache.get("queriedAt")) if isinstance(cache, dict) else None
            cached_result = None
            if queried is not None and 0 <= now - queried < CACHE_SECONDS and cache.get("schemaVersion") == 1:
                try:
                    cached_result = parse_costs(cache.get("response"), start, end)
                except metrics.MetricsError:
                    pass  # A damaged local cache is not an AWS failure.
            if cached_result is not None:
                result.update(cached_result, queriedAt=queried, cached=True)
            else:
                response = reader.costs(account, start, end)
                result.update(parse_costs(response, start, end))
                try:
                    turtle.write_json(path, {"schemaVersion": 1, "queriedAt": now, "response": response})
                except OSError:
                    pass
    except metrics.MetricsError as error:
        result.update(status=error.status, message=str(error))
    except OSError:
        result.update(status="unavailable", message="The private billing cache is unavailable. Check this Mac's storage permissions and refresh.")
    return result


def main():
    os.umask(0o077)
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--resource-dir")
        command = parser.add_subparsers(dest="command", required=True).add_parser("costs")
        command.add_argument("id")
        args = parser.parse_args()
        paths = turtle.Paths(resources=args.resource_dir)
        connection = turtle.find_connection(turtle.Store(paths).read(), args.id)
        print(json.dumps(snapshot(connection, paths), ensure_ascii=False, allow_nan=False))
    except Exception as error:
        message = str(error) if isinstance(error, (metrics.MetricsError, ValueError)) else "Could not retrieve actual AWS spend. Try again."
        print(json.dumps({"ok": False, "error": message}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
