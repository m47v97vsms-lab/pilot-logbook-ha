import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from logten_package import parse_logten_package


class LogTenPackageTests(unittest.TestCase):
    def _files(self):
        return [
            {"filename": "Export Flights (Tab).txt", "content":
                "flight_flightDate\t flight_from\t flight_to\t flight_selectedCrewPIC\t flight_selectedCrewSIC\t aircraft_aircraftID\taircraftType_type\n"
                "2020-01-01\tCYYC\tCYYC\tPILOT ONE\t\tC-GABC\tB738\n"},
            {"filename": "Address Book Export.txt", "content":
                "Name\tID\nPILOT ONE\t1\n"},
            {"filename": "Export Aircraft.txt", "content":
                "Aircraft ID\tSecondary ID\tType\nC-GABC\t\tB738\n"},
            {"filename": "Export Aircraft Types.txt", "content":
                "Type\tMake\nB738\tBOEING\n"},
            {"filename": "Export Places (Tab).txt", "content":
                "ICAO ID\tIATA ID\t Name\nCYYC\tYYC\tCalgary\n"},
            {"filename": "Export Places (KML).kml", "content":
                "<kml><Document><Placemark><name>Calgary</name><Point><coordinates>-114,51,0</coordinates></Point></Placemark></Document></kml>"},
        ]

    def test_parses_six_exports_and_reconciles_references(self):
        result = parse_logten_package(self._files())
        self.assertEqual(result["counts"]["flights"], 1)
        self.assertEqual(result["counts"]["kml_placemarks"], 1)
        self.assertEqual(result["unmatched"]["crew_names"], [])
        self.assertEqual(result["unmatched"]["aircraft_or_type_values"], [])
        self.assertEqual(result["unmatched"]["airport_codes"], [])
        self.assertFalse(result["canonical_database_writes"])

    def test_single_pilot_blank_sic_is_not_an_unmatched_crew_reference(self):
        result = parse_logten_package(self._files())
        self.assertEqual(result["unmatched"]["crew_names"], [])


    def test_aircraft_type_is_resolved_through_aircraft_reference(self):
        files = self._files()
        files[0]["content"] = (
            "flight_flightDate\\t flight_from\\t flight_to\\t flight_selectedCrewPIC"
            "\\t flight_selectedCrewSIC\\t aircraft_aircraftID\\taircraftType_type\\n"
            "2020-01-01\\tCYYC\\tCYYC\\tPILOT  ONE\\t\\tC-GABC\\t\\n"
        )
        files[1]["content"] = "Name\\tID\\nPILOT ONE\\t1\\n"
        result = parse_logten_package(files)
        self.assertEqual(result["unmatched"]["crew_names"], [])
        self.assertEqual(result["unmatched"]["aircraft_or_type_values"], [])
        self.assertFalse(result["source_data_persisted"])

    def test_missing_required_companion_is_reported(self):
        with self.assertRaisesRegex(ValueError, "Missing required"):
            parse_logten_package(self._files()[:-2])

    def test_duplicate_role_is_rejected(self):
        files = self._files()
        files.append(files[0])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            parse_logten_package(files)

    def test_unknown_file_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unrecognized"):
            parse_logten_package(self._files() + [{"filename": "other.txt", "content": "x"}])

    def test_extra_source_cells_are_not_silently_discarded(self):
        files = self._files()
        files[1]["content"] = "Name\nPILOT ONE\textra-cell\n"
        with self.assertRaisesRegex(ValueError, "extra source data"):
            parse_logten_package(files)


if __name__ == "__main__":
    unittest.main()
