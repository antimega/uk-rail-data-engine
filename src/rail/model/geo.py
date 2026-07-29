"""OS grid references to latitude and longitude.

Every station in this project is positioned in **OS National Grid metres** —
`station.easting` and `station.northing`, resolved by corroboration across MSN,
the Network Rail FOI file and NaPTAN. That is the right storage: the grid is a
planar projection of Great Britain, distances in it are metres, and
`model/distance.py` measures straight lines with plain Pythagoras because of it.

A web map wants latitude and longitude. So this converts, and the conversion is
in two parts because the grid is not merely a different unit:

1. **Transverse Mercator, inverted** — undo the projection to get latitude and
   longitude on the **Airy 1830** ellipsoid, the shape OSGB36 is defined against.
2. **Helmert transformation** — shift from OSGB36 to **WGS84**, which is what a
   web map means by latitude and longitude. Airy 1830 is a nineteenth-century
   best fit to Britain alone and sits about 100 m from WGS84 here, so skipping
   this step is not a rounding error — it is a visible offset, roughly a street.

## How accurate, and how we know

The claim is measured rather than asserted, and **NaPTAN is what makes that
possible**: it publishes an easting and northing *and* a latitude and longitude
for the same 2,754 stops — two representations of one position, from one source.
Converting its grid reference and comparing against its own lat/lon isolates
this arithmetic from every other disagreement in the stack:

```
conversion error, 2,754 NaPTAN stops, its own grid against its own lat/lon
  median 0.19 m    p90 0.19    p99 0.20
```

Twenty centimetres, which is NaPTAN's own coordinate rounding rather than
anything here. Dropping the Helmert step moves the median to **113 m** — so the
datum shift is doing exactly what it should, and that comparison is the test
that would catch it being inverted.

**Do not measure this against the station's resolved position instead.** That
comparison reads ~31 m median and up to 900 m, and none of it is conversion
error: it is the long-recorded disagreement between the FOI file and NaPTAN,
whose median the working notes already give as 33 m. York converts 102 m from
NaPTAN's reading because the FOI file puts it on the platform at 459512, 451648
and NaPTAN rounds to 459600, 451700 — a difference this module is measuring, not
making. Confusing the two makes an exact transform look like a broken one.

OSTN15, the official transformation, is a 1 km shift grid accurate to a
centimetre. It is a 25 MB dataset, and at 0.19 m there is nothing here for it to
improve.

## What this is not for

Straight-line distances stay on the grid, in metres, where they belong. Latitude
and longitude are for drawing. Nothing in the routeing guide, the fares engine
or `rail distance` reads anything from here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import duckdb

# --- Airy 1830, and the OS National Grid projection defined on it -------------
#
# OS document "A guide to coordinate systems in Great Britain", appendix C.

#: Semi-major and semi-minor axes of the Airy 1830 ellipsoid, in metres.
_AIRY_A = 6377563.396
_AIRY_B = 6356256.909

#: Scale factor on the central meridian.
_F0 = 0.9996012717
#: True origin: 49°N, 2°W, in radians.
_LAT0 = math.radians(49.0)
_LON0 = math.radians(-2.0)
#: Eastings and northings of the true origin, in metres.
_E0 = 400000.0
_N0 = -100000.0

# --- OSGB36 to WGS84, the standard Helmert parameters ------------------------
#
# Translation in metres, rotation in seconds of arc, scale in parts per million.
# Accurate to roughly 5 m across Great Britain, which is what a station dot is
# worth. OSTN15 is the centimetre answer and needs a 25 MB grid.

_TX, _TY, _TZ = 446.448, -125.157, 542.060
_RX, _RY, _RZ = 0.1502, 0.2470, 0.8421
_S = -20.4894e-6

#: WGS84 ellipsoid.
_WGS84_A = 6378137.000
_WGS84_B = 6356752.3142

#: The measured median conversion error is 0.19 m, and dropping the datum shift
#: takes it to 113 m. A metre therefore sits two orders of magnitude clear of the
#: real answer and two below any plausible breakage — it is a tripwire, not a
#: tolerance, and the only thing that could cross it is the transform itself
#: going wrong.
CONVERSION_MEDIAN_LIMIT_METRES = 1.0

#: Individual stops where NaPTAN's own grid reference and its own latitude and
#: longitude disagree by more than this are reported. Two do today — `HORD` by
#: 104 m and `SWNACFC` by 12 m — and both are NaPTAN disagreeing with itself,
#: which no arithmetic here can fix.
NAPTAN_SELF_AGREEMENT_METRES = 5.0

#: Metres per degree of latitude, near enough for turning a small angular
#: difference into a distance. Only used to express a disagreement.
_METRES_PER_DEGREE = 111_320.0


@dataclass(frozen=True)
class LatLon:
    latitude: float
    longitude: float


def _ellipsoid_to_cartesian(lat: float, lon: float, height: float,
                            a: float, b: float) -> tuple[float, float, float]:
    """Geodetic latitude/longitude/height to geocentric x, y, z."""
    e2 = (a * a - b * b) / (a * a)
    nu = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    return (
        (nu + height) * math.cos(lat) * math.cos(lon),
        (nu + height) * math.cos(lat) * math.sin(lon),
        ((1 - e2) * nu + height) * math.sin(lat),
    )


def _cartesian_to_ellipsoid(x: float, y: float, z: float,
                            a: float, b: float) -> tuple[float, float]:
    """Geocentric x, y, z back to geodetic latitude and longitude.

    Iterative because latitude appears on both sides. Four passes is ample —
    it converges to well under a millimetre in three.
    """
    e2 = (a * a - b * b) / (a * a)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - e2))
    for _ in range(4):
        nu = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        lat = math.atan2(z + e2 * nu * math.sin(lat), p)
    return lat, math.atan2(y, x)


def _helmert(x: float, y: float, z: float) -> tuple[float, float, float]:
    """OSGB36 geocentric coordinates to WGS84 ones.

    Airy 1830 is a nineteenth-century fit to Britain and sits about 100 m from
    WGS84 here, so this step is the difference between a station being on its
    platform and being in the next street.
    """
    rx, ry, rz = (math.radians(v / 3600.0) for v in (_RX, _RY, _RZ))
    scale = 1 + _S
    return (
        _TX + scale * x - rz * y + ry * z,
        _TY + rz * x + scale * y - rx * z,
        _TZ - ry * x + rx * y + scale * z,
    )


def grid_to_latlon(easting: float, northing: float) -> LatLon:
    """An OS National Grid reference as WGS84 latitude and longitude.

    Inverts the Transverse Mercator projection onto Airy 1830, then shifts to
    WGS84. Accurate to about 5 m across Great Britain.

    **Only valid for Great Britain.** The Irish stations MSN carries are in
    neither this grid nor the Irish one — see the working notes — and feeding
    them here produces a confident answer in the wrong country. Callers filter
    before converting; nothing about the arithmetic can detect it.
    """
    a, b = _AIRY_A, _AIRY_B
    e2 = (a * a - b * b) / (a * a)
    n = (a - b) / (a + b)
    n2, n3 = n * n, n * n * n

    # Walk the meridional arc northwards until it reaches this northing. The
    # series is exact enough that a handful of passes settles it completely.
    lat = _LAT0
    m = 0.0
    for _ in range(100):
        lat = (northing - _N0 - m) / (a * _F0) + lat
        d_lat, s_lat = lat - _LAT0, lat + _LAT0
        m = b * _F0 * (
            (1 + n + 1.25 * n2 + 1.25 * n3) * d_lat
            - (3 * n + 3 * n2 + 2.625 * n3) * math.sin(d_lat) * math.cos(s_lat)
            + (1.875 * n2 + 1.875 * n3) * math.sin(2 * d_lat) * math.cos(2 * s_lat)
            - (35 / 24) * n3 * math.sin(3 * d_lat) * math.cos(3 * s_lat)
        )
        if abs(northing - _N0 - m) < 1e-5:
            break

    sin_lat, cos_lat, tan_lat = math.sin(lat), math.cos(lat), math.tan(lat)
    nu = a * _F0 / math.sqrt(1 - e2 * sin_lat ** 2)
    rho = a * _F0 * (1 - e2) / (1 - e2 * sin_lat ** 2) ** 1.5
    eta2 = nu / rho - 1

    tan2, tan4, tan6 = tan_lat ** 2, tan_lat ** 4, tan_lat ** 6
    sec_lat = 1.0 / cos_lat
    de = easting - _E0
    de2 = de * de

    vii = tan_lat / (2 * rho * nu)
    viii = tan_lat / (24 * rho * nu ** 3) * (5 + 3 * tan2 + eta2 - 9 * tan2 * eta2)
    ix = tan_lat / (720 * rho * nu ** 5) * (61 + 90 * tan2 + 45 * tan4)
    x = sec_lat / nu
    xi = sec_lat / (6 * nu ** 3) * (nu / rho + 2 * tan2)
    xii = sec_lat / (120 * nu ** 5) * (5 + 28 * tan2 + 24 * tan4)
    xiia = sec_lat / (5040 * nu ** 7) * (61 + 662 * tan2 + 1320 * tan4 + 720 * tan6)

    lat = lat - vii * de2 + viii * de2 * de2 - ix * de2 * de2 * de2
    lon = _LON0 + x * de - xi * de * de2 + xii * de * de2 * de2 - xiia * de * de2 * de2 * de2

    cx, cy, cz = _ellipsoid_to_cartesian(lat, lon, 0.0, _AIRY_A, _AIRY_B)
    wlat, wlon = _cartesian_to_ellipsoid(*_helmert(cx, cy, cz), _WGS84_A, _WGS84_B)
    return LatLon(math.degrees(wlat), math.degrees(wlon))


def separation_metres(here: LatLon, there: LatLon) -> float:
    """Roughly how far apart two positions are, in metres.

    Equirectangular rather than great-circle: these are used to express a
    disagreement of a few hundred metres, where the difference between the two
    formulae is millimetres.
    """
    mean_lat = math.radians((here.latitude + there.latitude) / 2)
    dy = (there.latitude - here.latitude) * _METRES_PER_DEGREE
    dx = (there.longitude - here.longitude) * _METRES_PER_DEGREE * math.cos(mean_lat)
    return math.hypot(dx, dy)


@dataclass
class ConversionCheck:
    """How far `grid_to_latlon` lands from NaPTAN's own latitude and longitude."""

    stops: int
    median_metres: float
    #: `(tiploc, metres)` past `NAPTAN_SELF_AGREEMENT_METRES`, worst first.
    outliers: list[tuple[str, float]]

    def __bool__(self) -> bool:
        return self.stops > 0


def compare_with_naptan(naptan_dir=None) -> ConversionCheck:
    """Check the conversion against NaPTAN's two readings of the same place.

    NaPTAN publishes an easting and northing **and** a latitude and longitude for
    each stop. Converting the first and comparing against the second isolates
    this arithmetic completely: one source, one position, two representations, so
    any difference is the transform and nothing else.

    That isolation is the point. Comparing against the *station's resolved*
    position instead measures the FOI file against NaPTAN — a real and already
    documented ~33 m disagreement — and would report an exact conversion as a
    30-metre error. See the module docstring.

    Returns an empty check when NaPTAN has not been fetched; absence is not a
    failure, the same as everywhere else this data is optional.
    """
    if naptan_dir is None:
        return ConversionCheck(stops=0, median_metres=0.0, outliers=[])
    parquet = naptan_dir / "naptan_rail.parquet"
    if not parquet.exists():
        return ConversionCheck(stops=0, median_metres=0.0, outliers=[])

    connection = duckdb.connect()
    try:
        rows = connection.execute("""
            select tiploc, easting, northing, latitude, longitude
            from read_parquet($naptan)
            where latitude is not null and longitude is not null
              and easting is not null and northing is not null
        """, {"naptan": parquet.as_posix()}).fetchall()
    finally:
        connection.close()

    gaps = [
        (tiploc, separation_metres(
            grid_to_latlon(easting, northing), LatLon(latitude, longitude)))
        for tiploc, easting, northing, latitude, longitude in rows
    ]
    if not gaps:
        return ConversionCheck(stops=0, median_metres=0.0, outliers=[])

    ordered = sorted(gap for _, gap in gaps)
    return ConversionCheck(
        stops=len(gaps),
        median_metres=ordered[len(ordered) // 2],
        outliers=sorted(
            ((tiploc, gap) for tiploc, gap in gaps
             if gap > NAPTAN_SELF_AGREEMENT_METRES),
            key=lambda row: -row[1],
        ),
    )
