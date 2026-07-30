# Ticket types: what the feed ships, how it is classified, and what uses it

The fares feed carries **3,425 current ticket types** and has no field saying
which of them is a fare a member of the public can buy for a journey. It ships
staff passes, carnets, penalty notices, tour-operator rates, seat upgrades,
railcard-shaped age conditions, dummy records and things described `FOR TEST USE
ONLY`, all in the same table as `SDS ANYTIME DAY S`.

So the classification is a set of rules over the feed's own labels, and this
document is what they are, why each exists, and what reads the answer.

**Everything here is derived from `ticket_type_current`**, built by
`build_fares_reference` in [`model/fares.py`](../src/rail/model/fares.py). Two
tables publish the reasons - `fare_reject` and `advance_reject` - so a
classification can be argued with rather than taken on trust.

```bash
rail tickets                    # every type, its class, and why
rail tickets advance            # filter by code, description or class
rail tickets --review           # what is new or has moved since the register
```

---

## The four classes

| class | types | fares | what uses it |
|---|---|---|---|
| **walk-up** | 824 | 3,175,645 | `rail reachable`, `rail roundtrip`, `rail stopover`, `rail fares` - and the **Railaway** map |
| **advance** | 1,437 | 2,828,084 | `--advance` on the CLI, `include_advance` / `advance_only` in the library - and the **Advances** map |
| **not-a-real-advance** | 63 | 947 | nothing prices it; see below |
| **rejected** | 1,101 | 1,213,684 | nothing at all |

A rejected type can still carry a million fares. That is the point: the feed
prices things that are not fares to somewhere, and 1.2M of its 7.6M fare records
belong to products no journey planner should quote.

### walk-up - `is_walk_up`

A fare you can turn up and buy. This is the default everywhere, and it is the
class Railaway maps, because a walk-up price is buyable by definition.

The biggest families are exactly what you would expect:

```
ANYTIME DAY S   3 codes   386,387 fares     ANYTIME R      3 codes  215,628
OFF-PEAK R      2 codes   307,140           ANYTIME 1R     3 codes  172,171
ANYTIME DAY R   2 codes   275,489           ANYTIME DAY 1S 2 codes  164,429
OFF-PEAK DAY R  5 codes   220,942           OFF-PEAK 1R    2 codes  122,214
```

### advance - `is_real_advance`

A fare tied to a booked train, that a member of the public can buy. Opt-in,
because the feed carries Advance **prices** and no **availability** - the quota
field is empty throughout - so these are the best published price rather than a
bookable one.

```
ADVANCE       383 codes  1,684,990 fares    ADVANCE STDPREM  27 codes  10,727
ADVANCE 1ST   205 codes  1,022,076          LUMOFIXED        36 codes   4,495
LumoFixed      33 codes     60,900          ADVANCE PROMO     3 codes   3,373
Semi Flex R     9 codes     29,304          AIRPORT ADV STD  14 codes   1,626
```

Several of these do not call themselves Advances and are caught structurally -
`LumoFixed`, `Semi Flex R`, `SUPERFARE`, the `70min Flex` family. See
[the five signals](#how-a-type-is-classified) below.

**Sleeper berths are Advances**, and it is worth saying why they look wrong.
`CLASSIC SOLO` is eight ticket codes sharing one description, on the same six
flows, at £155 / £180 / £200 / £225 / £250 / £275 / £295 / £315 - with **no
walk-up fare on the flow at all**. That is an Advance quota ladder, the same
shape as `Day-Flex`'s `FE0`-`FE9`. A sleeper runs one train a day, so its fares
are genuinely Advance-shaped however far above a seated fare they sit. The twins
are excluded on `max_passengers` and the Caledonian Sleeper's 10p staff and
friends-and-family berths on the flat-rate test.

### not-a-real-advance - `is_advance_fare and not is_real_advance`

**`is_advance_fare` is a residual, not a classification.** A sellable type is
`is_walk_up` when none of five booked-train signals fires and `is_advance_fare`
when any of them does - so the Advance half means "sellable and not a walk-up",
which is not the same thing as an Advance ticket. 63 types land there and are
not Advances anybody can buy. `advance_reject` says why:

| excluded | why |
|---|---|
| 20 | no reservation needed, so not tied to a booked train |
| 19 | sold through one retailer scheme, not published |
| 14 | rover or explorer pass, not a single journey |
| 8 | a change to a ticket already held, not a fare |
| 2 | operator loyalty scheme, not a published fare |
| 1 | a named scheme, not a published fare |

**The case this exists for.** Validity code `11` is *described* "AS ADVERTISED"
and its `out_description` reads `BOOKDTRAINONLY`. That one field made Grand
Central's `GTS ANYTIME S` an Advance - 205 fares, **not one carrying a
restriction**, `reservation_required = 'N'` - at 0.61 of the real Advance on the
same flow, and it duly won as the cheapest "Advance" to Hartlepool and Thirsk.

The rule is that the other two signals outvote the validity: **a fare needing no
reservation, with no booked-train restriction on any of its prices, and not
calling itself an Advance is not tied to a booked train.**
`reservation_required` and the restriction are statements about the product; the
validity's `out_description` is one field on a code shared between products.

The rest is `PSEUDO_ADVANCE_MARKERS` - retailer schemes (`Secret Fare`,
`Seatfrog SF`, `PARTNER OFFER`, `BOOKING.COM`, `OMIO`, `MEGATRAIN`), rovers
(`HLAND EX*`, `GREAT SCOT`), and swaps.

Measured, narrow class against residual, **every price that moves gets dearer
and none cheaper**:

```
YRK  2,432 priced    2 dearer   HPL £20.80 -> £25.60, THI £8.00 -> £8.10
EUS  1,789          92 dearer   CDF £15.00 -> £29.00   (Secret Fare)
PAD  1,774          74 dearer
SRA  1,886          25 dearer   CBG  £7.00 ->  £8.00   (Seatfrog SF)
GLC  2,492          44 dearer, 29 lost their only Advance  (PARTNER OFFER)
INV  2,717           0 dearer, nothing moves at all
```

### rejected - not `is_sellable`

Not a fare to somewhere. `fare_reject` carries the reason for every one:

| excluded | why |
|---|---|
| 225 | flat rate, not a distance-based fare |
| 217 | family or group product (`max_passengers > 1`) |
| 153 | group product, priced per person within a party |
| 83 | inclusive tour rate, priced inside a package |
| 60 | bundle of journeys, not a single fare |
| 59 | complimentary, not sold |
| 54 | family product priced for several people |
| 47 | supplement, not a fare on its own |
| 45 | age-restricted fare, not an adult fare |
| 45 | package, priced with parking or admission |
| 23 | corporate scheme, not sold to the public |
| 17 | test data in the feed |
| 15 | staff travel, not sold to the public |
| 15 | not for travel |
| 13 | concessionary fare, not an adult fare |
| 10 | dummy record, the feed says do not use |
| 6 | flexi bundle, priced for several journeys |
| 6 | penalty fare notice, not a ticket |
| 4 | pay-as-you-go information record |
| 2 | a fee to change a ticket, not a fare |
| 1 | upgrade bought on board, not a fare on its own |
| 1 | train swap, not a fare on its own |

Every one of those rows was added because a real product slipped through and
priced a journey absurdly. A few worth knowing:

- **`SCR GROUP 05`** at 80p was the cheapest fare from Glasgow Central to 358 of
  its 2,748 destinations. Group products declare `max_passengers = 1`, because
  the price is per person *within* a party.
- **`25Q STDPREM ONBOARD`** is Avanti's on-board upgrade. Its price varies with
  distance, its validity is the ordinary "ON DATE SHOWN", and it declares one
  passenger - so nothing structural sees it. It was the cheapest standard
  walk-up to 44 destinations from Euston and 52 from Liverpool.
- **`ILF DUMY-DO NOT USE`** carried 8 fares from £18.90 to £26.90 and was the
  winning cheapest walk-up on **every one of its flows**.
- **`NS1 SN-GEX 1ST S UP`** is a Gatwick Express upgrade whose 15-character
  description ran out of room before `UPG`. Both its fares are **£0.00**, so as
  a walk-up it won every comparison it entered.

**The description field is 15 characters**, which is why several markers look
too wide: `%UPG%` and `% UP` for the same word truncated at two lengths, `%DUM%`
because the feed ships `Z12 NR SDS DUMM` and `Z123 NR SDS DUM`, `%SUPP%` because
`GE PM PEAK SUPP` runs out before the `L`.

---

## How a type is classified

In order. The first rule that fires decides.

```
1. not sellable?                         -> rejected
     tkt_group 'E', a flexi bundle, package_mkr <> 'N',
     max_passengers > 1, a flat rate, or a NON_PUBLIC_MARKERS description

2. any of five booked-train signals?     -> not a walk-up
     a. the feed's own advance-ticket file (TAP) names it
     b. its validity says BOOKDTRAINONLY / BOOKEDTRAIN
     c. every one of its fares carries a booked-train restriction
     d. reservation_required <> 'N'
     e. the description contains ADVANCE
   none of them?                         -> walk-up

3. of those, still an Advance anyone can buy?
     not a PSEUDO_ADVANCE_MARKERS description, and not
     (no reservation and no booked-train restriction and no ADVANCE in the name)
                                          -> advance
   otherwise                              -> not-a-real-advance
```

Two of the five signals are the feed making a **statement of fact** rather than
us reading prose, and they are the ones to trust:

- **`RESERVATION_REQUIRED`** (TTY field 23, RSPS5045 4.6.2). `'N'` is the only
  value meaning no reservation - `'O'` and `'R'` are outward, `'B'` both legs,
  `'E'` either. It is what catches the 36 `AIRPORT ADV` ladders and the
  `BOOKING.COM` fares, which name a booked train in their description, their
  validity and their restriction precisely nowhere.
- **`PACKAGE_MKR`** (TTY field 29). `'N'` is not a package. The `TPK` file names
  45 ticket codes and `package_mkr <> 'N'` marks 45, and they are **the same
  45** - two independent parts of the feed agreeing on the membership, which is
  why this is trusted over descriptions. The `8A*` series is described `ANYTIME
  DAY S`, `OFF-PEAK R`, `ANYTIME DAY R`, indistinguishable from the ordinary
  fare of the same name, and `8AB` is priced from £5.10 across 2,825 flows.

**What is *not* a signal, having been checked:** `publication_ind` looks like the
missing "is this public" flag and covers 194,630 flows carrying walk-up fares,
far too many to mean unsellable - it is almost certainly "not in the printed
manual". `usage_code = 'G'` covers 718,998 fares, same reasoning. Neither is
used.

### Where the classification is structurally weak

**83 sellable types sit on a single flow, and the flat-rate test cannot judge
those at all** - with one flow the modal share is trivially 1.0. That is how a
5p `FAM&FRIENDS STD`, several ITX rates and a 75p `TrainLinkC16-18` got through,
each found several exclusion rounds later. Blanket-excluding single-flow types
would be wrong, `XOS OFF-PEAK DAY S` at £5.20 being a legitimate niche fare, so
the description markers carry that weight instead.

It is the one weakness here that keeps producing, and it is why the review
below exists.

---

## Reviewing a new generation

**A new generation ships new ticket types, and a misclassified one is silent.**
It lands in the wrong class and wins immediately, because the wrong class is
nearly always the cheaper one. None of the cases above raised anything; every
one was found by hand, months later, by someone checking a price against a
retailer.

So [`src/rail/reviewed_tickets.json`](../src/rail/reviewed_tickets.json) records
what every code was classified as when somebody last looked. It is checked in -
`data/` is git-ignored, and this is a human judgement that belongs in a diff.

```bash
rail refresh                # brings the new generation
rail tickets --review       # what is new, and what has moved class
```

```
┏━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━┳━━━━━┳━━━━━┓
┃ code ┃ description   ┃ class   ┃ fares   ┃ why ┃ new ┃
┡━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━╇━━━━━╇━━━━━┩
│ SDS  │ ANYTIME DAY S │ walk-up │ 386,315 │ -   │ yes │
│ 2AA  │ ADVANCE       │ advance │ -       │ -   │ yes │
└──────┴───────────────┴─────────┴─────────┴─────┴─────┘
moved 2AA ADVANCE: walk-up -> advance
1 new, 1 changed class.
1 of them already carry fares - SDS
```

Three things it reports, and they are not equally urgent:

- **added** - a code the register has never seen.
- **moved** - a code whose class has changed. `GTS ANYTIME S` did not arrive
  new; it was reclassified by a validity record that had always been there.
- **withdrawn** - a code the feed no longer ships. Cannot misprice anything.

**Only codes already carrying fares set an exit code**, and `rail tickets
--review` exits 1 on those. A code an operator has registered without filing
prices cannot be wrong about anything yet. `rail validate` reports the same
thing as a **warn** rather than a fail, so an ordinary refresh does not stop on
a Tuesday.

### What to do with one

Look at what it is, then either:

- **agree** - it is in the right class, so accept it; or
- **change the rules** in `model/fares.py` - add a marker with the reason beside
  it, rebuild, and check what moved.

Then:

```bash
rail tickets --accept       # records the current classification
git add src/rail/reviewed_tickets.json && git commit
```

**The register is not an override.** There is no way to write in it that `GTS`
is really a walk-up: editing a class and re-reviewing simply reports the code as
having *moved*. That is deliberate. The rules carry their reasons in comments
beside them and the two reject tables publish those reasons per code, which is
what makes the classification arguable; an override would move a decision out of
that record and into a file nothing explains, and it would be the first thing to
go stale.

### Useful things to check a new code against

```bash
rail tickets NEW                        # its class and reason
rail fares --from EUS --to BHM --advance   # every fare for a pair, with its
                                           # route, restriction and validity
```

And the signature that has found several: rank a type's median price as a
fraction of the next-cheapest fare on the same flow. An upgrade, a swap or a
concession sits far below 1.0, because that is what those things are. It is how
`25Q STDPREM ONBOARD`, ten age-restricted products and `Secret Fare` at 0.79
were all found.

---

## Related

- [CAPABILITIES.md](CAPABILITIES.md) - what the engine can answer, and the
  caveats on each.
- [INTERPRETING-THE-FEEDS.md](INTERPRETING-THE-FEEDS.md) - the conventions that
  bite when reading the raw records.
- `model/fares.py` - `NON_PUBLIC_MARKERS`, `PSEUDO_ADVANCE_MARKERS`,
  `BOOKED_TRAIN_PHRASES`, and the SQL that applies them.
- `model/tickets.py` - the register, and why it is a record rather than a
  control.
