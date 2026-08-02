# Architecture

Written for someone about to read the source code. It gives the shape before the
detail, and spends most of its length on the four places where the obvious
design is wrong.

---

## The pipeline

Four stages, each writing something durable that the next reads. Nothing is held
only in memory, and every stage can be re-run without the ones before it.

```
fetch    portal        →  data/raw/<feed>/<NAME>.ZIP  + manifest.json
ingest   ZIP           →  data/parquet/<feed>/<snapshot>/<table>.parquet
build    parquet       →  data/rail.duckdb
query    the CLI, or your own SQL
```

**`acquire/`** talks to the portal (`nrdp.py`) behind a `FeedSource` interface
(`source.py`). That indirection is not speculative: the operator has said it
intends to retire this portal for a different one, so the interface is what
keeps that change to a single new implementation.

`snapshots.py` is an immutable store: a download is written once under its own
name, never overwritten, with a manifest recording its SHA-256 and the time it
arrived. Two other acquirers - `geography.py` and `naptan.py` - handle the
optional position sources, and `supplementary.py` the separately-licensed
reference data.

**`layouts/`** declares the record formats. These are fixed-width files, so a
layout is a list of (name, offset, length, type). `spec.py` holds the machinery;
`fares.py` and `timetable.py` hold the actual field tables. Keeping them
declarative is what made it possible to check every offset mechanically against
the specifications' own position tables - see the README.

**`parse/`** reads them. `fixed_width.py` is a vectorised reader: it slices
whole columns out of a byte buffer rather than looping over records, which is
what makes a seven-million-row file tolerable. `ingest.py` drives the ZIP to
Parquet conversion. `special.py` and `routeing.py` handle the files that are not
fixed-width records.

**`model/`** turns Parquet into the query surface - one module per domain, each
with a `build_*` function that writes tables and returns a counts object. The
counts are what the CLI prints and what `validate.py` checks.

**`engine/`** is the router: `network.py` builds the connection list,
`csa.py` scans it.

### Why DuckDB and Parquet, and no server

The workload is analytical and single-user: read tens of millions of rows, group
and join, write nothing. That is what a columnar engine is for, and DuckDB is an
embedded one, so there is no process to run and no port to open.

Parquet in between matters more than it looks. It means **ingest and build are
separable** - you can rebuild the database in a couple of minutes without
re-reading the ZIPs, which is what makes iterating on the model bearable. It
also means the intermediate is queryable on its own, so a question about what
the feed actually says can be answered without a build at all:

```sql
select * from 'data/parquet/timetable/<snapshot>/stop_time.parquet' limit 5
```

The database being a file has one more consequence worth knowing: many processes
can read it concurrently, but a single writer locks all of them out. Open it
`read_only=True` for anything that is not a build.

---

## The crosswalk, and why it is checked three ways

**The timetable feed and the fares feed are separate systems with separate
identifiers**, and joining them is the foundation everything else stands on. The
timetable names locations by TIPLOC; the fares feed names them by NLC. Both
carry a CRS code, so joining on CRS is the obvious move.

**It is also the single most dangerous join in the project**, because getting it
wrong does not raise an error. It silently attaches one station's fares to
another station's trains, and every downstream answer stays plausible.

So it is verified against something that is *not* CRS. Each feed carries its own
location number, and they are related: the fares NLC should be the first four
digits of the timetable's own location code. Comparing those instead of CRS,
every rail station agrees, with no exceptions. The ones that disagree are all
non-rail - bus stops, tram stops, airports, ferry terminals - which the two
feeds number on entirely different schemes.

A third file in the routeing feed gives CRS against NLC directly, produced by a
third process. All 3,430 of its entries that name a CRS agree with the crosswalk
on both the NLC and the fare group. `rail validate` asserts all of this, because
a drift here would be invisible in the output.

`model/reference.py` builds it. `station_tiploc` is deliberately **one-to-many**
- a station can have several TIPLOCs, and some of the extras are junctions
several kilometres away, which matters when positions are resolved.

**A station may also carry several names**, one per TIPLOC, and 18 do. Which one
`station.name` keeps used to be settled by the TIPLOC tie-break alone, which
took the platform-level record six times - Paddington came out named after its
Elizabeth line box and Reading after three of its platforms. The station file's
own subsidiary flag separates them exactly, and is what decides now. Nothing
joins on the name, so this is a display fix rather than a correctness one; it is
recorded because the failure was invisible until someone looked a station up by
the name on their ticket.

`station_alias` carries the other names the feed knows a station by: the Welsh
name, the name it used to have, the landmark it is known for, and the
untruncated form of one the feed has cut to its 26-character field. 298 of them,
resolved against *every* name a station carries rather than the one kept above -
the alias file names a station by whichever of its records it likes. Nothing in
the engine reads it; it is there for search and display.

---

## The router

`engine/network.py` builds a list of connections - each one a train leaving
somewhere at a time and arriving somewhere else at a later time - and
`engine/csa.py` scans them in departure order, relaxing arrival times. This is
Connection Scan. Roughly 530,000 connections build in about a second and scan in
about 35 milliseconds, which is why every "one origin to everywhere" question
here is cheap.

Pure Python lists beat numpy for the scan, incidentally: it is sequential access,
and list indexing is faster than numpy scalar access.

Four things about it are not obvious, and three of them were bugs first.

### Two clocks per station

`arrival` is when you can *be* somewhere - the answer to the question.
`ready` is when you can *board* there - arrival plus the station's minimum
interchange time. Conflating them is the classic error in this algorithm.
Staying on the same train bypasses `ready` entirely.

Operator-specific interchange times **replace** a station's own rather than
competing with it. Most are shorter than the default, but the ones that matter
are longer, and taking `min(default, rule)` is invisible where the rule is
shorter and sells an impossible connection where it is longer.

### Two consecutive days, not one

A schedule's own overnight wrap is handled by a day offset. But a journey
continuing onto a service the feed dates *tomorrow* was simply invisible.

The sleeper is the case that proves it: it leaves London late in the evening,
divides in Scotland, and the onward portion is a **separate schedule dated the
next day** with no same-day overlap at all. Before this, four Scottish
destinations were not merely late from a 21:00 query - they were unreachable.

The cost is that an evening query now answers with next-morning arrivals rather
than "unreachable". That is the true answer to "when can I be there", and it is
another reason not to read the reported minutes as a journey duration.

**A schedule running on both days is two trains**, so the trip key is (schedule,
day) rather than the schedule alone. Sharing one would let a passenger board at
23:00 and stay aboard to wherever the same service reaches the next morning.

### The path is traced along the train, not between stations

Walking back station to station looks equivalent and is not. A through train
passes places whose own best arrival is *later* than the moment it went by, so
following their history leads somewhere the passenger never was - and in the
worst case loops.

`ScanResult` therefore records where each trip was boarded and reconstructs
along that trip's own stops. `_segments` does that walk once, and `path_to`,
`trips_to`, `changes_to`, `operators_to`, `modes_to` and `legs_to` all read it.
Keeping them separate is exactly how they came to disagree: a journey that was
one train throughout once reported a change and two operators.

**That is not cosmetic.** The operators feed route conditions - some fares are
valid only on a named operator's trains - so a stray one gives the wrong ticket.

### A fixed link runs both ways

Walks and Underground hops between neighbouring stations are stated **once** in
the feed and must be read in both directions. The data settles it: of the
thousands of such pairs, not one carries a reverse record. Treating them as
one-way used half of every fixed link.

This is the **opposite** convention from the routeing guide's map links, which
*are* directional and do carry the reverse wherever it is valid - there,
unioning them invents permissions. Two files, two conventions, and the data says
which is which.

A link also **costs the interchange time at both ends** rather than replacing it:
the walk leaves at `ready`, and boarding at the far end costs that station's own
change time too. The intuitive door-to-door reading made 41% of destinations
arrive too early, by a median of half an hour.

---

## How a fare is derived

**Fares are not point-to-point.** This is the single biggest surprise in the
fares feed, and code written on the other assumption finds a small fraction of
the fares that exist.

A fare belongs to a *flow*, and a flow's endpoints are not stations. Each end is
a code that may be:

- the station's own location number,
- a **fare group** it belongs to (a ticket to "Manchester Stations" is valid at
  any of them),
- a **cluster** it is a member of - an arbitrary set used to price a group of
  locations together,
- or a **county code**.

So pricing A to B means expanding each end into the full set of codes that can
stand for it, and matching any flow between the two sets. `fare_alias` is that
expansion, precomputed. A flow marked reversible matches in either direction.

The county level is the one most likely to be missed, because it is rare -
only a handful of flows in the whole feed use it, and none names a county code
directly; the only way in is through a cluster. Missing it leaves one island's
fares unreachable from anywhere.

On top of the flow price sit overrides - a separate file can replace or withdraw
a flow's fare for a given ticket and railcard. A withdrawal is expressed as a
sentinel value that is *not* a price, and treating it as one produces a
999,999-penny fare.

`model/fares.py` does this; `model/restrictions.py` decides when a fare may be
used; `model/railcards.py` discounts it; `model/returns.py` works out what a
return ticket actually buys you and by when you must come back.

---

## The routeing guide

`model/routeing.py`. A fare is priced for a *route*, and the guide decides
whether the journey you actually made is one that route permits.

`RouteingGuide.permits()` returns True, False, or **None meaning the guide has
no opinion** - and that third value must never be collapsed into a refusal.
Silence is common and is not a prohibition.

The structure, briefly:

- Each station maps to one or more **routeing points**, which may be group codes
  rather than individual stations.
- A pair of routeing points maps to a set of permitted **maps**.
- A map is a **graph**, and the test is *reachability* across it - not adjacency.
  Trains pass through places without calling, so demanding that consecutive
  calling points be directly linked refuses obviously valid journeys.
- Two blanket permissions short-circuit all of that: a journey with no change of
  train is permitted, and so is one taking the shortest route by rail or within
  three miles of it.
- **Easements** are published exceptions. There are more that *grant* a route
  than withdraw one, so ignoring them is not the conservative choice it appears
  to be. They can depend on the ticket's route code, the ticket type, and the
  operators used - so `permits()` takes those, and gives no verdict rather than
  guessing when something it depends on is unknown.

A negative easement beats a positive one where both match. The guide does not
say which wins; refusing is the answer that cannot sell someone a ticket they
may not use.

---

## Where positions come from

Three sources, resolved by **corroboration rather than hierarchy**: a position
is accepted when a second source agrees within a kilometre.

That is not over-engineering. With two sources, several dozen stations disagreed
by more than a kilometre and there was no way to tell which was right - and the
conservative choice of keeping the timetable's own value turned out to be wrong
slightly more often than it was right. The third source adjudicates, and the
split was near even.

Corroboration decides *which* position is right; precision decides which copy to
keep. Where no two sources agree the more precise value is taken, the source is
recorded as uncorroborated, and the station is listed in a conflicts table -
three stations are in that position today.

`model/geo.py` also does the OS grid to WGS84 conversion, including the datum
shift, which is about a hundred metres and not optional. The test for it
compares NaPTAN's two representations of the same stop against each other, which
isolates the arithmetic from every other disagreement in the stack.

---

## Validation

`model/validate.py`, run by `rail validate`. 76 checks in five categories, exit
code 1 on any failure.

The bands are deliberately loose: they exist to catch a broken pipeline, not to
fire when an operator adds a service. The most valuable ones assert *outcomes*
rather than rules - that no walk-up fare requires a reservation, say, rather
than that the reservation field was read correctly. An outcome check fails when
the parse drifts, which is the failure that is otherwise silent.

Several checks exist because a specific bug got through once, and each of those
is worth more than its line count.

**One check is a warn rather than a fail, and the distinction is the point.**
A new fares generation legitimately ships ticket types nobody has seen, so
failing on one would stop a scheduled refresh on an ordinary Tuesday. But a new
type lands in the wrong class *silently* and wins immediately, the wrong class
being nearly always the cheaper one. So `rail validate` warns when an unreviewed
ticket type is already carrying fares, and `rail tickets --review` - which exits
1 - is where it gets acted on. `src/rail/reviewed_tickets.json` is the register
that makes "unreviewed" mean something; see
[TICKET-TYPES.md](TICKET-TYPES.md).
