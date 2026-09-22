"""Independent one-shot naive reference implementation, for cross-checking.

Deliberately written in the most naive whole-string style (plain dicts,
recursive decomposition with an explicit trail, insertion reordering,
linear composition scan).  It shares no code with the ``streamnorm``
package under test and never imports it, so agreement between the two is
meaningful evidence.
"""


def _fully_decompose(cp, decompositions, trail=()):
    if cp in trail:
        raise ValueError(f"cyclic decomposition at U+{cp:04X}")
    mapping = decompositions.get(cp)
    if mapping is None:
        return [cp]
    out = []
    for part in mapping:
        out.extend(_fully_decompose(part, decompositions, trail + (cp,)))
    return out


def naive_normalize(text, decompositions, ccc, compositions):
    """Decompose -> reorder -> compose, whole string at once."""
    # 1. full recursive decomposition
    cps = []
    for ch in text:
        cps.extend(_fully_decompose(ord(ch), decompositions))

    # 2. canonical ordering: insert each char, bubbling left past
    #    strictly-higher non-zero ccc neighbours (stable)
    ordered = []
    for cp in cps:
        ordered.append(cp)
        i = len(ordered) - 1
        while i > 0:
            prev_cc = ccc.get(ordered[i - 1], 0)
            cur_cc = ccc.get(ordered[i], 0)
            if cur_cc == 0 or prev_cc == 0 or prev_cc <= cur_cc:
                break
            ordered[i - 1], ordered[i] = ordered[i], ordered[i - 1]
            i -= 1

    # 3. canonical composition with the blocking rule
    out = []
    starter = None
    prev_cc = 0
    for cp in ordered:
        cc = ccc.get(cp, 0)
        composite = None
        if starter is not None and (prev_cc == 0 or prev_cc < cc):
            composite = compositions.get((out[starter], cp))
        if composite is not None:
            out[starter] = composite
            continue
        out.append(cp)
        if cc == 0:
            starter = len(out) - 1
        prev_cc = cc
    return "".join(chr(c) for c in out)
