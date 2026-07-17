"""measure-twice — local benchmarking toolkit + tier-claim evidence ledger.

Package at repo root (NOT ``src/``), mirroring the sibling switchboard convention.
The core is stdlib-only; ``switchboard`` is the sole runtime dependency (imported for the
agreement/kill math — never re-implemented). See ``plan.md`` for the full design.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
