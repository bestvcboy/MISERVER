import unittest

from phase_replay import Poll, evaluate, load, schedule


class PhaseReplayTests(unittest.TestCase):
    def test_complete_history_passes_but_polling_can_miss_a_strict_interval(self):
        timeline, rows = load()
        direct = evaluate(timeline, rows, [Poll(row['mono'], n) for n, row in enumerate(rows)])
        sparse = evaluate(timeline, rows, schedule(rows, 0))
        self.assertEqual(direct['verdict'], 'accepted_counter_only')
        self.assertAlmostEqual(direct['earned'], 30.723721185)
        self.assertEqual(sparse['verdict'], 'startup_rejected')
        self.assertAlmostEqual(sparse['earned'], 24.676989164)
        self.assertGreater(rows[16]['readback'] - rows[14]['readback'], 6)
        self.assertNotIn(15, sparse['source_indexes'])


if __name__ == '__main__':
    unittest.main()
