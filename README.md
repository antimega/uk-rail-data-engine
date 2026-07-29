# UK rail schedules & fares

A query engine for GB rail **future** timetables and fares, built on the Rail
Delivery Group **DTD** feeds. No live running data, no disruption, no seat
availability — this is the published plan and the published prices.

It answers questions that a journey planner's search box cannot ask, because
they are one-to-many rather than one-to-one:

```bash
rail journey-times --from YRK --date 2026-08-04 --profile
rail reachable --from YRK --date 2026-08-01 --max-fare 20 --railcard YNG
rail fares --from YRK --to KGX
rail roundtrip --from YRK --to KGX --date 2026-08-04 --return-on 2026-08-06
```

The first sweeps departures across a day and keeps the best journey to every
station in Britain. The second prices every one of them and keeps what fits a
budget. The third lists every fare between a pair with the route, restriction
and validity that govern each. The fourth answers the ordinary question that is
surprisingly hard — whether a return or two singles is cheaper — for which
neither product reliably wins.

**An accredited journey planner or retailer is authoritative; this is not.**
See [docs/CAPABILITIES.md](docs/CAPABILITIES.md) for what it does well, what it
does with caveats, and what it cannot do at all.

## Where to start

| | |
|---|---|
| [docs/GETTING-STARTED.md](docs/GETTING-STARTED.md) | account, fetch, ingest, build — in full |
| [docs/CAPABILITIES.md](docs/CAPABILITIES.md) | what it can and cannot answer |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | how it works, for a reader of the code |
| [docs/DATA-SOURCES.md](docs/DATA-SOURCES.md) | the three sources, their licences, and the attribution you owe |
| [docs/INTERPRETING-THE-FEEDS.md](docs/INTERPRETING-THE-FEEDS.md) | the readings chosen where the formats are ambiguous |

## Rules of use

Read [docs/DATA-SOURCES.md](docs/DATA-SOURCES.md) before publishing anything
derived from this data. In short:

The DTD feeds are licensed under the
[NRE Developer Terms & Conditions v3.0](https://opendata.nationalrail.co.uk/terms).
You may copy, publish and distribute the data, adapt its *format* (but not amend
its content), and use it commercially. In return:

- **Acknowledge National Rail Enquiries as the source** wherever the data or
  anything derived from it is published.
- **Portal access is personal to the licensee and cannot be assigned.**
  Credentials live in a git-ignored `.env` and nowhere else. `data/` is
  git-ignored too: the feeds are licensed to you, and are not redistributed by
  this repository.
- **Poll no more than daily.** High Volume Usage can attract charges under the
  NRE Usage Charging Document. `rail fetch` enforces a 24-hour guard and skips
  the download body when `Last-Modified` is unchanged.
- The data is licensed **"as is"** — no warranty, no continuity commitment, and
  NRE is not liable for errors or omissions. Treat published figures as derived
  and indicative.
- Nothing here may imply official status or NRE endorsement.

Two optional sources carry their own licences and their own attributions —
**Network Rail** for an FOI grid-reference release and the **Department for
Transport** for NaPTAN, both Open Government Licence v3. Anything published from
a mixture of sources owes all of them.

**Keep-alive:** the portal deletes accounts after roughly 30 days without feed
consumption. Scheduling `rail refresh` fortnightly doubles as the keep-alive — a
poll counts even when no bytes are downloaded, and `rail status` shows the
remaining margin. See `deploy/refresh.plist.template`.

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
  data alongside ordinary fares. Two structural fields do part of the job and
  the rest is a curated set of rules; every exclusion is recorded in the
  `fare_reject` table with its reason, which is the first place to look when a
  fare seems wrong.

**Deliberately not implemented:**

- **The routeing guide's local-journey rules are not applied to the end
  segments.** The guide splits a journey into three parts and judges the outer
  two by more permissive rules; this judges the whole path by the map rules.
  That is **stricter than the real guide, never looser**, so it can only turn a
  permission into a refusal — the safe direction, and the reason it has never
  produced a wrong fare.
- **One doubleback rule cannot be implemented from here**, because it states its
  conditions as fare comparisons: the guide would be asking the fares engine a
  question while the fares engine is asking the guide one.
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
**every field offset has been checked mechanically against the specifications'
own position tables** — parsing the name, length and position columns out of the
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
