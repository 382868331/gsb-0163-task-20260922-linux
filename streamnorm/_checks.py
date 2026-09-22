"""Shared argument-validation helpers (internal, not part of the public API)."""


def require_int(value, *, name, error):
    """Return ``value`` if it is a genuine ``int``; raise ``error`` otherwise.

    ``bool`` is rejected even though it subclasses ``int`` (fixed contract:
    integer parameters reject bool).  Floats are rejected outright — NaN and
    Infinity have no discrete meaning here, and non-integral floats would be
    silently truncated by a naive ``int()`` conversion, which this contract
    forbids.
    """
    if isinstance(value, bool):
        raise error(f"{name}: expected int, got bool {value!r}")
    if isinstance(value, float):
        raise error(
            f"{name}: expected int, got float {value!r} "
            "(NaN/Infinity and fractional values are not accepted)"
        )
    if not isinstance(value, int):
        raise error(f"{name}: expected int, got {type(value).__name__}")
    return value
