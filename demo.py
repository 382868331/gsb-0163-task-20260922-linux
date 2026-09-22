"""Deterministic offline demo for streamnorm.

Runs in well under 8 seconds.  Prints one normal result (with chunk-size
invariance and idempotency checks, computed for real) and one actually
triggered rejection boundary (the 1024-code-point segment cap), plus a
cyclic-table rejection.  No sleeps, no prerecorded output, no network.
"""

from streamnorm import (
    CharacterTable,
    CyclicDecompositionError,
    SegmentTooLongError,
    StreamNormalizer,
    normalize,
)

# --- A small explicit, invented character table -------------------------
# Starters: ASCII letters.  Marks live in the Private Use Area and are
# entirely made up for this demo; the table below is the *only* authority.
A, B = 0x41, 0x42
HOOK, DOT, RING = 0xE000, 0xE001, 0xE002          # ccc 220, 230, 230
A_HOOK, A_DOT, B_DOT = 0xE100, 0xE101, 0xE103     # one-mark composites
A_HOOK_DOT = 0xE102                               # (A+HOOK)+DOT, recursive

TABLE = CharacterTable(
    decompositions={
        A_HOOK: (A, HOOK),
        A_DOT: (A, DOT),
        A_HOOK_DOT: (A_HOOK, DOT),   # expands to (A, HOOK, DOT): depth 2
        B_DOT: (B, DOT),
    },
    ccc={HOOK: 220, DOT: 230, RING: 230},
    compositions={
        (A, HOOK): A_HOOK,
        (A, DOT): A_DOT,
        (A_HOOK, DOT): A_HOOK_DOT,
        (B, DOT): B_DOT,
    },
)

NAMES = {
    A: "A", B: "B",
    HOOK: "<hook:220>", DOT: "<dot:230>", RING: "<ring:230>",
    A_HOOK: "A+HOOK", A_DOT: "A+DOT", A_HOOK_DOT: "A+HOOK+DOT", B_DOT: "B+DOT",
}


def show(text):
    return " ".join(NAMES.get(ord(ch), f"U+{ord(ch):04X}") for ch in text) or "(empty)"


def main():
    print("=== streamnorm demo (explicit table, fully offline) ===\n")

    # 1. A normal result: unordered marks get reordered, then compose.
    sample = chr(A) + chr(DOT) + chr(HOOK) + chr(B) + chr(DOT) + chr(RING)
    one_shot = normalize([sample], TABLE)
    print(f"input      : {show(sample)}")
    print(f"normalized : {show(one_shot.text)}")
    print(f"segments   : {list(one_shot.segments)}  (source intervals into the input)")

    # 2. Chunk-size invariance, computed for real.
    print("\n-- chunk-size invariance --")
    for size in (1, 2, 3, 5, len(sample)):
        chunks = [sample[i:i + size] for i in range(0, len(sample), size)]
        result = normalize(chunks, TABLE)
        ok = result == one_shot
        print(f"chunk size {size:2d}: identical text+intervals -> {ok}")
        assert ok, "chunking changed the result!"

    # Streaming push/finish gives the same answer too.
    n = StreamNormalizer(TABLE)
    streamed = n.push(sample[:2]) + n.push(sample[2:4]) + n.push(sample[4:]) + n.finish()
    assert streamed == one_shot.text
    print(f"push/finish streaming API agrees -> {streamed == one_shot.text}")

    # 3. Idempotency, computed for real.
    print("\n-- idempotency --")
    again = normalize([one_shot.text], TABLE)
    print(f"normalize(normalize(x)) == normalize(x) -> {again.text == one_shot.text}")
    assert again.text == one_shot.text

    # 4. A real rejection: the 1024-code-point segment cap.
    print("\n-- rejection boundary: segment cap (1024 code points) --")
    ok_text = chr(A) + chr(HOOK) * 1023          # exactly 1024 code points
    ok_result = normalize([ok_text], TABLE)
    print(f"1024-code-point segment accepted -> normalized to {len(ok_result.text)} "
          "code points (one A+HOOK composition)")
    try:
        normalize([chr(A) + chr(HOOK) * 1024], TABLE)   # 1025: one too many
    except SegmentTooLongError as exc:
        print(f"1025-code-point segment rejected -> {type(exc).__name__}: {exc}")

    # 5. A real rejection: cyclic decomposition table.
    print("\n-- rejection boundary: cyclic table --")
    try:
        CharacterTable(decompositions={0x41: (0x42,), 0x42: (0x41,)})
    except CyclicDecompositionError as exc:
        print(f"cyclic decomposition table rejected -> {type(exc).__name__}: {exc}")

    print("\ndemo finished: all checks passed")


if __name__ == "__main__":
    main()
