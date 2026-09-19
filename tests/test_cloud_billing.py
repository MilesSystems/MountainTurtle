import concurrent.futures
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import cloud_billing as billing
import drive_metrics as metrics
import turtle_service as turtle

NOW = datetime(2026, 9, 4, 12, tzinfo=timezone.utc).timestamp()
START, END = '2026-09-01', '2026-09-04'
ACCOUNT = '123456789012'


def daily(date, end, amount, currency='USD', estimated=True):
    return {'TimePeriod': {'Start': date, 'End': end}, 'Estimated': estimated,
            'Total': {'UnblendedCost': {'Amount': amount, 'Unit': currency}}, 'Groups': []}


def report():
    return {'ResultsByTime': [daily('2026-09-01', '2026-09-02', '1.21'),
                              daily('2026-09-02', '2026-09-03', '-0.30'),
                              daily('2026-09-03', '2026-09-04', '2.09')]}


class BillingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = turtle.Paths(home=self.temp.name)
        self.connection = {'id': 'billing-fixture', 'bucket': 'photos-bucket', 'profile': 'test-profile', 'region': 'us-west-2'}

    def reader(self, response=None, account=ACCOUNT):
        return Mock(account=Mock(return_value=account), costs=Mock(return_value=report() if response is None else response))

    def snapshot(self, reader=None, now=NOW, connection=None):
        return billing.snapshot(connection or self.connection, self.paths, reader or self.reader(), now)

    def test_complete_report_sums_decimal_costs_and_preserves_negative_credits(self):
        result = billing.parse_costs(report(), START, END)
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['total'], 3.0)
        self.assertEqual(result['history'][1]['amount'], -.3)
        self.assertEqual(result['currency'], 'USD')
        self.assertTrue(result['estimated'])
        self.assertEqual(result['projectedTotal'], 30)
        self.assertEqual(result['projectionMethod'], 'completedDaysRunRate')
        self.assertTrue(result['projectionEstimated'])
        self.assertEqual(result['projectedPeriodEnd'], '2026-10-01')
        self.assertEqual(result['observedDays'], 3)
        self.assertEqual(result['daysInMonth'], 30)

    def test_exact_decimal_addition_and_finalized_flags(self):
        rows = report()
        for row, amount in zip(rows['ResultsByTime'], ['0.1', '0.2', '0']):
            row['Total']['UnblendedCost']['Amount'] = amount
            row['Estimated'] = False
        result = billing.parse_costs(rows, START, END)
        self.assertEqual(result['total'], .3)
        self.assertFalse(result['estimated'])
        self.assertEqual(result['projectedTotal'], 3)
        self.assertTrue(result['projectionEstimated'])

    def test_missing_days_withhold_month_total_and_do_not_fill_zero(self):
        raw = report()
        raw['ResultsByTime'].pop(1)
        result = billing.parse_costs(raw, START, END)
        self.assertEqual(result['status'], 'partial')
        self.assertIsNone(result['total'])
        self.assertEqual(len(result['history']), 2)
        self.assertEqual(result['observedDays'], 2)
        self.assertIsNone(result['projectedTotal'])
        self.assertIsNone(result['projectionMethod'])
        self.assertFalse(result['projectionEstimated'])

    def test_explicit_zero_differs_from_no_daily_data(self):
        raw = report()
        for row in raw['ResultsByTime']:
            row['Total']['UnblendedCost']['Amount'] = '0'
        zero = billing.parse_costs(raw, START, END)
        self.assertEqual(zero['total'], 0)
        self.assertEqual(zero['projectedTotal'], 0)
        self.assertTrue(zero['projectionEstimated'])
        empty = billing.parse_costs({'ResultsByTime': []}, START, END)
        self.assertEqual(empty['status'], 'noData')
        self.assertIsNone(empty['total'])
        self.assertIsNone(empty['currency'])
        self.assertIsNone(empty['projectedTotal'])
        self.assertEqual(empty['observedDays'], 0)

    def test_multiple_currencies_are_never_added(self):
        raw = report()
        raw['ResultsByTime'][1]['Total']['UnblendedCost']['Unit'] = 'EUR'
        with self.assertRaisesRegex(metrics.MetricsError, 'multiple currencies'):
            billing.parse_costs(raw, START, END)

    def test_invalid_amounts_units_estimated_flags_and_rows_make_report_partial(self):
        variants = [None, {}, 'bad',
                    daily('2026-09-02', '2026-09-03', 'NaN'),
                    daily('2026-09-02', '2026-09-03', 'Infinity'),
                    daily('2026-09-02', '2026-09-03', '1e10000'),
                    daily('2026-09-02', '2026-09-03', 'one'),
                    daily('2026-09-02', '2026-09-03', '1', 'usd'),
                    daily('2026-09-02', '2026-09-03', 1),
                    dict(daily('2026-09-02', '2026-09-03', '1'), Estimated='true'),
                    dict(daily('2026-09-02', '2026-09-03', '1'), Groups=[{'Keys': ['other']}])]
        for row in variants:
            with self.subTest(row=row):
                raw = report()
                raw['ResultsByTime'][1] = row
                result = billing.parse_costs(raw, START, END)
                self.assertEqual(result['status'], 'partial')
                self.assertIsNone(result['total'])
                self.assertIsNone(result['projectedTotal'])
                json.dumps(result, allow_nan=False)

    def test_duplicate_partial_day_and_out_of_window_rows_withhold_total(self):
        for extra in [daily('2026-09-01', '2026-09-02', '1000'),
                      daily('2026-09-02', '2026-09-02', '1000'),
                      daily('2026-08-31', '2026-09-01', '1000')]:
            raw = report()
            raw['ResultsByTime'].append(extra)
            result = billing.parse_costs(raw, START, END)
            self.assertEqual(result['status'], 'partial')
            self.assertIsNone(result['total'])
            self.assertEqual(len(result['history']), 3)
            self.assertIsNone(result['projectedTotal'])

    def test_paginated_or_grouped_reports_never_claim_complete_month(self):
        for key, value in [('NextPageToken', 'next'), ('GroupDefinitions', [{'Type': 'DIMENSION', 'Key': 'REGION'}])]:
            raw = report()
            raw[key] = value
            result = billing.parse_costs(raw, START, END)
            self.assertEqual(result['status'], 'partial')
            self.assertIsNone(result['total'])
            self.assertIsNone(result['projectedTotal'])

    def test_projection_preserves_negative_credit_total_and_reported_currency(self):
        raw = report()
        for row, amount in zip(raw['ResultsByTime'], ['-1.21', '0.30', '-2.09']):
            row['Total']['UnblendedCost'].update(Amount=amount, Unit='EUR')
        result = self.snapshot(self.reader(raw))
        self.assertEqual(result['total'], -3)
        self.assertEqual(result['projectedTotal'], -30)
        self.assertEqual(result['currency'], 'EUR')
        self.assertEqual(result['scope'], 'accountS3AllRegions')
        self.assertEqual(result['accountID'], ACCOUNT)

    def test_projection_calendar_lengths_leap_year_and_december_rollover(self):
        for year, month, days, next_month in [
            (2026, 9, 30, '2026-10-01'), (2026, 1, 31, '2026-02-01'),
            (2027, 2, 28, '2027-03-01'), (2028, 2, 29, '2028-03-01'),
            (2100, 2, 28, '2100-03-01'), (2026, 12, 31, '2027-01-01'),
        ]:
            with self.subTest(year=year, month=month):
                first = datetime(year, month, 1, tzinfo=timezone.utc)
                # Each completed day costs 1; current day contributes nothing.
                raw = {'ResultsByTime': [daily((first + timedelta(days=i)).date().isoformat(),
                                               (first + timedelta(days=i + 1)).date().isoformat(), '1')
                                         for i in range(3)]}
                result = self.snapshot(self.reader(raw), (first + timedelta(days=3, hours=23)).timestamp())
                self.assertEqual(result['total'], 3)
                self.assertEqual(result['projectedTotal'], days)
                self.assertEqual(result['daysInMonth'], days)
                self.assertEqual(result['projectedPeriodEnd'], next_month)
                self.assertEqual(result['observedDays'], 3)

    def test_projection_keeps_fractional_daily_pace_until_final_display(self):
        raw = {'ResultsByTime': [daily('2026-01-01', '2026-01-02', '0.01'),
                                 daily('2026-01-02', '2026-01-03', '0.02')]}
        result = billing.parse_costs(raw, '2026-01-01', '2026-01-03')
        self.assertEqual(result['total'], .03)
        self.assertEqual(result['projectedTotal'], .465)

    def test_projection_overflow_is_withheld_without_discarding_finite_actuals(self):
        raw = report()
        for row in raw['ResultsByTime']:
            row['Total']['UnblendedCost']['Amount'] = '1e307'
        result = billing.parse_costs(raw, START, END)
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['total'], 3e307)
        self.assertIsNone(result['projectedTotal'])
        self.assertFalse(result['projectionEstimated'])
        json.dumps(result, allow_nan=False)

    def test_partial_month_or_cross_month_period_cannot_claim_monthly_projection(self):
        for start, end, rows in [
            ('2026-09-02', '2026-09-04', report()['ResultsByTime'][1:]),
            ('2026-09-01', '2026-10-02', [daily((datetime(2026, 9, 1) + timedelta(days=i)).date().isoformat(),
                                               (datetime(2026, 9, 2) + timedelta(days=i)).date().isoformat(), '1')
                                         for i in range(31)]),
        ]:
            result = billing.parse_costs({'ResultsByTime': rows}, start, end)
            self.assertEqual(result['status'], 'available')
            self.assertIsNone(result['projectedTotal'])

    def test_account_service_filter_never_names_a_bucket_or_current_day(self):
        reader = billing.BillingReader(self.connection, self.paths)
        with patch.object(reader, 'request', return_value=report()) as request:
            reader.costs(ACCOUNT, START, END)
        service, operation, payload = request.call_args.args
        self.assertEqual((service, operation), ('ce', 'get-cost-and-usage'))
        self.assertEqual(payload['TimePeriod'], {'Start': START, 'End': END})
        self.assertEqual(payload['Granularity'], 'DAILY')
        self.assertEqual(payload['Metrics'], ['UnblendedCost'])
        self.assertEqual(payload['Filter'], {'And': [
            {'Dimensions': {'Key': 'SERVICE', 'Values': ['Amazon Simple Storage Service']}},
            {'Dimensions': {'Key': 'LINKED_ACCOUNT', 'Values': [ACCOUNT]}}]})
        self.assertNotIn('photos-bucket', json.dumps(payload))
        result = self.snapshot()
        self.assertEqual(result['scope'], 'accountS3AllRegions')
        self.assertEqual(result['periodEnd'], END)
        self.assertEqual(result['accountID'], ACCOUNT)

    def test_invalid_account_prevents_cost_query(self):
        for account in ('', '123', '12345678901x', None, 123456789012):
            reader = self.reader(account=account)
            result = self.snapshot(reader)
            self.assertEqual(result['status'], 'unavailable')
            self.assertIsNone(result['total'])
            self.assertIsNone(result['projectedTotal'])
            reader.costs.assert_not_called()
        reader = billing.BillingReader(self.connection, self.paths)
        with patch.object(reader, 'request', return_value={'Account': 'bad'}), self.assertRaises(metrics.MetricsError):
            reader.account()

    def test_first_day_has_no_completed_day_and_never_calls_aws(self):
        now = datetime(2026, 10, 1, 23, tzinfo=timezone.utc).timestamp()
        reader = self.reader()
        result = self.snapshot(reader, now)
        self.assertEqual(result['status'], 'noData')
        self.assertIsNone(result['projectedTotal'])
        self.assertFalse(result['projectionEstimated'])
        self.assertEqual(result['observedDays'], 0)
        reader.account.assert_not_called()
        reader.costs.assert_not_called()

    def test_projection_uses_utc_month_even_when_local_date_is_previous_month(self):
        now = datetime.fromisoformat('2026-09-30T18:30:00-06:00').timestamp()
        reader = self.reader()
        result = self.snapshot(reader, now)
        self.assertEqual(result['monthStart'], '2026-10-01')
        self.assertEqual(result['periodEnd'], '2026-10-01')
        self.assertEqual(result['projectedPeriodEnd'], '2026-11-01')
        self.assertEqual(result['daysInMonth'], 31)
        self.assertIsNone(result['projectedTotal'])
        reader.account.assert_not_called()
        reader.costs.assert_not_called()

    def test_auth_failure_has_no_projection_even_with_previously_cached_report(self):
        reader = self.reader()
        self.assertEqual(self.snapshot(reader)['projectedTotal'], 30)
        reader.account.side_effect = metrics.MetricsError('Sign in again.', 'authenticationRequired')
        result = self.snapshot(reader, NOW + 60)
        self.assertEqual(result['status'], 'authenticationRequired')
        self.assertIsNone(result['total'])
        self.assertIsNone(result['projectedTotal'])
        self.assertIsNone(result['projectionMethod'])
        self.assertFalse(result['projectionEstimated'])
        self.assertEqual(reader.costs.call_count, 1)

    def test_sftp_does_not_call_billing(self):
        reader = self.reader()
        result = self.snapshot(reader, connection=dict(self.connection, backend='sftp'))
        self.assertEqual(result['status'], 'unsupported')
        self.assertIsNone(result['projectedTotal'])
        reader.account.assert_not_called()
        reader.costs.assert_not_called()

    def test_cache_is_shared_across_buckets_but_isolates_profile_and_account(self):
        reader = self.reader()
        self.snapshot(reader)
        cached = self.snapshot(reader, NOW + 10, dict(self.connection, id='other', bucket='other-bucket'))
        self.assertTrue(cached['cached'])
        self.assertEqual(cached['projectedTotal'], 30)
        self.assertEqual(cached['observedDays'], 3)
        self.assertEqual(cached['queriedAt'], NOW)
        self.assertEqual(reader.costs.call_count, 1)
        self.snapshot(reader, NOW + 20, dict(self.connection, profile='different-profile'))
        self.assertEqual(reader.costs.call_count, 2)
        reader.account.return_value = '999999999999'
        self.snapshot(reader, NOW + 30)
        self.assertEqual(reader.costs.call_count, 3)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in (self.paths.base / 'billing').glob('*')))

    def test_cache_expires_after_six_hours_and_date_changes(self):
        reader = self.reader()
        self.snapshot(reader)
        self.snapshot(reader, NOW + billing.CACHE_SECONDS - 1)
        self.assertEqual(reader.costs.call_count, 1)
        self.snapshot(reader, NOW + billing.CACHE_SECONDS)
        self.assertEqual(reader.costs.call_count, 2)
        next_day = self.snapshot(reader, NOW + 86400)
        self.assertEqual(reader.costs.call_count, 3)
        self.assertEqual(reader.costs.call_args.args, (ACCOUNT, START, '2026-09-05'))
        self.assertEqual(next_day['status'], 'partial')
        self.assertIsNone(next_day['projectedTotal'])

    def test_month_rollover_never_reuses_previous_month_projection(self):
        first = datetime(2026, 9, 1, tzinfo=timezone.utc)
        raw = {'ResultsByTime': [daily((first + timedelta(days=i)).date().isoformat(),
                                      (first + timedelta(days=i + 1)).date().isoformat(), '1')
                                for i in range(29)]}
        reader = self.reader(raw)
        september = self.snapshot(reader, datetime(2026, 9, 30, 23, tzinfo=timezone.utc).timestamp())
        self.assertEqual(september['projectedTotal'], 30)
        october_first = self.snapshot(reader, datetime(2026, 10, 1, 1, tzinfo=timezone.utc).timestamp())
        self.assertIsNone(october_first['projectedTotal'])
        self.assertEqual(reader.costs.call_count, 1)
        reader.costs.return_value = {'ResultsByTime': [daily('2026-10-01', '2026-10-02', '2')]}
        october = self.snapshot(reader, datetime(2026, 10, 2, 1, tzinfo=timezone.utc).timestamp())
        self.assertEqual(october['projectedTotal'], 62)
        self.assertEqual(october['total'], 2)
        self.assertFalse(october['cached'])
        self.assertEqual(reader.costs.call_count, 2)

    def test_simultaneous_same_account_reads_pay_for_one_request(self):
        reader = self.reader()
        def costs(*_):
            time.sleep(.03)
            return report()
        reader.costs.side_effect = costs
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: self.snapshot(reader), range(2)))
        self.assertEqual(reader.costs.call_count, 1)
        self.assertEqual(sorted(item['cached'] for item in results), [False, True])

    def test_corrupt_cached_response_is_refetched(self):
        reader = self.reader()
        self.snapshot(reader)
        path = next((self.paths.base / 'billing').glob('*.json'))
        stored = json.loads(path.read_text())
        stored['response'] = []
        turtle.write_json(path, stored)
        result = self.snapshot(reader, NOW + 10)
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['total'], 3)
        self.assertEqual(result['projectedTotal'], 30)
        self.assertFalse(result['cached'])
        self.assertEqual(reader.costs.call_count, 2)

    def test_parser_rejects_non_object_top_level_without_raw_exception(self):
        for raw in (None, [], 'bad'):
            with self.assertRaises(metrics.MetricsError):
                billing.parse_costs(raw, START, END)

    def test_reader_allows_only_billing_reads_with_endpoint_isolation_and_timeout(self):
        reader = billing.BillingReader(self.connection, self.paths)
        env = {'AWS_ENDPOINT_URL_CE': 'https://wrong.example', 'AWS_ENDPOINT_URL_STS': 'https://wrong.example'}
        with patch.object(turtle, 'executable', return_value='/aws'), patch.object(turtle, 'environment', return_value=env), \
             patch.object(billing.subprocess, 'run', return_value=Mock(returncode=0, stdout='{}')) as run:
            reader.request('ce', 'get-cost-and-usage', {})
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ['/aws', 'ce', 'get-cost-and-usage'])
        self.assertIn('--region=us-east-1', command)
        self.assertIn('--no-paginate', command)
        self.assertEqual(run.call_args.kwargs['timeout'], 30)
        self.assertEqual(run.call_args.kwargs['env'], {'AWS_MAX_ATTEMPTS': '1', 'AWS_IGNORE_CONFIGURED_ENDPOINT_URLS': 'true'})
        with self.assertRaises(metrics.MetricsError):
            reader.request('ce', 'enable-billing', {})

    def test_permission_signin_timeout_and_no_data_errors_are_sanitized(self):
        reader = billing.BillingReader(self.connection, self.paths)
        for error, status in [('AccessDenied SECRET', 'permissionDenied'), ('ExpiredToken SECRET', 'authenticationRequired'),
                              ('DataUnavailableException SECRET', 'noData'), ('Other failure SECRET', 'unavailable')]:
            with patch.object(turtle, 'executable', return_value='/aws'), patch.object(billing.subprocess, 'run', return_value=Mock(returncode=1, stderr=error)):
                with self.assertRaises(metrics.MetricsError) as raised:
                    reader.request('ce', 'get-cost-and-usage', {})
            self.assertEqual(raised.exception.status, status)
            self.assertNotIn('SECRET', str(raised.exception))
        with patch.object(turtle, 'executable', return_value='/aws'), patch.object(billing.subprocess, 'run', side_effect=subprocess.TimeoutExpired('aws', 30)):
            with self.assertRaisesRegex(metrics.MetricsError, 'too long'):
                reader.request('ce', 'get-cost-and-usage', {})

    def test_reported_spend_survives_cache_write_failure(self):
        with patch.object(turtle, 'write_json', side_effect=OSError('full disk')):
            result = self.snapshot()
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['total'], 3)


if __name__ == '__main__':
    unittest.main()
