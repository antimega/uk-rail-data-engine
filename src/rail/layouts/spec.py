"""Declarative record layouts for the DTD fixed-width files.

Offsets are transcribed from RSPS5045 (fares) and RSPS5046 (timetable) and
cross-checked field-by-field against the record definitions in
planarnetwork/dtd2mysql. That project is GPLv3: it is used here to *verify*
offsets, which are facts about the file format, not as a source of code.

All offsets are 0-based from the start of the record, matching the raw line.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from enum import Enum


class Kind(str, Enum):
    #: Trimmed text; blank becomes null.
    TEXT = "text"
    #: Integer; blank or all-asterisk becomes null.
    INT = "int"
    #: ``DDMMYYYY``. The open-ended sentinel 31122999 is kept as a real date so
    #: that ``date BETWEEN start_date AND end_date`` just works.
    DATE = "date"
    #: ``YYMMDD`` as used by CIF. Years >= 60 are 19xx, otherwise 20xx.
    SHORT_DATE = "short_date"
    #: ZTR ``runs_to`` uses CIF dates except that ``991231`` means open-ended.
    #: It becomes the repository-wide finite sentinel 2999-12-31 at ingest.
    ZTR_END_DATE = "ztr_end_date"
    #: ``HHMM`` public timetable time, stored as minutes after midnight.
    #: CIF uses 0000 to mean "no public time", so that becomes null.
    PUBLIC_TIME = "public_time"
    #: ``HHMMH`` working timetable time (trailing H = half minute), stored as
    #: seconds after midnight.
    WORKING_TIME = "working_time"
    #: Single character flag; Y/X/1/T are true, everything else false.
    BOOL = "bool"


@dataclass(frozen=True)
class Field:
    name: str
    start: int
    length: int
    kind: Kind = Kind.TEXT

    @property
    def end(self) -> int:
        return self.start + self.length


@dataclass(frozen=True)
class RecordSpec:
    """One record type within a file, and the table it lands in."""

    #: Output table name, e.g. "schedule". Records sharing a name are unioned,
    #: which is how LO/LI/LT collapse into one stop_time table.
    name: str
    fields: tuple[Field, ...]
    #: Extra constant columns, e.g. {"record_type": "LO"}.
    constants: dict[str, str] = dc_field(default_factory=dict)

    @property
    def width(self) -> int:
        return max((f.end for f in self.fields), default=0)


@dataclass(frozen=True)
class FileSpec:
    """A DTD file: how to find each line's record type, and what each means."""

    extension: str
    feed: str
    records: dict[str, RecordSpec] = dc_field(default_factory=dict)
    #: Set for files where every line is the same record type. Several fares
    #: files are like this, with an I/A/D/R update marker where a multi-record
    #: file would carry its discriminator.
    single: RecordSpec | None = None
    #: Where the record-type discriminator sits in the line.
    key_start: int = 0
    key_length: int = 1
    #: Record types present in the file that we deliberately ignore
    #: (headers, trailers, and record types out of scope).
    ignore: tuple[str, ...] = ()

    def record_for(self, key: str) -> RecordSpec | None:
        if self.single is not None:
            return self.single
        return self.records.get(key)

    @property
    def all_records(self) -> tuple[RecordSpec, ...]:
        if self.single is not None:
            return (self.single,)
        return tuple(self.records.values())

    @property
    def tables(self) -> set[str]:
        return {record.name for record in self.all_records}


def fields(*specs: tuple) -> tuple[Field, ...]:
    """Terse constructor: ``fields(("uic", 2, 7), ("end_date", 9, 8, Kind.DATE))``."""
    built = []
    for spec in specs:
        name, start, length = spec[0], spec[1], spec[2]
        kind = spec[3] if len(spec) > 3 else Kind.TEXT
        built.append(Field(name=name, start=start, length=length, kind=kind))
    return tuple(built)
