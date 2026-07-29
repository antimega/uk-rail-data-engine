# Data sources, licences, and the attribution you must use

Four sources feed this engine. They arrive by different routes, under different
licences, and **only the first is required**. If you publish anything derived
from a mixture, you must carry every attribution in that mixture.

None of this data is contained in this repository. `data/` is git-ignored, and
the MIT licence on the software covers the software alone.

| source | command | licence | required? |
|---|---|---|---|
| DTD feeds - timetable, fares, routeing guide | `rail fetch` | NRE Developer Terms v3.0 | **yes** |
| RSPS5052 supplementary reference data | `rail fetch --supplementary` | its own; check before publishing | no |
| Network Rail grid references (FOI release) | `rail geography <file>` | Open Government Licence v3 | no |
| NaPTAN | `rail naptan` | Open Government Licence v3 | no |

---

## 1. The DTD feeds

The timetable, fares and routeing-guide feeds come from the National Rail Data
Portal and are licensed under the
[NRE Developer Terms & Conditions v3.0](https://opendata.nationalrail.co.uk/terms).
Register at <https://opendata.nationalrail.co.uk> for an account.

**What you may do.** Copy, publish and distribute the data; adapt its *format*
but not amend its content; use it commercially.

**What you must do.**

- **Acknowledge National Rail Enquiries as the source**, wherever the data or
  anything derived from it is published. Not only bulk republication - a chart,
  a map or a quoted fare all count.
- **Nothing may imply official status or NRE endorsement.** Do not present
  output as authoritative, and do not brand it as though it came from the
  industry.
- The data is supplied **"as is"** - no warranty, no continuity commitment, and
  NRE is not liable for errors or omissions. Anything you publish should read as
  derived and indicative.

**Constraints on how you fetch.**

- **Portal access is personal to the licensee and cannot be assigned.**
  Credentials belong in a git-ignored `.env` and nowhere else - never in a
  commit, a manifest, a log line or a screenshot. Nothing in this codebase
  writes them anywhere but the HTTP request.
- **Poll no more than once a day.** High Volume Usage can attract charges under
  the NRE Usage Charging Document. `rail fetch` enforces a 24-hour guard per
  feed and sends a conditional request, so an unchanged feed costs a header
  exchange and no body.
- **Accounts are deleted after roughly 30 days of no consumption.** A poll
  counts even when nothing is downloaded, so a fortnightly `rail refresh` is
  also the keep-alive. `rail status` reports the remaining margin and turns red
  at 21 days.

**Portal migration.** RDG has said it plans to retire this portal in favour of
the [Rail Data Marketplace](https://raildata.org.uk/). Acquisition sits behind a
`FeedSource` interface in `src/rail/acquire/source.py`, so that migration should
mean one new implementation and no downstream change.

---

## 2. RSPS5052 supplementary reference data

A public S3 bucket, no authentication, and **not a DTD feed** - so it is not
obtained under the terms above and should not be assumed to be covered by them.
Check RSPS5052's own licensing before publishing anything derived from it. That
is why it sits behind a separate `--supplementary` switch rather than being
another feed name, and why the CLI prints a warning every time.

Two files are used: a list of which location codes are genuinely GB rail
stations, and a list of ticket products that bundle several journeys into one
price.

**The station list is informational only.** RSPS5052 §7.1.2 is explicit that it
must not affect journey planning or ticket selection. So it labels output here
and filters nothing - a journey may perfectly well route *through* a bus
interchange, and refusing to let it would be both wrong and a breach.

**The URLs are `http`, not `https`, and that is not an oversight.** The bucket
name contains dots, so it cannot be addressed virtual-host style under Amazon's
wildcard certificate, and path-style addressing is refused. The specification
itself prints the plain-HTTP form. Nothing is authenticated and everything is
public reference data, so the exposure is a middlebox serving a wrong station
list - which is what the recorded SHA-256 of every download is for.

---

## 3. Network Rail grid references (an FOI disclosure)

Metre-precise eastings and northings for TIPLOC codes, released by **Network
Rail** under the
**[Open Government Licence v3](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/)**.

Copying, publishing and adapting are all permitted **provided you acknowledge
the source and name the OGL**. Attribute **Network Rail**. That obligation is
*in addition to* the National Rail attribution above - a map built from the
timetable and positioned by this file owes both.

**An FOI release is a point-in-time snapshot, not a feed**: no schedule, no
version, nothing to poll. That is why `rail geography` takes a path rather than
downloading anything - you supply the spreadsheet. It goes stale as stations open
and move, and `rail refresh` rebuilds without it, so re-run `rail geography`
afterwards and check `station.grid_source` to see which positions came from
where.

**Where to find it, and the wider picture.** See the
[openraildata wiki](https://wiki.openraildata.com/index.php/Identifying_Locations) for more information - it collects the ways GB rail
locations are identified, and where the public releases live. It is a
community-maintained wiki rather than an industry feed, so treat it as a pointer
and a cross-check, not as a source to ingest.

---

## 4. NaPTAN

The **Department for Transport**'s gazetteer of public transport access nodes,
also under the
**[Open Government Licence v3](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/)**,
Crown copyright. Attribute **DfT**, and name the OGL.

No account and no key. Unlike the FOI file it is *maintained*, which is exactly
what makes it useful here: with two sources there is no way to tell which is
right when they disagree, and a third breaks the tie. See
[ARCHITECTURE.md](ARCHITECTURE.md) for how positions are resolved by
corroboration rather than by ranking the sources.

It joins on the ATCO code - rail stops sit in a namespace where the rest of the
code *is* the TIPLOC, so there is no new identifier and no fuzzy matching. That
namespace is rail only, so NaPTAN cannot improve on the supplementary list for
bus, tram and ferry locations.

---

## What must *not* be republished

The RSPS specifications themselves. They are Rail Settlement Plan's copyrighted
documents and are not covered by any of the licences above.

The distinction that matters, and it is a sharp one:

- **The feed data may be published.** The NRE terms say so directly. Short
  verbatim strings from the feeds appear throughout this codebase in comments
  and tests - ticket descriptions, restriction wordings - each one evidence for
  why a rule exists.
- **The specification prose may not.** Nothing in this repository quotes it.
  Where a reading needs justifying, the documentation describes the decision in
  its own words and cites the section number, so anyone holding a licensed copy
  can check the claim without it being reproduced here.

---

## A worked attribution

For a public web page built from the timetable, the fares feed and both OGL
position sources, something of this shape discharges all of it:

> Contains data from National Rail Enquiries. Station positions contain data
> from Network Rail and from the Department for Transport (NaPTAN), licensed
> under the Open Government Licence v3.0. This is not an official National Rail
> service and is not endorsed by National Rail Enquiries. Data is supplied as
> is; fares and times shown are derived and indicative - check with an
> accredited retailer before travelling.

The last sentence is not required by any licence. It is there because the
alternative is a reader treating a derived fare as a quote.
