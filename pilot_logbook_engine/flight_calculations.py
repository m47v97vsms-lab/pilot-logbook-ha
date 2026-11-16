"""Great-circle and solar-position estimates for logbook analysis.

Night estimates use a configurable solar-centre elevation threshold. This is an
astronomical estimate, not a determination of a jurisdiction's legal night-time.
"""
from datetime import datetime, timezone, timedelta
import calendar
import math

EARTH_RADIUS_NM = 3440.065

def _utc(value):
    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('Timestamp must include Z or a UTC offset.')
    return dt.astimezone(timezone.utc)

def great_circle_nm(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2-lat1)
    dl = math.radians((lon2-lon1+180) % 360 - 180)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * EARTH_RADIUS_NM * math.asin(min(1, math.sqrt(a)))

def _slerp(a, b, fraction):
    def vec(lat, lon):
        p, l = math.radians(lat), math.radians(lon)
        return (math.cos(p)*math.cos(l), math.cos(p)*math.sin(l), math.sin(p))
    va, vb = vec(*a), vec(*b)
    dot = max(-1.0, min(1.0, sum(x*y for x,y in zip(va,vb))))
    omega = math.acos(dot)
    if omega < 1e-9:
        v = va
    elif math.pi - omega < 1e-8:
        raise ValueError("Airport endpoints are nearly antipodal; the great-circle route is ambiguous.")
    else:
        s = math.sin(omega)
        v = tuple(
            math.sin((1 - fraction) * omega) / s * x
            + math.sin(fraction * omega) / s * y
            for x, y in zip(va, vb)
        )
    lat = math.degrees(math.atan2(v[2], math.hypot(v[0],v[1])))
    lon = math.degrees(math.atan2(v[1],v[0]))
    return lat, lon

def solar_elevation(instant, latitude, longitude):
    """Approximate apparent solar-centre elevation in degrees (NOAA-style)."""
    dt = _utc(instant)
    day = dt.timetuple().tm_yday
    hour = dt.hour + dt.minute/60 + dt.second/3600
    year_days = 366 if calendar.isleap(dt.year) else 365
    gamma = 2*math.pi/year_days * (day-1 + (hour-12)/24)
    eqtime = 229.18*(0.000075 + 0.001868*math.cos(gamma)-0.032077*math.sin(gamma)-0.014615*math.cos(2*gamma)-0.040849*math.sin(2*gamma))
    decl = (0.006918 - 0.399912*math.cos(gamma) + 0.070257*math.sin(gamma)
            -0.006758*math.cos(2*gamma) + 0.000907*math.sin(2*gamma)
            -0.002697*math.cos(3*gamma) + 0.00148*math.sin(3*gamma))
    minutes = hour*60
    true_solar = (minutes + eqtime + 4*longitude) % 1440
    ha = math.radians(true_solar/4 - 180)
    lat = math.radians(latitude)
    cosz = math.sin(lat)*math.sin(decl) + math.cos(lat)*math.cos(decl)*math.cos(ha)
    return 90 - math.degrees(math.acos(max(-1,min(1,cosz))))

def calculate_route(start_utc, end_utc, departure, arrival, threshold=-6.0, step_minutes=1):
    start, end = _utc(start_utc), _utc(end_utc)
    if end <= start: raise ValueError('ON must be later than OFF.')
    if not (-90 <= departure[0] <= 90 and -90 <= arrival[0] <= 90): raise ValueError('Invalid airport latitude.')
    if not (-180 <= departure[1] <= 180 and -180 <= arrival[1] <= 180): raise ValueError('Invalid airport longitude.')
    if not (-18 <= float(threshold) <= 0): raise ValueError('Solar elevation threshold must be between -18 and 0 degrees.')
    if isinstance(step_minutes, bool):
        raise ValueError('Sampling interval must be between 0.1 and 60 minutes.')
    try:
        sampling_minutes = float(step_minutes)
    except (TypeError, ValueError):
        raise ValueError('Sampling interval must be between 0.1 and 60 minutes.')
    if not math.isfinite(sampling_minutes) or not 0.1 <= sampling_minutes <= 60:
        raise ValueError('Sampling interval must be between 0.1 and 60 minutes.')
    duration = (end-start).total_seconds()
    distance = great_circle_nm(*departure, *arrival)
    steps = max(1, math.ceil(duration/(sampling_minutes*60)))
    night_seconds = 0.0
    for i in range(steps):
        f = (i+0.5)/steps
        lat, lon = _slerp(departure, arrival, f)
        instant = start + timedelta(seconds=duration*f)
        if solar_elevation(instant, lat, lon) < float(threshold):
            night_seconds += duration/steps
    return {'great_circle_nm': round(distance,1), 'night_minutes': round(night_seconds/60),
            'night_hours': round(night_seconds/3600,2), 'solar_threshold_degrees': float(threshold),
            'method': 'great_circle_solar_estimate', 'step_minutes': sampling_minutes}
