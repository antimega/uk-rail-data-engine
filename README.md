# UK rail schedules & fares

A query engine for GB rail timetables and fares, built on the Rail Delivery
Group DTD feeds. No live running data, no disruption, no seat availability -
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
most of what the engine knows - every fare for a pair, with the three records
that govern each (abridged here; the real table carries the validity too):

```
YORK to LONDON KINGS CROSS - 11 fares

  fare      ticket                class  type    route                restriction
  £70.70    G2S OFF-PEAK S        std    single  00049 GRAND CTRL ONLY  GZ GRAND CENTRAL OFF-PEAK
  £75.00    SSS SUPER OFFPEAK S   std    single  00000 ANY PERMITTED    1L IEC SUPER OFF-PK
  £116.20   G1S OFF-PEAK 1S       1st    single  00049 GRAND CTRL ONLY  GZ GRAND CENTRAL OFF-PEAK
  …
```

The £70.70 is cheaper than the £75.00 and valid on fewer trains - its route is
one operator only. That is the sort of thing the engine is for: not just the
price, but what you would be buying.

**An accredited journey planner or retailer is authoritative; this is not.**
See [docs/CAPABILITIES.md](docs/CAPABILITIES.md) for what it does well, what it
does with caveats, and what it cannot do at all.

## Where to start

| | |
|---|---|
| [docs/GETTING-STARTED.md](docs/GETTING-STARTED.md) | account, fetch, ingest, build - in full |
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
each obligation in full** - read it before publishing, not after.

**Three optional sources carry their own licences and their own attributions**:
RSPS5052 supplementary reference data, **Network Rail**'s FOI grid-reference
release, and the **Department for Transport**'s NaPTAN, the last two under the
Open Government Licence v3. `rail fetch --supplementary` and `rail naptan`
download themselves; `rail geography` takes a file you supply, an FOI release
having no URL to poll. Anything published from a mixture of sources must carry
every one of their attributions.

**Keep-alive:** the portal deletes accounts after roughly 30 days without feed
consumption. Scheduling `rail refresh` fortnightly keeps the data current and
doubles as the keep-alive - a poll counts even when no bytes are downloaded, and
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

## The commands

Twenty of them. `--json` on any of the query commands gives machine-readable
output; `--help` on any command gives its full options.

Stations are CRS codes throughout - `rail stations york` finds them. Dates are
`YYYY-MM-DD` and must fall inside the horizon you built, which `rail status`
reports.

**Tables print every row by default.** `--limit N` is there for when you want
less, and a limited table says what it held back:

```
Showing 20 of 2,901 stations. Omit --limit for all of them.
```

That notice is the point of the flag being opt-in. A table that silently stops
at row 20 reads as the whole answer, which is how you conclude a station is
unreachable when it was simply row 21. `--json` is never limited, on any
command - a machine-readable answer that quietly truncates is a trap rather
than a convenience.

### Getting and keeping the data

| command | what it does |
|---|---|
| `rail fetch` | Download the feed ZIPs. `--feed timetable\|fares\|routeing\|all`, `--force` to override the once-daily guard, `--supplementary` for the separately-licensed RSPS5052 reference data. |
| `rail ingest` | Fixed-width records → Parquet. `--feed` to narrow it, `--only` for one file. |
| `rail build` | Parquet → `rail.duckdb`. `--horizon N` sets how many days of running dates to materialise (default 90). |
| `rail refresh` | All three, rebuilding only when something was downloaded. Written for scheduled runs. `--force`, `--rebuild`, `--horizon`. |
| `rail status` | Snapshot ages, the dates the timetable covers, and how close the portal account is to expiring. |
| `rail snapshots` | Every stored snapshot, with its checksum and when it arrived. |
| `rail validate` | 70 data-quality checks. Exit code 1 on any failure, so it works in a pipeline. `--json`. |

Two optional position sources, each under its own licence:

| command | what it does |
|---|---|
| `rail geography <path>` | Import Network Rail's FOI grid references. Takes a **path**, because an FOI release has no URL to poll. |
| `rail naptan` | Fetch DfT NaPTAN. Downloads itself. |

Both need a `rail build` afterwards to apply, and `rail refresh` rebuilds
without them - so re-run them after a refresh. `station.grid_source` names the
winning source per station, which is how staleness stays visible.

### Asking about journeys

| command | what it does |
|---|---|
| `rail journey-times --from --date` | One origin to every station in Britain. `--depart`, or `--profile` to sweep the day (`--until`, `--step`). Reports `journey` and `elapsed` separately - see below. |
| `rail distance --from [--to]` | Rail miles from the routeing guide's link graph, and straight-line distance from grid references. `--least-direct` ranks by the ratio of the two. |
| `rail stations [search]` | Look up CRS, NLC, TIPLOCs, fare group and interchange time. |

### Asking about fares

| command | what it does |
|---|---|
| `rail reachable --from --date --max-fare` | Every destination within a budget. `--railcard`, `--advance`, `--first-class`, `--plusbus`, `--return-on`, `--depart`, `--ignore-restrictions`, and the two route checks below. |
| `rail fares --from --to` | Every fare for a pair with the route, restriction and validity governing each. Deliberately *not* filtered by time. |
| `rail roundtrip --from --to --date --return-on` | Prices a return against two singles and names the cheaper. The only command that routes the journey home, so the only one that can evaluate return-leg restrictions. |
| `rail stopover --from --to --via --date` | A deliberate break of journey, priced as one ticket. `--dwell` is time you actually get. |
| `rail plusbus <station> [--with]` | The bus add-on around a station, including whether one may be sold at all. |
| `rail railcards [search]` | Which railcards a party of a given shape can use. `--adults`, `--children`, `--all`. |

### Asking why a fare is what it is

| command | what it does |
|---|---|
| `rail restrictions <code>` | Spells a restriction code out in English. Every band is a **bar**, not a permission. |
| `rail routings --from --to` | Every routing the National Routeing Guide permits, and the easements that grant or withdraw one. |

`rail reachable` has two checks worth knowing apart, both opt-in because each
narrows the question:

- **`--check-routes`** applies the fare's own route conditions to the journey
  actually found. Without it, "where can I get for £20" permits picking a route
  to suit the fare, which is the right default for that question.
- **`--check-guide`** asks the routeing guide whether it permits the winning
  fare's route, and steps up the price list to the cheapest fare it does permit
  rather than dropping the destination.

### Two clocks, and why both are shown

`journey` is the travelling time, from the first boarding. `elapsed` counts from
`--depart`, so it includes waiting for the first train. York to Poppleton is a
five-minute journey and nineteen minutes elapsed - the difference is a wait on
the platform, and hiding it would be the misleading choice.

`--profile` reports the journey alone, because a sweep of many departures has no
single wait, arrival or elapsed time.

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

Parquet is queryable without the database at all -
`select * from 'data/parquet/timetable/<snapshot>/stop_time.parquet'`.

**If you query the tables directly, pin `rail.model.SCHEMA_VERSION`.** That is
the real contract: a renamed column breaks SQL with no import to catch it, and
no error until a query quietly returns nothing.

### Disk space

**About 800 MB for a working install**, measured rather than estimated:

| | |
|---|---|
| `.venv` (DuckDB, PyArrow, numpy, httpx) | 209 MB |
| source, tests, docs | under 3 MB |
| `data/raw` - one generation of feed ZIPs | 113 MB |
| `data/parquet` - the same generation, parsed | 85 MB |
| `data/rail.duckdb` | 382 MB |
| NaPTAN, the FOI grid file, RSPS5052 | under 1 MB |

Per generation the feeds are: timetable 68 MB zipped and 50 MB as Parquet, fares
44 MB and 35 MB, routeing 1.5 MB and nothing - its files are read straight from
the ZIP rather than converted.

**It grows by roughly 200 MB whenever a feed generation changes**, because the
snapshot store is immutable: a download is written once, never overwritten, so
any figure stays traceable to the exact bytes it came from. The database is
replaced rather than added to, so only `raw/` and `parquet/` accumulate. Nothing
prunes them, and old snapshots are safe to delete once you no longer need to
reproduce a figure from one.

Budget for that on the timetable as well as the fares. Fares change a few times a
year, and the timetable is usually described as changing rarely - but two
timetable generations landed within a week while this was being written.

**`--horizon` is a smaller lever than it looks.** It scales `service_date`, which
is 2.2M rows of the 9.3M in the database; the bulk is `schedule_stop`, which is
whatever the feed contains regardless. Dropping from 90 days to 30 removes about
16% of the rows, so expect to save tens of megabytes rather than hundreds.

During `ingest`, add roughly the uncompressed size of one feed on top,
transiently.

## Known limits

Stated up front because most cannot be engineered away. The figures are from the
feed generation this was last measured against; re-measure before reading
anything into a change.

**In the data, and permanent:**

- **Advance prices are in the feed, Advance availability is not.** The prices
  are real and vary with distance, but nothing says which price point is on sale
  for a given train on a given date - the quota field is empty throughout. They
  are opt-in via `--advance`, and should be read as the best published price
  rather than one you can definitely buy.
- **No seat availability, no reservations, no live running, no disruption.**
  None of it is in these feeds.
- **Railcard minimum fares are thinly encoded.** The mechanism is implemented
  and correct, but the feed carries minima for only 12 railcards, and for the
  16-25 every listed ticket code is a Travelcard - so an ordinary single takes
  no minimum and can come out below what a retailer quotes.
- **There is no single "this is a normal retail fare" flag.** The feed ships
  carnets, group rates, upgrades, staff tickets, tour-operator rates and test
  data alongside ordinary fares. Two fields in the feed do part of the job -
  `reservation_required`, and `package_mkr` for a price that bundles travel with
  parking or admission - and the rest is a curated set of rules. Every
  exclusion is recorded in the
  `fare_reject` table with its reason, which is the first place to look when a
  fare seems wrong.

**Deliberately not implemented:**

- **The routeing guide's local-journey rules are not applied to the end
  segments.** The guide splits a journey into three parts and judges the outer
  two by more permissive rules; this judges the whole path by the map rules.
  That is **stricter than the real guide, never looser**, so the mistake it can
  make is refusing a fare that is really valid - never accepting one that is
  not. That is the safe direction, but it does mean a refusal here should be
  read as "not obviously permitted" rather than "not allowed".
- **One of the guide's rules about doubling back cannot be implemented here.**
  It allows a doubleback only when the fare to the point you turn back from is
  no more than the fare for the journey as a whole - so settling it needs a
  price, while the price depends on which routes are permitted. Each would have
  to answer the other first. In practice it costs nothing: the router finds
  earliest arrivals, and revisiting a station cannot make an arrival earlier, so
  the journeys it returns never double back. It would matter for a deliberate
  stopover.
- **Ticket calendars are parsed and not evaluated** - date bands saying when a
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
  refresh` rebuilds without them - so re-run `rail geography` and `rail naptan`
  afterwards. `station.grid_source` names the winning source for each position,
  so staleness is visible rather than silent.

## Conventions

- Money is integer **pence**, exactly as the feed stores it.
- Public times are **minutes after midnight**; working times are **seconds**.
  They are not interchangeable - working times include passing points and are
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
own position tables - parsing the name, length and position columns out of the
documents and diffing them against the layouts here. Fares: 284 fields matched
by name, 0 mismatched. Timetable: 83 matched, 0 mismatched. The one discrepancy
it found was in a field nothing reads.

That check is worth more than it sounds. These are fixed-width records, so an
offset wrong by two bytes yields plausible values rather than an error.

Offsets were additionally cross-checked against
[planarnetwork/dtd2mysql](https://github.com/planarnetwork/dtd2mysql). That
project is GPLv3; it was consulted to verify offsets - facts about a published
file format - and no code was taken from it.

Where a specification and the data disagree, the reasoning is written down in
[docs/INTERPRETING-THE-FEEDS.md](docs/INTERPRETING-THE-FEEDS.md) rather than
buried in a commit message. Several of those cases are ones where the obvious
reading of a field name turns out to be the wrong one.

## Licence

MIT - see [LICENSE](LICENSE).

**It covers the software in this repository and nothing else.** It does not cover
the data the software downloads. The Rail Delivery Group's DTD feeds are licensed
to you directly by National Rail Enquiries, on terms you accept when you register
for an account; the two Open Government Licence sources carry their own
conditions. Publishing anything derived from any of them requires the
attributions set out in [docs/DATA-SOURCES.md](docs/DATA-SOURCES.md). None of
that data is contained here.
