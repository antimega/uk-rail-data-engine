"""Deriving walk-up fares.

A fare is not stored against a pair of stations. It is stored against a *flow*
between two codes, and a code may be a station's own NLC, the NLC of the group
it belongs to (Euston sits in group 1072, London Terminals), or a *cluster* - a
bag of stations that share a price. Finding the fare between two stations means
expanding each end into every code that can stand for it, matching flows between
those sets, and then reading the fare records hanging off the winning flow.

Clusters average 91 members and reach 246, so expanding every flow to every
station pair would produce hundreds of millions of rows. Instead the expansion
happens per query, from one origin outwards, which is the shape the questions
take anyway.

**Advance prices are in the feed; Advance availability is not.** They are stored
as a ladder of price-point ticket codes, real and varying with distance - York to
London runs £22.00 to £73.00 against a £70.70 walk-up Off-Peak. What the feed
does not carry is quota: nothing says which price point is on sale for a given
train on a given date, which lives in the reservation system. Advance fares are
therefore opt-in via `include_advance`, and the result is the best published
price rather than a bookable one.

Not every product describing itself as Advance is a fare. "SALE ADVANCE" is 50p
on every flow - a placeholder, caught by the flat-rate test - and Inclusive Tour
Excursion rates are sold to operators inside a package at a nominal 5p.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pyarrow as pa

from .plusbus import ZONE_MARKER
from .restrictions import applicable_bands

#: Ticket group E is literally described "NOT FOR TRAVEL".
NOT_FOR_TRAVEL_GROUP = "E"

#: TTY field 23, `RESERVATION_REQUIRED` (RSPS5045 4.6.2, position 99). **`'N'`
#: is the only value meaning no reservation**; every other value states one is
#: required, and the spec enumerates them as `'O'` and `'R'` (outward), `'B'`
#: (both legs) and `'E'` (either). A fare you cannot use without reserving a
#: place on a particular train is not a walk-up fare.
#:
#: The rule is `<> 'N'` rather than a list because the spec's own text for `'O'`
#: and `'R'` is identical - "reservation required on outward journey" - which is
#: plainly an editing slip, and guessing which one was meant to say *return*
#: would be inventing a distinction that changes nothing. All four agree on the
#: part that matters.
#:
#: **Two products corroborate it against an entirely different file.**
#: `SF3 SUPERFARE` and `QFR LUMOFIXED` are `'B'`, and both were already
#: reclassified as Advance by the restriction-based test below - a structural
#: field and a behavioural one reaching the same answer by different routes.
#: Every ordinary walk-up is `'N'`: `SDS` ANYTIME DAY S, `CDS` OFF-PEAK DAY S,
#: `SSS` SUPER OFFPEAK S, `GTS`, `G2S`. So is `25Q STDPREM ONBOARD`, correctly -
#: an on-board upgrade reserves nothing, and it is excluded on other grounds.
#:
#: What the field adds is what nothing else could see: the 36 `AIRPORT ADV`
#: ladders and the `BOOKING.COM` fares say nothing about a booked train in their
#: description, their validity or their restriction, so all three existing tests
#: passed them.
#:
#: **`'O'` also settles `Day-Flex`, which these notes had wrong.** It was cited
#: as the reason not to match `%FLEX%` - "a real £5.50 walk-up on Manchester
#: local hops" - but ten codes `FE0`–`FE9` sit on the *same* 12–14 flows at ten
#: different prices (£5.50 to £15.70 on one of them), which is a quota ladder,
#: not a fare. `%FLEX%` is still the wrong marker, for the reason given there;
#: the product was simply misjudged.
#:
#: Reclassified, not discarded, like the rest of the booked-train family:
#: `--advance` still offers them.
NO_RESERVATION = "N"

#: TTY field 29, `PACKAGE_MKR` (RSPS5045 4.6.2, position 108). `'N'` is not a
#: package; `'S'` supplements, `'F'` fares and `'P'` both. A package price buys
#: rail plus something else - parking, admission, a bus zone - so it is not a
#: fare to somewhere, and it undercuts the one that is.
#:
#: **Two independent parts of the feed agree on the membership**, which is why
#: this is trusted over the descriptions. The `TPK` package file names 45 ticket
#: codes and `package_mkr <> 'N'` marks 45, and they are the same 45 - nothing
#: on either side alone. The descriptions could not have found them: `FIRST +
#: PARKING` and `BUS & ADMIT` give themselves away, but the `8A*` series is
#: described `ANYTIME DAY S`, `OFF-PEAK R`, `ANYTIME DAY R` - indistinguishable
#: from the ordinary fare of the same name, and `8AB` is priced from £5.10
#: across 2,825 flows.
NOT_A_PACKAGE = "N"

#: The feed has no flag for "this is a fare the public can buy", so the
#: classification rests on what the products call themselves. Every marker here
#: was added because a real product slipped through and priced a journey
#: absurdly - the reason is recorded so the list can be argued with rather than
#: accumulated blindly. Matched case-insensitively against the description.
NON_PUBLIC_MARKERS: tuple[tuple[str, str], ...] = (
    ("%TEST%", "test data in the feed"),
    # The description field is 15 characters, so words get truncated and spaces
    # squeezed out: both "NOT FOR TRAVEL" and "NOTFORTRAVL" occur.
    ("%NOT FOR TRAVEL%", "not for travel"),
    ("%NOTFORTRAV%", "not for travel"),
    ("%PENALTY%", "penalty fare notice, not a ticket"),
    # Complimentary staff and compensation tickets.
    ("%COMP %", "complimentary, not sold"),
    ("%COMP", "complimentary, not sold"),
    ("%STAFF%", "staff travel, not sold to the public"),
    # An upgrade on a ticket you already hold is not a fare to somewhere. The
    # description field is 15 characters, so "UPGRADE" is routinely truncated -
    # "WEEKEND 1ST UPG", "SEATFROG UPGR", "Std Plus Upgrde", "FESTIVAL UPGRDE".
    # Matching only the full word left 17 of them classed as walk-up fares, and
    # `rail reachable --first-class` duly quoted a £7.50 weekend upgrade as the
    # cheapest first-class fare from York to Darlington - 172 destinations from
    # York, 222 from Euston. Standard class was untouched, which is why it went
    # unnoticed until `rail fares` listed every ticket for a pair.
    ("%UPG%", "supplement, not a fare on its own"),
    # The same truncation one character further along. `NS1 SN-GEX 1ST S UP` is
    # a Gatwick Express first-class upgrade whose description ran out of room
    # before `UPG`, so the marker above cannot see it - and its sibling
    # `SN2 SN-GEX SGL UPG` keeps one more letter and is caught. It carries two
    # fares, both **£0.00**, so as a walk-up fare it won every comparison it
    # entered: a zero price is the cheapest price there is.
    #
    # A suffix rather than `%UP%`, which would match half the feed. It also
    # catches `DH1`/`WCC` "ON THE UP", which are not upgrades and are already
    # excluded on other grounds, so the reason recorded against them is wrong
    # while the outcome is right. `rail validate` guards the outcome that
    # matters - no zero-priced walk-up fare - rather than this rule.
    ("% UP", "supplement, not a fare on its own"),
    # Same category, said the other way round. `AFW` "1ST SUPPLEMENT" was £5-£10
    # on ten flows and classed as a walk-up fare; the validate check below
    # caught it within a minute of being written.
    ("%SUPPL%", "supplement, not a fare on its own"),
    # Seatfrog swaps you onto a different train from the one you booked, which
    # is a change to a ticket you already hold rather than a fare to somewhere.
    #
    # **Not a blanket `%SEATFROG%`**, which was the first guess and was wrong:
    # that brand also sells Secret Fares, genuine discounted journey tickets.
    # Those are Advance products - `GA4 Seatfrog SF` sits on restriction `OA`,
    # "LER ADVANCE ... VALID ON DATE&TRAIN SHOWN ONLY" - and the booked-train
    # rule below reclassifies them correctly without any name being read. Its
    # six upgrade codes are already caught by `%UPG%`.
    ("%SEATFROG SWAP%", "train swap, not a fare on its own"),
    # And a third way of saying it, which names neither. `25Q STDPREM ONBOARD`
    # is Avanti's on-board Standard Premium upgrade - bought from the crew once
    # you already hold a ticket, subject to space, exactly as Weekend First is.
    # Its price varies with distance (153 flows, 18 prices, 80p to £48) so the
    # flat-rate test cannot see it, and its validity is the ordinary "ON DATE
    # SHOWN", so the booked-train rule does not catch it either. It was being
    # quoted as a £26.50 walk-up Euston to Birmingham against a retailer's
    # cheapest walk-up of £20.90. It is the only ticket type in the feed whose
    # description says "onboard", so the marker is as narrow as the case.
    ("%ONBOARD%", "upgrade bought on board, not a fare on its own"),
    # Contactless pay-as-you-go records are informational.
    ("%INFO%", "pay-as-you-go information record"),
    # Inclusive Tour Excursion rates are sold to operators inside a package, so
    # the fare here is a nominal 5p.
    ("%ITX%", "inclusive tour rate, priced inside a package"),
    ("TOUROPS%", "inclusive tour rate, priced inside a package"),
    # Family products price several people. Most are caught by max_passengers,
    # but at least one claims to carry a single passenger at 5p.
    ("%FAM&FRIENDS%", "family product priced for several people"),
    ("%FAMILY%", "family product priced for several people"),
    # And so do group products - GroupSave, party rates, school groups. All 58
    # of them declare `max_passengers = 1`, which is how they slipped past the
    # family check: the price is per person *within a group*, meaningless for
    # one traveller. `SCR GROUP 05` at 80p was the cheapest fare from Glasgow
    # Central to 358 of its 2,748 destinations.
    ("%GROUP%", "group product, priced per person within a party"),
    ("%GRP%", "group product, priced per person within a party"),
    # Concessionary fares need the passenger to be eligible, exactly as a
    # railcard does - but they are separate ticket types rather than a discount,
    # so nothing else here catches them. `CCS SCR CONCESS SGL` at £1.05 became
    # the cheapest fare from Glasgow Central the moment the group products
    # above stopped winning. All 13 matches in the feed are concessions; none
    # is an ordinary fare with the word in its name.
    ("%CONC%", "concessionary fare, not an adult fare"),
    # Age-restricted products are the same shape as a concession - a condition
    # the passenger must meet, written as a ticket type rather than a discount -
    # and nothing else here sees them. Found by ranking every walk-up type by
    # how far below the next-cheapest fare on the same flow it sits, which is
    # the signature `25Q` had.
    #
    # Only `TRQ` carries a fare at all: 75p Headbolt Lane to Skelmersdale Bus
    # Link, on a single flow, where the flat-rate test cannot judge it because a
    # modal share over one flow is trivially 1.0. The other nine are seasons or
    # carry no fare, so this moves one price - but `is_walk_up` should mean what
    # it says whether or not a wrong answer happens to follow.
    ("%YOUTH%", "age-restricted fare, not an adult fare"),
    ("%CHILD%", "age-restricted fare, not an adult fare"),
    ("%16-18%", "age-restricted fare, not an adult fare"),
    ("%SCHOL%", "age-restricted fare, not an adult fare"),
    # Club 50 and Club 55 are the over-50s and over-55s promotionals, so they
    # belong here with the other age conditions rather than with the corporate
    # schemes below, whatever the shared word suggests.
    #
    # **Not a blanket `%CLUB%`, which would be the third `%FLEX%`.** That word
    # is also the Caledonian Sleeper's *accommodation*: `CLUB SOLO`, `CLUB
    # TWIN`, `CLUB FLEXIPASS`, `SF SEAT TO CLUB` - 51 ticket types carry it and
    # only seven are the age product. None of the sleeper's is a walk-up fare
    # today, so a blanket marker would move nothing and be wrong anyway; the
    # day one of them is reclassified it would start quietly excluding berths.
    ("%CLUB 5%", "age-restricted fare, not an adult fare"),
    ("%CLUB5%", "age-restricted fare, not an adult fare"),
    # **A fare only sold under a company's negotiated scheme is not one the
    # public can buy**, which is the same argument that excludes group and
    # concessionary products. Every ticket type matching either word is such a
    # scheme - checked across all 30, not just the walk-up ones - so these two
    # need no narrowing the way `%CLUB%` did.
    #
    # Worth little and worth doing: 20 types, 35 fares in the whole feed, and
    # only two cheaper than any public walk-up on the same flow - Manchester to
    # Sheffield at £39.10 and £40.80 against a public £78.20. `C0S CORP ANYTIME
    # S` has 24 fares and never undercuts. It surfaced as the winning *name* on
    # King's Cross to Manchester, which looked like a leak and was a tie: the
    # public `SOS ANYTIME S` is the same £193.00 on the same route.
    ("%CORP%", "corporate scheme, not sold to the public"),
    ("%BUSINESS%", "corporate scheme, not sold to the public"),
    # A carnet's price buys a bundle of journeys, and nothing in RSPS5045 says
    # so: min and max passengers are both 1 and the price varies with distance.
    # RSPS5052's FlexiProducts names six of them; these are the rest. Euston was
    # quoting `CO5 CARNET OFFPK 5` - £5.70 for five journeys - to 13 stations.
    #
    # Deliberately not a blanket `%FLEXI%`: "FLEXI ADVANCE" is a real
    # single fare, a changeable Advance, and four ticket codes carry it. The
    # products below are named precisely so that one survives.
    ("%CARNET%", "bundle of journeys, not a single fare"),
    ("%FLXIPASS%", "bundle of journeys, not a single fare"),
    ("%DAYSAVE%", "bundle of journeys, not a single fare"),
    ("%FLEXI-RES%", "bundle of journeys, not a single fare"),
    ("%FLEXIDAY%", "bundle of journeys, not a single fare"),
    ("%SMART FLEXI%", "bundle of journeys, not a single fare"),
    ("%FLEXI BACKUP%", "bundle of journeys, not a single fare"),
    ("%FLEXI COUPON%", "bundle of journeys, not a single fare"),
)


def _marker_sql(column: str = "description") -> str:
    return " or ".join(
        f"upper({column}) like '{pattern}'" for pattern, _ in NON_PUBLIC_MARKERS
    )


def _marker_reason_sql(column: str = "description") -> str:
    return "\n".join(
        f"when upper({column}) like '{pattern}' then '{reason}'"
        for pattern, reason in NON_PUBLIC_MARKERS
    )

#: How a restriction says "the train you booked, not any train". Taken from the
#: restriction header's own `desc_out`, which is free text written by the
#: operator - so this is a list of the phrasings the feed actually uses, and it
#: reads like one. 74 of the current codes match.
#:
#: Deliberately **not** matching the header's `description` on "ADVANCE": `1B`
#: is called "EIF Advance" and says only "VALID TO ARRIVE INTO LONDON AFTER 1126
#: MON-FRI", which is an ordinary time restriction that happens to sit on an
#: Advance product. The name is what the operator calls it; `desc_out` is what
#: it does.
BOOKED_TRAIN_PHRASES: tuple[str, ...] = (
    "%DATE&TRAIN SHOWN%",
    "%DATE & TRAIN SHOWN%",
    "%DATE/TRAIN SHOWN%",
    "%TRAIN SHOWN ONLY%",
    "%TRAINS SHOWN%",
    "%RESVD TRAINS SHOWN%",
    "%BOOKED TRAIN%",
    "%BOOKDTRAIN%",
    "%DATE & TIME SHOWN%",
)

#: And one that says it in the restriction's *name* rather than its rule text.
#: `FL` is called "LNER FLEX ON SET TIME" while its `desc_out` reads only "VALID
#: ON DATE ONLY WITH FLEX. LTD CHANGE. NO RFND." - no phrasing in that sentence
#: distinguishes it from an ordinary day restriction, and matching "LTD CHANGE"
#: or "NO RFND" would sweep up half the file. The name is where it is stated,
#: so the name is what is read.
BOOKED_TRAIN_NAME_PHRASES: tuple[str, ...] = ("%ON SET TIME%",)

_BOOKED_TRAIN_SQL = " or ".join(
    [f"upper(coalesce(desc_out, '')) like '{phrase}'"
     for phrase in BOOKED_TRAIN_PHRASES]
    + [f"upper(coalesce(description, '')) like '{phrase}'"
       for phrase in BOOKED_TRAIN_NAME_PHRASES]
)

#: Standard class. 1 is first; 9 appears on a handful of oddities.
STANDARD_CLASS = 2

#: TTY field 9. See `model/returns.py` - this is what decides a return, not the
#: validity record, because validity codes are shared between the two.
RETURN_TYPE = "R"

#: A ticket type whose modal price covers at least this share of its flows is a
#: flat-rate product, not a distance-based fare. Real walk-up tickets sit below
#: 0.06 and the flat-rate ones sit at 1.0, so anything between the two would do -
#: except at the very bottom of the sample. A type with two flows at two prices
#: has a modal share of exactly 0.5, so a threshold of 0.5 wrongly condemns it.
#: 0.9 keeps the genuine cases, which are all 1.0.
_FLAT_RATE_THRESHOLD = 0.9
#: With a single flow the modal share is trivially 1.0, so two is the smallest
#: sample that means anything. It needs to be this low: Caledonian Sleeper staff
#: and friends-and-family tickets exist on exactly two flows at 10p each, and a
#: higher guard let them through as the cheapest way to reach Scotland.
_FLAT_RATE_MIN_FLOWS = 2


@dataclass
class FaresCounts:
    aliases: int
    ticket_types: int
    walk_up: int
    #: (reason, count) for every ticket type excluded from walk-up pricing.
    rejected: list[tuple[str, int]]


def _load_flexi_products(
    connection: duckdb.DuckDBPyConnection, supplementary_dir: Path | None
) -> None:
    """Bundle sizes for flexi season and carnet tickets, from RSPS5052.

    A flexi-carnet prices 12 or 50 journeys as one ticket and RSPS5045 has no
    field that says so - min and max passengers are both 1, the price varies
    with distance, and the description is free text. Every walk-up test here
    passes it. This file is the only place the bundle size is stated.

    It covers six codes, of which three currently reach the walk-up set: FFL
    and FSL at 50 journeys and FL1 at 8. Two dozen other tickets describing
    themselves FLEXI or FLEXIDAY are *not* in it, so this closes part of the
    hole and `NON_PUBLIC_MARKERS` would have to close the rest.

    An empty table when the file is absent, so the SQL below needs no branch.
    """
    listing = (
        None if supplementary_dir is None
        else supplementary_dir / "flexi_product.parquet"
    )
    if listing is not None and listing.exists():
        connection.execute(f"""
            create or replace table flexi_product as
            select * from read_parquet('{listing.as_posix()}')
            where current_date between start_date and end_date
        """)
        return
    connection.execute("""
        create or replace table flexi_product (
            ticket_code varchar, start_date date, end_date date,
            bundle_size integer, bi_directional boolean, transferable boolean
        )
    """)


def build_fares_reference(
    connection: duckdb.DuckDBPyConnection,
    fares_dir: Path,
    supplementary_dir: Path | None = None,
) -> FaresCounts:
    """Build fare_alias and ticket_type_current."""
    ticket_types = (fares_dir / "ticket_type.parquet").as_posix()
    advance = (fares_dir / "advance_ticket.parquet").as_posix()
    fare_records = (fares_dir / "fare.parquet").as_posix()
    validity = (fares_dir / "ticket_validity.parquet").as_posix()
    restriction_header = (fares_dir / "restriction_header.parquet").as_posix()

    _load_flexi_products(connection, supplementary_dir)

    # Every code that can stand for a station when matching a flow.
    #
    # **A flow endpoint is not always an NLC.** RSPS5045 4.1.2 allows "4 digit
    # NLC code, county code, zone code", and the county form is genuinely used:
    # `LOC` field `county` gives Euston `01`, and cluster `Q797` has `CC01` as a
    # member. Expanding only NLCs and their clusters missed those flows entirely.
    #
    # Today that is **five flows, every one of them to Douglas, Isle of Man** -
    # the Steam Packet's five fare bands, £97.30 to £187.20 across 48 counties,
    # which is how a rail-and-ferry fare is set when the rail leg can start
    # anywhere. Without the county code, Douglas had no fare from anywhere and
    # the map said so; the real answer from Euston is £145.40, band `Q797`.
    #
    # Narrow, then, but not a special case: the county code is a legitimate flow
    # endpoint and this is simply the expansion the spec describes. No flow names
    # a `CC` code directly, so the only route in is through a cluster.
    connection.execute(f"""
        create or replace table fare_alias as
        with own as (
            select crs, nlc as code, 'nlc' as kind from station_nlc
            union all
            select crs, fare_group, 'group'
            from station_nlc
            where fare_group is not null and fare_group <> nlc
            union all
            select distinct crs, 'CC' || county, 'county'
            from read_parquet('{(fares_dir / "location.parquet").as_posix()}')
            where crs is not null and county is not null and trim(county) <> ''
              -- This arm reads LOC directly rather than going through
              -- `station_nlc`, so it needs the PlusBus exclusion too - the four
              -- zones that gained a CRS all carry a county code as well, and
              -- would come back in through here alone.
              and coalesce(description, '') not like '{ZONE_MARKER}'
              and current_date between start_date and end_date
        ),
        clustered as (
            select o.crs, sc.cluster_id as code, 'cluster' as kind
            from own o
            join station_cluster sc on sc.cluster_nlc = o.code
        )
        select distinct crs, code, kind
        from (select * from own union all select * from clustered)
    """)

    # TVL is a version history like the rest. break_out says whether a break of
    # journey is permitted on the outward leg - 41 of the 104 validity codes say
    # it is not, covering 651 of the walk-up ticket types.
    connection.execute(f"""
        create or replace table ticket_validity_current as
        select validity_code, description, out_days, out_months,
               break_out, break_in
        from (
            select *, row_number() over (
                partition by validity_code order by start_date desc
            ) as rn
            from read_parquet('{(fares_dir / "ticket_validity.parquet").as_posix()}')
            where current_date between start_date and end_date
        ) where rn = 1
    """)

    # Route conditions, as stations a journey must or must not pass through.
    # "VIA APPLEBY" includes APP; "NOT VIA CHELTNHM" excludes CNM. Only 627 of
    # the 1,478 routes carry these; the rest state their condition in prose only
    # and cannot be checked without the routeing guide.
    connection.execute(f"""
        create or replace table route_via as
        select distinct
               l.route_code,
               coalesce(l.crs_code, n.crs) as crs,
               l.incl_excl
        from read_parquet('{(fares_dir / "route_location.parquet").as_posix()}') l
        left join station_nlc n on n.nlc = l.nlc_code
        where coalesce(l.crs_code, n.crs) is not null
          and l.incl_excl in ('I', 'E')
    """)

    # TTY is a version history like LOC, so take the current record per code.
    markers = _marker_sql()
    connection.execute(f"""
        create or replace table ticket_type_current as
        with current_records as (
            select *, row_number() over (
                partition by ticket_code order by start_date desc
            ) as rn
            from read_parquet('{ticket_types}')
            where current_date between start_date and end_date
        ),
        advance_codes as (
            select distinct ticket_code
            from read_parquet('{advance}')
            where current_date between start_date and end_date
        ),
        -- A real walk-up fare varies with distance; a promotional or child
        -- flat-rate product does not. "Kid with Adult" is £2 on all 30,980 of
        -- its flows, while Anytime Day Single has 2,365 distinct prices. The
        -- gap between the two populations is more than twentyfold, so the
        -- threshold below is nowhere near any real ticket type.
        fare_spread as (
            select ticket_code,
                   count(*) as flow_count,
                   max(same_price) * 1.0 / count(*) as modal_share
            from (
                select ticket_code,
                       count(*) over (partition by ticket_code, fare) as same_price
                from read_parquet('{fare_records}')
                where fare is not null
            )
            group by ticket_code
        ),
        -- **The restriction says what the validity does not.** A fare valid
        -- only on the train you booked is an Advance product whatever it calls
        -- itself, and the six validity codes reading `BOOKDTRAINONLY` are not
        -- the only place that is stated: 74 current restriction codes say it in
        -- their own `desc_out`, and the products carrying them mostly sit on
        -- entirely ordinary validities.
        --
        -- LNER's Simpler Fares trial is the case that found this. `70min Flex`
        -- - 52 codes replacing Off-Peak and Anytime on the East Coast - carries
        -- validity `61` "ON DATE SHOWN" and restriction `FL`, "LNER FLEX ON SET
        -- TIME". Greater Anglia's `Seatfrog SF` is the same shape on `OA`,
        -- "LER ADVANCE". Both were being quoted as walk-up fares, and from
        -- King's Cross a `70min Flex` single made two singles look £100 cheaper
        -- than the return.
        --
        -- The test is **every** fare, not any: a ticket type is Advance when
        -- there is no journey on which it is not tied to a booked train. A type
        -- with a mix would be an ordinary fare that happens to have an Advance
        -- variant on one flow, and withdrawing it would be too strict.
        booked_train_restrictions as (
            select distinct restriction_code
            from read_parquet('{restriction_header}')
            where cf_mkr = 'C' and ({_BOOKED_TRAIN_SQL})
        ),
        restriction_bound as (
            select ticket_code,
                   count(*) as priced,
                   count(*) filter (
                       where restriction_code in
                           (select restriction_code from booked_train_restrictions)
                   ) as on_booked_train
            from read_parquet('{fare_records}')
            where fare is not null and fare > 0
            group by ticket_code
        ),
        classified as (
            select c.ticket_code, c.description, c.tkt_type, c.tkt_class, c.tkt_group,
                   c.validity_code, c.max_passengers, c.min_passengers,
                   -- Indexes into DIS with the railcard's passenger status.
                   c.discount_category,
                   c.restricted_by_date, c.restricted_by_train,
                   -- Structural flags the feed sets itself. Both were unread
                   -- until an audit went looking, and each catches a family
                   -- the description markers could not.
                   c.reservation_required, c.package_mkr,
                   c.ticket_code in (select ticket_code from advance_codes) as is_advance,
                   c.ticket_code in (select ticket_code from flexi_product)
                       as tkt_code_is_flexi_bundle,
                   coalesce(s.flow_count, 0) as flow_count,
                   s.modal_share,
                   coalesce(
                       s.flow_count >= {_FLAT_RATE_MIN_FLOWS}
                       and s.modal_share >= {_FLAT_RATE_THRESHOLD},
                       false
                   ) as is_flat_rate,
                   coalesce(r.priced > 0 and r.on_booked_train = r.priced, false)
                       as every_fare_on_a_booked_train
            from current_records c
            left join fare_spread s using (ticket_code)
            left join restriction_bound r using (ticket_code)
            where c.rn = 1
        ),
        -- One definition of "a real adult fare to somewhere", then split into
        -- the two things you can actually buy. Used by the rejects table and
        -- the fare query alike.
        sellable as (
            select *,
                   not (
                       tkt_group = '{NOT_FOR_TRAVEL_GROUP}'
                       -- One price on every flow is a promotional or child
                       -- product, not a distance-based fare. This is also what
                       -- separates the real Advance ladders from the "SALE
                       -- ADVANCE" placeholders priced at a flat 50p. It cannot
                       -- judge a type with only one flow, where the modal share
                       -- is trivially 1.0 - hence the markers below.
                       or is_flat_rate
                       -- Family and group products price several people at
                       -- once, so they undercut the adult fare without being one.
                       or coalesce(max_passengers, 1) > 1
                       -- A carnet's price buys a bundle of journeys, not one.
                       -- Only RSPS5052 says which tickets those are.
                       or tkt_code_is_flexi_bundle
                       -- A package price buys travel plus parking, admission
                       -- or a bus zone. The feed says so outright, and `TPK`
                       -- names the same 45 codes independently.
                       or coalesce(package_mkr, '{NOT_A_PACKAGE}')
                           <> '{NOT_A_PACKAGE}'
                       or ({markers})
                   ) as is_sellable
            from classified
        ),
        -- A fare valid only on the train it was booked on is not a walk-up
        -- fare, whatever it calls itself. TVL says so directly: six validity
        -- codes read "BOOKDTRAINONLY", and the types on them are Advance
        -- products by another name - `SF3 SUPERFARE`, every `LumoFixed` and
        -- `FIXED SINGLE`, Avanti's Standard Premium ladders, the sleeper's Solo
        -- and Club berths. Superfare was quoting £9.00 Euston to Birmingham
        -- against a retailer's cheapest Advance of £31.00.
        --
        -- They are reclassified, not discarded: `--advance` still offers them,
        -- with the same caveat as every Advance - the price is real and the
        -- availability is not in the feed.
        -- Two ways a fare says "this train, not any train".
        --
        -- The validity record is the honest one - six codes read
        -- `BOOKDTRAINONLY` - and it catches Superfare, LumoFixed and the
        -- sleeper's berths.
        --
        -- **`70min Flex` says it nowhere near the validity.** LNER's Simpler
        -- Fares trial replaces Off-Peak and Anytime on the East Coast with
        -- Advance plus a semi-flexible ticket valid within 70 minutes of a
        -- booked departure, and its validity code `61` reads the entirely
        -- ordinary "ON DATE SHOWN". What gives it away is its restriction:
        -- `FL`, described **"LNER FLEX ON SET TIME"** - "VALID ON DATE ONLY
        -- WITH FLEX. LTD CHANGE. NO RFND." - carrying 696 bands that are
        -- two-minute windows at named stations, which is a list of particular
        -- trains written as times.
        --
        -- So the 52 codes are matched by name. `%MIN FLEX%` hits those four
        -- descriptions and nothing else in the feed; `%FLEX%` would still be
        -- the wrong marker, because "FLEXI ADVANCE" is a real changeable
        -- Advance single. What this used to give as the reason - that
        -- `Day-Flex` is a real £5.50 walk-up - was simply wrong, and
        -- `NO_RESERVATION` now catches it.
        --
        -- The third way, and the only one that is a plain statement of fact
        -- rather than an inference from prose: the ticket type says a
        -- reservation is required. See `NO_RESERVATION`.
        booked_only as (
            select s.*,
                   coalesce(v.booked_train_only, false) as booked_train_only,
                   coalesce(s.reservation_required, '{NO_RESERVATION}')
                       <> '{NO_RESERVATION}' as needs_reservation
            from sellable s
            left join (
                select validity_code,
                       upper(coalesce(out_description, '')) like '%BOOKDTRAIN%'
                    or upper(coalesce(description, '')) like '%BOOKEDTRAIN%'
                       as booked_train_only,
                       row_number() over (
                           partition by validity_code order by start_date desc
                       ) as rn
                from read_parquet('{validity}')
                where current_date between start_date and end_date
            ) v on v.validity_code = s.validity_code and v.rn = 1
        )
        select * exclude (booked_train_only, every_fare_on_a_booked_train,
                          needs_reservation),
               -- Advance prices are real and distance-varying; what the feed
               -- does not carry is whether a given price point is on sale for a
               -- given train, which lives in the reservation system.
               is_sellable
                   and (is_advance or booked_train_only
                        or every_fare_on_a_booked_train
                        or needs_reservation
                        or upper(description) like '%ADVANCE%')
                   as is_advance_fare,
               is_sellable
                   and not (is_advance or booked_train_only
                            or every_fare_on_a_booked_train
                            or needs_reservation
                            or upper(description) like '%ADVANCE%')
                   as is_walk_up
        from booked_only
    """)

    # Excluded ticket types are recorded, not silently dropped: the feed really
    # does ship products described "FOR TEST USE ONLY" and "NOT FOR TRAVEL".
    connection.execute(f"""
        create or replace table fare_reject as
        select ticket_code, description,
               case
                   when tkt_group = 'E' then 'not for travel'
                   when tkt_code_is_flexi_bundle
                       then 'flexi bundle, priced for several journeys'
                   when coalesce(package_mkr, '{NOT_A_PACKAGE}')
                        <> '{NOT_A_PACKAGE}'
                       then 'package, priced with parking or admission'
                   {_marker_reason_sql()}
                   when coalesce(max_passengers, 1) > 1 then 'family or group product'
                   when is_flat_rate then 'flat rate, not a distance-based fare'
               end as reason
        from ticket_type_current
        where not is_sellable
    """)

    scalar = lambda sql: connection.execute(sql).fetchone()[0]
    return FaresCounts(
        aliases=scalar("select count(*) from fare_alias"),
        ticket_types=scalar("select count(*) from ticket_type_current"),
        walk_up=scalar("select count(*) from ticket_type_current where is_walk_up"),
        rejected=connection.execute(
            "select reason, count(*) from fare_reject group by 1 order by 2 desc"
        ).fetchall(),
    )


#: RSPS5045 4.4.3 fields 12 and 13: an 8-character fare of 99999999 means no
#: fare is available for the ticket/railcard combination, not a price.
_NO_FARE = 99999999

#: Flow fares for one origin, expanded out to destination stations, with
#: non-derivable fares (NFO) overriding them on the same
#: origin/destination/route/ticket. Adult, no railcard.
_PRICING_CTES = f"""
with origin_codes as (
    select distinct code from fare_alias where crs = $origin
),
flows as (
    select flow_id, destination_code as other_code, route_code, ns_disc_ind
    from read_parquet($flow_path)
    where origin_code in (select code from origin_codes)
      and $travel_date between start_date and end_date
    union all
    -- "R" means the flow may be used in either direction.
    select flow_id, origin_code, route_code, ns_disc_ind
    from read_parquet($flow_path)
    where direction = 'R'
      and destination_code in (select code from origin_codes)
      and $travel_date between start_date and end_date
),
flow_fares as (
    select a.crs as dest_crs, f.other_code, f.route_code, f.ns_disc_ind,
           r.ticket_code, r.fare, r.restriction_code, 'flow' as source
    from flows f
    join read_parquet($fare_path) r using (flow_id)
    join fare_alias a on a.code = f.other_code
    where r.fare is not null and r.fare > 0
),
-- Non-derivable fares are stated directly against a code pair and override the
-- flow-derived price.
--
-- Two conventions from RSPS5045 4.4.3, both easy to read backwards:
--
-- * composite_indicator 'Y' means *use* this record; 'N' means do not, because
--   the fare is already in the flow file. Every record in RJFAF833 is 'Y', so
--   the filter is currently a no-op - but inverting it would silently discard
--   all 249,917 of them.
-- * 99999999 is not a price. It means no adult fare is available for this
--   ticket/railcard combination, which is exactly why the record still has to
--   take part: it withdraws the flow fare it overrides. 56,763 non-railcard
--   records say this. suppress_mkr is obsolete and always 'N'.
non_derivable_all as (
    select a.crs as dest_crs, n.destination_code as other_code, n.route_code,
           -- A non-derivable fare is stated outright and takes no discount, so
           -- non-standard discounts have nothing to act on.
           null::integer as ns_disc_ind,
           n.ticket_code,
           case when n.adult_fare >= {_NO_FARE} then null else n.adult_fare end
               as fare,
           n.restriction_code, 'ndf' as source,
           n.railcard_code is not null as is_railcard_fare
    from read_parquet($ndf_path) n
    join fare_alias a on a.code = n.destination_code
    where n.origin_code in (select code from origin_codes)
      and n.composite_indicator = 'Y'
      -- The generic adult fare, plus any stated for this railcard: those are
      -- already-discounted prices and take no further percentage off.
      and (n.railcard_code is null or n.railcard_code = $railcard)
      and $travel_date between n.start_date and n.end_date
),
non_derivable as (
    select * exclude (rn) from (
        select *, row_number() over (
            partition by dest_crs, other_code, ticket_code, route_code
            order by is_railcard_fare desc
        ) as rn
        from non_derivable_all
    ) where rn = 1
),
combined as (
    select * from non_derivable
    union all
    select f.*, false as is_railcard_fare from flow_fares f
    where not exists (
        select 1 from non_derivable n
        where n.dest_crs = f.dest_crs
          and n.other_code = f.other_code
          and n.ticket_code = f.ticket_code
          and coalesce(n.route_code, '') = coalesce(f.route_code, '')
    )
),
sellable as (
    select c.dest_crs, c.other_code, c.ns_disc_ind,
           c.ticket_code, c.fare, c.route_code, c.restriction_code,
           t.description, t.tkt_type, t.tkt_class, t.validity_code,
           t.discount_category, c.is_railcard_fare, t.is_advance_fare,
           j.minutes as arrival_minutes
    from combined c
    join ticket_type_current t using (ticket_code)
    left join ticket_validity_current v on v.validity_code = t.validity_code
    -- Joined here rather than inside the EXISTS below: DuckDB cannot correlate
    -- an outer join within a subquery.
    left join journey_arrival j on j.crs = c.dest_crs
    where c.fare is not null
      -- Advance prices are real, but whether one is on sale for a given train
      -- is not in this feed, so they are opt-in.
      --
      -- Three states, not two: walk-up only (the default), both, or Advance
      -- alone. The third is for a caller asking "what is the cheapest Advance",
      -- which is a different question from "what is the cheapest fare" - and
      -- without it that caller has to price every walk-up and discard it.
      and (($advance_only and t.is_advance_fare)
           or (not $advance_only
               and (t.is_walk_up or ($include_advance and t.is_advance_fare))))
      and ($ticket_class is null or t.tkt_class = $ticket_class)
      -- S single, R return. N is a season ticket and is priced differently.
      and t.tkt_type in ('S', 'R')
      and ($include_returns or t.tkt_type = 'S')
      -- A journey deliberately broken needs a ticket that allows it. TVL field
      -- 12 governs the outward leg and field 13 the return, and they differ:
      -- 651 of the 1,379 walk-up ticket types bar a break outward, 32 of the
      -- 444 walk-up returns bar one on the way home. A validity the feed says
      -- nothing about is not assumed permissive either way - silence is not
      -- permission when the question is whether you may stop off.
      and (not $break_of_journey or coalesce(v.break_out, false))
      and (not $break_returning or coalesce(v.break_in, false))
      -- A restriction band names a time window in which the fare may not be
      -- used. Bands at the origin (departing) and destination (arriving) can be
      -- judged from a journey time; ones naming another station cannot, and are
      -- reported by `rail build` rather than silently applied.
      -- A route condition names stations the journey must or must not pass
      -- through, checked against the path the router actually took.
      --
      -- The routeing guide's own RGK file states these properly and is used
      -- first: ALL-of ('A') and ANY-of ('I') are distinct senses, an excluded
      -- station can stand for its whole routeing group, and London is a marker
      -- on the route rather than a couple of terminals in a location list.
      -- The fares feed's RTE records, which have none of that, are the fallback
      -- for the routes RGK is silent on.
      --
      -- 'E' - none of these may appear on the journey.
      and not exists (
          select 1 from route_rule r
          join journey_path p
            on p.crs = c.dest_crs and p.via_crs = r.crs
          where r.route_code = c.route_code and r.entry_type = 'E'
      )
      -- 'A' - every one of these must appear.
      and (
          not $check_routes
          or not exists (
              select 1 from route_rule r
              where r.route_code = c.route_code and r.entry_type = 'A'
                and not exists (
                    select 1 from journey_path p
                    where p.crs = c.dest_crs and p.via_crs = r.crs
                )
          )
      )
      -- 'I' - at least one of these must appear.
      and (
          not $check_routes
          or not exists (
              select 1 from route_rule r
              where r.route_code = c.route_code and r.entry_type = 'I'
          )
          or exists (
              select 1 from route_rule r
              join journey_path p
                on p.crs = c.dest_crs and p.via_crs = r.crs
              where r.route_code = c.route_code and r.entry_type = 'I'
          )
      )
      -- The London marker: '0' the route excludes London, '1' it must include
      -- London. '2' (may include) and '3' (does not mention it) constrain
      -- nothing on their own - for '2' the alternative is carried as an 'I'.
      and (
          not $check_routes
          or not exists (
              select 1 from route_london l
              where l.route_code = c.route_code
                and l.london_marker in ('0', '1')
                and (l.london_marker = '1') <> exists (
                    select 1 from journey_path p
                    join london_station s on s.crs = p.via_crs
                    where p.crs = c.dest_crs and s.is_terminal
                )
          )
      )
      -- 'T' - at least one of these operators must be used, and 'X' - none of
      -- them may be. The router now records the operator of every leg, so these
      -- are testable: route 00085 "TPE ONLY" is `T:TP` plus `X` on 25 others,
      -- and York to Newcastle was offering its £28.20 against an LNER train.
      and (
          not $check_routes
          or not exists (
              select 1 from route_condition r
              where r.route_code = c.route_code and r.entry_type = 'T'
          )
          -- No operators recorded for this journey means the question cannot be
          -- answered, not that the answer is no. Without this guard, supplying
          -- paths alone silently refused every fare on a TOC-restricted route.
          or not exists (
              select 1 from journey_operator o where o.crs = c.dest_crs
          )
          or exists (
              select 1 from route_condition r
              join journey_operator o on o.crs = c.dest_crs and o.toc = r.toc_id
              where r.route_code = c.route_code and r.entry_type = 'T'
          )
      )
      and not exists (
          select 1 from route_condition r
          join journey_operator o on o.crs = c.dest_crs and o.toc = r.toc_id
          where r.route_code = c.route_code and r.entry_type = 'X'
      )
      -- 'L' - at least one leg must use this transport mode, and 'N' - none
      -- may. 95 routes say so: route 00002 requires an Underground leg. Modes
      -- are numbered as RSPS5047 4.12.3 numbers them, so a train is 0 and a
      -- fixed link carries whichever ALF or FLF gave it.
      and (
          not $check_routes
          or not exists (
              select 1 from route_condition r
              where r.route_code = c.route_code and r.entry_type = 'L'
          )
          or not exists (
              select 1 from journey_mode m where m.crs = c.dest_crs
          )
          or exists (
              select 1 from route_condition r
              join journey_mode m on m.crs = c.dest_crs and m.mode = r.mode_code
              where r.route_code = c.route_code and r.entry_type = 'L'
          )
      )
      and not exists (
          select 1 from route_condition r
          join journey_mode m on m.crs = c.dest_crs and m.mode = r.mode_code
          where r.route_code = c.route_code and r.entry_type = 'N'
      )
      -- RTE, only where RGK says nothing about this route.
      and not exists (
          select 1 from route_via v
          join journey_path p
            on p.crs = c.dest_crs and p.via_crs = v.crs
          where v.route_code = c.route_code and v.incl_excl = 'E'
            and v.route_code not in (select route_code from route_rgk_covered)
      )
      and (
          not $check_routes
          or c.route_code in (select route_code from route_rgk_covered)
          or not exists (
              select 1 from route_via v
              where v.route_code = c.route_code and v.incl_excl = 'I'
          )
          or exists (
              select 1 from route_via v
              join journey_path p
                on p.crs = c.dest_crs and p.via_crs = v.crs
              where v.route_code = c.route_code and v.incl_excl = 'I'
          )
      )
      -- RSPS5045 4.19.3 field 10: the restriction header says whether a change
      -- of trains is allowed at all, and 36 of the 839 current restrictions say
      -- it is not - the Avanti "Valid on booked service only" fares, TfW's
      -- Advance Flex, LNER's "DIRECT LNER TRN". The bands cannot express this;
      -- it is a property of the whole restriction.
      --
      -- Only tested where the journey has been routed. A destination absent
      -- from `journey_changes` gives no verdict rather than a refusal, the same
      -- guard the TOC and return-leg conditions needed.
      and not exists (
          select 1
          from restriction_current rc
          join journey_changes jc on jc.crs = c.dest_crs
          where rc.restriction_code = c.restriction_code
            and rc.cf_mkr = $marker
            and not rc.change_allowed
            and jc.changes > 0
      )
      and not exists (
          select 1
          from applicable_band b
          where b.restriction_code = c.restriction_code
            and not b.min_fare_flag
            and (
                -- The outward leg. RSPS5045 4.19.8 field 10: three spaces means
                -- the band is not station specific, so it bites at whichever end
                -- of the journey its arrive/depart marker names. Requiring a
                -- station dropped restriction 3V entirely - "VALID ON ANY TRAIN
                -- 0930 OR LATER M-F" - and York to Newcastle offered its £30.10
                -- Off-Peak Single on the 09:06, which no retailer will sell.
                (b.out_ret = 'O' and (
                    (b.arr_dep_via = 'D'
                     and (b.location is null or b.location = $origin)
                     and $depart_minutes between b.time_from and b.time_to)
                    or
                    (b.arr_dep_via = 'A'
                     and (b.location is null or b.location = c.dest_crs)
                     and j.minutes % 1440 between b.time_from and b.time_to)
                    or
                    -- **`V` is the only marker that means a station in the
                    -- middle**, and RSPS5045 4.19.8 says so twice: field 9 is
                    -- "arrivals at, departures from or *changing at* the
                    -- location", and field 10 is "a location denoting a journey
                    -- origin/destination or via location".
                    --
                    -- So an `A` or `D` band naming a station is about a journey
                    -- that *ends or starts* there, not one passing through, and
                    -- the two clauses above are right to test only the ends.
                    -- Restriction `LK` proves it: its band 0018 bars departing
                    -- Euston before 10:29 while band 0006 bars departing
                    -- Leighton Buzzard before 12:33, and one train cannot
                    -- satisfy both - they are per-origin rules, not a way of
                    -- naming trains.
                    --
                    -- Three `V` bands are in force and only one is outward
                    -- (`PB`, changing at Farringdon before 10:55), so this
                    -- earns almost nothing today. It is here because it is the
                    -- reading the spec gives, and because getting it wrong the
                    -- other way made 1,648 fares dearer than any retailer sells.
                    --
                    -- `journey_call` is empty when the caller supplies no
                    -- calling times, and then nothing here bites - the same
                    -- guard the return leg and the TOC conditions use.
                    (b.arr_dep_via = 'V' and exists (
                        select 1 from journey_call k
                        where k.crs = c.dest_crs
                          and k.via_crs = b.location
                          and k.is_change
                          and k.arrive % 1440 between b.time_from and b.time_to
                    ))
                ))
                or
                -- The return leg, which runs the other way: a departure band
                -- bites where the journey home starts, which is the outward
                -- *destination*, and an arrival band bites back at the origin.
                -- Getting those two the wrong way round would silently apply
                -- London's morning arrival bans to a journey leaving London.
                --
                -- Only evaluated when the caller has routed the way back and
                -- supplied its times; -1 means it has not, and no band bites.
                (b.out_ret = 'R' and (
                    (b.arr_dep_via = 'D'
                     and (b.location is null or b.location = c.dest_crs)
                     and $return_depart_minutes between b.time_from and b.time_to)
                    or
                    (b.arr_dep_via = 'A'
                     and (b.location is null or b.location = $origin)
                     and $return_arrival_minutes between b.time_from and b.time_to)
                ))
            )
      )
),
-- RSPS5045 4.5: a flow whose ns_disc_ind is 1 or 3 does not take the standard
-- percentage. FNS says what happens instead, keyed on origin, destination,
-- route, railcard and ticket, any of which may be a wildcard - and an explicit
-- record beats a wildcard one, so the match is ranked by how specific it is.
--
-- Only relevant while discounting: 4.5.1.1 is explicit that the file is not
-- used for undiscounted adult fares, which are in the flow file already.
non_standard as (
    select * exclude (rn) from (
        select s.dest_crs, s.ticket_code, s.route_code,
               n.adult_nodis_flag, n.adult_add_on_amount, n.adult_rebook_flag,
               row_number() over (
                   partition by s.dest_crs, s.ticket_code, s.route_code
                   order by (n.origin_code <> '****') desc,
                            (n.destination_code <> '****') desc,
                            (n.route_code <> '*****') desc,
                            (n.railcard_code <> '***') desc,
                            (n.ticket_code <> '***') desc
               ) as rn
        from sellable s
        join no_standard_discount n
          on (n.origin_code = '****' or n.origin_code in (select code from origin_codes))
         and (n.destination_code = '****' or n.destination_code = s.other_code)
         and (n.route_code = '*****' or n.route_code = s.route_code)
         and (n.ticket_code = '***' or n.ticket_code = s.ticket_code)
         -- Three spaces means "no railcard needed", which is the child-fare
         -- case, not this one.
         and (n.railcard_code = '***' or n.railcard_code = $railcard)
        where $railcard is not null and s.ns_disc_ind in (1, 3)
    ) where rn = 1
),
-- The discount before rounding, kept separate so the FRR band can be found
-- from it: RSPS5045 4.18.1.1 says the *discounted* fare selects the band.
priced as (
    select s.*, d.discount_percentage, m.minimum_fare,
           n.adult_nodis_flag, n.adult_rebook_flag, n.adult_add_on_amount,
           s.fare * (1000 - coalesce(d.discount_percentage, 0)) / 1000.0
               as before_rounding,
           -- An area-restricted railcard is only valid within its own
           -- geography, and the Network Railcard and Annual Gold Card have
           -- different ones. Both ends must be inside it. A railcard flagged
           -- restricted but carrying no geography is left alone: that is not
           -- knowing the area, not knowing it is empty.
           coalesce(not rc.restricted_by_area, true)
           or not exists (
               select 1 from railcard_geography g where g.railcard_code = $railcard
           )
           or (exists (
                   select 1 from railcard_geography g
                   where g.railcard_code = $railcard and g.crs = $origin
               ) and exists (
                   select 1 from railcard_geography g
                   where g.railcard_code = $railcard and g.crs = s.dest_crs
               )) as valid_here,
           -- A railcard may be banned outright for a ticket, a route or an
           -- origin: 1,715 ticket bans and 103 route bans for the Network
           -- Railcard and Gold Card alone. A record naming none of the three
           -- would ban everything, so it is ignored rather than obeyed.
           not exists (
               select 1 from railcard_ban b
               where b.railcard_code = $railcard
                 and coalesce(b.ticket_code, b.route_code, b.location) is not null
                 and (b.ticket_code is null or b.ticket_code = s.ticket_code)
                 and (b.route_code is null or b.route_code = s.route_code)
                 and (b.location is null or b.location = $origin)
           ) as railcard_allowed,
           -- The railcard's own restriction, on top of the fare's. Judged like
           -- any other band: origin departures and destination arrivals only.
           not exists (
               select 1
               from railcard_restriction rr
               join applicable_band rb on rb.restriction_code = rr.restriction_code
               where rr.railcard_code = $railcard
                 and (rr.ticket_code is null or rr.ticket_code = s.ticket_code)
                 and (rr.route_code is null or rr.route_code = s.route_code)
                 and rb.out_ret = 'O'
                 -- 'Y' means a minimum fare, not a refusal. RN's spans the
                 -- whole day, so reading it as a bar withdraws the Network
                 -- Railcard outright.
                 and not rb.min_fare_flag
                 and (
                     (rb.arr_dep_via = 'D'
                      and (rb.location is null or rb.location = $origin)
                      and $depart_minutes between rb.time_from and rb.time_to)
                     or
                     (rb.arr_dep_via = 'A'
                      and (rb.location is null or rb.location = s.dest_crs)
                      and s.arrival_minutes % 1440
                          between rb.time_from and rb.time_to)
                 )
           ) as railcard_in_time,
           -- RSPS5045 4.16.1.1: a railcard minimum fare applies "when railcards
           -- are used on certain trains (determined by the train restriction)",
           -- and the restriction says which by setting min_fare_flag on a band.
           -- The 16-25's R1 is one band, 04:30-09:59 Mon-Fri: the £12 minimum
           -- before ten. Charging it all day would overprice every off-peak
           -- journey on a ticket that has a minimum at all.
           exists (
               select 1
               from railcard_restriction rr
               join applicable_band rb on rb.restriction_code = rr.restriction_code
               where rr.railcard_code = $railcard
                 and (rr.ticket_code is null or rr.ticket_code = s.ticket_code)
                 and (rr.route_code is null or rr.route_code = s.route_code)
                 and rb.out_ret = 'O'
                 and rb.min_fare_flag
                 and (
                     (rb.arr_dep_via = 'D'
                      and (rb.location is null or rb.location = $origin)
                      and $depart_minutes between rb.time_from and rb.time_to)
                     or
                     (rb.arr_dep_via = 'A'
                      and (rb.location is null or rb.location = s.dest_crs)
                      and s.arrival_minutes % 1440
                          between rb.time_from and rb.time_to)
                 )
           ) as minimum_fare_applies
    from sellable s
    left join railcard_current rc on rc.railcard_code = $railcard
    left join railcard_discount d
      on d.railcard_code = $railcard and d.discount_category = s.discount_category
    left join railcard_minimum m
      on m.railcard_code = $railcard and m.ticket_code = s.ticket_code
    left join non_standard n
      on n.dest_crs = s.dest_crs and n.ticket_code = s.ticket_code
     and coalesce(n.route_code, '') = coalesce(s.route_code, '')
),
discounted as (
    select p.dest_crs, p.ticket_code, p.description, p.route_code,
           p.other_code, p.ns_disc_ind,
           p.restriction_code, p.tkt_type, p.tkt_class, p.validity_code,
           p.is_advance_fare, p.fare as undiscounted,
           case
               -- No railcard, or a fare already stated for this railcard.
               when $railcard is null or p.is_railcard_fare then p.fare
               -- 'X' no adult fare at all; 'D' no *discounted* adult fare, so
               -- the undiscounted price stands; a rebook flag of 'Y' or 'S'
               -- means the customer must be sent to book it another way.
               when p.adult_nodis_flag = 'X' then null
               when p.adult_rebook_flag in ('Y', 'S') then null
               when p.adult_nodis_flag = 'D' then p.fare
               -- The ticket type attracts no discount for this status.
               when p.discount_percentage is null then p.fare
               -- The railcard is not valid at both ends of this journey.
               when not p.valid_here then p.fare
               -- Banned outright for this ticket or route, or barred at this
               -- hour by the railcard's own restriction.
               when not p.railcard_allowed then p.fare
               when not p.railcard_in_time then p.fare
               else least(
                   p.fare,
                   greatest(
                       -- Per mille, then *down* to the band's rounding amount.
                       floor(p.before_rounding / b.round_to) * b.round_to,
                       case when p.minimum_fare_applies
                            then coalesce(p.minimum_fare, 0) else 0 end
                   )
                   -- The add-on is charged on top and takes no discount: it is
                   -- the part of the journey the railcard does not cover.
                   + coalesce(p.adult_add_on_amount, 0)
               )
           end::bigint as fare
    from priced p
    -- The first band whose ceiling the discounted fare reaches. Rule 01 is
    -- 5p throughout so this is uniform today, but a banded rule would show
    -- itself on a large fare.
    left join lateral (
        select round_to from rounding_band
        where upper_limit >= p.before_rounding
        order by upper_limit limit 1
    ) b on true
)
"""

#: The cheapest price per destination, over the shared CTEs above.
_CHEAPEST_SQL = _PRICING_CTES + """
-- One row per distinct price a destination can be reached at, cheapest
-- first. `cheapest_from` keeps the first of each group; `fare_options`
-- hands the rest to a caller that has a reason to reject the cheapest.
-- `fare` is in the GROUP BY, so it is constant within a group and cannot
-- break a tie: `min_by(ticket_code, fare)` picks arbitrarily among every
-- ticket at that price. Two ticket types genuinely tie often - a fare is a
-- price, and several products can sell it - and with parallel aggregation
-- the arbitrary choice is not even stable between two runs on one database.
-- Building the same origin twice produced payloads naming different tickets
-- at identical prices. So the ordering key is (ticket_code, route_code),
-- which is deterministic and picks the same winner every time.
select dest_crs,
       min_by(ticket_code, (ticket_code, route_code)) as ticket_code,
       min_by(description, (ticket_code, route_code)) as description,
       fare,
       min_by(is_advance_fare, (ticket_code, route_code)) as is_advance,
       -- The route the fare is priced on, which is what settles an easement
       -- whose condition is "customers with tickets routed X".
       min_by(route_code, (ticket_code, route_code)) as route_code,
       -- TTY field 9: 'S' single, 'R' return, 'N' season. A return sometimes
       -- undercuts two singles and wins here, so the caller has to be able to
       -- say what it quoted.
       min_by(tkt_type, (ticket_code, route_code)) as tkt_type
from discounted
where dest_crs <> $origin and fare is not null
group by dest_crs, fare
order by dest_crs, fare
"""


_BAND_COLUMNS = (
    "restriction_code", "out_ret", "time_from", "time_to", "arr_dep_via",
    "location", "min_fare_flag",
)


def _register_journey_tables(
    connection, *, bands, arrivals, paths, operators=None, modes=None,
    changes=None, calls=None,
) -> None:
    """The three per-query tables the shared pricing CTEs join to.

    Registered rather than passed as parameters because they are sets, not
    scalars. Empty is meaningful: no restriction bands means no time filtering,
    no paths means no route conditions.
    """
    connection.register("applicable_band", pa.table(
        {
            name: pa.array(
                [row[index] for row in bands],
                type=pa.int32() if name in ("time_from", "time_to")
                else pa.bool_() if name == "min_fare_flag"
                else pa.string(),
            )
            for index, name in enumerate(_BAND_COLUMNS)
        }
    ))
    connection.register("journey_arrival", pa.table({
        "crs": pa.array(list(arrivals), type=pa.string()),
        "minutes": pa.array(list(arrivals.values()), type=pa.int32()),
    }))
    # One row per (destination, calling point, when the journey was there).
    # Separate from `journey_path` because that answers "did it pass here" and
    # this answers "at what time" - a route condition needs the first, a
    # restriction band naming an intermediate station needs the second.
    timed = [(dest, via, arrive, depart, changed)
             for dest, stops in (calls or {}).items()
             for via, arrive, depart, changed in stops]
    connection.register("journey_call", pa.table({
        "crs": pa.array([d for d, *_ in timed], type=pa.string()),
        "via_crs": pa.array([v for _d, v, *_ in timed], type=pa.string()),
        "arrive": pa.array([a for _d, _v, a, _x, _c in timed], type=pa.int32()),
        "depart": pa.array([x for _d, _v, _a, x, _c in timed], type=pa.int32()),
        "is_change": pa.array([c for *_, c in timed], type=pa.bool_()),
    }))
    # One row per (destination, station passed through on the way there).
    pairs = [(dest, via) for dest, route in paths.items() for via in route]
    connection.register("journey_path", pa.table({
        "crs": pa.array([d for d, _ in pairs], type=pa.string()),
        "via_crs": pa.array([v for _, v in pairs], type=pa.string()),
    }))
    # One row per (destination, operator whose train the journey there uses).
    used = [(dest, toc) for dest, tocs in (operators or {}).items() for toc in tocs]
    connection.register("journey_operator", pa.table({
        "crs": pa.array([d for d, _ in used], type=pa.string()),
        "toc": pa.array([o for _, o in used], type=pa.string()),
    }))
    # One row per destination reached, with how many times the journey changes
    # train. Absent means the caller has not routed anything, and a restriction
    # barring a change then gives no verdict rather than refusing.
    counted = changes or {}
    connection.register("journey_changes", pa.table({
        "crs": pa.array(list(counted), type=pa.string()),
        "changes": pa.array(list(counted.values()), type=pa.int32()),
    }))
    # One row per (destination, transport mode the journey there uses).
    travelled = [(dest, m) for dest, ms in (modes or {}).items() for m in ms]
    connection.register("journey_mode", pa.table({
        "crs": pa.array([d for d, _ in travelled], type=pa.string()),
        "mode": pa.array([m for _, m in travelled], type=pa.string()),
    }))


def _unregister_journey_tables(connection) -> None:
    for name in ("applicable_band", "journey_arrival", "journey_path",
                 "journey_call", "journey_operator", "journey_mode",
                 "journey_changes"):
        connection.unregister(name)


def fare_options(
    connection: duckdb.DuckDBPyConnection,
    fares_dir: Path,
    origin: str,
    travel_date: dt.date,
    *,
    ticket_class: int = STANDARD_CLASS,
    include_returns: bool = True,
    depart_minutes: int | None = None,
    arrivals: dict[str, int] | None = None,
    railcard: str | None = None,
    paths: dict[str, list[str]] | None = None,
    operators: dict[str, set[str]] | None = None,
    modes: dict[str, set[str]] | None = None,
    include_advance: bool = False,
    advance_only: bool = False,
    break_of_journey: bool = False,
    break_returning: bool = False,
    return_on: dt.date | None = None,
    return_depart_minutes: int | None = None,
    return_arrival_minutes: int | None = None,
    changes: dict[str, int] | None = None,
    calls: dict[str, list[tuple[str, int, int, bool]]] | None = None,
) -> list[tuple[str, str, str, int, bool, str | None, str]]:
    """Every price each reachable station can be reached at, cheapest first.

    Returns (destination CRS, ticket code, description, pence, is_advance,
    route code, ticket type), ordered by destination then price. One row per
    distinct price: where several tickets cost the same the cheapest-named one
    stands for them. The ticket type is `S`, `R` or `N` - a return can undercut
    two singles and win, so a caller quoting the price has to be able to say
    which it is.

    Most callers want :func:`cheapest_from`, which keeps the first of each
    group. This exists for the caller that has a reason to reject the cheapest
    - chiefly the routeing guide, which may refuse the cheapest fare on the
    journey actually found while permitting a dearer one.

    `include_advance` adds Advance price points. Their prices are real and vary
    with distance, but the feed carries no quota, so nothing here says whether a
    given price point is actually on sale for a given train. Treat the result as
    the best published price, not as a bookable one.

    `advance_only` returns Advance fares *instead of* walk-ups rather than as
    well as, for the caller asking what the cheapest Advance is rather than what
    the cheapest fare is. It implies `include_advance` and overrides it. The
    same quota caveat applies with more force: a walk-up price is buyable by
    definition and an Advance price is not, so an answer made only of Advances
    is entirely made of prices that may not be on sale.

    Pass `depart_minutes` - and, for arrival-side restrictions, `arrivals`
    mapping destination CRS to arrival time - to exclude fares that are not
    valid at the time the journey is actually made. Without it, an Off-Peak
    fare will be quoted for a peak departure.

    Pass `paths` (destination CRS to the stations passed through) to apply route
    conditions, so a "NOT VIA LONDON" fare is not offered for a journey that
    goes through London. Only routes carrying location records can be checked;
    the rest state their condition in prose.

    `return_on` drops return fares whose validity does not permit travelling
    back on that date. Singles are kept: a single is not made invalid by the
    question, and two of them answer it perfectly well - often more cheaply.

    `return_depart_minutes` and `return_arrival_minutes` describe the journey
    home - when you leave the destination and when you get back. Supply both to
    evaluate the return-leg restriction bands, which are 13,803 of the bands in
    force on a weekday and otherwise go unapplied. They need the way back to
    have been routed, so a one-to-all sweep leaves them out.

    `changes` maps destination CRS to how many times the journey there changes
    train, from `ScanResult.changes()`. Supply it to enforce the 36 restrictions
    that bar a change outright. A destination absent from it gives no verdict,
    so not routing is not a refusal.
    """
    from .restrictions import marker_for
    from .returns import returnable_on

    restrict = depart_minutes is not None
    bands = applicable_bands(connection, travel_date) if restrict else []
    arrivals = arrivals or {}
    paths = paths or {}
    operators = operators or {}
    modes = modes or {}

    _register_journey_tables(connection, bands=bands, arrivals=arrivals,
                             paths=paths, operators=operators, modes=modes,
                             changes=changes, calls=calls)
    try:
        rows = connection.execute(
            _CHEAPEST_SQL,
            {
                "origin": origin,
                "travel_date": travel_date,
                "ticket_class": ticket_class,
                "include_returns": include_returns,
                "depart_minutes": depart_minutes if restrict else -1,
                "railcard": railcard,
                "include_advance": include_advance,
                "advance_only": advance_only,
                "check_routes": bool(paths),
                "break_of_journey": break_of_journey,
                "break_returning": break_returning,
                # -1 rather than null: the band test is a BETWEEN, so a
                # sentinel outside every band is what "not routed" means.
                "return_depart_minutes": (
                    return_depart_minutes if return_depart_minutes is not None else -1),
                "return_arrival_minutes": (
                    return_arrival_minutes if return_arrival_minutes is not None else -1),
                # The restriction header carries two versions like everything
                # else, so the change-of-trains test has to pick the right one.
                "marker": marker_for(connection, travel_date),
                "flow_path": (fares_dir / "flow.parquet").as_posix(),
                "fare_path": (fares_dir / "fare.parquet").as_posix(),
                "ndf_path": (fares_dir / "non_derivable_fare_override.parquet").as_posix(),
            },
        ).fetchall()
    finally:
        _unregister_journey_tables(connection)

    if return_on is None:
        return rows
    # Computed once for the whole sweep rather than per destination - the
    # window depends on the ticket and the outward date, not on where you go.
    permitted = returnable_on(connection, travel_date, return_on)
    return [row for row in rows if row[6] != RETURN_TYPE or row[1] in permitted]


def cheapest_from(
    connection: duckdb.DuckDBPyConnection,
    fares_dir: Path,
    origin: str,
    travel_date: dt.date,
    **options,
) -> list[tuple[str, str, str, int, bool, str | None, str]]:
    """The single cheapest fare to each reachable station, cheapest first.

    The same arguments as :func:`fare_options`, reduced to one row per
    destination. Ordered by price so the caller can take the head of the list.
    """
    cheapest: dict[str, tuple] = {}
    for row in fare_options(connection, fares_dir, origin, travel_date, **options):
        cheapest.setdefault(row[0], row)
    return sorted(cheapest.values(), key=lambda row: row[3])


#: Every fare between one pair, with the records that say when it may be used.
#: Same pricing as `fare_options`, without the grouping and joined out to the
#: descriptive tables - the route's own words, the restriction's, the validity
#: period, and whether a railcard touched the price.
_FARES_BETWEEN_SQL = _PRICING_CTES + """
-- `distinct`, because a station is named by several codes and a flow may exist
-- under more than one of them. Birmingham New Street is reached from Euston as
-- its own NLC `1127` and as cluster `T120`, and both flows carry `25Q` on route
-- 00474 at £26.50 - one fare, listed twice, which is what `rail fares` was
-- printing. Every column below is a property of the ticket, the route or the
-- restriction, so identical rows are identical fares.
--
-- Rows differing only in *price* are deliberately kept. Aston's own NLC prices
-- a Super Off-Peak Single at £62.60 and cluster `Q451` at £67.00, and RSPS5045
-- 4.2.2 ranks the two nowhere - it says only that a location's fares "may be
-- set using the Cluster NLC instead of this NLC". Collapsing them would be
-- inventing a precedence the feed does not state. `cheapest_from` takes the
-- lower, which is the fare a passenger would be sold; this command lists what
-- exists.
select distinct d.ticket_code,
       d.description,
       d.fare,
       d.undiscounted,
       d.tkt_type,
       d.tkt_class,
       d.is_advance_fare,
       d.route_code,
       r.description as route_description,
       d.restriction_code,
       h.description as restriction_description,
       h.desc_out as restriction_note,
       v.description as validity_description,
       v.out_days, v.out_months, v.break_out
from discounted d
left join (
    select route_code, description, row_number() over (
        partition by route_code order by start_date desc
    ) as rn
    from read_parquet($route_path)
    where $travel_date between start_date and end_date
) r on r.route_code = d.route_code and r.rn = 1
left join (
    select restriction_code, description, desc_out
    from read_parquet($restriction_header_path)
    where cf_mkr = $marker
) h on h.restriction_code = d.restriction_code
left join (
    select validity_code, description, out_days, out_months, break_out,
           row_number() over (
               partition by validity_code order by start_date desc
           ) as rn
    from read_parquet($validity_path)
    where $travel_date between start_date and end_date
) v on v.validity_code = d.validity_code and v.rn = 1
where d.dest_crs = $destination and d.fare is not null
order by d.fare
"""


def fares_between(
    connection: duckdb.DuckDBPyConnection,
    fares_dir: Path,
    origin: str,
    destination: str,
    travel_date: dt.date,
    *,
    ticket_class: int | None = None,
    include_returns: bool = True,
    railcard: str | None = None,
    include_advance: bool = False,
    advance_only: bool = False,
    return_on: dt.date | None = None,
) -> list[dict]:
    """Every fare from `origin` to `destination`, with what governs its use.

    Deliberately *unfiltered by time*: this answers "what fares exist and when
    may each be used", so a peak-barred fare belongs in the answer with its
    restriction explained, not removed from it. `cheapest_from` is the one that
    prices a journey.

    `ticket_class` of None returns both classes.

    Every row carries a ``return_window`` - None for a single, otherwise the
    dates the return leg may be travelled. `return_on` additionally drops
    returns that cannot come back on that date; singles are kept, because a
    single is not made invalid by the question, and two of them are a perfectly
    good answer to it.
    """
    from .restrictions import marker_for
    from .returns import return_windows

    marker = marker_for(connection, travel_date)
    # The shared CTEs reference these; this query judges no journey, so they
    # are empty rather than absent.
    _register_journey_tables(connection, bands=[], arrivals={}, paths={})
    try:
        rows = connection.execute(
            _FARES_BETWEEN_SQL,
            {
                "origin": origin,
                "destination": destination,
                "travel_date": travel_date,
                # A class filter of None means "both"; the SQL handles it.
                "ticket_class": ticket_class,
                "include_returns": include_returns,
                "depart_minutes": -1,
                "railcard": railcard,
                "include_advance": include_advance,
                "advance_only": advance_only,
                "check_routes": False,
                "break_of_journey": False,
                "break_returning": False,
                # `fares_between` judges no journey, so no band bites.
                "return_depart_minutes": -1,
                "return_arrival_minutes": -1,
                "marker": marker,
                "flow_path": (fares_dir / "flow.parquet").as_posix(),
                "fare_path": (fares_dir / "fare.parquet").as_posix(),
                "ndf_path": (fares_dir / "non_derivable_fare_override.parquet").as_posix(),
                "route_path": (fares_dir / "route.parquet").as_posix(),
                "validity_path": (fares_dir / "ticket_validity.parquet").as_posix(),
                "restriction_header_path": (
                fares_dir / "restriction_header.parquet"
                ).as_posix(),
            },
        )
        columns = [d[0] for d in rows.description]
        priced = [dict(zip(columns, row)) for row in rows.fetchall()]
    finally:
        _unregister_journey_tables(connection)

    windows = return_windows(connection, travel_date)
    for row in priced:
        row["return_window"] = windows.get(row["ticket_code"])
    if return_on is None:
        return priced
    return [
        row for row in priced
        if row["return_window"] is None or row["return_window"].covers(return_on)
    ]
