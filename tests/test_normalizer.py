"""Core normalization semantics: blocking, ordering, streaming, limits."""

import unittest

from streamnorm import (
    CharacterTable,
    InputError,
    NormalizerStateError,
    SegmentTooLongError,
    SourceInterval,
    StreamNormalizer,
    normalize,
)

from fixtures import (
    A,
    B,
    M1,
    M2,
    M3,
    M4,
    M5,
    X_A_M2,
    X_A_M3,
    X_AM3_M2,
    tiny_table,
)


def cps(text):
    return [f"U+{ord(c):04X}" for c in text]


class TestDecomposeOrderCompose(unittest.TestCase):
    def setUp(self):
        self.table = tiny_table()

    def test_basic_composition(self):
        result = normalize([chr(A) + chr(M2)], self.table)
        self.assertEqual(result.text, chr(X_A_M2))

    def test_precomposed_round_trip(self):
        # A precomposed char decomposes then recomposes to itself.
        result = normalize([chr(X_A_M2)], self.table)
        self.assertEqual(result.text, chr(X_A_M2))

    def test_recursive_decomposition_then_recomposition(self):
        # X_AM3_M2 expands to (A, M3, M2) and must rebuild itself.
        result = normalize([chr(X_AM3_M2)], self.table)
        self.assertEqual(result.text, chr(X_AM3_M2))

    def test_stable_reordering_by_ccc(self):
        # M1 (ccc 230) before M4 (ccc 210) in input -> swapped, stably.
        # (M1/M4 never compose, so this isolates the reordering step.)
        result = normalize([chr(A) + chr(M1) + chr(M4)], self.table)
        self.assertEqual(result.text, chr(A) + chr(M4) + chr(M1))

    def test_equal_ccc_keeps_input_order(self):
        # M1 and M5 both have ccc 230 and never compose: a stable sort
        # must not swap them, in either input order.
        result = normalize([chr(A) + chr(M1) + chr(M5)], self.table)
        self.assertEqual(result.text, chr(A) + chr(M1) + chr(M5))
        result = normalize([chr(A) + chr(M5) + chr(M1)], self.table)
        self.assertEqual(result.text, chr(A) + chr(M5) + chr(M1))

    def test_same_ccc_blocks_composition(self):
        # A + M1(230) + M2(230): M2 is blocked by the uncomposed M1
        # (previous uncomposed ccc 230 is not < 230), so no composition.
        result = normalize([chr(A) + chr(M1) + chr(M2)], self.table)
        self.assertEqual(result.text, chr(A) + chr(M1) + chr(M2))

    def test_lower_ccc_does_not_block(self):
        # A + M4(210, never composes) + M2(230): the uncomposed M4 has
        # ccc 210 < 230, so M2 still composes with A.
        result = normalize([chr(A) + chr(M4) + chr(M2)], self.table)
        self.assertEqual(result.text, chr(X_A_M2) + chr(M4))

    def test_composition_chain_through_composite(self):
        # A + M3(220) + M2(230): M3 composes onto A, then M2 composes onto
        # the composite (previous uncomposed ccc is the starter's 0).
        result = normalize([chr(A) + chr(M3) + chr(M2)], self.table)
        self.assertEqual(result.text, chr(X_AM3_M2))

    def test_reordering_unblocks_composition(self):
        # A + M2(230) + M3(220): after reordering M3 sits next to A and
        # composes; M2 then composes onto the composite (prev ccc is 0).
        result = normalize([chr(A) + chr(M2) + chr(M3)], self.table)
        self.assertEqual(result.text, chr(X_AM3_M2))

    def test_leading_combining_marks(self):
        # Marks before the first starter: reordered among themselves,
        # never composed (no starter exists in that segment).
        result = normalize([chr(M2) + chr(M3) + chr(A)], self.table)
        self.assertEqual(result.text, chr(M3) + chr(M2) + chr(A))

    def test_leading_marks_equal_ccc_stable(self):
        result = normalize([chr(M1) + chr(M2)], self.table)
        self.assertEqual(result.text, chr(M1) + chr(M2))

    def test_empty_input(self):
        self.assertEqual(normalize([], self.table).text, "")
        self.assertEqual(normalize([], self.table).segments, ())
        self.assertEqual(normalize(["", ""], self.table).text, "")


class TestStreaming(unittest.TestCase):
    def setUp(self):
        self.table = tiny_table()
        # A + M3 + M2 composes; B + M1 stays literal.  Exercises a starter
        # mid-stream and a segment boundary.
        self.text = chr(A) + chr(M3) + chr(M2) + chr(B) + chr(M1)
        self.expected = normalize([self.text], self.table)

    def test_every_two_way_split_matches_one_shot(self):
        for cut in range(len(self.text) + 1):
            chunks = [self.text[:cut], self.text[cut:]]
            result = normalize(chunks, self.table)
            self.assertEqual(result.text, self.expected.text, msg=f"cut={cut}")
            self.assertEqual(result.segments, self.expected.segments, msg=f"cut={cut}")

    def test_every_chunk_size_matches_one_shot(self):
        for size in range(1, len(self.text) + 2):
            chunks = [self.text[i:i + size] for i in range(0, len(self.text), size)]
            result = normalize(chunks, self.table)
            self.assertEqual(result.text, self.expected.text, msg=f"size={size}")
            self.assertEqual(result.segments, self.expected.segments, msg=f"size={size}")

    def test_cross_chunk_starter_and_marks(self):
        # Split between starter and its combining marks, and inside the
        # mark run: the open segment must survive across push() calls.
        n = StreamNormalizer(self.table)
        self.assertEqual(n.push(chr(A)), "")          # segment still open
        self.assertEqual(n.pending_length, 1)
        self.assertEqual(n.push(chr(M3) + chr(M2)), "")
        self.assertEqual(n.pending_length, 3)
        tail = n.push(chr(B))                          # B closes segment 1
        self.assertEqual(tail, chr(X_AM3_M2))
        self.assertEqual(n.push(chr(M1)), "")          # joins B's segment
        self.assertEqual(n.finish(), chr(B) + chr(M1))

    def test_push_after_finish_and_double_finish_rejected(self):
        n = StreamNormalizer(self.table)
        n.push("x")
        n.finish()
        with self.assertRaises(NormalizerStateError):
            n.push("y")
        with self.assertRaises(NormalizerStateError):
            n.finish()

    def test_push_rejects_non_str_without_poisoning(self):
        n = StreamNormalizer(self.table)
        for bad in (b"x", 5, None, ["x"]):
            with self.assertRaises(InputError):
                n.push(bad)
        # Contract violation happened before any mutation: still usable.
        self.assertEqual(n.push(chr(A) + chr(M2)) + n.finish(), chr(X_A_M2))


class TestSourceIntervals(unittest.TestCase):
    def setUp(self):
        self.table = tiny_table()

    def test_starters_only(self):
        result = normalize(["xyz"], self.table)
        self.assertEqual(
            result.segments,
            (SourceInterval(0, 1), SourceInterval(1, 2), SourceInterval(2, 3)),
        )

    def test_intervals_cover_input_across_chunks(self):
        text = chr(A) + chr(M3) + chr(M2) + chr(B) + chr(M1)
        chunks = [text[:2], text[2:4], text[4:]]
        result = normalize(chunks, self.table)
        self.assertEqual(
            result.segments,
            (SourceInterval(0, 3), SourceInterval(3, 5)),
        )

    def test_interval_may_overlap_when_expansion_spans_boundary(self):
        # Z decomposes to (M1, C): the mark joins the previous segment,
        # the starter C opens a new one — both segments cite input char 1.
        z = 0x5A
        table = CharacterTable(
            decompositions={z: (M1, 0x43)},
            ccc={M1: 230},
        )
        result = normalize([chr(A) + chr(z)], table)
        self.assertEqual(result.text, chr(A) + chr(M1) + "C")
        self.assertEqual(
            result.segments,
            (SourceInterval(0, 2), SourceInterval(1, 2)),
        )


class TestSegmentLimit(unittest.TestCase):
    def setUp(self):
        self.table = tiny_table()

    def test_exactly_at_limit_is_accepted(self):
        # 1 starter + 1023 marks = 1024 code points: the documented cap.
        text = chr(A) + chr(M1) * 1023
        result = normalize([text], self.table)
        self.assertEqual(len(result.text), 1024)

    def test_one_past_limit_is_rejected(self):
        text = chr(A) + chr(M1) * 1024  # 1025 code points in one segment
        with self.assertRaises(SegmentTooLongError) as ctx:
            normalize([text], self.table)
        self.assertEqual(ctx.exception.limit, 1024)
        self.assertEqual(ctx.exception.segment_start, 0)

    def test_limit_counts_decomposed_code_points(self):
        # The cap applies after decomposition.  mark_pair expands to two
        # marks, so 512 of them plus the starter exceed 1024 code points.
        mark_pair = 0xE050
        table = CharacterTable(
            decompositions={mark_pair: (M1, M2)},
            ccc={M1: 230, M2: 230},
        )
        ok = chr(A) + chr(mark_pair) * 511 + chr(M1)        # 1 + 1022 + 1 = 1024
        self.assertEqual(len(normalize([ok], table).text), 1024)
        too_long = chr(A) + chr(mark_pair) * 512            # 1 + 1024 = 1025
        with self.assertRaises(SegmentTooLongError):
            normalize([too_long], table)

    def test_custom_limit(self):
        n = StreamNormalizer(self.table, max_segment_length=2)
        n.push(chr(A) + chr(M1))          # 2 code points: ok
        with self.assertRaises(SegmentTooLongError) as ctx:
            n.push(chr(M2))               # would be the 3rd
        self.assertEqual(ctx.exception.limit, 2)

    def test_instance_poisoned_after_overflow(self):
        n = StreamNormalizer(self.table, max_segment_length=1)
        with self.assertRaises(SegmentTooLongError):
            n.push(chr(A) + chr(M1))
        with self.assertRaises(NormalizerStateError):
            n.push("more")
        with self.assertRaises(NormalizerStateError):
            n.finish()

    def test_new_segment_resets_budget(self):
        # Each starter opens a fresh segment with a fresh budget.
        text = (chr(A) + chr(M1) * 1023) * 3
        result = normalize([text], self.table)
        self.assertEqual(len(result.segments), 3)


if __name__ == "__main__":
    unittest.main()
