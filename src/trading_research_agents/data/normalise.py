"""What every normaliser shares. Layouts live in `data.venues` and `data.mapping`.

Each normaliser checks the layout it was written against on every file, not once at
integration time. A source that changes a column silently should stop the pipeline with the
header it actually sent, rather than produce a dataset that looks right.
"""

from __future__ import annotations

NORMALISER_VERSION = "2026.09.13"


class ArchiveFormatError(ValueError):
    """The file is not laid out the way the normaliser was written against."""
