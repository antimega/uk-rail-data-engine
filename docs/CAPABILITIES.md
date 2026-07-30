# What this can and cannot answer

**An accredited journey planner or retailer is authoritative; this is not.**

The data is supplied "as is" by its publishers. Everything below is derived from
it, and nothing here may imply official status or endorsement. Where a figure
matters - a fare you intend to buy, a connection you intend to make - check it
with a retailer. Several sections below exist precisely because a retailer check
found this engine wrong.

That said, it is right about a great deal, and the point of this page is to say
which is which.

---

## Answers well

These are the questions the engine was built for. Each has been checked against
a real journey planner or retailer on specific journeys.

**Journey times, one origin to everywhere.**
`rail journey-times --from YRK --date 2026-08-04 --profile`
A Connection Scan over two consecutive days. One scan answers every destination
in Britain at once, in tens of milliseconds, so sweeping departures across a day
and keeping the best journey per station is cheap. `--profile` does that sweep,
which is the fair way to compare a Sunday against a weekday - a single departure
time flatters whichever day happens to suit it.

**Everywhere reachable within a budget.**
`rail reachable --from YRK --date 2026-08-01 --max-fare 20 --railcard YNG`
Prices every destination from one origin in a fraction of a second. Optionally
applies railcards, PlusBus add-ons, the fare's own route conditions, and the
routeing guide.

**Every fare between a pair, and what governs it.**
`rail fares --from YRK --to KGX`
Lists each fare with its route, its restriction code and its validity period.
Deliberately *not* filtered by time: a peak-barred fare belongs in the answer
with its restriction named, not removed from it.

**What a restriction code actually bars.**
`rail restrictions 0W`
Spells the time bands out in English. Every band is a bar rather than a
permission, which is the thing most often got backwards.

**Return versus two singles.**
`rail roundtrip --from YRK --to KGX --date 2026-08-04 --return-on 2026-08-06`
Neither product reliably wins, which is why this needs its own command. Across a
sample of ten hand-picked pairs the return won seven and two singles won three.
This is also the only command that routes the journey home, and therefore the
only one that can evaluate return-leg restrictions at all.

**Breaking a journey.**
`rail stopover --from YRK --to KGX --via DON --date 2026-08-04 --dwell 90`
Priced as one ticket, which is the point. Only fares whose validity permits a
break of journey are offered - and a validity that says nothing is *not* treated
as permission.

**Distance, in both senses.**
`rail distance --from YRK --to KGX`
Rail miles from the routeing guide's own station-link graph, and straight-line
distance from grid references. These answer different questions and are kept
apart: one is a routeing *rule*, the other is not a rule at all. The pair
reproduces two published figures for York–King's Cross that look irreconcilable
until you notice they are measuring different things.

**Routeing-guide permission, with easements.**
`rail reachable --check-guide`, `rail routings --from YRK --to PNZ`
Returns permitted, not permitted, or **no opinion** - and the third must never
be read as a refusal. Published exceptions are applied, in both directions:
there are more that *grant* a route than withdraw one, so ignoring them was
never the conservative choice it looked like.

**PlusBus.** `rail plusbus YRK --with LDS` - including the rule that an add-on
cannot be sold when both ends sit in the same zone.

**What actually calls at a location.** `rail stations` - the feeds include bus
interchanges, ferry piers and metro stops alongside national rail stations, and
the timetable can tell them apart better than any supplied flag.

**Data quality.** `rail validate` - 76 checks, exit code 1 on any failure. The
ones that matter most are the ones that would catch a drifted parse: every stop
location resolving to a known code, every fare to a known ticket type, no
journey running backwards in time, a weekday busier than a Sunday.

---

## Answers with caveats

Right in mechanism, limited by what the feed carries. Each of these will give
you an answer; read the caveat before trusting it.

**Advance fares** (`--advance`). The prices are real and vary sensibly with
distance. What the feed lacks is **quota** - which price point is on sale for a
given train on a given date. The relevant field is empty throughout. So these
are the best *published* price, not a bookable one, and that is why they are
opt-in rather than on by default.

The library also takes `advance_only`, which returns Advances *instead of*
walk-ups rather than as well as - for the caller asking what the cheapest
Advance is rather than what the cheapest fare is. The quota caveat applies with
more force there: a walk-up price is buyable by definition and an Advance price
is not, so an answer made only of Advances is made entirely of prices that may
not be on sale.

**The two Advance switches read different columns, and the difference is
deliberate.** A sellable ticket type is `is_walk_up` when none of five
booked-train signals fires and `is_advance_fare` when any of them does - so the
second is a *residual*, "sellable and not a walk-up", which is not the same
thing as "an Advance ticket". `is_real_advance` is the narrower class, and
`advance_reject` records why each of the 64 exclusions is not one:

| excluded | why |
|---|---|
| 20 | no reservation needed, so not tied to a booked train |
| 19 | sold through one retailer scheme, not published |
| 14 | rover or explorer pass, not a single journey |
| 8 | a change to a ticket already held, not a fare |
| 3 | an operator loyalty or named scheme |

`include_advance` *widens* an answer, so it reads the residual: adding a
retailer's fare to a list of walk-ups over-reports a little, and narrowing it
would silently move every existing caller. `advance_only` *is* the answer, so it
reads the narrow class - quoting a Highland Rover or a transfer fee as "the
cheapest Advance" would not be over-reporting, it would be wrong.

The case that made this necessary: validity code `11` is *described* "AS
ADVERTISED" and its `out_description` reads `BOOKDTRAINONLY`, which put Grand
Central's `GTS ANYTIME S` - 205 fares, not one carrying a restriction,
`reservation_required = 'N'` - into the Advance class at 0.61 of the real
Advance on the same flow. Measured on the narrow class against the broad one,
every price that moves gets **dearer and none cheaper**: 2 destinations from
York, 92 from Euston (Cardiff £15.00 to £29.00, a `Secret Fare`), 25 from
Stratford, and 29 from Glasgow Central lose their only Advance because a
`PARTNER OFFER` was all they had. Inverness does not move at all.

**Sleeper berths stay in.** `CLASSIC SOLO` prices in an eight-rung ladder from
£155 to £315 on one flow with no walk-up on it at all, which is what an Advance
quota ladder looks like; a sleeper runs one train a day, so its fares are
genuinely Advance-shaped whatever their size relative to a seated fare. Two things about Advance prices are worth knowing before relying
on one:

- **Neither the day nor the departure time changes them.** An Advance
  restriction says "valid on the booked train", not "not before 09:29", so
  nothing in the feed varies the price by when you travel. Measured across three
  days and five departure times from one origin: no difference at all.
- **Railcards apply, but not all of them.** The standard cards discount an
  Advance by the usual third; the Network Railcard and the Annual Gold Card
  discount none of them, which the feed models through its railcard-ban records
  rather than by omitting the discount. Checking only that a discount exists
  gets this wrong.

**Railcards.** The discount chain is fully implemented - percentage, minimum
fares, geography, operator and product bans, non-standard discount add-ons. Two
limits. The feed's **minimum-fare coverage is thin**: 12 railcards have any, and
for the 16-25 every listed ticket code is a Travelcard, so an ordinary single
takes no minimum and can come out below a retailer's quote. And the **rounding
rule had to be chosen by measurement**, because nothing in the feed says which
of its 36 rule sets applies - see
[INTERPRETING-THE-FEEDS.md](INTERPRETING-THE-FEEDS.md).

**Which fares count as ordinary walk-up fares.** There is no single flag for
this, and the feed ships a great deal that is not an adult retail fare. Two
structural fields do part of the job; the rest is a curated set of rules over
descriptions and behaviour. Every exclusion is recorded in the `fare_reject`
table with its reason. The structural weakness is known: a ticket type appearing
on a single flow cannot be judged by the statistical tests at all, and that is
where the errors found so far have been.

**Return-leg restrictions.** Evaluated only by `rail roundtrip`. Judging one
needs the time you travel back, so a one-to-all sweep cannot do it. `rail
reachable` filters the outward journey only and says so in its footer.

**Cross-London journeys** are validated by the routeing guide as two halves with
a transfer between, which is how the guide itself works, rather than as one
route. Related: rail mileage between two London terminals is nonsense, because
the link graph has no link between them.

**The routeing guide is stricter than the real one.** The guide judges the two
ends of a journey by more permissive local rules than the middle; this judges
the whole path by the map rules. It can therefore only turn a permission into a
refusal, never the reverse - the safe direction, and the reason it has not
produced a wrong fare. A refusal means "not obviously permitted", not
"forbidden".

**Two clocks, and you have to pick the right one.** `Journey.minutes` counts
from the moment you asked, so it includes waiting for the first train.
`ScanResult.journey_minutes_to()` counts from the first boarding, which is the
number a timetable would show. York to Cardiff is 4h23 by the second and 4h59
by the first, because the train leaves at 09:36.

`rail journey-times` and `rail reachable` show both, in columns named
`journey` and `elapsed`, so the difference is visible rather than something you
have to know about. York to Poppleton is a five-minute journey and nineteen
minutes elapsed, which is a fourteen-minute wait on the platform at York.

`--profile` reports the **journey** only, and must: it sweeps many departures
and there is no single wait, arrival or elapsed time across a window.

Not computed: time actually moving as against waiting at an intermediate
change. The journey time includes both.

**An evening query answers with next-morning arrivals** rather than
"unreachable", because the network loads two consecutive days - which is
necessary for sleepers, whose portions are dated the following day. The results
are marked, but the effect is real: a late-evening query reaches most of Britain
by breakfast.

---

## Cannot answer

Not limitations of this code. The information is not in the feeds.

- **Seat availability and reservations.** Nothing.
- **Which Advance bucket is on sale.** The prices are there; the quota is not.
- **Live running, delays, cancellations on the day, disruption.** These are
  future timetables. Short-term plan changes *are* applied - cancellations and
  overlays resolve properly - but that is the published plan changing, not
  today's railway.
- **Split ticketing.** Deliberately out of scope: it is a search over
  itineraries and ticket combinations, a different problem from pricing one
  journey.
- **Ticket calendars** - when a ticket is on sale at all, as opposed to when it
  may be used. Parsed, not evaluated, covering 114 walk-up ticket codes.
- **The operator qualifier on a restriction band.** Parsed and not applied. It
  looks like a large correction and it may well be one, but the single case
  tested against a retailer turned out to be barred for an unrelated reason, so
  two errors were cancelling. It needs real quotes before it can be trusted.
- **One of the routeing guide's doubleback rules**, which states its conditions
  as fare comparisons - the guide would have to ask the fares engine a question
  while the fares engine is asking the guide one.
- **Anything about a station that no feed states.** Two Underground stations are
  classified here as national rail stations because national rail services call
  there on shared track; nothing in the data distinguishes that from a new
  station opening.

---

## How to check a number you doubt

In order of usefulness:

1. **`rail fares --from A --to B`** lists every fare with the records governing
   it. If a price looks wrong, its route or restriction usually explains it.
2. **The `fare_reject` table** says why a ticket type was not treated as a
   walk-up fare, with a reason string.
3. **`rail validate`** catches a drifted parse rather than a modelling mistake,
   but it catches that decisively.
4. **Compare against a retailer**, with the date, the departure time and the
   flags written down. A figure quoted without them is not reproducible - the
   build horizon rolls forward, so the reachable set genuinely changes over
   time, and an undated figure will eventually accuse working code.
