"""Pure domain logic: financial calculation, with no I/O of any kind.

Nothing in this package imports from `akmarkets.db`, `akmarkets.providers`,
`akmarkets.web` or `akmarkets.services`. It takes plain values and frozen
records in, and returns plain values and frozen records out. That is what
makes the figures testable to the cent without a database, and it is
enforced by `tests/test_layering.py` rather than left to discipline.
"""
