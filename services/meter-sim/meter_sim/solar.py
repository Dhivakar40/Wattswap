"""Solar geometry and a clear-sky irradiance model. Pure functions, no I/O.

SIMULATION ONLY: these produce plausible numbers for a labeled meter simulator.
"""

import math
from datetime import UTC, datetime


def solar_declination_and_eqtime(t: datetime) -> tuple[float, float]:
    """(declination in radians, equation of time in minutes) via the NOAA series."""
    t = t.astimezone(UTC)
    day_of_year = t.timetuple().tm_yday
    gamma = 2 * math.pi / 365 * (day_of_year - 1 + (t.hour - 12) / 24)
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    decl = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    return decl, eqtime


def cos_zenith(lat_deg: float, lon_deg: float, t: datetime) -> float:
    """Cosine of the solar zenith angle at (lat, lon) and UTC time `t`."""
    t = t.astimezone(UTC)
    decl, eqtime = solar_declination_and_eqtime(t)
    minutes = t.hour * 60 + t.minute + t.second / 60
    true_solar_minutes = minutes + eqtime + 4 * lon_deg  # timezone offset is 0 (UTC)
    hour_angle = math.radians(true_solar_minutes / 4 - 180)
    lat = math.radians(lat_deg)
    return math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(hour_angle)


def solar_elevation_deg(lat_deg: float, lon_deg: float, t: datetime) -> float:
    return 90.0 - math.degrees(math.acos(max(-1.0, min(1.0, cos_zenith(lat_deg, lon_deg, t)))))


def clear_sky_ghi(lat_deg: float, lon_deg: float, t: datetime) -> float:
    """Haurwitz clear-sky global horizontal irradiance in W/m2 (0 when the sun is down)."""
    cz = cos_zenith(lat_deg, lon_deg, t)
    if cz <= 0.0:
        return 0.0
    return 1098.0 * cz * math.exp(-0.059 / cz)
