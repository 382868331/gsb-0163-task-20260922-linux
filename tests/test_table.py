"""Table validation and cycle-detection tests."""

import math
import unittest

from streamnorm import (
    CharacterTable,
    CyclicDecompositionError,
    InputError,
    StreamNormalizer,
    TableError,
    normalize,
)


class TestTableValidation(unittest.TestCase):
    def test_empty_table_is_valid(self):
        table = CharacterTable()
        self.assertEqual(table.ccc_of(0x41), 0)
        self.assertEqual(table.expansion_of(0x41), (0x41,))
        self.assertIsNone(table.compose(0x41, 0x42))

    def test_bool_rejected_for_code_point(self):
        with self.assertRaises(TableError):
            CharacterTable(ccc={True: 5})
        with self.assertRaises(TableError):
            CharacterTable(decompositions={0x41: (True,)})

    def test_bool_rejected_for_ccc_value(self):
        with self.assertRaises(TableError):
            CharacterTable(ccc={0x41: False})

    def test_float_rejected_including_nan_and_infinity(self):
        for bad in (1.5, math.nan, math.inf, -math.inf):
            with self.assertRaises(TableError, msg=f"ccc float {bad}"):
                CharacterTable(ccc={0x41: bad})
            with self.assertRaises(TableError, msg=f"decomp key float {bad}"):
                CharacterTable(decompositions={bad: (0x41,)})

    def test_out_of_range_and_surrogate_code_points(self):
        for bad in (-1, 0x110000, 0xD800, 0xDFFF):
            with self.assertRaises(TableError, msg=f"cp {bad}"):
                CharacterTable(ccc={bad: 1})

    def test_ccc_range(self):
        with self.assertRaises(TableError):
            CharacterTable(ccc={0x41: 255})
        with self.assertRaises(TableError):
            CharacterTable(ccc={0x41: -1})
        CharacterTable(ccc={0x41: 0})
        CharacterTable(ccc={0x41: 254})

    def test_decomposition_shape(self):
        with self.assertRaises(TableError):
            CharacterTable(decompositions={0x41: ()})          # empty
        with self.assertRaises(TableError):
            CharacterTable(decompositions={0x41: "AB"})        # str, not cps
        with self.assertRaises(TableError):
            CharacterTable(decompositions={0x41: 0x42})        # not a sequence
        with self.assertRaises(TableError):
            CharacterTable(decompositions="not a mapping")

    def test_composition_shape(self):
        with self.assertRaises(TableError):
            CharacterTable(compositions={(0x41,): 0x42})       # not a pair
        with self.assertRaises(TableError):
            CharacterTable(compositions={(0x41, 0x42, 0x43): 0x44})
        with self.assertRaises(TableError):
            CharacterTable(compositions={"AB": 0x43})          # str key
        with self.assertRaises(TableError):
            CharacterTable(compositions={(0x41, 0x42): -1})

    def test_direct_self_cycle(self):
        with self.assertRaises(CyclicDecompositionError) as ctx:
            CharacterTable(decompositions={0x41: (0x41,)})
        self.assertEqual(ctx.exception.cycle, (0x41, 0x41))

    def test_indirect_cycle(self):
        with self.assertRaises(CyclicDecompositionError) as ctx:
            CharacterTable(decompositions={0x41: (0x42,), 0x42: (0x43,), 0x43: (0x41,)})
        cycle = ctx.exception.cycle
        self.assertEqual(cycle[0], cycle[-1])
        self.assertEqual(len(cycle), 4)
        self.assertIn("U+0041", str(ctx.exception))

    def test_cycle_reachable_from_diamond(self):
        # 0x40 -> 0x41 -> 0x42 -> 0x41 : cycle must be found from any root.
        with self.assertRaises(CyclicDecompositionError):
            CharacterTable(
                decompositions={0x40: (0x41,), 0x41: (0x42,), 0x42: (0x41,)}
            )

    def test_recursive_expansion(self):
        table = CharacterTable(
            decompositions={0x100: (0x101, 0x60), 0x101: (0x62, 0x63)}
        )
        self.assertEqual(table.expansion_of(0x100), (0x62, 0x63, 0x60))
        self.assertEqual(table.expansion_of(0x101), (0x62, 0x63))
        self.assertEqual(table.expansion_of(0x62), (0x62,))

    def test_expansion_size_limit(self):
        # Doubling chain: depth 7 expands to 128 > MAX_EXPANSION (64).
        decomp = {}
        for i in range(7):
            decomp[0x1000 + i] = (0x1000 + i + 1, 0x1000 + i + 1)
        with self.assertRaises(TableError) as ctx:
            CharacterTable(decompositions=decomp)
        self.assertIn("expands beyond", str(ctx.exception))

    def test_deep_acyclic_chain_does_not_hit_recursion_limit(self):
        # 5000-deep chain: fine for the iterative implementation.
        decomp = {0x2000 + i: (0x2000 + i + 1,) for i in range(5000)}
        table = CharacterTable(decompositions=decomp)
        self.assertEqual(table.expansion_of(0x2000), (0x2000 + 5000,))

    def test_tables_are_immutable_views(self):
        table = CharacterTable(ccc={0x41: 1})
        with self.assertRaises(TypeError):
            table.ccc[0x41] = 9


class TestParameterContracts(unittest.TestCase):
    def test_max_segment_length_rejects_bool_float_and_nonpositive(self):
        table = CharacterTable()
        for bad in (True, False, 1.5, math.nan, math.inf, "10", None):
            with self.assertRaises(InputError, msg=f"max_segment_length={bad!r}"):
                StreamNormalizer(table, max_segment_length=bad)
        for bad in (0, -3):
            with self.assertRaises(InputError):
                StreamNormalizer(table, max_segment_length=bad)

    def test_table_argument_type(self):
        with self.assertRaises(InputError):
            StreamNormalizer({})
        with self.assertRaises(InputError):
            normalize(["x"], table=None)

    def test_chunks_argument(self):
        table = CharacterTable()
        with self.assertRaises(InputError):
            normalize("abc", table)          # bare str, not an iterable of chunks
        with self.assertRaises(InputError):
            normalize(b"abc", table)
        with self.assertRaises(InputError):
            normalize(None, table)
        with self.assertRaises(InputError) as ctx:
            normalize(["ok", 5], table)
        self.assertIn("chunks[1]", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
