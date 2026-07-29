"""OS grid references to latitude and longitude.

The whole stack stores positions as OS National Grid metres, which is right -
the grid is a planar projection of Britain and distances in it are metres. A web
map wants WGS84. Getting the conversion wrong by a hundred metres puts a station
in the next street, and nothing downstream would notice.

The check that matters is against NaPTAN, which publishes both representations
of the same position, so the comparison isolates this arithmetic from every
other disagreement in the stack. See `test_the_datum_shift_is_not_optional`.
"""

from __future__ import annotations

import math
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from rail.config import load_config
from rail.model import geo
from rail.model.geo import (
    CONVERSION_MEDIAN_LIMIT_METRES,
    LatLon,
    compare_with_naptan,
    grid_to_latlon,
    separation_metres,
)

NAPTAN = pa.schema([
    ("tiploc", pa.string()), ("atco_code", pa.string()), ("name", pa.string()),
    ("stop_type", pa.string()), ("is_active", pa.bool_()),
    ("easting", pa.int64()), ("northing", pa.int64()),
    ("latitude", pa.float64()), ("longitude", pa.float64()),
])


@pytest.fixture
def naptan(tmp_path):
    """A NaPTAN directory carrying both readings of each position."""

    def _write(stops):
        target = tmp_path / "naptan"
        target.mkdir(parents=True, exist_ok=True)
        pq.write_table(
            pa.Table.from_pylist(
                [{"tiploc": t, "atco_code": "9100" + t, "name": t,
                  "stop_type": "RLY", "is_active": True,
                  "easting": e, "northing": n, "latitude": la, "longitude": lo}
                 for t, e, n, la, lo in stops],
                schema=NAPTAN),
            target / "naptan_rail.parquet")
        return target

    return _write


# --- the projection ----------------------------------------------------------


def test_the_true_origin_lands_where_the_projection_defines_it():
    """The OS National Grid's true origin is 49°N 2°W at easting 400000,
    northing −100000. Converting it back must return that point - on the *Airy*
    ellipsoid, which after the shift to WGS84 sits about 130 m away. That the
    answer is not exactly 49, −2 is the datum shift being applied, not an error.
    """
    got = grid_to_latlon(400000, -100000)

    assert separation_metres(got, LatLon(49.0, -2.0)) == pytest.approx(130, abs=20)


def test_stations_land_in_the_right_part_of_the_country():
    """A coarse guard that would catch axes swapped, a sign flipped or the
    projection constants mistyped - each of which produces a confident answer in
    the wrong place."""
    york = grid_to_latlon(459512, 451648)
    kings_cross = grid_to_latlon(530265, 183152)
    penzance = grid_to_latlon(147637, 30688)

    assert (53.9, -1.2) < (york.latitude, york.longitude) < (54.0, -1.0)
    assert kings_cross.latitude < york.latitude          # London is south of York
    assert penzance.longitude < kings_cross.longitude    # Cornwall is west of London
    assert 50.0 < penzance.latitude < 50.2


# --- the check that isolates the arithmetic ----------------------------------


def test_naptans_two_readings_of_one_place_agree():
    """NaPTAN gives an easting/northing *and* a latitude/longitude for each
    stop, so converting the first and comparing with the second measures this
    module alone - one source, one position, two representations.

    Measured on the real feed: 2,754 stops, median 0.19 m. Twenty centimetres is
    NaPTAN's own coordinate rounding.
    """
    # Via the config, not a relative path: the data directory is shared
    # between checkouts through RAIL_DATA_DIR, and a hardcoded "data/"
    # silently skips this wherever the working directory is not the one
    # holding the feeds.
    naptan_dir = load_config().parquet_dir / "naptan"
    if not (naptan_dir / "naptan_rail.parquet").exists():
        pytest.skip("NaPTAN not fetched")

    check = compare_with_naptan(naptan_dir)

    assert check.stops > 2000
    assert check.median_metres < CONVERSION_MEDIAN_LIMIT_METRES


def test_the_datum_shift_is_not_optional(naptan, monkeypatch):
    """Airy 1830 is a nineteenth-century fit to Britain and sits about 100 m
    from WGS84 here, so omitting the Helmert step is not a rounding error.

    Measured on the real feed, dropping it moves the median from 0.19 m to
    **113 m** - which is what this test pins, because a transform that silently
    stopped shifting would still return plausible coordinates.
    """
    # York, King's Cross and Penzance, with WGS84 readings taken from NaPTAN.
    stops = [("YORK", 459600, 451700, 53.95797, -1.09318),
             ("KNGX", 530300, 183000, 51.53088, -0.12293),
             ("PENZNCE", 147600, 30700, 50.12167, -5.53257)]
    directory = naptan(stops)

    assert compare_with_naptan(directory).median_metres < 20

    monkeypatch.setattr(geo, "_helmert", lambda x, y, z: (x, y, z))
    without = compare_with_naptan(directory)

    assert without.median_metres > 90


def test_no_naptan_is_an_absence_rather_than_a_failure(tmp_path):
    """NaPTAN is optional and `rail refresh` rebuilds without it, so an
    unfetched source must report nothing to check - never a conversion error."""
    assert compare_with_naptan(None).stops == 0
    assert compare_with_naptan(tmp_path / "nothing-here").stops == 0
    assert not compare_with_naptan(None)


def test_a_stop_disagreeing_with_itself_is_reported():
    """Two stops in the real feed carry a grid reference and a latitude that are
    not the same place - `HORD` by 104 m. That is NaPTAN disagreeing with
    itself, which no arithmetic here can fix, so it is surfaced rather than
    absorbed into the median."""
    # Via the config, not a relative path: the data directory is shared
    # between checkouts through RAIL_DATA_DIR, and a hardcoded "data/"
    # silently skips this wherever the working directory is not the one
    # holding the feeds.
    naptan_dir = load_config().parquet_dir / "naptan"
    if not (naptan_dir / "naptan_rail.parquet").exists():
        pytest.skip("NaPTAN not fetched")

    check = compare_with_naptan(naptan_dir)

    assert [tiploc for tiploc, _ in check.outliers] == ["HORD", "SWNACFC"]
    # And they are a rounding error away from nothing: two stops in 2,754.
    assert len(check.outliers) * 1000 < check.stops


# --- separation --------------------------------------------------------------


def test_separation_is_metres():
    """A degree of latitude is about 111.3 km."""
    assert separation_metres(LatLon(53.0, -1.0), LatLon(54.0, -1.0)) == pytest.approx(
        111_320, rel=0.01)
    assert separation_metres(LatLon(53.0, -1.0), LatLon(53.0, -1.0)) == 0.0


def test_separation_narrows_with_latitude():
    """A degree of longitude is shorter the further north you go - at 54°N it is
    about cos(54) of its width at the equator. Getting this wrong would overstate
    every east-west disagreement in Scotland."""
    north = separation_metres(LatLon(58.0, -1.0), LatLon(58.0, 0.0))
    south = separation_metres(LatLon(50.0, -1.0), LatLon(50.0, 0.0))

    assert north < south
    assert north / south == pytest.approx(
        math.cos(math.radians(58)) / math.cos(math.radians(50)), rel=0.01)
