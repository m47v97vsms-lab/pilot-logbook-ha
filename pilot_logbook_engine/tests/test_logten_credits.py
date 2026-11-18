import unittest

from pilot_logbook_engine.logten_credits import sqlite_credit


class SqliteCreditTests(unittest.TestCase):
    def test_missing_credit_defaults_to_zero(self):
        self.assertEqual(sqlite_credit(None, "PIC"), 0.0)
        self.assertEqual(sqlite_credit("", "SIC"), 0.0)
        self.assertEqual(sqlite_credit("   ", "SIC"), 0.0)

    def test_valid_decimal_hours_are_preserved(self):
        self.assertEqual(sqlite_credit(1.5, "PIC"), 1.5)
        self.assertEqual(sqlite_credit("0.75", "SIC"), 0.75)
        self.assertEqual(sqlite_credit("0", "PIC"), 0.0)

    def test_non_finite_and_out_of_range_values_are_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf"), "nan", "inf", -0.1, 24.1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    sqlite_credit(value, "PIC")

    def test_non_numeric_values_are_rejected(self):
        with self.assertRaises(ValueError):
            sqlite_credit("not a number", "SIC")


if __name__ == "__main__":
    unittest.main()
