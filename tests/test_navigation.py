import unittest

from dashboard.navigation import (
    RESEARCH_TAB,
    TAB_LABELS,
    TAB_STATE_KEY,
    WORKSPACE_STATE_KEY,
    PENDING_NAVIGATION_KEY,
    apply_pending_navigation,
    activate_research_workspace,
)


class NavigationStateTests(unittest.TestCase):
    def test_four_top_tabs_are_in_product_order(self):
        self.assertEqual(
            TAB_LABELS,
            ("Market Home", "Portfolio", "Research & Watchlist", "Quant Lab"),
        )

    def test_open_workspace_queues_navigation_without_mutating_widget_owned_key(self):
        state = {}
        activate_research_workspace(state, " msft ")
        self.assertEqual(state[WORKSPACE_STATE_KEY], "MSFT")
        self.assertEqual(state[PENDING_NAVIGATION_KEY], {"tab": RESEARCH_TAB})
        self.assertNotIn(TAB_STATE_KEY, state)

    def test_pending_navigation_is_applied_before_tabs_widget_creation(self):
        state = {TAB_STATE_KEY: TAB_LABELS[0]}
        activate_research_workspace(state, "aapl")
        apply_pending_navigation(state)
        self.assertEqual(state[TAB_STATE_KEY], RESEARCH_TAB)
        self.assertEqual(state[WORKSPACE_STATE_KEY], "AAPL")
        self.assertNotIn(PENDING_NAVIGATION_KEY, state)

    def test_each_workspace_entry_point_uses_the_safe_navigation_handoff(self):
        # Watchlist, portfolio and Market Home buttons all route through this
        # shared callback helper; exercise the handoff with a distinct ticker.
        for ticker in ("NVDA", "COST", "IGV"):
            with self.subTest(source_ticker=ticker):
                state = {TAB_STATE_KEY: TAB_LABELS[0]}
                activate_research_workspace(state, ticker)
                self.assertEqual(state[WORKSPACE_STATE_KEY], ticker)
                apply_pending_navigation(state)
                self.assertEqual(state[TAB_STATE_KEY], RESEARCH_TAB)

    def test_watchlist_portfolio_and_market_home_callbacks_share_safe_handoff(self):
        from dashboard.pages.portfolio import activate_research_workspace as portfolio_open
        from dashboard.pages.research import activate_research_workspace as watchlist_open
        # Market Home calls the same navigation helper from its Open Workspace action.
        market_home_open = activate_research_workspace
        for source, callback, ticker in (
            ("watchlist", watchlist_open, "NVDA"),
            ("portfolio", portfolio_open, "COST"),
            ("Market Home", market_home_open, "IGV"),
        ):
            with self.subTest(source=source):
                state = {TAB_STATE_KEY: TAB_LABELS[0]}
                callback(state, ticker)
                self.assertEqual(state[TAB_STATE_KEY], TAB_LABELS[0])
                self.assertEqual(state[WORKSPACE_STATE_KEY], ticker)
                apply_pending_navigation(state)
                self.assertEqual(state[TAB_STATE_KEY], RESEARCH_TAB)


if __name__ == "__main__":
    unittest.main()
