import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import cloud_activity as activity
import drive_metrics as metrics
import turtle_service as turtle

NOW = 20000 * 86400 + 12
END = int(NOW // 60) * 60
START = END - 3600


def response(values=None):
    values = values or {}
    return {'MetricDataResults': [{'Id': f'm{index}', 'StatusCode': 'Complete',
                                  'Timestamps': [point[0] for point in values.get(key, [])],
                                  'Values': [point[1] for point in values.get(key, [])]}
                                 for index, key in enumerate(activity.METRICS)]}


class CloudActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = turtle.Paths(home=self.temp.name)
        self.connection = {'id': 'activity-fixture', 'bucket': 'photos-bucket', 'profile': 'test-profile', 'region': 'us-east-1'}

    def test_only_whole_bucket_filters_are_accepted_and_never_combined(self):
        for whole in ({'Id': 'all'}, {'Id': 'all', 'Filter': {}}, {'Id': 'all', 'Filter': {'Prefix': ''}}):
            self.assertEqual(activity.whole_bucket_configuration({'MetricsConfigurationList': [whole]}), 'all')
        configurations = [{'Id': 'subset', 'Filter': {'Prefix': 'photos/'}}, {'Id': 'z'}, {'Id': 'a'}]
        self.assertEqual(activity.whole_bucket_configuration({'MetricsConfigurationList': configurations}), 'a')
        for subset in ({'Prefix': 'photos/'}, {'Tag': {'Key': 'team', 'Value': 'a'}}, {'AccessPointArn': 'test'}, {'And': {'Prefix': '', 'Tags': []}}):
            with self.assertRaises(metrics.MetricsError) as error:
                activity.whole_bucket_configuration({'MetricsConfigurationList': [{'Id': 'subset', 'Filter': subset}]})
            self.assertEqual(error.exception.status, 'notConfigured')

    def test_empty_config_is_not_configured_and_never_enables_or_queries_data(self):
        reader = Mock(configurations=Mock(return_value={'IsTruncated': False}))
        result = activity.snapshot(self.connection, self.paths, reader=reader, now=NOW)
        self.assertEqual(result['status'], 'notConfigured')
        self.assertIsNone(result['summary']['uploadedBytes'])
        self.assertIsNone(result['current'])
        reader.activity.assert_not_called()
        self.assertIn('additional AWS monitoring charges', result['message'])

    def test_truncated_configuration_list_is_not_claimed_unconfigured(self):
        reader = Mock(configurations=Mock(return_value={'IsTruncated': True, 'NextContinuationToken': 'next'}))
        result = activity.snapshot(self.connection, self.paths, reader=reader, now=NOW)
        self.assertEqual(result['status'], 'configurationIncomplete')
        reader.activity.assert_not_called()

    def test_minute_uploaded_bytes_are_server_observations_and_not_local_or_size_growth(self):
        raw = response({'uploadedBytes': [(END - 120, 60 * 1024), (END - 60, 120 * 1024)],
                        'downloadedBytes': [(END - 60, 600)], 'putRequests': [(END - 60, 4)],
                        'firstByteLatencyMs': [(END - 60, 12)], 'totalRequestLatencyMs': [(END - 60, 140)]})
        result = activity.parse_activity(raw, START, END, NOW)
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['summary']['uploadedBytes'], 180 * 1024)
        self.assertEqual(result['current']['uploadBytesPerSecond'], 2 * 1024)
        self.assertEqual(result['current']['downloadBytesPerSecond'], 10)
        self.assertEqual(result['current']['firstByteLatencyMs'], 12)
        self.assertEqual(result['summary']['putRequests'], 4)
        self.assertNotIn('storageGrowth', result)
        self.assertNotIn('monthlyUSD', result)
        self.assertEqual(result['coverage'], {'expectedMinutes': 60, 'observedMinutes': 2, 'uploadedMinutes': 2, 'downloadedMinutes': 1})

    def test_missing_is_unknown_and_explicit_zero_is_preserved(self):
        result = activity.parse_activity(response({'uploadedBytes': [(END - 60, 0)]}), START, END, NOW)
        self.assertEqual(result['summary']['uploadedBytes'], 0)
        self.assertEqual(result['current']['uploadBytesPerSecond'], 0)
        self.assertIsNone(result['summary']['downloadedBytes'])
        self.assertIsNone(result['current']['downloadBytesPerSecond'])
        self.assertEqual(len(result['history']), 1)

    def test_duplicate_samples_do_not_double_count_and_conflicts_are_partial(self):
        raw = response({'uploadedBytes': [(END - 60, 100), (END - 60, 100)]})
        self.assertEqual(activity.parse_activity(raw, START, END, NOW)['summary']['uploadedBytes'], 100)
        raw = response({'uploadedBytes': [(END - 60, 100), (END - 60, 200)]})
        self.assertEqual(activity.parse_activity(raw, START, END, NOW)['status'], 'partial')

    def test_stale_data_is_not_presented_as_current_speed(self):
        result = activity.parse_activity(response({'uploadedBytes': [(END - 1200, 120)]}), START, END, NOW)
        self.assertEqual(result['status'], 'stale')
        self.assertIsNone(result['current'])
        self.assertEqual(result['summary']['uploadedBytes'], 120)
        self.assertEqual(result['sourceTimestamp'], END - 1200)

    def test_configured_empty_window_reports_no_data_not_zero(self):
        reader = Mock(configurations=Mock(return_value={'MetricsConfigurationList': [{'Id': 'all'}]}), activity=Mock(return_value=response()))
        result = activity.snapshot(self.connection, self.paths, reader=reader, now=NOW)
        self.assertEqual(result['status'], 'noData')
        self.assertEqual(result['filterID'], 'all')
        self.assertEqual(result['scope'], 'bucketAllClients')
        self.assertIsNone(result['summary']['uploadedBytes'])
        reader.activity.assert_called_once_with('all', START, END)

    def test_metric_level_errors_and_pagination_are_not_complete_results(self):
        for change in ('partial', 'missing', 'next'):
            raw = response({'uploadedBytes': [(END - 60, 120)]})
            if change == 'partial': raw['MetricDataResults'][0]['StatusCode'] = 'PartialData'
            elif change == 'missing': raw['MetricDataResults'].pop()
            else: raw['NextToken'] = 'more'
            self.assertEqual(activity.parse_activity(raw, START, END, NOW)['status'], 'partial')
        raw = response()
        raw['MetricDataResults'][0]['StatusCode'] = 'InternalError'
        self.assertEqual(activity.parse_activity(raw, START, END, NOW)['status'], 'unavailable')
        raw['MetricDataResults'][0]['StatusCode'] = 'Forbidden'
        with self.assertRaises(metrics.MetricsError) as error:
            activity.parse_activity(raw, START, END, NOW)
        self.assertEqual(error.exception.status, 'permissionDenied')

    def test_invalid_and_out_of_window_points_are_not_counted(self):
        raw = response({'uploadedBytes': [(START - 60, 10000), (END, 20000), (END - 60, float('nan')), (END - 120, -1)]})
        result = activity.parse_activity(raw, START, END, NOW)
        self.assertIsNone(result['summary']['uploadedBytes'])
        json.dumps(result, allow_nan=False)

    def test_24_hour_query_is_bounded_and_uses_correct_dimensions_and_statistics(self):
        reader = activity.ActivityReader(self.connection, self.paths)
        with patch.object(reader, 'request', return_value=response()) as request:
            reader.activity('whole-bucket', END - 86400, END)
        service, operation, payload = request.call_args.args
        self.assertEqual((service, operation), ('cloudwatch', 'get-metric-data'))
        self.assertEqual(payload['MaxDatapoints'], 20000)
        self.assertEqual(len(payload['MetricDataQueries']), 9)
        for query, (name, statistic) in zip(payload['MetricDataQueries'], activity.METRICS.values()):
            self.assertEqual(query['MetricStat']['Period'], 60)
            self.assertEqual(query['MetricStat']['Stat'], statistic)
            metric = query['MetricStat']['Metric']
            self.assertEqual(metric['MetricName'], name)
            self.assertEqual(metric['Dimensions'], [{'Name': 'BucketName', 'Value': 'photos-bucket'}, {'Name': 'FilterId', 'Value': 'whole-bucket'}])
            self.assertNotIn('StorageType', str(metric['Dimensions']))

    def test_transport_allows_only_two_read_operations_and_bounds_aws_process(self):
        reader = activity.ActivityReader(self.connection, self.paths)
        with patch.object(turtle, 'executable', return_value='/aws'), patch.object(activity.subprocess, 'run', return_value=Mock(returncode=0, stdout='{}')) as run:
            reader.configurations()
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ['/aws', 's3api', 'list-bucket-metrics-configurations'])
        self.assertIn('--no-paginate', command)
        self.assertEqual(run.call_args.kwargs['timeout'], 25)
        with self.assertRaises(metrics.MetricsError):
            reader.request('s3api', 'put-bucket-metrics-configuration', {})

    def test_access_errors_are_specific_and_never_echo_credentials(self):
        reader = activity.ActivityReader(self.connection, self.paths)
        for service, operation, expected in [('s3api', 'list-bucket-metrics-configurations', 's3:GetMetricsConfiguration'),
                                              ('cloudwatch', 'get-metric-data', 'cloudwatch:GetMetricData')]:
            with patch.object(turtle, 'executable', return_value='/aws'), patch.object(activity.subprocess, 'run', return_value=Mock(returncode=1, stderr='AccessDenied SECRET')):
                with self.assertRaises(metrics.MetricsError) as error:
                    reader.request(service, operation, {})
            self.assertEqual(error.exception.status, 'permissionDenied')
            self.assertIn(expected, str(error.exception))
            self.assertNotIn('SECRET', str(error.exception))

    def test_timeout_is_actionable_and_sftp_never_queries_aws(self):
        reader = activity.ActivityReader(self.connection, self.paths)
        with patch.object(turtle, 'executable', return_value='/aws'), patch.object(activity.subprocess, 'run', side_effect=subprocess.TimeoutExpired('aws', 25)):
            with self.assertRaisesRegex(metrics.MetricsError, 'too long'):
                reader.configurations()
        fake = Mock()
        result = activity.snapshot(dict(self.connection, backend='sftp'), self.paths, reader=fake, now=NOW)
        self.assertEqual(result['status'], 'unsupported')
        fake.configurations.assert_not_called()

    def test_invalid_windows_do_not_query_aws(self):
        reader = Mock()
        for hours in (0, 2, 7, 48):
            with self.assertRaises(metrics.MetricsError):
                activity.snapshot(self.connection, self.paths, hours=hours, reader=reader)
        reader.configurations.assert_not_called()


if __name__ == '__main__':
    unittest.main()
