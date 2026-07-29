# UK rail schedules & fares

A query engine for GB rail timetables and fares, built on the Rail Delivery
Group DTD feeds. No live running data, no disruption, no seat availability —
these are the published future timetables and the published prices.

A few of the questions it answers:

```bash
# The best journey to every station in Britain, swept across the whole day
rail journey-times --from YRK --date 2026-08-04 --profile

# Everywhere reachable for £20 or less, with a railcard applied
rail reachable --from YRK --date 2026-08-01 --max-fare 20 --railcard YNG

# Every fare between a pair, and the route, restriction and validity governing each
rail fares --from YRK --to KGX

# A return, or two singles? Neither reliably wins
rail roundtrip --from YRK --to KGX --date 2026-08-04 --return-on 2026-08-06
```

Each prints a table, or JSON with `--json`. `rail fares` is the one that shows
most of what the engine knows — every fare for a pair, with the three records
that govern each (abridged here; the real table carries the validity too):

```
YORK to LONDON KINGS CROSS — 11 fares

  fare      ticket                class  type    route                restriction
  £70.70    G2S OFF-PEAK S        std    single  00049 GRAND CTRL ONLY  GZ GRAND CENTRAL OFF-PEAK
  £75.00    SSS SUPER OFFPEAK S   std    single  00000 ANY PERMITTED    1L IEC SUPER OFF-PK
  £116.20   G1S OFF-PEAK 1S       1st    single  00049 GRAND CTRL ONLY  GZ GRAND CENTRAL OFF-PEAK
  …
```

The £70.70 is cheaper than the £75.00 and valid on fewer trains — its route is
one operator only. That is the sort of thing the engine is for: not just the
price, but what you would be buying.

**An accredited journey planner or retailer is authoritative; this is not.**
See [docs/CAPABILITIES.md](docs/CAPABILITIES.md) for what it does well, what it
does with caveats, and what it cannot do at all.

## Where to start

| | |
|---|---|
| [docs/GETTING-STARTED.md](docs/GETTING-STARTED.md) | account, fetch, ingest, build — in full |
| [docs/CAPABILITIES.md](docs/CAPABILITIES.md) | what it can and cannot answer |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | how it works, for a reader of the code |
| [docs/DATA-SOURCES.md](docs/DATA-SOURCES.md) | every source, its licence, and the attribution you must use |
| [docs/INTERPRETING-THE-FEEDS.md](docs/INTERPRETING-THE-FEEDS.md) | the readings chosen where the formats are ambiguous |

## Rules of use

Read [docs/DATA-SOURCES.md](docs/DATA-SOURCES.md) before publishing anything
derived from this data. In short:

The DTD feeds come under the
[NRE Developer Terms & Conditions v3.0](https://opendata.nationalrail.co.uk/terms),
which let you publish and adapt the data, including commercially, and require
you to credit National Rail Enquiries wherever you do. Your portal credentials
are personal to you, the feeds may be polled no more than once a day, and the
data is supplied as is. **[docs/DATA-SOURCES.md](docs/DATA-SOURCES.md) sets out
each obligation in full** — read it before publishing, not after.

**Three optional sources carry their own licences and their own attributions**:
RSPS5052 supplementary reference data, **Network Rail**'s FOI grid-reference
release, and the **Department for Transport**'s NaPTAN, the last two under the
Open Government Licence v3. `rail fetch --supplementary` and `rail naptan`
download themselves; `rail geography` takes a file you supply, an FOI release
having no URL to poll. Anything published from a mixture of sources must carry
every one of their attributions.

**Keep-alive:** the portal deletes accounts after roughly 30 days without feed
consumption. Scheduling `rail refresh` fortnightly keeps the data current and
doubles as the keep-alive — a poll counts even when no bytes are downloaded, and
`rail status` shows the remaining margin.

## Setup

```bash
uv sync
cp .env.example .env   # then add your portal username and password
uv run rail fetch && uv run rail ingest && uv run rail build
```

`rail refresh` afterwards does all three and rebuilds only what changed. The
full walkthrough, including how to register, is in
[docs/GETTING-STARTED.md](docs/GETTING-STARTED.md).

## Layout

```
src/rail/
  acquire/    portal client, immutable checksummed snapshot store
  layouts/    declarative fixed-width record specs
  parse/      vectorised reader, ZIP → Parquet ingest, non-fixed-width readers
  model/      reference, timetable, fares, restrictions, railcards, routeing
              guide, easements, PlusBus, returns, distance, validation
  engine/     the network and the Connection Scan router
  cli.py      every command
data/
  raw/        downloaded ZIPs, never overwritten, with provenance manifests
  parquet/    typed columnar output, one directory per snapshot
  rail.duckdb the query surface
```

The database is as much the interface as the Python is. Anything can query it:

```python
import duckdb
c = duckdb.connect("data/rail.duckdb", read_only=True)
```

Parquet is queryable without the database at all —
`select * from 'data/parquet/timetable/<snapshot>/stop_time.parquet'`.

**If you query the tables directly, pin `rail.model.SCHEMA_VERSION`.** That is
the real contract: a renamed column breaks SQL with no import to catch it, and
no error until a query quietly returns nothing.

## Known limits

Stated up front because most cannot be engineered away. The figures are from the
feed generation this was last measured against; re-measure before reading
anything into a change.

**In the data, and permanent:**

- **Advance prices are in the feed, Advance availability is not.** The prices
  are real and vary with distance, but nothing says which price point is on sale
  for a given train on a given date — the quota field is empty throughout. They
  are opt-in via `--advance`, and should be read as the best published price
  rather than one you can definitely buy.
- **No seat availability, no reservations, no live running, no disruption.**
  None of it is in these feeds.
- **Railcard minimum fares are thinly encoded.** The mechanism is implemented
  and correct, but the feed carries minima for only 12 railcards, and for the
  16-25 every listed ticket code is a Travelcard — so an ordinary single takes
  no minimum and can come out below what a retailer quotes.
- **There is no single "this is a normal retail fare" flag.** The feed ships
  carnets, group rates, upgrades, staff tickets, tour-operator rates and test
  data alongside ordinary fares. Two fields in the feed do part of the job —
  `reservation_required`, and `package_mkr` for a price that bundles travel with
  parking or admission — and the rest is a curated set of rules. Every
  exclusion is recorded in the
  `fare_reject` table with its reason, which is the first place to look when a
  fare seems wrong.

**Deliberately not implemented:**

- **The routeing guide's local-journey rules are not applied to the end
  segments.** The guide splits a journey into three parts and judges the outer
  two by more permissive rules; this judges the whole path by the map rules.
  That is **stricter than the real guide, never looser**, so the mistake it can
  make is refusing a fare that is really valid — never accepting one that is
  not. That is the safe direction, but it does mean a refusal here should be
  read as "not obviously permitted" rather than "not allowed".
- **One of the guide's rules about doubling back cannot be implemented here.**
  It allows a doubleback only when the fare to the point you turn back from is
  no more than the fare for the journey as a whole — so settling it needs a
  price, while the price depends on which routes are permitted. Each would have
  to answer the other first. In practice it costs nothing: the router finds
  earliest arrivals, and revisiting a station cannot make an arrival earlier, so
  the journeys it returns never double back. It would matter for a deliberate
  stopover.
- **Ticket calendars are parsed and not evaluated** — date bands saying when a
  ticket is on sale at all, covering 114 walk-up ticket codes.
- **The operator qualifier on a restriction band is parsed and not applied.**
  Applying it looks like a large correction, but the one case checked against a
  retailer turned out to be barred for an unrelated reason, so it needs more
  real quotes before it can be trusted.
- **Return-leg restrictions are evaluated only by `rail roundtrip`**, the one
  command that routes the journey home. Judging them needs the time you travel
  back, so a one-to-all sweep cannot do it, and says so in its output.

**Known soft spots:**

- Two Underground stations are classified as national rail stations, because
  national rail services call there on shared track. Neither the timetable nor
  the routeing feed can tell the difference.
- Three station positions are unresolved, no two of the three sources agreeing.
- The two Open Government Licence sources are fetched manually, and `rail
  refresh` rebuilds without them — so re-run `rail geography` and `rail naptan`
  afterwards. `station.grid_source` names the winning source for each position,
  so staleness is visible rather than silent.

## Conventions

- Money is integer **pence**, exactly as the feed stores it.
- Public times are **minutes after midnight**; working times are **seconds**.
  They are not interchangeable — working times include passing points and are
  not what a passenger experiences. Journey-time analysis uses public times.
- **Overnight trains wrap**, so use `arrival_minutes`/`departure_minutes`, which
  add a day offset and are guaranteed to increase along a journey. Never compute
  a journey time from the raw public times.
- The open-ended date sentinel `31122999` is kept as a real date, so that
  `date between start_date and end_date` works without special-casing.
- Every row carries `snapshot_id`, so results are traceable to their input.

## On the field definitions

The record layouts are transcribed from the published specifications, and
every field offset has been checked mechanically against the specifications'
own position tables — parsing the name, length and position columns out of the
documents and diffing them against the layouts here. Fares: 284 fields matched
by name, 0 mismatched. Timetable: 83 matched, 0 mismatched. The one discrepancy
it found was in a field nothing reads.

That check is worth more than it sounds. These are fixed-width records, so an
offset wrong by two bytes yields plausible values rather than an error.

Offsets were additionally cross-checked against
[planarnetwork/dtd2mysql](https://github.com/planarnetwork/dtd2mysql). That
project is GPLv3; it was consulted to verify offsets — facts about a published
file format — and no code was taken from it.

Where a specification and the data disagree, the reasoning is written down in
[docs/INTERPRETING-THE-FEEDS.md](docs/INTERPRETING-THE-FEEDS.md) rather than
buried in a commit message. Several of those cases are ones where the obvious
reading of a field name turns out to be the wrong one.

## Licence

MIT — see [LICENSE](LICENSE). The licence covers the software only. The data it
downloads is licensed to you directly by its publishers, on the terms above.
