import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))
import drive_metrics as metrics
import s3_details
import turtle_service as turtle

DAY = 86400
NOW = 20000 * DAY + 3600


def observations(values=None, day=None, partial=None):
    values = values or {}
    day = NOW - 3600 if day is None else day
    return {"MetricDataResults": [
        {"Id": "objects" if name == "objects" else f"s{index}",
         "StatusCode": "PartialData" if name == partial else "Complete",
         "Timestamps": [day] if name in values else [],
         "Values": [values[name]] if name in values else []}
        for index, name in enumerate((*metrics.STORAGE_TYPES, "objects"))
    ]}


class S3DetailsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = turtle.Paths(home=self.temp.name)
        self.connection = {"id": "photos", "name": "Photos", "bucket": "photo-bucket",
                           "profile": "personal", "region": "us-east-1", "readOnly": True}

    def details(self, response=None, **kwargs):
        reader = kwargs.pop("reader", Mock(read=Mock(return_value=response or observations())))
        return s3_details.details(self.connection, self.paths, reader=reader, now=kwargs.pop("now", NOW), **kwargs)

    def test_bucket_types_follow_reserved_suffix_without_cloud_call(self):
        self.assertEqual(s3_details.bucket_type(self.connection), "General purpose")
        for bucket in ("photos--usw2-az1--x-s3", "photos--usw2-lax1-az1--x-s3"):
            self.assertEqual(s3_details.bucket_type(dict(self.connection, bucket=bucket)), "Directory")
        for bucket in ("", "an-alias-s3alias", "object-lambda--ol-s3", "regional.mrap", "table--table-s3"):
            self.assertEqual(s3_details.bucket_type(dict(self.connection, bucket=bucket)), "Unavailable")

    def test_single_storage_class_uses_observation(self):
        result = self.details(observations({"StandardStorage": 100}))
        self.assertTrue(result["ok"])
        self.assertEqual(result["bucketType"], "General purpose")
        self.assertEqual(result["storageClass"], "S3 Standard")
        self.assertEqual(result["storageClasses"], ["S3 Standard"])
        self.assertEqual(result["sourceTimestamp"], NOW - 3600)
        self.assertEqual(result["status"], "available")

    def test_mixed_storage_classes_and_tiering_components_are_deduplicated(self):
        result = self.details(observations({"StandardStorage": 100,
                                           "IntelligentTieringFAStorage": 80,
                                           "IntelligentTieringDAAStorage": 20}))
        self.assertEqual(result["storageClass"], "Mixed: S3 Standard, S3 Intelligent-Tiering")
        self.assertEqual(len(result["storageClasses"]), 2)

    def test_metadata_is_classified_with_its_objects_not_its_billing_rate(self):
        result = self.details(observations({"GlacierStorage": 100, "GlacierS3ObjectOverhead": 8}))
        self.assertEqual(result["storageClass"], "S3 Glacier Flexible Retrieval")
        result = self.details(observations({"IntDAAS3ObjectOverhead": 8, "IntAAObjectOverhead": 32}), refresh=True)
        self.assertEqual(result["storageClass"], "S3 Intelligent-Tiering")

    def test_every_known_component_has_an_object_class(self):
        mapped = [name for names in s3_details.STORAGE_CLASSES.values() for name in names]
        self.assertCountEqual(mapped, metrics.STORAGE_TYPES)
        self.assertEqual(len(mapped), len(set(mapped)))

    def test_missing_and_zero_data_never_default_to_standard(self):
        for response in (observations(), observations({"StandardStorage": 0, "objects": 0})):
            result = self.details(response, refresh=True)
            self.assertEqual(result["storageClass"], "Unavailable")
            self.assertEqual(result["storageClasses"], [])
            self.assertEqual(result["status"], "noData")

    def test_directory_bucket_class_is_measured_not_assumed(self):
        self.connection["bucket"] = "photos--usw2-az1--x-s3"
        result = self.details()
        self.assertEqual(result["bucketType"], "Directory")
        self.assertEqual(result["storageClass"], "Unavailable")
        result = self.details(observations({"ExpressOneZoneStorage": 100}), refresh=True)
        self.assertEqual(result["storageClass"], "S3 Express One Zone")

    def test_partial_and_stale_results_remain_visible_as_qualified_measurements(self):
        result = self.details(observations({"StandardStorage": 100}, partial="GlacierStorage"))
        self.assertEqual(result["storageClass"], "S3 Standard (partial)")
        self.assertEqual(result["status"], "partial")
        result = self.details(observations({"StandardStorage": 100}, day=NOW - 5 * DAY), refresh=True)
        self.assertEqual(result["storageClass"], "S3 Standard (stale)")
        self.assertEqual(result["status"], "stale")
        self.assertTrue(result["isStale"])

    def test_reopening_details_uses_cache_and_refresh_bypasses_it(self):
        reader = Mock(read=Mock(return_value=observations({"StandardStorage": 100})))
        first = self.details(reader=reader)
        second = self.details(reader=reader, now=NOW + 30)
        self.assertEqual(reader.read.call_count, 1)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(second["queriedAt"], NOW)
        self.details(reader=reader, refresh=True, now=NOW + 40)
        self.assertEqual(reader.read.call_count, 2)

    def test_cache_expires_and_changed_connection_identity_is_never_reused(self):
        reader = Mock(read=Mock(return_value=observations({"StandardStorage": 100})))
        self.details(reader=reader)
        self.details(reader=reader, now=NOW + s3_details.CACHE_SECONDS)
        self.assertEqual(reader.read.call_count, 2)
        for field, value in (("bucket", "new-bucket"), ("profile", "work"), ("region", "us-west-2")):
            self.connection[field] = value
            self.assertFalse(self.details(reader=reader, now=NOW + s3_details.CACHE_SECONDS)["cached"])
        self.assertEqual(reader.read.call_count, 5)

    def test_cache_reparses_age_against_current_time(self):
        first_now = 20000 * DAY - 30
        source_time = 19997 * DAY
        first = self.details(observations({"StandardStorage": 100}, day=source_time), now=first_now)
        self.assertFalse(first["isStale"])
        result = self.details(now=first_now + 40)
        self.assertTrue(result["cached"])
        self.assertTrue(result["isStale"])
        self.assertEqual(result["storageClass"], "S3 Standard (stale)")

    def test_failures_are_cached_briefly_and_keep_bucket_type_available(self):
        reader = Mock(read=Mock(side_effect=metrics.MetricsError("Sign in again.", "authenticationRequired")))
        result = self.details(reader=reader)
        self.assertEqual(result["bucketType"], "General purpose")
        self.assertEqual(result["storageClass"], "Unavailable")
        self.assertEqual(result["status"], "authenticationRequired")
        self.assertTrue(self.details(reader=reader, now=NOW + 30)["cached"])
        self.assertEqual(reader.read.call_count, 1)
        self.details(reader=reader, now=NOW + s3_details.FAILURE_CACHE_SECONDS)
        self.assertEqual(reader.read.call_count, 2)

    def test_denied_metric_responses_use_short_failure_cache(self):
        response = observations({"StandardStorage": 100})
        response["MetricDataResults"][0]["StatusCode"] = "Forbidden"
        reader = Mock(read=Mock(return_value=response))
        result = self.details(reader=reader)
        self.assertEqual(result["status"], "permissionDenied")
        self.details(reader=reader, now=NOW + s3_details.FAILURE_CACHE_SECONDS)
        self.assertEqual(reader.read.call_count, 2)

    def test_failed_refresh_does_not_display_previous_class_as_current(self):
        self.details(observations({"StandardStorage": 100}))
        result = self.details(refresh=True, reader=Mock(read=Mock(side_effect=metrics.MetricsError("Network unavailable."))))
        self.assertEqual(result["storageClass"], "Unavailable")
        self.assertIsNone(result["sourceTimestamp"])

    def test_other_backends_do_not_call_aws(self):
        self.connection["backend"] = "sftp"
        reader = Mock()
        result = self.details(reader=reader)
        self.assertEqual(result["status"], "unsupported")
        reader.read.assert_not_called()

    def test_corrupt_cache_is_replaced_and_cache_file_is_private(self):
        directory = metrics.metric_directory(self.paths, self.connection)
        directory.mkdir(parents=True)
        path = directory / "s3-details.json"
        path.write_text("corrupt")
        self.details(observations({"StandardStorage": 100}))
        self.assertEqual(json.loads(path.read_text())["schemaVersion"], 1)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_default_reader_only_queries_bounded_read_only_cloudwatch(self):
        with patch.object(turtle, "executable", return_value="/fake/aws"), \
                patch.object(metrics.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(observations({"StandardStorage": 100})), "")) as run:
            result = s3_details.details(self.connection, self.paths, now=NOW)
        self.assertEqual(result["storageClass"], "S3 Standard")
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["/fake/aws", "cloudwatch", "get-metric-data"])
        self.assertEqual(run.call_args.kwargs["timeout"], 25)
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
