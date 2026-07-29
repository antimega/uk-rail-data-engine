"""Timetable feed layouts (RSPS5046): MCA, MSN, ZTR.

MCA is CIF: an 80-character record whose first two characters give the record
type. LO (origin), LI (intermediate) and LT (terminating) stops have different
layouts but land in one ``stop_time`` table, distinguished by ``record_type``.

Public times (HHMM) and working times (HHMMH) are both present and are *not*
interchangeable — working times include passing points and are not what a
passenger experiences. Journey-time analysis must use the public times.
"""

from __future__ import annotations

from .spec import FileSpec, Kind, RecordSpec, fields

D = Kind.SHORT_DATE
B = Kind.BOOL
I = Kind.INT
W = Kind.WORKING_TIME
P = Kind.PUBLIC_TIME

_SCHEDULE_FIELDS = fields(
    ("transaction_type", 2, 1),
    ("train_uid", 3, 6),
    ("runs_from", 9, 6, D),
    ("runs_to", 15, 6, D),
    ("monday", 21, 1, B),
    ("tuesday", 22, 1, B),
    ("wednesday", 23, 1, B),
    ("thursday", 24, 1, B),
    ("friday", 25, 1, B),
    ("saturday", 26, 1, B),
    ("sunday", 27, 1, B),
    ("bank_holiday_running", 28, 1),
    ("train_status", 29, 1),
    ("train_category", 30, 2),
    ("train_identity", 32, 4),
    ("headcode", 36, 4),
    ("course_indicator", 40, 1),
    ("profit_centre", 41, 8),
    ("business_sector", 49, 1),
    ("power_type", 50, 3),
    ("timing_load", 53, 4),
    ("speed", 57, 3),
    ("operating_chars", 60, 6),
    ("train_class", 66, 1),
    ("sleepers", 67, 1),
    ("reservations", 68, 1),
    ("connect_indicator", 69, 1),
    ("catering_code", 70, 4),
    ("service_branding", 74, 4),
    ("stp_indicator", 79, 1),
)

_schedule = RecordSpec("schedule", _SCHEDULE_FIELDS)

_schedule_extra = RecordSpec(
    "schedule_extra",
    fields(
        ("traction_class", 2, 4),
        ("uic_code", 6, 5),
        ("atoc_code", 11, 2),
        ("applicable_timetable_code", 13, 1),
        ("retail_train_id", 14, 8),
        ("source", 22, 1),
    ),
)

_tiploc = RecordSpec(
    "tiploc",
    fields(
        ("tiploc_code", 2, 7),
        ("capitals", 9, 2),
        ("nalco", 11, 6),
        ("nlc_check_character", 17, 1),
        ("tps_description", 18, 26),
        ("stanox", 44, 5),
        ("po_mcp_code", 49, 4),
        ("crs_code", 53, 3),
        ("capri_description", 56, 16),
        # Only populated on TA (amend) records.
        ("new_tiploc", 72, 7),
    ),
)

_origin_stop = RecordSpec(
    "stop_time",
    fields(
        ("location", 2, 7),
        ("suffix", 9, 1, I),
        ("scheduled_departure", 10, 5, W),
        ("public_departure", 15, 4, P),
        ("platform", 19, 3),
        ("line", 22, 3),
        ("engineering_allowance", 25, 2),
        ("pathing_allowance", 27, 2),
        ("activity", 29, 12),
        ("performance_allowance", 41, 2),
    ),
)

_intermediate_stop = RecordSpec(
    "stop_time",
    fields(
        ("location", 2, 7),
        ("suffix", 9, 1, I),
        ("scheduled_arrival", 10, 5, W),
        ("scheduled_departure", 15, 5, W),
        ("scheduled_pass", 20, 5, W),
        ("public_arrival", 25, 4, P),
        ("public_departure", 29, 4, P),
        ("platform", 33, 3),
        ("line", 36, 3),
        ("path", 39, 3),
        ("activity", 42, 12),
        ("engineering_allowance", 54, 2),
        ("pathing_allowance", 56, 2),
        ("performance_allowance", 58, 2),
    ),
)

_terminating_stop = RecordSpec(
    "stop_time",
    fields(
        ("location", 2, 7),
        ("suffix", 9, 1, I),
        ("scheduled_arrival", 10, 5, W),
        ("public_arrival", 15, 4, P),
        ("platform", 19, 3),
        ("path", 22, 3),
        ("activity", 25, 12),
    ),
)

_change_en_route = RecordSpec(
    "change_en_route",
    fields(
        ("location", 2, 7),
        ("suffix", 9, 1, I),
        ("train_category", 10, 2),
        ("train_identity", 12, 4),
        ("headcode", 16, 4),
        ("course_indicator", 20, 1),
        ("profit_centre", 21, 8),
        ("business_sector", 29, 1),
        ("power_type", 30, 3),
        ("timing_load", 33, 4),
        ("speed", 37, 3),
        ("operating_chars", 40, 6),
        ("train_class", 46, 1),
        ("sleepers", 47, 1),
        ("reservations", 48, 1),
        ("connect_indicator", 49, 1),
        ("catering_code", 50, 4),
        ("service_branding", 54, 4),
        ("traction_class", 58, 4),
        ("uic_code", 62, 5),
        ("retail_train_id", 67, 8),
    ),
)

_association = RecordSpec(
    "association",
    fields(
        ("transaction_type", 2, 1),
        ("base_uid", 3, 6),
        ("assoc_uid", 9, 6),
        ("start_date", 15, 6, D),
        ("end_date", 21, 6, D),
        ("monday", 27, 1, B),
        ("tuesday", 28, 1, B),
        ("wednesday", 29, 1, B),
        ("thursday", 30, 1, B),
        ("friday", 31, 1, B),
        ("saturday", 32, 1, B),
        ("sunday", 33, 1, B),
        ("assoc_cat", 34, 2),
        ("assoc_date_ind", 36, 1),
        ("assoc_location", 37, 7),
        ("base_location_suffix", 44, 1),
        ("assoc_location_suffix", 45, 1),
        ("diagram_type", 46, 1),
        ("association_type", 47, 1),
        ("stp_indicator", 79, 1),
    ),
)

MCA = FileSpec(
    extension="MCA",
    feed="timetable",
    key_start=0,
    key_length=2,
    records={
        "BS": _schedule,
        "BX": _schedule_extra,
        "TI": _tiploc,
        "TA": _tiploc,
        "TD": _tiploc,
        "LO": _origin_stop,
        "LI": _intermediate_stop,
        "LT": _terminating_stop,
        "CR": _change_en_route,
        "AA": _association,
    },
    ignore=("HD", "ZZ"),
)


MSN = FileSpec(
    extension="MSN",
    feed="timetable",
    key_start=0,
    key_length=1,
    records={
        "A": RecordSpec(
            "physical_station",
            fields(
                ("station_name", 5, 26),
                ("cate_interchange_status", 35, 1),
                ("tiploc_code", 36, 7),
                ("crs_reference_code", 43, 3),
                ("crs_code", 49, 3),
                # Grid references are encoded: true easting = (value - 10000) * 100
                # and true northing = (value - 60000) * 100, in OS metres.
                ("easting", 52, 5, I),
                ("estimated_coordinates", 57, 1),
                ("northing", 58, 5, I),
                ("minimum_change_time", 63, 2, I),
            ),
        ),
        "L": RecordSpec(
            "station_alias",
            fields(("station_name", 5, 26), ("station_alias", 36, 26)),
        ),
    },
    # MSN carries a version header ("MSED 1.00 ..."), a numeric header, a
    # legacy interchange grid of -1/0 values, and two end-of-file markers.
    # None hold station data.
    ignore=("M", "0", "-", " ", "Z", "E"),
)


# ZTR carries bus and ferry links in a quasi-CIF format. Same shape as MCA
# except that locations are 3-character CRS codes rather than 7-char TIPLOCs.
def _z_stop(name: str, extra: tuple) -> RecordSpec:
    return RecordSpec("z_stop_time", fields(("location", 2, 3), *extra))


ZTR = FileSpec(
    extension="ZTR",
    feed="timetable",
    key_start=0,
    key_length=2,
    records={
        "BS": RecordSpec("z_schedule", _SCHEDULE_FIELDS),
        "BX": RecordSpec("z_schedule_extra", fields(("atoc_code", 11, 2))),
        "LO": _z_stop(
            "LO",
            (
                ("scheduled_departure", 10, 5, W),
                ("public_departure", 15, 4, P),
                ("platform", 19, 3),
                ("activity", 29, 12),
            ),
        ),
        "LI": _z_stop(
            "LI",
            (
                ("scheduled_arrival", 10, 5, W),
                ("scheduled_departure", 15, 5, W),
                ("scheduled_pass", 20, 5, W),
                ("public_arrival", 25, 4, P),
                ("public_departure", 29, 4, P),
                ("platform", 33, 3),
                ("activity", 42, 12),
            ),
        ),
        "LT": _z_stop(
            "LT",
            (
                ("scheduled_arrival", 10, 5, W),
                ("public_arrival", 15, 4, P),
                ("platform", 19, 3),
                ("activity", 25, 12),
            ),
        ),
    },
    ignore=("HD", "ZZ"),
)

TIMETABLE_FILES = {spec.extension: spec for spec in (MCA, MSN, ZTR)}
