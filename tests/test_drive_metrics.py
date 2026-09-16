import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))
import drive_metrics as metrics
import turtle_service as turtle

DAY = 86400
NOW = 20000 * DAY + 3600


def cloud_result(values=None, object_values=None, status=None):
    values = values or {}
    result = {"MetricDataResults": []}
    for index, name in enumerate(metrics.STORAGE_TYPES):
        observations = values.get(name, [])
        result["MetricDataResults"].append({"Id": f"s{index}", "StatusCode": (status or {}).get(name, "Complete"),
                                            "Timestamps": [day for day, _ in observations],
                                            "Values": [value for _, value in observations]})
    result["MetricDataResults"].append({"Id": "objects", "StatusCode": "Complete",
                                        "Timestamps": [day for day, _ in object_values or []],
                                        "Values": [value for _, value in object_values or []]})
    return result


def price_document():
    return {"offerCode": "AmazonS3", "publicationDate": "2024-10-01T00:00:00Z", "version": "test-version",
            "products": {"STANDARD": {"productFamily": "Storage", "attributes": {
                "servicecode": "AmazonS3", "regionCode": "us-east-1", "locationType": "AWS Region",
                "volumeType": "Standard", "usagetype": "TimedStorage-ByteHrs"}}},
            "terms": {"OnDemand": {"STANDARD": {"STANDARD.offer": {
                "effectiveDate": "2024-10-01T00:00:00Z", "termAttributes": {}, "priceDimensions": {
                    "first": {"unit": "GB-Mo", "beginRange": "0", "endRange": "51200", "pricePerUnit": {"USD": "0.023"}, "appliesTo": []},
                    "second": {"unit": "GB-Mo", "beginRange": "51200", "endRange": "512000", "pricePerUnit": {"USD": "0.022"}, "appliesTo": []},
                    "last": {"unit": "GB-Mo", "beginRange": "512000", "endRange": "Inf", "pricePerUnit": {"USD": "0.021"}, "appliesTo": []}}}}}}}


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = turtle.Paths(home=self.temp.name)
        self.connection = {"id": "test-connection", "name": "Photos", "bucket": "sample-bucket",
                           "profile": "development", "region": "us-east-1", "readOnly": True}
        self.live = {"pid": 123, "sessionID": "session-one", "rcPort": 5555, "rcUser": "local", "rcPass": "private-token"}

    def snapshot(self, core=None, vfs=None, now=NOW, live=None):
        reader = Mock()
        reader.read.side_effect = [core or {"bytes": 0, "speed": 0, "transfers": 0}, vfs or {"diskCache": {"bytesUsed": 0}}]
        return metrics.snapshot(self.connection, self.paths, live or self.live, reader, now)

    def storage(self, response, rate=None):
        prices = Mock(read=Mock(side_effect=metrics.MetricsError("No public prices available in this test")))
        return metrics.storage(self.connection, self.paths, rate=rate, reader=Mock(read=Mock(return_value=response)), now=NOW, pricing_reader=prices)

    def test_local_speed_uses_observed_delta_instead_of_session_average(self):
        first = self.snapshot(core={"bytes": 1000, "speed": 14, "elapsedTime": 50})
        second = self.snapshot(core={"bytes": 1500, "speed": 16, "elapsedTime": 55}, now=NOW + 5)
        self.assertIsNone(first["current"]["speedBytesPerSecond"])
        self.assertEqual(second["current"]["speedBytesPerSecond"], 100)
        self.assertEqual(second["current"]["averageSpeedBytesPerSecond"], 16)
        self.assertEqual(second["current"]["intervalBytes"], 500)
        self.assertFalse(second["capabilities"]["directionalTransferBytes"])
        self.assertNotIn("private-token", json.dumps(second))

    def test_counter_reset_and_new_mount_never_produce_false_speed(self):
        self.snapshot(core={"bytes": 1000})
        reset = self.snapshot(core={"bytes": 20}, now=NOW + 5)
        self.assertTrue(reset["current"]["counterReset"])
        self.assertIsNone(reset["current"]["speedBytesPerSecond"])
        self.assertIsNone(reset["current"]["intervalBytes"])
        next_point = self.snapshot(core={"bytes": 40}, now=NOW + 10)
        self.assertEqual(next_point["current"]["speedBytesPerSecond"], 4)
        different = self.snapshot(core={"bytes": 90000}, now=NOW + 15, live=dict(self.live, sessionID="session-two"))
        self.assertTrue(different["current"]["counterReset"])
        self.assertIsNone(different["current"]["intervalBytes"])

    def test_long_gap_does_not_claim_live_speed(self):
        self.snapshot(core={"bytes": 1000})
        result = self.snapshot(core={"bytes": 9000}, now=NOW + 180)
        self.assertIsNone(result["current"]["speedBytesPerSecond"])
        self.assertEqual(result["current"]["intervalBytes"], 8000)

    def test_history_is_bounded_and_discards_old_future_and_malformed_points(self):
        directory = metrics.metric_directory(self.paths, self.connection)
        directory.mkdir(parents=True)
        points = [{"timestamp": NOW - index - 1} for index in range(metrics.HISTORY_LIMIT + 10)]
        points += [{"timestamp": NOW - metrics.HISTORY_SECONDS - 1}, {"timestamp": NOW + 1}, {"timestamp": "bad"}, None]
        turtle.write_json(directory / "history.json", points)
        result = self.snapshot()
        self.assertEqual(len(result["history"]), metrics.HISTORY_LIMIT)
        self.assertEqual(result["history"][-1]["timestamp"], NOW)
        self.assertTrue(all(NOW - metrics.HISTORY_SECONDS <= item["timestamp"] <= NOW for item in result["history"]))
        self.assertEqual((directory / "history.json").stat().st_mode & 0o777, 0o600)

    def test_invalid_local_history_is_replaced(self):
        directory = metrics.metric_directory(self.paths, self.connection)
        directory.mkdir(parents=True)
        (directory / "history.json").write_text("broken JSON")
        self.assertEqual(len(self.snapshot()["history"]), 1)

    def test_telemetry_preserves_partial_cache_and_drops_file_names(self):
        reader = Mock()
        reader.read.side_effect = [metrics.MetricsError("Not available"),
                                   {"diskCache": {"bytesUsed": 100, "files": 3, "uploadsQueued": 2,
                                                  "uploadsInProgress": 1, "outOfSpace": True, "path": "/private/path"},
                                    "metadataCache": {"files": 5, "dirs": 2}, "inUse": 4}]
        result = metrics.snapshot(self.connection, self.paths, self.live, reader, NOW)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["current"]["cacheBytes"], 100)
        self.assertEqual(result["current"]["uploadsQueued"], 2)
        self.assertTrue(result["current"]["cacheOutOfSpace"])
        self.assertIsNone(result["current"]["transferredBytes"])
        self.assertNotIn("/private/path", json.dumps(result))

    def test_disconnected_does_not_invent_zero_counters(self):
        result = metrics.snapshot(self.connection, self.paths, {}, now=NOW)
        self.assertEqual(result["status"], "disconnected")
        self.assertIsNone(result["current"]["cacheBytes"])
        self.assertIsNone(result["current"]["activeTransfers"])
        self.assertIsNone(result["current"]["transferredBytes"])

    def test_sftp_snapshot_uses_saved_backend_for_capabilities(self):
        self.connection["backend"] = "sftp"
        result = self.snapshot(core={"bytes": 100})
        self.assertEqual(result["provider"], "sftp")
        self.assertFalse(result["capabilities"]["cloudStorage"])
        self.assertEqual(result["current"]["transferredBytes"], 100)

    def test_local_transport_is_authenticated_read_only_loopback_and_bounded(self):
        response = Mock()
        response.read.return_value = b'{"bytes": 123}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(metrics.urllib.request, "build_opener", return_value=opener) as build:
            self.assertEqual(metrics.LocalReader(self.live).read("core/stats"), {"bytes": 123})
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, "http://127.0.0.1:5555/core/stats")
            self.assertEqual(request.get_method(), "POST")
            self.assertTrue(request.headers["Authorization"].startswith("Basic "))
            self.assertEqual(opener.open.call_args.kwargs["timeout"], 2)
            self.assertEqual(build.call_args.args[0].proxies, {})
            self.assertIsInstance(build.call_args.args[1], metrics.NoRedirect)
        with self.assertRaises(metrics.MetricsError):
            metrics.LocalReader(self.live).read("core/quit")
        with self.assertRaises(metrics.MetricsError):
            metrics.LocalReader(dict(self.live, rcPort="5555")).read("core/stats")

    def test_empty_cloudwatch_data_is_unknown_not_zero(self):
        result = self.storage(cloud_result())
        self.assertEqual(result["status"], "noData")
        self.assertIsNone(result["totalBytes"])
        self.assertIsNone(result["objectCount"])
        self.assertEqual(result["history"], [])
        self.assertIsNone(result["estimate"]["monthlyUSD"])

    def test_explicit_zero_measurement_is_valid(self):
        day = NOW // DAY * DAY - DAY
        result = self.storage(cloud_result({"StandardStorage": [(day, 0)]}, [(day, 0)]), rate=0.023)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["totalBytes"], 0)
        self.assertEqual(result["objectCount"], 0)
        self.assertEqual(result["estimate"]["monthlyUSD"], 0)

    def test_all_storage_components_are_summed_only_once(self):
        day = NOW // DAY * DAY - DAY
        result = self.storage(cloud_result({"StandardStorage": [(day, 100), (day, 100)],
                                            "GlacierStorage": [(day, 500)], "GlacierObjectOverhead": [(day, 32)],
                                            "GlacierS3ObjectOverhead": [(day, 8)]}, [(day, 9)]))
        self.assertEqual(result["totalBytes"], 640)
        self.assertEqual(result["objectCount"], 9)
        self.assertEqual(len(result["storageClasses"]), 4)

    def test_conflicting_duplicate_values_are_partial_instead_of_arbitrarily_complete(self):
        day = NOW // DAY * DAY - DAY
        result = self.storage(cloud_result({"StandardStorage": [(day, 100), (day, 500)]}))
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["totalBytes"])

    def test_class_observations_never_mix_different_days(self):
        day = NOW // DAY * DAY - DAY
        result = self.storage(cloud_result({"StandardStorage": [(day - DAY, 100), (day, 200)],
                                            "GlacierStorage": [(day - DAY, 500)]}, [(day - DAY, 9), (day, 10)]))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["sourceTimestamp"], day - DAY)
        self.assertEqual(result["latestObservedTimestamp"], day)
        self.assertEqual(result["totalBytes"], 600)
        self.assertEqual(result["reportedBytes"], 200)
        self.assertEqual(result["objectCount"], 9)
        self.assertIsNone(result["history"][-1]["totalBytes"])
        self.assertEqual(result["missingStorageTypes"], ["GlacierStorage"])

    def test_without_any_aligned_day_partial_total_is_unknown(self):
        day = NOW // DAY * DAY - DAY
        result = self.storage(cloud_result({"StandardStorage": [(day, 200)], "GlacierStorage": [(day - DAY, 500)]}))
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["totalBytes"])
        self.assertEqual(result["reportedBytes"], 200)

    def test_incomplete_api_results_and_pagination_never_claim_complete_total(self):
        day = NOW // DAY * DAY - DAY
        for modification in ("status", "token", "omitted", "messages"):
            with self.subTest(modification=modification):
                response = cloud_result({"StandardStorage": [(day, 200)]})
                if modification == "status":
                    response["MetricDataResults"][0]["StatusCode"] = "PartialData"
                elif modification == "token":
                    response["NextToken"] = "more"
                elif modification == "omitted":
                    response["MetricDataResults"].pop(2)
                else:
                    response["Messages"] = [{"Code": "InternalError"}]
                result = self.storage(response)
                self.assertEqual(result["status"], "partial")
                self.assertIsNone(result["totalBytes"])

    def test_stale_source_is_flagged_with_actual_reporting_date(self):
        day = NOW // DAY * DAY - 5 * DAY
        result = self.storage(cloud_result({"StandardStorage": [(day, 1024)]}))
        self.assertEqual(result["status"], "stale")
        self.assertTrue(result["isStale"])
        self.assertEqual(result["sourceTimestamp"], day)
        self.assertIsNone(result["objectCount"])

    def test_manual_monthly_cost_is_labeled_estimate(self):
        day = NOW // DAY * DAY - DAY
        response = cloud_result({"StandardStorage": [(day, 10 * metrics.GIB)]})
        self.assertEqual(self.storage(response)["estimate"]["method"], "awsPublicPricing")
        self.assertIsNone(self.storage(response)["estimate"]["monthlyUSD"])
        estimate = self.storage(response, rate=0.023)["estimate"]
        self.assertAlmostEqual(estimate["monthlyUSD"], 0.23)
        self.assertAlmostEqual(estimate["annualUSD"], 2.76)
        self.assertIn("not an AWS bill", " ".join(estimate["assumptions"]))
        for invalid in (float("nan"), float("inf"), -1, 1000001):
            reader = Mock()
            with self.assertRaises(metrics.MetricsError):
                metrics.storage(self.connection, self.paths, rate=invalid, reader=reader, now=NOW)
            reader.read.assert_not_called()

    def test_sftp_asks_server_for_capacity_and_never_invents_price(self):
        reader = Mock(read=Mock(return_value={"total": 1000, "used": 400, "free": 550}))
        result = metrics.storage(dict(self.connection, backend="sftp"), self.paths, rate=0.023, reader=reader, now=NOW)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["scope"], "remoteFilesystem")
        self.assertIsNone(result["estimate"])
        self.assertEqual(result["totalBytes"], 1000)
        self.assertEqual(result["usedBytes"], 400)
        self.assertEqual(result["freeBytes"], 550)
        reader.read.assert_called_once_with()

    def test_sftp_unavailable_statistics_stay_unknown_with_history_retained(self):
        connection = dict(self.connection, backend="sftp")
        metrics.storage(connection, self.paths, reader=Mock(read=Mock(return_value={"total": 1000, "used": 400})), now=NOW)
        result = metrics.storage(connection, self.paths, reader=Mock(read=Mock(return_value={})), now=NOW + 5)
        self.assertEqual(result["status"], "unsupported")
        self.assertIsNone(result["totalBytes"])
        self.assertIsNone(result["usedBytes"])
        self.assertEqual(len(result["history"]), 1)
        self.assertEqual(result["history"][0]["usedBytes"], 400)

    def test_sftp_partial_and_zero_are_distinct_from_unsupported(self):
        result = metrics.storage(dict(self.connection, backend="sftp"), self.paths, reader=Mock(read=Mock(return_value={"total": 0, "used": 0})), now=NOW)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["usedBytes"], 0)
        self.assertIsNone(result["freeBytes"])

    def test_sftp_capacity_transport_is_private_bounded_and_bypasses_icon_union(self):
        fields = {"remotePath": '/Family "photos":ro'}
        seen = {}
        def run(command, **kwargs):
            path = Path(command[command.index("--config") + 1])
            seen.update(command=command, config=path.read_text(), mode=path.stat().st_mode & 0o777, path=path, kwargs=kwargs)
            return Mock(returncode=0, stdout='{"total":1000,"used":400,"free":550}')
        with patch.object(turtle, "executable", return_value="/rclone"), patch.object(turtle, "validate_sftp", return_value=fields), \
             patch.object(turtle, "connection_config", return_value=("private-config", "volume:")), \
             patch.object(turtle, "mount_environment", return_value={"RCLONE_CONFIG_SFTP_PASS": "obscured-private"}), \
             patch.object(metrics.subprocess, "run", side_effect=run):
            result = metrics.SFTPReader(dict(self.connection, backend="sftp"), self.paths).read()
        self.assertEqual(result["used"], 400)
        self.assertEqual(seen["command"][:3], ["/rclone", "about", 'sftp:/Family "photos":ro'])
        self.assertIn("--sftp-disable-hashcheck", seen["command"])
        self.assertEqual(seen["command"][seen["command"].index("--sftp-shell-type") + 1], "none")
        self.assertEqual(seen["kwargs"]["timeout"], 25)
        self.assertEqual(seen["mode"], 0o600)
        self.assertFalse(seen["path"].exists())
        self.assertNotIn("obscured-private", str(seen["command"]))

    def test_sftp_errors_are_sanitized_and_host_verification_never_relaxes(self):
        cases = [("knownhosts: key mismatch SECRET", "hostKeyMismatch"),
                 ("ssh: unable to authenticate SECRET", "authenticationRequired"),
                 ("About unsupported; shell type none SECRET", "unsupported"),
                 ("network failed SECRET", "unavailable")]
        for message, status in cases:
            with patch.object(turtle, "executable", return_value="/rclone"), patch.object(turtle, "validate_sftp", return_value={"remotePath": ""}), \
                 patch.object(turtle, "connection_config", return_value=("config", "sftp:")), patch.object(turtle, "mount_environment", return_value={}), \
                 patch.object(metrics.subprocess, "run", return_value=Mock(returncode=1, stderr=message)):
                with self.assertRaises(metrics.MetricsError) as raised:
                    metrics.SFTPReader(dict(self.connection, backend="sftp"), self.paths).read()
            self.assertEqual(raised.exception.status, status)
            self.assertNotIn("SECRET", str(raised.exception))

    def test_cloud_permission_error_is_actionable_without_raw_error_output(self):
        private = "AccessDenied: private-bucket private-token account-id"
        with patch.object(turtle, "executable", return_value="/fake/aws"), patch.object(metrics.subprocess, "run", return_value=Mock(returncode=1, stderr=private)):
            result = metrics.storage(self.connection, self.paths, now=NOW)
        self.assertEqual(result["status"], "permissionDenied")
        self.assertIn("cloudwatch:GetMetricData", result["message"])
        self.assertNotIn("private-token", json.dumps(result))
        self.assertTrue(result["ok"])

    def test_metric_level_errors_are_not_misreported_as_no_data(self):
        response = cloud_result(status={"StandardStorage": "Forbidden"})
        result = self.storage(response)
        self.assertEqual(result["status"], "permissionDenied")
        self.assertIsNone(result["totalBytes"])
        response = cloud_result(status={"StandardStorage": "InternalError"})
        self.assertEqual(self.storage(response)["status"], "unavailable")

    def test_cloud_request_is_one_bounded_read_with_valid_dimensions_and_environment(self):
        fake_env = {"AWS_ENDPOINT_URL_MONITORING": "https://wrong.example", "AWS_ENDPOINT_URL": "https://wrong.example"}
        with patch.object(turtle, "executable", return_value="/fake/aws"), patch.object(turtle, "environment", return_value=fake_env), patch.object(metrics.subprocess, "run", return_value=Mock(returncode=0, stdout=json.dumps(cloud_result()))) as run:
            metrics.CloudReader(self.connection, self.paths).read(NOW)
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["/fake/aws", "cloudwatch", "get-metric-data"])
        self.assertIn("--no-paginate", command)
        self.assertEqual(run.call_args.kwargs["timeout"], 25)
        self.assertEqual(run.call_args.kwargs["env"], {"AWS_MAX_ATTEMPTS": "1"})
        payload = json.loads(command[command.index("--cli-input-json") + 1])
        self.assertEqual(payload["MaxDatapoints"], 5000)
        queries = payload["MetricDataQueries"]
        self.assertEqual(len(queries), len(metrics.STORAGE_TYPES) + 1)
        dimensions = queries[-1]["MetricStat"]["Metric"]["Dimensions"]
        self.assertEqual(dimensions, [{"Name": "BucketName", "Value": "sample-bucket"}, {"Name": "StorageType", "Value": "AllStorageTypes"}])
        self.assertEqual(queries[-1]["MetricStat"]["Metric"]["MetricName"], "NumberOfObjects")
        for query in queries[:-1]:
            self.assertEqual(query["MetricStat"]["Metric"]["MetricName"], "BucketSizeBytes")
            self.assertEqual(query["MetricStat"]["Stat"], "Average")
            self.assertEqual(query["MetricStat"]["Period"], DAY)
            self.assertNotEqual(query["MetricStat"]["Metric"]["Dimensions"][-1]["Value"], "AllStorageTypes")

    def test_cloud_timeout_does_not_affect_local_metric_collection(self):
        with patch.object(turtle, "executable", return_value="/fake/aws"), patch.object(metrics.subprocess, "run", side_effect=subprocess.TimeoutExpired("aws", 25)):
            result = metrics.storage(self.connection, self.paths, now=NOW)
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("timed out", result["message"])
        local = self.snapshot(core={"bytes": 1000})
        self.assertEqual(local["current"]["transferredBytes"], 1000)
        self.assertEqual(local["status"], "available")

    def test_nonfinite_and_negative_cloud_measurements_do_not_leak_to_json(self):
        day = NOW // DAY * DAY - DAY
        result = self.storage(cloud_result({"StandardStorage": [(day, float("nan")), (day - DAY, -1)]}))
        self.assertIsNone(result["totalBytes"])
        json.dumps(result, allow_nan=False)


class PublicPricingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = turtle.Paths(home=self.temp.name)
        self.document = price_document()
        self.connection = {"id": "pricing-test", "bucket": "private-bucket", "profile": "private-profile", "region": "us-east-1"}

    def catalog(self):
        return dict(metrics.parse_price_list(self.document, "us-east-1", NOW), fetchedAt=NOW,
                    sourceURL=metrics.PRICE_SOURCE.format(region="us-east-1"))

    def cost(self, components):
        return metrics.automatic_cost({"totalBytes": sum(components.values()),
                                       "storageClasses": [{"name": k, "bytes": v} for k, v in components.items()]},
                                      "us-east-1", self.paths, NOW, Mock(read=Mock(return_value=self.catalog())))

    def test_standard_public_price_is_automatic_without_manual_rate(self):
        day = NOW // DAY * DAY - DAY
        result = metrics.storage(self.connection, self.paths, reader=Mock(read=Mock(return_value=cloud_result({"StandardStorage": [(day, 10 * metrics.GIB)]}))),
                                 pricing_reader=Mock(read=Mock(return_value=self.catalog())), now=NOW)
        estimate = result["estimate"]
        self.assertEqual(estimate["method"], "awsPublicPricing")
        self.assertEqual(estimate["status"], "available")
        self.assertAlmostEqual(estimate["monthlyUSD"], .23)
        self.assertAlmostEqual(estimate["annualUSD"], 2.76)
        self.assertEqual(estimate["currency"], "USD")
        self.assertEqual(estimate["pricingFetchedAt"], NOW)
        self.assertEqual(estimate["region"], "us-east-1")
        self.assertIn("account-wide", " ".join(estimate["assumptions"]).lower())
        self.assertNotIn("private-bucket", estimate["sourceURL"])
        self.assertNotIn("private-profile", json.dumps(estimate))

    def test_volume_tiers_apply_incrementally_at_boundaries(self):
        estimate = self.cost({"StandardStorage": 51300 * metrics.GIB})
        self.assertAlmostEqual(estimate["monthlyUSD"], 51200 * .023 + 100 * .022)
        estimate = self.cost({"StandardStorage": 512100 * metrics.GIB})
        self.assertAlmostEqual(estimate["monthlyUSD"], 51200 * .023 + 460800 * .022 + 100 * .021)

    def test_components_charged_at_standard_price_share_tier_allowance(self):
        estimate = self.cost({"StandardStorage": 50000 * metrics.GIB, "GlacierS3ObjectOverhead": 2000 * metrics.GIB})
        self.assertAlmostEqual(estimate["monthlyUSD"], 51200 * .023 + 800 * .022)
        self.assertAlmostEqual(sum(row["monthlyUSD"] for row in estimate["components"]), estimate["monthlyUSD"])

    def test_unsupported_components_keep_subtotal_separate_from_full_estimate(self):
        estimate = self.cost({"StandardStorage": 10 * metrics.GIB, "DeepArchiveStorage": 100 * metrics.GIB})
        self.assertEqual(estimate["status"], "partial")
        self.assertEqual(estimate["unsupportedStorageTypes"], ["DeepArchiveStorage"])
        self.assertAlmostEqual(estimate["knownMonthlyUSD"], .23)
        self.assertIsNone(estimate["monthlyUSD"])
        self.assertIsNone(estimate["annualUSD"])
        only_unknown = self.cost({"DeepArchiveStorage": 100 * metrics.GIB})
        self.assertIsNone(only_unknown["knownMonthlyUSD"])

    def test_zero_bytes_do_not_create_a_false_unsupported_cost(self):
        estimate = self.cost({"StandardStorage": 0, "DeepArchiveStorage": 0})
        self.assertEqual(estimate["status"], "available")
        self.assertEqual(estimate["monthlyUSD"], 0)

    def test_manual_override_never_needs_public_price_download(self):
        day = NOW // DAY * DAY - DAY
        prices = Mock()
        result = metrics.storage(self.connection, self.paths, rate=.5, reader=Mock(read=Mock(return_value=cloud_result({"StandardStorage": [(day, 2 * metrics.GIB)]}))),
                                 pricing_reader=prices, now=NOW)
        self.assertEqual(result["estimate"]["method"], "manualBlended")
        self.assertEqual(result["estimate"]["monthlyUSD"], 1)
        prices.read.assert_not_called()

    def test_unaligned_storage_does_not_trigger_pricing_or_invent_estimate(self):
        prices = Mock()
        estimate = metrics.automatic_cost({"totalBytes": None}, "us-east-1", self.paths, NOW, prices)
        self.assertEqual(estimate["status"], "unavailable")
        self.assertIsNone(estimate["monthlyUSD"])
        prices.read.assert_not_called()

    def test_price_parser_rejects_wrong_region_product_and_ambiguous_matches(self):
        document = self.document
        attributes = document["products"]["STANDARD"]["attributes"]
        for key, value in (("regionCode", "us-west-2"), ("servicecode", "Other"), ("volumeType", "Annotations"), ("usagetype", "Request-Tier1")):
            previous = attributes[key]
            attributes[key] = value
            self.assertNotIn("standard", self.catalog()["products"])
            attributes[key] = previous
        document["products"]["DUPLICATE"] = document["products"]["STANDARD"]
        document["terms"]["OnDemand"]["DUPLICATE"] = document["terms"]["OnDemand"]["STANDARD"]
        self.assertNotIn("standard", self.catalog()["products"])

    def test_price_tiers_reject_overlaps_gaps_wrong_units_currency_and_conditions(self):
        for field, value in (("beginRange", "1"), ("unit", "Requests"), ("pricePerUnit", {"EUR": ".023"}),
                             ("pricePerUnit", {"USD": "NaN"}), ("pricePerUnit", {"USD": "-1"}), ("appliesTo", ["conditional"])):
            self.document = price_document()
            dimensions = self.document["terms"]["OnDemand"]["STANDARD"]["STANDARD.offer"]["priceDimensions"]
            dimensions["first"][field] = value
            self.assertNotIn("standard", self.catalog()["products"])
        self.document = price_document()
        self.document["terms"]["OnDemand"]["STANDARD"]["STANDARD.offer"]["priceDimensions"]["second"]["beginRange"] = "50000"
        self.assertNotIn("standard", self.catalog()["products"])

    def test_price_parser_uses_latest_effective_term_and_rejects_future_publication(self):
        offers = self.document["terms"]["OnDemand"]["STANDARD"]
        offers["future"] = dict(offers["STANDARD.offer"], effectiveDate="2099-01-01T00:00:00Z")
        self.assertEqual(self.catalog()["products"]["standard"]["tiers"][0]["rateUSDPerGiBMonth"], .023)
        self.document["publicationDate"] = "2099-01-01T00:00:00Z"
        with self.assertRaises(metrics.MetricsError):
            self.catalog()

    def test_public_price_cache_is_regional_private_and_expires_after_24_hours(self):
        reader = metrics.PublicPriceReader(self.paths)
        with patch.object(reader, "fetch", return_value=self.document) as fetch:
            first = reader.read("us-east-1", NOW)
            cached = reader.read("us-east-1", NOW + 60)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(first["fetchedAt"], cached["fetchedAt"])
            reader.read("us-east-1", NOW + metrics.PRICE_CACHE_SECONDS)
            self.assertEqual(fetch.call_count, 2)
        cache = self.paths.base / "metrics/pricing/us-east-1.json"
        self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.paths.base / "connections.json").exists())

    def test_bad_cache_is_refreshed_and_expired_cache_is_not_silently_used(self):
        reader = metrics.PublicPriceReader(self.paths)
        with patch.object(reader, "fetch", return_value=self.document):
            reader.read("us-east-1", NOW)
        cache = self.paths.base / "metrics/pricing/us-east-1.json"
        value = json.loads(cache.read_text())
        value["document"] = {"offerCode": "Wrong"}
        turtle.write_json(cache, value)
        with patch.object(reader, "fetch", return_value=self.document) as fetch:
            reader.read("us-east-1", NOW + 1)
            fetch.assert_called_once()
        with patch.object(reader, "fetch", side_effect=metrics.MetricsError("Prices unavailable")):
            with self.assertRaises(metrics.MetricsError):
                reader.read("us-east-1", NOW + metrics.PRICE_CACHE_SECONDS + 2)

    def test_invalid_region_cannot_change_public_endpoint(self):
        reader = metrics.PublicPriceReader(self.paths)
        for region in ("../../secret", "us-east-1?bucket=secret", "https://evil.example", "", None):
            with patch.object(reader, "fetch") as fetch, self.assertRaises(metrics.MetricsError):
                reader.read(region, NOW)
            fetch.assert_not_called()

    def test_public_fetch_sends_no_auth_uses_no_proxy_and_has_size_limit(self):
        response = Mock()
        response.read.side_effect = [b"{}", b""]
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock(open=Mock(return_value=response))
        url = metrics.PRICE_SOURCE.format(region="us-east-1")
        with patch.object(metrics.urllib.request, "build_opener", return_value=opener) as build:
            self.assertEqual(metrics.PublicPriceReader(self.paths).fetch(url), {})
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, url)
            self.assertNotIn("Authorization", request.headers)
            self.assertEqual(opener.open.call_args.kwargs["timeout"], 5)
            self.assertEqual(build.call_args.args[0].proxies, {})
            self.assertIsInstance(build.call_args.args[1], metrics.NoRedirect)
        response.read.side_effect = [b"0123456789ABC"]
        with patch.object(metrics, "MAX_PRICE_BYTES", 12), patch.object(metrics.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(metrics.MetricsError):
                metrics.PublicPriceReader(self.paths).fetch(url)

    def test_price_failure_does_not_discard_available_storage(self):
        day = NOW // DAY * DAY - DAY
        result = metrics.storage(self.connection, self.paths, reader=Mock(read=Mock(return_value=cloud_result({"StandardStorage": [(day, 100)]}))),
                                 pricing_reader=Mock(read=Mock(side_effect=metrics.MetricsError("Prices unavailable"))), now=NOW)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["totalBytes"], 100)
        self.assertEqual(result["estimate"]["status"], "unavailable")
        self.assertIsNone(result["estimate"]["monthlyUSD"])


if __name__ == "__main__":
    unittest.main()
