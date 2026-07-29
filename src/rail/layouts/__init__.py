from .fares import FARES_FILES
from .spec import Field, FileSpec, Kind, RecordSpec, fields
from .timetable import TIMETABLE_FILES

#: Every file we know how to parse, keyed by its extension.
ALL_FILES: dict[str, FileSpec] = {**TIMETABLE_FILES, **FARES_FILES}

__all__ = [
    "ALL_FILES",
    "FARES_FILES",
    "Field",
    "FileSpec",
    "Kind",
    "RecordSpec",
    "TIMETABLE_FILES",
    "fields",
]
