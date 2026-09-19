"""Compile the real cost summary model without launching the app or reading AWS."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "Sources/S3CostSummary.swift"
HARNESS = r'''
import Foundation

__COST_SUMMARY_MODEL__

let fixture = Data(#"""
{
  "status": "available", "scope": "accountS3AllRegions",
  "monthStart": "2026-09-01", "periodEnd": "2026-09-19",
  "total": 12.34, "currency": "USD", "estimated": true,
  "projectedTotal": 20.5666666667, "projectionMethod": "completedDaysRunRate",
  "projectedPeriodEnd": "2026-10-01", "observedDays": 18, "daysInMonth": 30
}
"""#.utf8)

func summary(_ edit: (inout S3CostSummary) -> Void = { _ in }) throws -> S3CostSummary {
    var value = try JSONDecoder().decode(S3CostSummary.self, from: fixture)
    edit(&value)
    return value
}

let normal = try summary()
let zero = try summary { $0.total = 0; $0.projectedTotal = 0 }
let credit = try summary { $0.total = -12.34; $0.projectedTotal = -20.5666666667 }
let statuses = ["partial", "noData", "permissionDenied", "authenticationRequired", "unavailable", "unsupported"]
var unavailable: [String: [String]] = [:]
for status in statuses {
    // Even a contradictory leftover amount must not bypass the status guard.
    let value = try summary { $0.status = status }
    unavailable[status] = [value.actualValue, value.projectedValue]
}
let actualInvalid = try [
    summary { $0.total = nil },
    summary { $0.total = .nan },
    summary { $0.total = .infinity },
    summary { $0.total = -.infinity },
    summary { $0.scope = "bucket" },
    summary { $0.scope = "organization" },
    summary { $0.currency = nil },
].map(\.actualValue)
let projectedInvalid = try [
    summary { $0.projectedTotal = nil },
    summary { $0.projectedTotal = .nan },
    summary { $0.projectedTotal = .infinity },
    summary { $0.projectionMethod = nil },
    summary { $0.projectionMethod = "awsForecast" },
    summary { $0.scope = "bucket" },
    summary { $0.currency = nil },
].map(\.projectedValue)

let firstDay = try summary { $0.periodEnd = $0.monthStart }
let leapDay = try summary {
    $0.monthStart = "2024-02-01"; $0.periodEnd = "2024-03-01"
    $0.projectedPeriodEnd = "2024-03-01"; $0.observedDays = 29; $0.daysInMonth = 29
}
let invalidDate = try summary { $0.monthStart = "not-a-date" }
let output: [String: Any] = [
    "locale": Locale.current.identifier, "timezone": TimeZone.current.identifier,
    "actual": normal.actualValue, "projected": normal.projectedValue,
    "zeroActual": zero.actualValue, "zeroProjected": zero.projectedValue,
    "creditActual": credit.actualValue, "creditProjected": credit.projectedValue,
    "eurActual": try summary { $0.currency = "EUR" }.actualValue,
    "unavailable": unavailable, "actualInvalid": actualInvalid, "projectedInvalid": projectedInvalid,
    "actualPeriod": normal.actualPeriod, "projectedPeriod": normal.projectedPeriod,
    "leapPeriod": leapDay.actualPeriod, "leapProjectedPeriod": leapDay.projectedPeriod,
    "firstDayPeriod": firstDay.actualPeriod,
    "invalidActualPeriod": invalidDate.actualPeriod, "invalidProjectedPeriod": invalidDate.projectedPeriod,
    "actualHelp": normal.actualHelp, "projectionHelp": normal.projectionHelp,
    "finalizedHelp": try summary { $0.estimated = false }.actualHelp,
]
print(String(decoding: try JSONSerialization.data(withJSONObject: output), as: UTF8.self))
'''


class S3CostSummaryNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        swiftc = shutil.which("swiftc")
        if swiftc is None:
            raise unittest.SkipTest("Swift is required for native cost summary tests")
        source = SOURCE.read_text()
        model = source[source.index("struct S3CostSummary:"):source.index("struct S3CostSummaryRows:")]
        cls.temporary = tempfile.TemporaryDirectory(prefix="mountainturtle-cost-summary-tests-")
        cls.addClassCleanup(cls.temporary.cleanup)
        directory = Path(cls.temporary.name)
        harness = directory / "main.swift"
        harness.write_text(HARNESS.replace("__COST_SUMMARY_MODEL__", model))
        cls.executable = directory / "cost-summary-tests"
        compiled = subprocess.run([swiftc, "-swift-version", "5", str(harness), "-o", str(cls.executable)],
                                  capture_output=True, text=True, timeout=90)
        if compiled.returncode:
            raise AssertionError("Native cost summary compilation failed:\n" + compiled.stdout + compiled.stderr)
        cls.result = cls.run_harness("America/Denver")

    @classmethod
    def run_harness(cls, timezone):
        environment = dict(os.environ, TZ=timezone, LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8")
        result = subprocess.run([str(cls.executable), "-AppleLocale", "en_US_POSIX"],
                                env=environment, check=True, capture_output=True, text=True, timeout=10)
        return json.loads(result.stdout)

    def test_completed_utc_dates_do_not_shift_in_denver(self):
        self.assertEqual(self.result["timezone"], "America/Denver")
        self.assertEqual(self.result["actualPeriod"], "Sep 1 – Sep 18 · UTC")
        self.assertTrue(self.result["projectedPeriod"].startswith("September 2026"))
        self.assertEqual(self.result["leapPeriod"], "Feb 1 – Feb 29 · UTC")
        self.assertTrue(self.result["leapProjectedPeriod"].startswith("February 2024"))
        for timezone in ["UTC", "Pacific/Honolulu", "Pacific/Kiritimati"]:
            with self.subTest(timezone=timezone):
                other = self.run_harness(timezone)
                for field in ["actualPeriod", "projectedPeriod", "leapPeriod", "leapProjectedPeriod"]:
                    self.assertEqual(other[field], self.result[field])

    def test_actual_and_projected_currency_keep_rounding_and_credit_sign(self):
        self.assertEqual(self.result["actual"], "$12.34")
        self.assertEqual(self.result["projected"], "≈$20.57")
        self.assertEqual(self.result["creditActual"], "-$12.34")
        self.assertEqual(self.result["creditProjected"], "≈-$20.57")
        self.assertIn("€", self.result["eurActual"])

    def test_real_zero_is_a_reported_amount_not_missing_data(self):
        self.assertEqual(self.result["zeroActual"], "$0.00")
        self.assertEqual(self.result["zeroProjected"], "≈$0.00")
        expected = {
            "partial": "Incomplete data", "noData": "Not yet reported",
            "permissionDenied": "Billing access needed", "authenticationRequired": "Sign in to view",
            "unavailable": "Unavailable", "unsupported": "Unavailable",
        }
        for status, label in expected.items():
            with self.subTest(status=status):
                self.assertEqual(self.result["unavailable"][status], [label, label])

    def test_actual_refuses_missing_nonfinite_wrong_scope_and_unknown_currency(self):
        self.assertEqual(self.result["actualInvalid"], ["Unavailable"] * 7)

    def test_projection_requires_supported_method_finite_amount_and_account_scope(self):
        self.assertEqual(self.result["projectedInvalid"], ["Unavailable"] * 7)

    def test_missing_or_empty_period_uses_descriptive_fallback(self):
        self.assertEqual(self.result["firstDayPeriod"], "Month to date")
        self.assertEqual(self.result["invalidActualPeriod"], "Month to date")
        self.assertEqual(self.result["invalidProjectedPeriod"], "Month-end estimate")

    def test_help_separates_provisional_reported_spend_from_local_projection(self):
        self.assertIn("across all buckets and regions", self.result["actualHelp"])
        self.assertIn("completed UTC days", self.result["actualHelp"])
        self.assertIn("provisional", self.result["actualHelp"])
        self.assertNotIn("provisional", self.result["finalizedHelp"])
        self.assertIn("18 completed UTC days", self.result["projectionHelp"])
        self.assertIn("multiplied by 30 days", self.result["projectionHelp"])
        self.assertIn("including any credits", self.result["projectionHelp"])
        self.assertIn("not an AWS forecast", self.result["projectionHelp"])


if __name__ == "__main__":
    unittest.main()
