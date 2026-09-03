"""Cross-border AML monitoring: typologies, detection, aggregation, investigation.

The layer added on top of the payments warehouse. It does not replace anything:
`core`, `risk` and `audit` keep their meaning, this adds an `aml` schema beside
them and reuses the same money handling, the same hash-chained audit log and the
same AI guardrails.

What it can say:  a pattern is present, here are the transfers it rests on, here
                  is what would argue against it, and here is what we cannot see.
What it cannot:   that anyone laundered money. Nothing here reaches an authority,
                  holds funds, or closes an account.
"""

from .typology import TYPOLOGIES, TYPOLOGY_VERSION, Typology

__all__ = ["TYPOLOGIES", "TYPOLOGY_VERSION", "Typology"]
