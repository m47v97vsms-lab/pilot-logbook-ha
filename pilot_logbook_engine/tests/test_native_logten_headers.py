import unittest

from pilot_logbook_engine.logten_validation import default_field_mapping


class NativeLogTenHeaderMappingTests(unittest.TestCase):
    def test_whitespace_padded_native_headers_map_to_canonical_fields(self):
        headers = [
            "flight_flightDate",
            "flight_flightNumber",
            " flight_from",
            " flight_to",
            " flight_actualDepartureTime",
            " flight_takeoffTime",
            " flight_landingTime",
            " flight_actualArrivalTime",
            " flight_totalTime",
            " flight_pic",
            " flight_sic",
            " flight_selectedCrewPIC",
            " flight_selectedCrewSIC",
            " aircraft_aircraftID",
            " aircraftType_type",
            " flight_remarks",
        ]
        mapping = default_field_mapping(headers)
        expected = {
            "flight_date": "flight_flightDate",
            "flight_number": "flight_flightNumber",
            "departure": " flight_from",
            "arrival": " flight_to",
            "out": " flight_actualDepartureTime",
            "off": " flight_takeoffTime",
            "on": " flight_landingTime",
            "in": " flight_actualArrivalTime",
            "total_time": " flight_totalTime",
            "pic": " flight_pic",
            "sic": " flight_sic",
            "pic_crew": " flight_selectedCrewPIC",
            "sic_crew": " flight_selectedCrewSIC",
            "aircraft_registration": " aircraft_aircraftID",
            "aircraft_type": " aircraftType_type",
            "remarks": " flight_remarks",
        }
        for target, source in expected.items():
            with self.subTest(target=target):
                self.assertEqual(mapping[target], source)

    def test_native_event_fields_are_not_swapped(self):
        headers = [
            " flight_actualDepartureTime",
            " flight_takeoffTime",
            " flight_landingTime",
            " flight_actualArrivalTime",
        ]
        mapping = default_field_mapping(headers)
        self.assertEqual(mapping["out"], " flight_actualDepartureTime")
        self.assertEqual(mapping["off"], " flight_takeoffTime")
        self.assertEqual(mapping["on"], " flight_landingTime")
        self.assertEqual(mapping["in"], " flight_actualArrivalTime")


if __name__ == "__main__":
    unittest.main()
