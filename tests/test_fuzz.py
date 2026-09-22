"""Cross-checks against the independent naive reference, plus idempotency.

The reference (``naive_reference.naive_normalize``) is a separate whole-
string implementation operating on plain dicts; it never imports or calls
the package under test.  Round-trip (idempotency) checks are only a
supplement on top of that comparison.
"""

import random
import unittest

from streamnorm import normalize

from fixtures import make_generated_table
from naive_reference import naive_normalize


def chunkings(text, rng):
    """Deterministic set of chunkings for one input string."""
    yield [text] if text else []
    yield list(text)  # size 1
    for size in (2, 3, 7):
        yield [text[i:i + size] for i in range(0, len(text), size)]
    # random splits
    cuts = sorted(rng.sample(range(1, len(text) + 1), min(4, max(0, len(text) - 1)))) if len(text) > 1 else []
    chunks, prev = [], 0
    for cut in cuts + [len(text)]:
        chunks.append(text[prev:cut])
        prev = cut
    yield chunks


class TestAgainstNaiveReference(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (cls.table, cls.decomp, cls.ccc, cls.comp, cls.alphabet) = make_generated_table()

    def test_streaming_matches_reference_under_many_chunkings(self):
        rng = random.Random(20260922)
        cases = 0
        for _ in range(150):
            length = rng.randrange(0, 40)
            text = "".join(chr(rng.choice(self.alphabet)) for _ in range(length))
            expected = naive_normalize(text, self.decomp, self.ccc, self.comp)
            for chunks in chunkings(text, rng):
                result = normalize(chunks, self.table)
                self.assertEqual(
                    result.text, expected,
                    msg=f"text={text!r} chunks={[len(c) for c in chunks]}",
                )
                cases += 1
        self.assertGreater(cases, 500)

    def test_intervals_partition_input(self):
        # The generated table's expansions always begin with a starter, so
        # segment intervals must partition [0, len(text)) exactly.
        rng = random.Random(7)
        for _ in range(60):
            length = rng.randrange(0, 30)
            text = "".join(chr(rng.choice(self.alphabet)) for _ in range(length))
            chunks = [text[i:i + 3] for i in range(0, len(text), 3)]
            result = normalize(chunks, self.table)
            pos = 0
            for interval in result.segments:
                self.assertEqual(interval.start, pos)
                pos = interval.end
            self.assertEqual(pos, len(text))

    def test_idempotency_supplement(self):
        # Supplementary round-trip check only; the authoritative oracle is
        # the naive reference above.
        rng = random.Random(99)
        for _ in range(100):
            length = rng.randrange(0, 40)
            text = "".join(chr(rng.choice(self.alphabet)) for _ in range(length))
            once = normalize([text], self.table).text
            twice = normalize([once], self.table).text
            self.assertEqual(once, twice, msg=f"text={text!r}")

    def test_reference_agrees_on_precomposed_input(self):
        # Every composite alone must round-trip through both implementations.
        for composite in self.comp.values():
            text = chr(composite)
            expected = naive_normalize(text, self.decomp, self.ccc, self.comp)
            self.assertEqual(normalize([text], self.table).text, expected)
            self.assertEqual(expected, text)


if __name__ == "__main__":
    unittest.main()
