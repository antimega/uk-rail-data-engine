# Getting started

From nothing to answering questions. Budget about half an hour, most of it
waiting for downloads and one build.

You need [uv](https://docs.astral.sh/uv/) and Python 3.11 or later. Everything
runs locally; there is no server and no service to sign up to beyond the data
portal itself.

---

## 1. An account

Register at <https://opendata.nationalrail.co.uk>. It is free. You are agreeing
to the [NRE Developer Terms & Conditions
v3.0](https://opendata.nationalrail.co.uk/terms) — read
[DATA-SOURCES.md](DATA-SOURCES.md) for what they oblige you to do, because the
obligations apply the moment you publish anything.

Two of them matter from the first command:

- **Your credentials are personal and cannot be assigned.** They go in `.env`,
  and nowhere else. `.gitignore` ships in the repository and lists it, so a
  fresh clone ignores `.env` before you have created it — there is no window in
  which it could be committed by accident.
- **Poll no more than once a day.** `rail fetch` enforces this itself, so you
  cannot breach it by accident, but do not work around the guard.

```bash
git clone <this repo> && cd uk-rail-data-engine
uv sync
cp .env.example .env
```

Then edit `.env`:

```
NRDP_USERNAME=you@example.com
NRDP_PASSWORD=…
```

---

## 2. Fetch, ingest, build

```bash
uv run rail fetch     # portal   → data/raw/<feed>/<NAME>.ZIP + a manifest
uv run rail ingest    # ZIP      → data/parquet/<feed>/<snapshot>/*.parquet
uv run rail build     # parquet  → data/rail.duckdb
```

**`fetch`** downloads the timetable, fares and routeing-guide feeds. They are
large — the timetable and fares are tens of megabytes compressed and expand a
great deal. Each download is written once, never overwritten, alongside a
manifest recording its SHA-256, so every later figure is traceable to the exact
bytes it came from.

**`ingest`** converts the fixed-width records to Parquet. This is the slow step
the first time. Nothing is dropped silently: anything excluded lands in a reject
table with a reason.

**`build`** assembles the DuckDB database — the station crosswalk, the resolved
timetable, the fares reference, restrictions, railcards, the routeing guide and
its easements.

`rail build --horizon N` sets how many days of concrete running dates to
materialise. The default is 90. A shorter horizon builds faster; a longer one
lets you ask about dates further out.

Check it worked:

```bash
uv run rail validate
```

70 checks, and it exits non-zero if any fails. If this is green, the parse is
sound.

---

## 3. First questions

```bash
uv run rail stations york
uv run rail journey-times --from YRK --date 2026-08-04 --depart 09:00
uv run rail fares --from YRK --to KGX
uv run rail reachable --from YRK --date 2026-08-04 --max-fare 20
```

Dates must be inside the horizon you built — `rail status` shows the range the
current snapshots cover. Stations are CRS codes: `rail stations <name>` finds
them.

Add `--json` to almost anything for machine-readable output.

Then query the database directly for anything the CLI does not cover:

```python
import duckdb
c = duckdb.connect("data/rail.duckdb", read_only=True)
c.sql("select count(*) from schedule_stop").show()
```

Read-only matters: several processes can read the database at once, and one
writer locks out every reader.

---

## 4. Keeping it fresh

```bash
uv run rail refresh          # fetch → ingest what changed → rebuild
uv run rail status           # snapshot ages, and the account margin
```

`refresh` only rebuilds when something was actually downloaded, so the usual run
is a few seconds of HTTP and nothing else. Fares change a few times a year and
the timetable rarely.

**Schedule it, for two reasons.** The obvious one is keeping the data current.
The other is that **the portal deletes accounts after roughly 30 days without
consumption**, and a
poll counts as consumption even when no bytes come back. A fortnightly refresh
is comfortably inside that, and `rail status` reports the remaining margin.

Anything that can run a command on a schedule will do it. The command is:

```bash
rail refresh && rail validate
```

On macOS, `deploy/refresh.plist.template` is a **launchd** agent set up to run
that on the 1st and 15th; its own header comments say what to substitute and how
to install it. On Linux a **systemd timer** or a **cron** entry is the
equivalent, and on a server you already operate, whatever runs your other
scheduled work is the right answer. Two things are worth carrying over whichever
you use:

- **Prefer calendar dates to a rolling interval.** A calendar schedule
  re-anchors itself after a reboot, where an interval restarts its clock — so a
  machine that sleeps often can drift a long way past 30 days.
- **Create the log directory first.** Most schedulers will not create it, and a
  missing directory fails the job with nothing written to say why.

**What counts as a successful run** is the subtle part: reaching the portal is
what renews the account, so a run that downloads nothing is still a success. A
run where the daily guard skipped every feed is *not* — nothing reached the
portal. `rail status` distinguishes them and turns red at 21 days.

---

## 5. The optional sources

None of these is needed to answer a question; each improves the answers. All
three carry their own licence — see [DATA-SOURCES.md](DATA-SOURCES.md).

```bash
uv run rail fetch --supplementary    # RSPS5052 reference data
uv run rail geography <file>         # Network Rail FOI grid references
uv run rail naptan                   # DfT NaPTAN
uv run rail build                    # then rebuild to apply them
```

**Positions are worth doing.** The timetable's own grid references are about a
kilometre accurate and occasionally much worse. With the two Open Government
Licence sources, a position is accepted when a second source agrees within a
kilometre — and that resolves the disagreements rather than picking a favourite.

**`geography` is the one that cannot fetch itself.** It takes a path because an
FOI release is a one-off publication with no URL to poll — you download the
spreadsheet yourself. See the [openraildata wiki](https://wiki.openraildata.com/index.php/Identifying_Locations) for more information
on where GB location data is published. `--supplementary` and `naptan` fetch
their own data.

**`rail refresh` rebuilds without any of them**, so re-run `geography` and
`naptan` after a refresh. `station.grid_source` names the winning
source for every station, so staleness is visible rather than silent, and `rail
validate` watches the corroborated share for exactly that reason.

---

## Running `rail` from anywhere

`uv run rail …` works from the project directory and needs no setup, which is
why the examples are written that way. For a bare `rail` in any terminal,
symlink the entry point into a directory on your `PATH`:

```bash
ln -sf "$PWD/.venv/bin/rail" ~/.local/bin/rail
```

Two things make that safe rather than a hack. The script's shebang points at the
virtualenv's own Python, so nothing needs activating; and the project root is
resolved from the source file rather than the working directory, so `rail` finds
its data wherever you run it.

Prefer it to the alternatives: an **alias** would not work in scripts or
non-interactive shells, and putting **`.venv/bin` on `PATH`** would shadow the
system `python` and `pip`.

The link points at this checkout, so moving the project breaks it — remake it.
`uv sync` recreating the virtualenv is fine, because the path does not change.

## Sharing one copy of the data

The data directory is several gigabytes, so several checkouts should share one:

```bash
export RAIL_DATA_DIR=/path/to/uk-rail-data-engine/data
```

That is the supported way to consume this as a library from another project.
`.env` is read from the working directory first and the engine checkout second,
so a consuming project keeps its own credentials.

---

## When something goes wrong

**`no <feed> snapshot — run rail fetch`** — self-explanatory, but it also
appears when a fetch was skipped by the daily guard and there was never a
previous download.

**`rail fetch` reports nothing downloaded.** Expected. The feeds change rarely,
and an unchanged feed is skipped on `Last-Modified`. `rail status` shows the
ages.

**A date outside the horizon is an error, and says so** — `no services on
2027-06-01 — is it inside the built horizon?`. It is not silently empty, because
an empty result and an unbuilt date look identical and mean quite different
things. `rail status` shows the range the snapshots cover; `rail build
--horizon N` extends it.

**A fare looks wrong.** Start with `rail fares --from A --to B`, which shows the
route, restriction and validity governing each price. Then the `fare_reject`
table, which records why a ticket type was not counted as a walk-up fare. Most
surprising prices are a route condition doing its job.

**A destination has a journey but no fare.** Usually correct. Either the fare's
route conditions exclude the journey that was found, or the location genuinely
has no fare to it — some are interchange points inside a through product rather
than places a ticket is sold to.

**`rail validate` fails.** Something in the parse has drifted, which is what it
is for. The check names the table and the count.
