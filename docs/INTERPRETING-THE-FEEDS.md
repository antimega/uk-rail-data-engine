# Interpreting the feeds

Where a format is ambiguous, someone has to decide what it means. This is a
record of those decisions and the evidence for each, so that a future reader can
disagree with a specific claim rather than with the whole edifice.

**On sources.** The RSPS specifications are Rail Settlement Plan's copyrighted
documents and are not reproduced here — not a phrase of them. Each entry
describes the conclusion in its own words and cites the section number, so
anyone holding a licensed copy can check the claim. Feed *data* is quoted freely
where it is the evidence, which the data licence permits.

**On method.** Most of these were settled by measurement first and confirmed
against the specification afterwards. Twice the two disagreed, and both times
the specification was right and the measurement had been reading a coincidence.
Once the measurement was right and the specification is simply wrong. Those
three are flagged.

---

## Fields whose names mean the opposite of what you would guess

This is the largest category, and each one is a silent bug rather than a loud
one.

**The composite indicator selects records *in*, not out.** (RSPS5045 4.4.3,
field 15.) The value that looks like "this is a composite, skip it" is the value
meaning *use this record*; the other means the fare is already present in the
flow file. Every single one of the 249,917 override records carries the "use it"
value, so inverting the test silently discards the entire file — and nothing
downstream errors, because a missing override just means the flow price stands.

**The non-standard-discount flag also reads backwards.** (RSPS5045 4.5.2,
field 11.) One value means the adult fare is worked out normally and *this
record can be ignored*. The interesting values are the others: no adult fare at
all, and no *discounted* adult fare so the undiscounted price stands. Reading it
the intuitive way applies the exception exactly where it does not apply.

**"Reservation required" has no value meaning "optional".** (RSPS5045 4.6.2,
field 23.) There is one value meaning no reservation is needed; every other
value names *which legs* must be reserved. The products carrying one particular
value made it look like an optional flag, and reading it that way left a
ten-deep Advance price ladder classified as ordinary walk-up fares. The
specification says plainly that the value means a reservation is required on the
outward journey.

The reason to trust the field over the guess: two products carrying it had
*already* been reclassified as Advance fares by a completely unrelated,
behavioural test. A structural field and a behavioural one reaching the same
answer independently is about as much confirmation as this data offers.

**The suppression marker is obsolete** (RSPS5045 4.4.3, field 11) and carries
the same value throughout. Whatever a reasonable guess at the name suggests, it
does nothing.

**Two fields that look like the missing "is this a public fare" flag are not.**
One is set to its negative value on nearly 195,000 flows that carry ordinary
walk-up fares — far too many to mean unsellable, and almost certainly meaning
"not in the printed manual". The other covers 718,998 fares, same reasoning.
Both were checked and neither is used.

---

## Sentinels that are not values

**A price of 99999999 is not a price.** It means no fare is available for that
ticket and railcard combination — and, crucially, it still *overrides*: it
withdraws the flow fare it lands on. 61,433 override records say it. Treating it
as a number produces a fare of £999,999.99; treating it as absent leaves a fare
on sale that should have been withdrawn.

**The open-ended date 31122999 is kept as a real date**, deliberately, so that
`date between start_date and end_date` needs no special case anywhere. It is not
converted to null.

**A public time of 0000 is null, not midnight.**

---

## Dates and byte order

**Restriction date ranges are month-day, not day-month.** (RSPS5045 4.19.) The
proof is in the data rather than the specification: one restriction's ranges,
read as month-day, leave gaps at Easter, both May bank holidays, the August bank
holiday and Christmas — exactly when peak restrictions lift. Read as day-month
they are gibberish.

**In a fares record, byte 0 is the update marker and byte 1 the record type.**
This is the reverse of the obvious guess and cost a bug. Every record on a full
refresh carries the same marker, so reading them the wrong way round produces a
file that parses and contains nothing recognisable.

---

## Where the specification and the data disagree

Three cases. In two of them the specification won.

**Fare rounding goes down, and the specification says up.** (RSPS5045 4.18.1.1.)
The feed carries 36 rounding rule sets and **no field anywhere says which one
applies** — not in the ticket type, not in the discount record, not in the
passenger status record. So the mapping the specification implies cannot be read
from the data at all.

What can be done instead is to test every rule against known-correct prices. Six
fares for one journey with one railcard, checked against a retailer; three
rounding directions; 36 rules; 108 combinations. Exactly two survive, both
rounding **down**, and they are identical at this granularity. Two of the six
fares separate down from nearest, and both land on down.

The same six confirm that the discount percentage is **per mille, not per cent**
— 334 means 33.4%, the familiar railcard third. Reading it as a percentage is
wrong on every discounted fare and not obviously so: £60 becomes £39.80 instead
of £39.96.

The selected rule's contents are asserted by `rail validate`, because a change
there would move every discounted fare and nothing else would notice.

**A restriction band naming a station is about the ends of the journey, not the
middle** — and here the specification corrected a measurement that looked
convincing. (RSPS5045 4.19.8, fields 9 and 10.)

Of the current bands, 32,206 name a station, against only 3 that carry the
marker meaning "via". Implementing intermediate-station bands on the strength of
that made 1,648 fares dearer across eight origins, every one dearer and none
lost — exactly the shape a real correction takes.

It was wrong. The specification describes the field as arrivals at, departures
from *or changing at* the location, and describes the location as denoting a
journey origin, destination or via point. So an arrival or departure band naming
a station is about a journey that *starts or ends* there.

One restriction settles it without needing the specification at all. It has a
departure band at a London terminus and another at a station down the line. A
train leaving the terminus inside the first band passes the second station
inside the second band. If these named *trains*, the two would contradict each
other — one train, allowed at one station and barred at the next. As per-origin
rules they are perfectly consistent.

The lesson is about the measurement, not the rule: 32,206 was a real number
answering a question nobody had asked.

**Doubleback locations are indexed, despite a note saying they need not be.**
(RSPS5047 4.10.3.) The specification defines a location modifier naming where a
doubleback is permitted, and adds a note promising that a matching "via" record
for the same station will also be present for backwards compatibility — which
reads as an invitation to ignore the modifier entirely.

**The promise does not hold in this data.** Of 322 such records, 83 have no via
record for the same station. One easement permits a doubleback through a
particular station and names that station nowhere else, so a consumer trusting
the note drops the easement from every journey it governs. 22 stations are known
to the guide *only* as a doubleback target.

It changes no verdict today, and the reason is worth keeping: Connection Scan
finds earliest arrival, and revisiting a station cannot make an arrival earlier,
so the router never produces a doubleback in the first place. It matters when
the route is *chosen* rather than found — a deliberate stopover.

---

## Two grouping systems that look like one

There are two independent systems for grouping stations, they answer different
questions, and they genuinely disagree. Conflating them is tempting and wrong.

| | fares group | routeing group |
|---|---|---|
| source | the fares location file | the routeing guide's station file |
| code shape | a four-character location number | `Gnn` |
| means | **a ticket to this group is valid here** | **this station routes via that point** |
| size | 43 groups, 115 stations | 67 groups, 214 stations |

Only 18 pair up by shared membership at all, and only 8 of those have identical
membership. The routeing groups are systematically larger.

**Aston is the case to remember.** Its fares group is its own location number in
every validity period the feed carries — it is *not* in the Birmingham fares
group. But it *is* in the Birmingham routeing group. Both are right: Aston has
no routeing point of its own so it routes via Birmingham, and a ticket to
"Birmingham Stations" is still not valid at Aston.

A code leaking between the two systems would widen either ticket validity or
permitted routes with nothing else noticing, so `rail validate` holds the line
in three ways: no station may sit in two groups of the same kind, no `Gnn` may
appear in the fares expansion, and no routeing group may be anything but `Gnn`.
What is deliberately *not* checked is that the two agree, because they do not
and should not.

---

## Conventions that differ between files in the same feed

**Fixed links are stated once and read both ways. Routeing-guide map links are
directional and carry their own reverses.** These are opposite conventions in
one download, and the data says which is which without ambiguity: of 1,149 fixed
link pairs, not one has a reverse record; of 5,874 routeing links, every one
does, and none disagrees with its reverse.

Getting either backwards is damaging in a different direction. Treating fixed
links as one-way uses half of them. Unioning routeing links invents permissions.

**Two schedule files name locations differently.** The main timetable file uses
TIPLOCs; the second one — which carries the services the main format cannot
express, including ferries, hovercraft and rail-replacement coaches — uses CRS
codes. A naive union matches only half, and the half that misses is *dropped*
rather than raised. The resolution to CRS therefore happens once, centrally, and
everything downstream reads that.

**Their line numbers collide**, the files being numbered separately, so an
identifier derived from a line number needs a per-file offset. The two files are
otherwise disjoint — checked rather than assumed; no schedule identifier appears
in both.

---

## Version histories that look like current views

**The fares location file is a version history, not a list of stations.** It
carries every generation of every location's attributes. Filtering on validity
dates is not optional — without it a station has several fare groups at once.

**PlusBus exclusions are a version history too**, shipping two annual
generations at once, so they have to be filtered on the travel date. And the
exclusion is *reversible*: a record from A to B applies from B to A, and the
file carries only one of the two.

**Restrictions ship in two versions simultaneously** — the set in force now and
the next set — each with its own window. Which one applies depends on the travel
date, and for a date past the current set's end the answer is the future one.

---

## Where a marker is a price rather than a bar

**Not every restriction band is a prohibition.** (RSPS5045 4.19.8, field 13.)
One flag leaves the fare valid and charges a minimum fare instead. Only 38
current bands set it, they all belong to railcards, and **one of them covers the
whole day** — so reading it as a bar withdrew a major railcard entirely, at
every time of day.

The companion error is charging the minimum whenever a minimum-fare record
exists. The record is a *price*; the band is *when it is charged*. Applying it
unconditionally charged a weekday minimum on a Sunday.

**A change of trains is barred by the restriction header, not by any band.**
(RSPS5045 4.19.3, field 10.) No time band can express "valid on the booked
service only", and 36 current restrictions say exactly that. The specification
also defines records naming stations where a change *is* permitted despite the
bar — this feed ships none, so a bar has no exceptions here, and `rail validate`
watches for any appearing.

---

## Two readings that only measurement could settle

**A ticket's return shape comes from the ticket type, not from its validity
record.** (RSPS5045 4.6.2, field 9.) Validity codes are shared between singles
and returns, so 185 current *single* ticket types point at a validity code
carrying a return period. Reading the validity alone calls them returns.

The arithmetic is pinned by two specific codes rather than assumed. An ordinary
day return carries a return-days value of 1, and a day return comes back the
same day — so the count is inclusive of the outward day. A five-day return
carries both a window and an earliest-return offset, and its own prose
description names the outward and return weekdays, which fixes the offset as a
plain number of days and confirms the inclusive reading. Neither rule can move by
a day without breaking one of the two.

Months are a calendar offset instead, clamped to the end of the month. The
asymmetry is real and follows from the day-return case: there is no day zero.

**An empty return window is the answer, not a defect.** One weekend product is
valid three days out but may not return until a particular weekday has passed.
Leave on a Wednesday and you must be back by Friday but may not travel until
Monday — nothing satisfies both, so the ticket is simply not for that outward
date. Clamping the window to keep it non-empty would sell an ordinary three-day
return valid any day of the week, which is exactly what reading the day counts
alone produces.

**A field that sounds like an alternative lookup is not one.** (RSPS5045 4.5.2.)
One field is described as an alternative origin or destination code for which a
fare should be calculated, which reads like a second lookup somewhere else. In
all 280,634 records it is simply whichever end of the flow is not a wildcard. So
the "alternative fare" is the fare already computed, and the substance of the
record is the flat amount added afterwards, covering the leg the railcard does
not.

---

## Files that contain nothing

Three files in the routeing feed are around 200 bytes each and contain only a
header. The specification (RSPS5047 4.17–4.19) says outright that they are no
longer used. They are not parsed, and that is not an oversight.

One file in the same feed was never opened for a long time, and that *was* an
oversight worth recording. It ties easements to operators — 993 rows, 624 of
them for easements actually held. The engine believed it had this information
from a different file, which supplied it for exactly 8 easements. So more than
600 published exceptions were being applied to every journey regardless of who
ran the trains. Finding it required sweeping every file in every download against
what the code actually reads, which is a check worth repeating after any feed
version change.
