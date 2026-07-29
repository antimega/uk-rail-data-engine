"""Fixed links and interchange times — the three non-fixed-width files."""

from __future__ import annotations

import io

from rail.parse.special import read_alf, read_flf, read_tsi


def test_flf_reads_the_sentence_format_and_ignores_the_trailer():
    handle = io.BytesIO(
        b"ADDITIONAL LINK: TUBE BETWEEN EUS AND KXX IN 5 MINUTES\r\n"
        b"ADDITIONAL LINK: WALK BETWEEN CHX AND EMB IN 8 MINUTES\r\n"
        b"END\r\n"
    )
    rows = read_flf(handle).to_pylist()

    assert rows == [
        {"mode": "TUBE", "origin": "EUS", "destination": "KXX", "duration": 5},
        {"mode": "WALK", "origin": "CHX", "destination": "EMB", "duration": 8},
    ]


def test_alf_reads_key_value_pairs_including_the_days_bitmap():
    """R is a 7-char bitmap starting Monday, not a list of day numbers."""
    handle = io.BytesIO(
        b"M=TUBE,O=EUS,D=KXX,T=6,S=0500,E=2359,P=1,F=31/05/2026,U=31/12/2999,R=1111110\r\n"
    )
    row = read_alf(handle).to_pylist()[0]

    assert row["mode"] == "TUBE"
    assert row["duration"] == 6
    assert row["start_time"] == 300 and row["end_time"] == 23 * 60 + 59
    assert [row[d] for d in
            ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")] == \
           [True, True, True, True, True, True, False]


def test_alf_sunday_only_link():
    """The same pair often appears twice: one Sunday row, one Mon-Sat row."""
    handle = io.BytesIO(
        b"M=WALK,O=AFK,D=ASI,T=5,S=0001,E=2359,P=4,R=0000001\r\n"
        b"M=WALK,O=AFK,D=ASI,T=5,S=0001,E=2359,P=4,R=1111110\r\n"
    )
    sunday_row, weekday_row = read_alf(handle).to_pylist()

    assert sunday_row["sunday"] is True and sunday_row["monday"] is False
    assert weekday_row["monday"] is True and weekday_row["sunday"] is False


def test_alf_without_a_days_field_runs_every_day():
    handle = io.BytesIO(b"M=WALK,O=CHX,D=EMB,T=8\r\n")
    row = read_alf(handle).to_pylist()[0]

    assert all(row[day] for day in ("monday", "saturday", "sunday"))


def test_tsi_reads_toc_specific_interchange_times():
    handle = io.BytesIO(b"BHM,XC,LM,10\r\nnot,a,valid,row\r\n")
    rows = read_tsi(handle).to_pylist()

    assert rows == [{"crs": "BHM", "from_toc": "XC", "to_toc": "LM", "minutes": 10}]
