"""Fares feed layouts (RSPS5045).

Fares are not point-to-point. A price is found by expanding each end of the
journey into a set of NLCs - the station, any clusters it belongs to (FSC), its
group station - matching a *flow* between those sets (FFL), then reading the
fare records hanging off that flow. Non-derivable fares (NDF/NFO) override the
result. See :mod:`rail.model.fares` for the derivation itself.

Money is held throughout as integer pence, exactly as the feed stores it.

Not yet specified: RST (restrictions), FNS (non-standard discounts), TSP, TAP.
Those land with phases 4b/4c; their offsets are transcribed then rather than
guessed at now.
"""

from __future__ import annotations

from .spec import FileSpec, Kind, RecordSpec, fields

D = Kind.DATE
I = Kind.INT
B = Kind.BOOL
#: HHMM, stored as minutes after midnight.
P = Kind.PUBLIC_TIME

# --- FFL: flows and their fares ---------------------------------------------

# Byte 0 is the update marker (R on a full refresh, I/A/D on updates) and byte 1
# is the record type. Confirmed against RJFAF833: every line starts with "R".
FFL = FileSpec(
    extension="FFL",
    feed="fares",
    key_start=1,
    key_length=1,
    records={
        "F": RecordSpec(
            "flow",
            fields(
                ("update_marker", 0, 1),
                ("origin_code", 2, 4),
                ("destination_code", 6, 4),
                ("route_code", 10, 5),
                ("status_code", 15, 3),
                ("usage_code", 18, 1),
                # "R" means the flow is valid in both directions.
                ("direction", 19, 1),
                ("end_date", 20, 8, D),
                ("start_date", 28, 8, D),
                ("toc", 36, 3),
                ("cross_london_ind", 39, 1, I),
                ("ns_disc_ind", 40, 1, I),
                ("publication_ind", 41, 1),
                ("flow_id", 42, 7, I),
            ),
        ),
        "T": RecordSpec(
            "fare",
            fields(
                ("update_marker", 0, 1),
                ("flow_id", 2, 7, I),
                ("ticket_code", 9, 3),
                ("fare", 12, 8, I),
                ("restriction_code", 20, 2),
            ),
        ),
    },
)

# --- LOC: locations, group associations, railcard availability --------------

# Five record types, confirmed against RJFAF833: L location, G group,
# M group membership, S synonym, R railcard availability.
LOC = FileSpec(
    extension="LOC",
    feed="fares",
    key_start=1,
    key_length=1,
    records={
        "L": RecordSpec(
            "location",
            fields(
                ("update_marker", 0, 1),
                ("uic", 2, 7),
                ("end_date", 9, 8, D),
                ("start_date", 17, 8, D),
                ("quote_date", 25, 8, D),
                # RSPS5045 4.20.2 field 7: ADMIN_AREA_CODE is 3 bytes at 34-36,
                # not 2. Transcribed one short; nothing reads it, so it moved no
                # answer, and the next field starts where the spec says.
                ("area_admin_code", 33, 3),
                ("nlc", 36, 4),
                ("description", 40, 16),
                ("crs", 56, 3),
                ("resv", 59, 5),
                ("ers_country", 64, 2),
                ("ers_code", 66, 3),
                ("fare_group", 69, 6),
                ("county", 75, 2),
                ("pte_code", 77, 2),
                ("zone_no", 79, 4),
                ("zone_ind", 83, 2),
                ("region", 85, 1),
                ("hierarchy", 86, 1),
            ),
        ),
        "G": RecordSpec(
            "location_group",
            fields(
                ("update_marker", 0, 1),
                ("uic", 2, 7),
                ("end_date", 9, 8, D),
                ("start_date", 17, 8, D),
                ("quote_date", 25, 8, D),
                ("description", 33, 16),
            ),
        ),
        "M": RecordSpec(
            "location_group_member",
            fields(
                ("update_marker", 0, 1),
                ("uic", 2, 7),
                ("end_date", 9, 8, D),
                ("member_uic", 17, 7),
                ("member_crs", 24, 3),
            ),
        ),
        "S": RecordSpec(
            "location_synonym",
            fields(
                ("update_marker", 0, 1),
                ("uic", 2, 7),
                ("end_date", 9, 8, D),
                ("start_date", 17, 8, D),
                ("description", 25, 16),
            ),
        ),
        "R": RecordSpec(
            "location_railcard",
            fields(
                ("update_marker", 0, 1),
                ("uic", 2, 7),
                ("railcard_code", 9, 3),
                ("end_date", 12, 8, D),
            ),
        ),
    },
)

# --- FSC: station clusters --------------------------------------------------

FSC = FileSpec(
    extension="FSC",
    feed="fares",
    single=RecordSpec(
        "station_cluster",
        fields(
            ("update_marker", 0, 1),
            ("cluster_id", 1, 4),
            ("cluster_nlc", 5, 4),
            ("end_date", 9, 8, D),
            ("start_date", 17, 8, D),
        ),
    ),
)

# --- NDF / NFO: non-derivable fares and their overrides ---------------------

_NON_DERIVABLE_FIELDS = fields(
    ("update_marker", 0, 1),
    ("origin_code", 1, 4),
    ("destination_code", 5, 4),
    ("route_code", 9, 5),
    ("railcard_code", 14, 3),
    ("ticket_code", 17, 3),
    ("nd_record_type", 20, 1),
    ("end_date", 21, 8, D),
    ("start_date", 29, 8, D),
    ("quote_date", 37, 8, D),
    ("suppress_mkr", 45, 1, B),
    ("adult_fare", 46, 8, I),
    ("child_fare", 54, 8, I),
    ("restriction_code", 62, 2),
    # "Composite record of N" entries are aggregates, not sellable fares, and
    # are excluded during derivation.
    ("composite_indicator", 64, 1),
    ("cross_london_ind", 65, 1, B),
    ("ps_ind", 66, 1),
)

NDF = FileSpec(
    extension="NDF",
    feed="fares",
    single=RecordSpec("non_derivable_fare", _NON_DERIVABLE_FIELDS),
)

NFO = FileSpec(
    extension="NFO",
    feed="fares",
    single=RecordSpec("non_derivable_fare_override", _NON_DERIVABLE_FIELDS),
)

# --- TTY / TVL: ticket types and their validity periods ---------------------

TTY = FileSpec(
    extension="TTY",
    feed="fares",
    single=RecordSpec(
        "ticket_type",
        fields(
            ("update_marker", 0, 1),
            ("ticket_code", 1, 3),
            ("end_date", 4, 8, D),
            ("start_date", 12, 8, D),
            ("quote_date", 20, 8, D),
            ("description", 28, 15),
            ("tkt_class", 43, 1, I),
            # S = single, R = return, N = season.
            ("tkt_type", 44, 1),
            ("tkt_group", 45, 1),
            ("last_valid_day", 46, 8, D),
            ("max_passengers", 54, 3, I),
            ("min_passengers", 57, 3, I),
            ("max_adults", 60, 3, I),
            ("min_adults", 63, 3, I),
            ("max_children", 66, 3, I),
            ("min_children", 69, 3, I),
            ("restricted_by_date", 72, 1, B),
            ("restricted_by_train", 73, 1, B),
            ("restricted_by_area", 74, 1, B),
            ("validity_code", 75, 2),
            ("atb_description", 77, 20),
            ("lul_xlondon_issue", 97, 1, I),
            ("reservation_required", 98, 1),
            ("capri_code", 99, 3),
            ("uts_code", 103, 2),
            ("time_restriction", 105, 1, I),
            ("package_mkr", 107, 1),
            ("fare_multiplier", 108, 3, I),
            ("discount_category", 111, 2, I),
        ),
    ),
)

TVL = FileSpec(
    extension="TVL",
    feed="fares",
    single=RecordSpec(
        "ticket_validity",
        fields(
            ("validity_code", 0, 2),
            ("end_date", 2, 8, D),
            ("start_date", 10, 8, D),
            ("description", 18, 20),
            ("out_days", 38, 2, I),
            ("out_months", 40, 2, I),
            ("ret_days", 42, 2, I),
            ("ret_months", 44, 2, I),
            ("ret_after_days", 46, 2, I),
            ("ret_after_months", 48, 2, I),
            ("ret_after_day", 50, 2),
            ("break_out", 52, 1, B),
            ("break_in", 53, 1, B),
            ("out_description", 54, 14),
            ("rtn_description", 68, 14),
        ),
    ),
)

# --- DIS: passenger statuses and the discounts they attract -----------------

DIS = FileSpec(
    extension="DIS",
    feed="fares",
    key_start=0,
    key_length=1,
    records={
        "S": RecordSpec(
            "status",
            fields(
                ("status_code", 1, 3),
                ("end_date", 4, 8, D),
                ("start_date", 12, 8, D),
                ("atb_desc", 20, 5),
                ("cc_desc", 25, 5),
                ("uts_code", 30, 1),
                ("first_single_max_flat", 31, 8, I),
                ("first_return_max_flat", 39, 8, I),
                ("std_single_max_flat", 47, 8, I),
                ("std_return_max_flat", 55, 8, I),
                ("first_lower_min", 63, 8, I),
                ("first_higher_min", 71, 8, I),
                ("std_lower_min", 79, 8, I),
                ("std_higher_min", 87, 8, I),
                ("fs_mkr", 95, 1, B),
                ("fr_mkr", 96, 1, B),
                ("ss_mkr", 97, 1, B),
                ("sr_mkr", 98, 1, B),
            ),
        ),
        "D": RecordSpec(
            "status_discount",
            fields(
                ("status_code", 1, 3),
                ("end_date", 4, 8, D),
                ("discount_category", 12, 2, I),
                ("discount_indicator", 14, 1),
                ("discount_percentage", 15, 3, I),
            ),
        ),
    },
)

# --- RLC / RCM: railcards and their minimum fares ---------------------------

RLC = FileSpec(
    extension="RLC",
    feed="fares",
    single=RecordSpec(
        "railcard",
        fields(
            ("railcard_code", 0, 3),
            ("end_date", 3, 8, D),
            ("start_date", 11, 8, D),
            ("quote_date", 19, 8, D),
            ("holder_type", 27, 1),
            ("description", 28, 20),
            ("restricted_by_issue", 48, 1, B),
            ("restricted_by_area", 49, 1, B),
            ("restricted_by_train", 50, 1, B),
            ("restricted_by_date", 51, 1, B),
            ("master_code", 52, 3),
            ("display_flag", 55, 1),
            ("max_passengers", 56, 3, I),
            ("min_passengers", 59, 3, I),
            ("max_holders", 62, 3, I),
            ("min_holders", 65, 3, I),
            ("max_acc_adults", 68, 3, I),
            ("min_acc_adults", 71, 3, I),
            ("max_adults", 74, 3, I),
            ("min_adults", 77, 3, I),
            ("max_children", 80, 3, I),
            ("min_children", 83, 3, I),
            ("price", 86, 8, I),
            ("discount_price", 94, 8, I),
            ("validity_period", 102, 4),
            ("last_valid_date", 106, 8, D),
            ("physical_card", 114, 1, B),
            ("capri_ticket_type", 115, 3),
            ("adult_status", 118, 3),
            ("child_status", 121, 3),
            ("aaa_status", 124, 3),
        ),
    ),
)

RCM = FileSpec(
    extension="RCM",
    feed="fares",
    single=RecordSpec(
        "railcard_minimum_fare",
        fields(
            ("railcard_code", 0, 3),
            ("ticket_code", 3, 3),
            ("end_date", 6, 8, D),
            ("start_date", 14, 8, D),
            ("minimum_fare", 22, 8, I),
        ),
    ),
)

# --- RTE / TOC: routes and operators ----------------------------------------

RTE = FileSpec(
    extension="RTE",
    feed="fares",
    key_start=1,
    key_length=1,
    records={
        "R": RecordSpec(
            "route",
            fields(
                ("update_marker", 0, 1),
                ("route_code", 2, 5),
                ("end_date", 7, 8, D),
                ("start_date", 15, 8, D),
                ("quote_date", 23, 8, D),
                ("description", 31, 16),
                ("cc_desc", 187, 16),
            ),
        ),
        "L": RecordSpec(
            "route_location",
            fields(
                ("update_marker", 0, 1),
                ("route_code", 2, 5),
                ("end_date", 7, 8, D),
                ("admin_area_code", 15, 3),
                ("nlc_code", 18, 4),
                ("crs_code", 22, 3),
                # I = included in the route, E = excluded.
                ("incl_excl", 25, 1),
            ),
        ),
    },
)

TOC = FileSpec(
    extension="TOC",
    feed="fares",
    key_start=0,
    key_length=1,
    records={
        "T": RecordSpec(
            "toc",
            fields(("toc_id", 1, 2), ("toc_name", 3, 30), ("active", 41, 1, B)),
        ),
        "F": RecordSpec(
            "toc_fare",
            fields(
                ("fare_toc_id", 1, 3),
                ("toc_id", 4, 2),
                ("fare_toc_name", 6, 30),
            ),
        ),
    },
)

# --- TAP: which ticket types are Advance Purchase --------------------------
# Advance fares are quota-controlled and priced in the reservation system, so
# the price carried in the fares feed is a placeholder (often under £3). TAP is
# the authoritative list of which ticket codes those are, and walk-up analysis
# has to exclude them or every "cheapest fare" becomes nonsense.

TAP = FileSpec(
    extension="TAP",
    feed="fares",
    single=RecordSpec(
        "advance_ticket",
        fields(
            ("ticket_code", 0, 3),
            ("restriction_code", 3, 2),
            ("restriction_flag", 5, 1),
            ("toc_id", 6, 2),
            ("end_date", 8, 8, D),
            ("start_date", 16, 8, D),
            ("check_type", 24, 1),
            ("ap_data", 25, 8),
            ("booking_time", 33, 4),
        ),
    ),
)

# --- RST: restrictions -------------------------------------------------------
# Twelve record types. Byte 0 is a constant "R", bytes 1-2 are the record type,
# and byte 3 is the current/future marker: the file carries both the
# restrictions in force now ("C") and a future version ("F").
#
# The shape is: a header (RH) names a restriction code, time bands (TR) say when
# it bites, and the date-band records (HD, TD) limit which dates and weekdays
# each of those applies to. Train-specific (SR/SQ), railcard (RR) and ticket
# calendar (CA) records refine it further.

#: Raw tuples, not Field objects, so they can be splatted into `fields(...)`.
_RESTRICTION_DATE_DAYS = (
    ("monday", 19, 1, B), ("tuesday", 20, 1, B), ("wednesday", 21, 1, B),
    ("thursday", 22, 1, B), ("friday", 23, 1, B), ("saturday", 24, 1, B),
    ("sunday", 25, 1, B),
)

RST = FileSpec(
    extension="RST",
    feed="fares",
    key_start=1,
    key_length=2,
    records={
        # Overall validity window of this restrictions dataset.
        "RD": RecordSpec(
            "restriction_dates",
            fields(("cf_mkr", 3, 1), ("start_date", 4, 8, D), ("end_date", 12, 8, D)),
        ),
        "RH": RecordSpec(
            "restriction_header",
            fields(
                ("cf_mkr", 3, 1),
                ("restriction_code", 4, 2),
                ("description", 6, 30),
                ("desc_out", 36, 50),
                ("desc_ret", 86, 50),
                ("type_out", 136, 1),
                ("type_ret", 137, 1),
                ("change_ind", 138, 1, B),
            ),
        ),
        # Dates and weekdays on which the whole restriction applies.
        "HD": RecordSpec(
            "restriction_header_date",
            fields(
                ("cf_mkr", 3, 1),
                ("restriction_code", 4, 2),
                # DDMM, no year: these recur annually.
                ("date_from", 6, 4),
                ("date_to", 10, 4),
                ("monday", 14, 1, B), ("tuesday", 15, 1, B), ("wednesday", 16, 1, B),
                ("thursday", 17, 1, B), ("friday", 18, 1, B), ("saturday", 19, 1, B),
                ("sunday", 20, 1, B),
            ),
        ),
        # The time bands themselves.
        "TR": RecordSpec(
            "restriction_time",
            fields(
                ("cf_mkr", 3, 1),
                ("restriction_code", 4, 2),
                ("sequence_no", 6, 4),
                # O outward, R return.
                ("out_ret", 10, 1),
                ("time_from", 11, 4, P),
                ("time_to", 15, 4, P),
                # D departing, A arriving, V via.
                ("arr_dep_via", 19, 1),
                ("location", 20, 3),
                ("rstr_type", 23, 1),
                ("train_type", 24, 1),
                ("min_fare_flag", 25, 1, B),
            ),
        ),
        "TD": RecordSpec(
            "restriction_time_date",
            fields(
                ("cf_mkr", 3, 1),
                ("restriction_code", 4, 2),
                ("sequence_no", 6, 4),
                ("out_ret", 10, 1),
                ("date_from", 11, 4),
                ("date_to", 15, 4),
                *_RESTRICTION_DATE_DAYS,
            ),
        ),
        "TT": RecordSpec(
            "restriction_time_toc",
            fields(
                ("cf_mkr", 3, 1),
                ("restriction_code", 4, 2),
                ("sequence_no", 6, 4),
                ("out_ret", 10, 1),
                ("toc_code", 11, 2),
            ),
        ),
        "SR": RecordSpec(
            "restriction_train",
            fields(
                ("cf_mkr", 3, 1),
                ("restriction_code", 4, 2),
                ("train_no", 6, 6),
                ("out_ret", 12, 1),
                ("quota_ind", 13, 1),
                ("sleeper_ind", 14, 1),
            ),
        ),
        "SD": RecordSpec(
            "restriction_train_date",
            fields(
                ("cf_mkr", 3, 1),
                ("restriction_code", 4, 2),
                ("train_no", 6, 6),
                ("out_ret", 12, 1),
                ("date_from", 13, 4),
                ("date_to", 17, 4),
                ("monday", 21, 1, B), ("tuesday", 22, 1, B), ("wednesday", 23, 1, B),
                ("thursday", 24, 1, B), ("friday", 25, 1, B), ("saturday", 26, 1, B),
                ("sunday", 27, 1, B),
            ),
        ),
        "SQ": RecordSpec(
            "restriction_train_quota",
            fields(
                ("cf_mkr", 3, 1),
                ("restriction_code", 4, 2),
                ("train_no", 6, 6),
                ("out_ret", 12, 1),
                ("location", 13, 3),
                ("quota_ind", 16, 1),
                ("arr_dep", 17, 1),
            ),
        ),
        "RR": RecordSpec(
            "restriction_railcard",
            fields(
                ("cf_mkr", 3, 1),
                ("railcard_code", 4, 3),
                ("sequence_no", 7, 4),
                ("ticket_code", 11, 3),
                ("route_code", 14, 5),
                ("location", 19, 3),
                ("restriction_code", 22, 2),
                ("total_ban", 24, 1, B),
            ),
        ),
        "EC": RecordSpec(
            "restriction_exception",
            fields(
                ("cf_mkr", 3, 1),
                ("exception_code", 4, 1),
                ("description", 5, 50),
            ),
        ),
        "CA": RecordSpec(
            "restriction_ticket_calendar",
            fields(
                ("cf_mkr", 3, 1),
                ("ticket_code", 4, 3),
                ("cal_type", 7, 1),
                ("route_code", 8, 5),
                ("country_code", 13, 1),
                ("date_from", 14, 4),
                ("date_to", 18, 4),
                ("monday", 22, 1, B), ("tuesday", 23, 1, B), ("wednesday", 24, 1, B),
                ("thursday", 25, 1, B), ("friday", 26, 1, B), ("saturday", 27, 1, B),
                ("sunday", 28, 1, B),
            ),
        ),
    },
)

# --- FNS: non-standard discounts --------------------------------------------
# Flow-specific exceptions to the standard railcard discount. Codes may be
# wildcarded with asterisks, which the parser leaves as text.

FNS = FileSpec(
    extension="FNS",
    feed="fares",
    single=RecordSpec(
        "non_standard_discount",
        fields(
            ("update_marker", 0, 1),
            ("origin_code", 1, 4),
            ("destination_code", 5, 4),
            ("route_code", 9, 5),
            ("railcard_code", 14, 3),
            ("ticket_code", 17, 3),
            ("end_date", 20, 8, D),
            ("start_date", 28, 8, D),
            ("quote_date", 36, 8, D),
            ("use_nlc", 44, 4),
            # "N" here means this flow gets no standard discount at all.
            ("adult_nodis_flag", 48, 1),
            ("adult_add_on_amount", 49, 8, I),
            ("adult_rebook_flag", 57, 1),
            ("child_nodis_flag", 58, 1),
            ("child_add_on_amount", 59, 8, I),
            ("child_rebook_flag", 67, 1),
        ),
    ),
)

# --- FRR: rounding rules ----------------------------------------------------
# 36 rule sets of 10 bands each: a fare at or below `upper_limit` rounds to
# `round_to`. Rule Z0 rounds to 10p below £14.99, 50p below £99.99, then £1.
#
# Which rule applies to which discount is *not* determinable from the feed --
# no field in TTY, DIS or the status records carries a rule id. The rules are
# parsed and available, but `model/railcards.py` rounds down to the nearest 5p,
# which is the documented industry convention.

FRR = FileSpec(
    extension="FRR",
    feed="fares",
    single=RecordSpec(
        "rounding_rule",
        fields(
            ("rule_id", 0, 2),
            ("end_date", 2, 8, D),
            ("sequence_no", 10, 2),
            ("start_date", 12, 8, D),
            # 99999997 and 99999999 are "no upper limit" sentinels.
            ("upper_limit", 20, 8, I),
            ("round_to", 28, 8, I),
        ),
    ),
)

FARES_FILES = {
    spec.extension: spec
    for spec in (
        FFL, LOC, FSC, NDF, NFO, TTY, TVL, DIS, RLC, RCM, RTE, TOC, TAP, RST, FNS, FRR
    )
}
