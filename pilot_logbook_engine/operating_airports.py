"""Additional supported operating airports in Alberta and British Columbia.

The IANA zones are airport-local display zones. Flight instants remain UTC.
"""
ADDITIONAL_AIRPORTS = [
    ("CYLW", "YLW", "Kelowna International", "America/Vancouver"),
    ("CYXX", "YXX", "Abbotsford International", "America/Vancouver"),
    ("CYQQ", "YQQ", "Comox Valley Airport", "America/Vancouver"),
    ("CYXS", "YXS", "Prince George Airport", "America/Vancouver"),
    ("CYQL", "YQL", "Lethbridge Airport", "America/Edmonton"),
    ("CYQU", "YQU", "Grande Prairie Airport", "America/Edmonton"),
    ("CYMM", "YMM", "Fort McMurray International", "America/Edmonton"),
]

OPERATING_AIRPORT_TIMEZONES = {
    "CYYC": "America/Edmonton",
    "CYEG": "America/Edmonton",
    "CYLW": "America/Vancouver",
    "CYVR": "America/Vancouver",
    "CYXX": "America/Vancouver",
    "CYYJ": "America/Vancouver",
    "CYCD": "America/Vancouver",
    "CYQQ": "America/Vancouver",
    "CYXS": "America/Vancouver",
    "CYQL": "America/Edmonton",
    "CYQU": "America/Edmonton",
    "CYMM": "America/Edmonton",
}

# Coordinates carried forward from the project's imported LogTen Places directory.
# Used for route visualization only, never navigation.
ADDITIONAL_AIRPORT_COORDS = {
    "CYLW": (49.956116, -119.377754),
    "CYXX": (49.025276, -122.360001),
    "CYQQ": (49.710835, -124.886665),
    "CYXS": (53.890301, -122.667503),
    "CYQL": (49.626667, -112.802559),
    "CYQU": (55.179859, -118.886909),
    "CYMM": (56.652637, -111.221947),
}
