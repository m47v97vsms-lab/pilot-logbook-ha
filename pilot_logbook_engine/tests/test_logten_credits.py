import sqlite3
import unittest

from pilot_logbook_engine.logten_credits import insert_logten_crew, sqlite_credit


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

    def test_pic_only_flight_inserts_no_synthetic_sic(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("""CREATE TABLE flight_crew(
            id INTEGER PRIMARY KEY, flight_id INTEGER NOT NULL,
            person_name TEXT NOT NULL, role TEXT NOT NULL,
            pic_credit REAL NOT NULL, sic_credit REAL NOT NULL,
            pf_credit REAL NOT NULL, pm_credit REAL NOT NULL,
            sort_order INTEGER NOT NULL)""")
        inserted = insert_logten_crew(conn, 1, {
            "pic_crew": "Pilot One", "sic_crew": None,
            "pic": 1.25, "sic": None,
        })
        rows = conn.execute(
            "SELECT person_name,role,pic_credit,sic_credit FROM flight_crew"
        ).fetchall()
        self.assertEqual(inserted, 1)
        self.assertEqual(rows, [("Pilot One", "PIC", 1.25, 0.0)])

    def test_sqlite_nan_is_normalized_before_not_null_insert(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("""CREATE TABLE flight_crew(
            id INTEGER PRIMARY KEY, flight_id INTEGER NOT NULL,
            person_name TEXT NOT NULL, role TEXT NOT NULL,
            pic_credit REAL NOT NULL, sic_credit REAL NOT NULL,
            pf_credit REAL NOT NULL, pm_credit REAL NOT NULL,
            sort_order INTEGER NOT NULL)""")
        inserted = insert_logten_crew(conn, 2, {
            "pic_crew": "Pilot Two", "sic_crew": None,
            "pic": float("nan"), "sic": None,
        })
        row = conn.execute(
            "SELECT pic_credit,sic_credit FROM flight_crew"
        ).fetchone()
        self.assertEqual(inserted, 1)
        self.assertEqual(row, (0.0, 0.0))

    def test_invalid_credit_rolls_back_without_nullable_value(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("""CREATE TABLE flight_crew(
            id INTEGER PRIMARY KEY, flight_id INTEGER NOT NULL,
            person_name TEXT NOT NULL, role TEXT NOT NULL,
            pic_credit REAL NOT NULL, sic_credit REAL NOT NULL,
            pf_credit REAL NOT NULL, pm_credit REAL NOT NULL,
            sort_order INTEGER NOT NULL)""")
        with self.assertRaises(ValueError):
            insert_logten_crew(conn, 3, {
                "pic_crew": "Pilot Three", "sic_crew": None,
                "pic": "not numeric", "sic": None,
            })
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM flight_crew").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
