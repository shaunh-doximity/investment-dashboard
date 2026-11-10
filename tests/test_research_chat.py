import tempfile
import unittest
from pathlib import Path

from dashboard.data_access import (
    append_research_chat_messages, list_research_chat_messages,
    list_research_strategy_scenarios, save_research_strategy_scenarios,
)
from dashboard.database import initialize_database


class ResearchChatPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "research.sqlite"
        initialize_database(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def test_chat_history_is_persistent_and_isolated_per_ticker(self):
        append_research_chat_messages("msft", [
            {"role": "user", "content": "What could invalidate my thesis?"},
            {"role": "assistant", "content": "Check retention and margins."},
        ], provider="deepseek", model="deepseek-chat", database_path=self.database)
        append_research_chat_messages("AAPL", [
            {"role": "user", "content": "Compare services growth."},
        ], provider="openai", model="gpt-5-nano", database_path=self.database)

        # Reinitialize to model an application restart; history must survive.
        initialize_database(self.database)
        msft = list_research_chat_messages("MSFT", database_path=self.database)
        self.assertEqual([message["role"] for message in msft], ["user", "assistant"])
        self.assertEqual(msft[0]["provider"], "deepseek")
        self.assertEqual(list_research_chat_messages("AAPL", database_path=self.database)[0]["model"], "gpt-5-nano")
        self.assertEqual(list_research_chat_messages("NVDA", database_path=self.database), [])

    def test_invalid_messages_are_rejected_without_partial_writes(self):
        with self.assertRaises(ValueError):
            append_research_chat_messages("NVDA", [
                {"role": "user", "content": "valid"},
                {"role": "system", "content": "invalid"},
            ], database_path=self.database)
        self.assertEqual(list_research_chat_messages("NVDA", database_path=self.database), [])

    def test_horizon_scenario_approval_is_separate_and_revisioned(self):
        first = {"horizon": "Swing / short-term", "thesis": "Short setup", "target_price": 120}
        save_research_strategy_scenarios("NVDA", [first], provider="deepseek", model="deepseek-chat", database_path=self.database)
        second = {"horizon": "Swing / short-term", "thesis": "Updated setup", "target_price": 125}
        save_research_strategy_scenarios("NVDA", [second], provider="openai", model="gpt-5-nano", database_path=self.database)
        current = list_research_strategy_scenarios("NVDA", database_path=self.database)
        history = list_research_strategy_scenarios("NVDA", history=True, database_path=self.database)
        self.assertEqual(current[0]["scenario"]["thesis"], "Updated setup")
        self.assertEqual(len(history), 2)
        self.assertEqual({item["scenario"]["thesis"] for item in history}, {"Short setup", "Updated setup"})


if __name__ == "__main__":
    unittest.main()
