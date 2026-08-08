from __future__ import annotations

import unittest

from tests.support.herdr_pane_tree import PaneTreeHerdr, Split


class PaneTreeHerdrContractTest(unittest.TestCase):
    def test_ratio_move_and_temp_tab_auto_close(self):
        herdr = PaneTreeHerdr("w1")
        main = herdr.new_tab()
        first = herdr.seed_pane(main, "first")
        second = herdr.split_pane(main, first, "down", "second")

        detached = herdr(
            ["herdr", "pane", "move", second, "--new-tab", "--no-focus"]
        )
        self.assertEqual(0, detached.returncode)
        self.assertEqual(2, len(herdr.tabs))

        attached = herdr(
            [
                "herdr", "pane", "move", second,
                "--tab", main.tab_id,
                "--split", "down",
                "--ratio", "0.35",
                "--target-pane", first,
                "--no-focus",
            ]
        )

        self.assertEqual(0, attached.returncode)
        self.assertEqual(1, len(herdr.tabs))
        self.assertIsInstance(main.root, Split)
        self.assertAlmostEqual(0.35, main.root.ratio)

    def test_swap_exchanges_leaf_positions_without_changing_dividers(self):
        herdr = PaneTreeHerdr("w1")
        tab = herdr.new_tab()
        first = herdr.seed_pane(tab, "first")
        second = herdr.split_pane(tab, first, "right", "second")
        root = tab.root
        self.assertIsInstance(root, Split)
        root.ratio = 0.4
        before = herdr.rects()

        completed = herdr(
            [
                "herdr", "pane", "swap",
                "--source-pane", first,
                "--target-pane", second,
            ]
        )

        self.assertEqual(0, completed.returncode)
        after = herdr.rects()
        self.assertEqual(before[first], after[second])
        self.assertEqual(before[second], after[first])
        self.assertAlmostEqual(0.4, root.ratio)

    def test_invalid_ratio_does_not_detach_source(self):
        herdr = PaneTreeHerdr("w1")
        main = herdr.new_tab()
        first = herdr.seed_pane(main, "first")
        source_tab = herdr.new_tab()
        moving = herdr.seed_pane(source_tab, "moving")

        completed = herdr(
            [
                "herdr", "pane", "move", moving,
                "--tab", main.tab_id,
                "--split", "down",
                "--ratio", "nan",
                "--target-pane", first,
                "--no-focus",
            ]
        )

        self.assertNotEqual(0, completed.returncode)
        self.assertIn(moving, source_tab.panes())
        self.assertEqual({first}, set(main.panes()))


if __name__ == "__main__":
    unittest.main()
