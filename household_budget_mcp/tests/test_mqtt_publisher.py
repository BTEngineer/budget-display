from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from budget_display import BudgetLedger
from budget_display.mqtt_publisher import BudgetMQTTPublisher, MQTTConfig


class FakeMQTTClient:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, int, bool]] = []
        self.on_connect = None

    def username_pw_set(self, username: str, password: str) -> None:
        pass

    def will_set(self, topic: str, payload: str, qos: int, retain: bool) -> None:
        pass

    def publish(
        self, topic: str, payload: str, qos: int = 0, retain: bool = False
    ) -> None:
        self.messages.append((topic, payload, qos, retain))


class BudgetMQTTPublisherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.ledger = BudgetLedger(
            Path(self.temporary_directory.name) / "budget.db",
            members=("Alpha", "Beta"),
            categories=(("Everyday", None, 100_000),),
        )
        self.ledger.initialize()
        self.client = FakeMQTTClient()
        self.publisher = BudgetMQTTPublisher(
            ledger=self.ledger,
            members=("Alpha", "Beta"),
            config=MQTTConfig(
                host="mqtt", port=1883, username="user", password="secret"
            ),
            client=self.client,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_publishes_stable_discovery_and_zero_value_states(self) -> None:
        self.publisher.publish_current()
        messages = {topic: payload for topic, payload, _, _ in self.client.messages}

        self.assertEqual(messages["household_budget/status"], "online")
        self.assertEqual(messages["household_budget/state/spent_month"], "0.00")
        self.assertEqual(messages["household_budget/state/remaining_month"], "1000.00")
        self.assertEqual(messages["household_budget/state/budget_month"], "1000.00")
        self.assertEqual(messages["household_budget/state/percent_used_month"], "0.0")
        self.assertEqual(messages["household_budget/state/person_1_month"], "0.00")
        self.assertEqual(messages["household_budget/state/person_2_month"], "0.00")
        self.assertEqual(
            messages["household_budget/state/category_1_month"],
            "0|Everyday|1000.00|0.00|0.00|0.00",
        )
        self.assertEqual(
            messages["household_budget/state/category_6_month"], "|||||"
        )

        discovery = json.loads(
            messages[
                "homeassistant/sensor/household_budget_person_1_month/config"
            ]
        )
        self.assertEqual(
            discovery["default_entity_id"],
            "sensor.household_budget_person_1_month",
        )
        self.assertEqual(discovery["name"], "Alpha this month")
        self.assertNotIn("secret", json.dumps(discovery))
        category_key = self.publisher._category_key("Everyday")
        self.assertEqual(
            messages[f"household_budget/state/category_{category_key}_budget_month"],
            "1000.00",
        )
        self.assertIn("household_budget/state/last_update", messages)

    def test_category_rows_include_member_and_parent_totals(self) -> None:
        ledger = BudgetLedger(
            Path(self.temporary_directory.name) / "category-budget.db",
            members=("Alpha", "Beta"),
            categories=(
                ("Meals", None, 60_000),
                ("Food", "Meals", None),
                ("Coffee", "Meals", None),
            ),
        )
        ledger.initialize()
        ledger.add_expense(
            request_id="food-a",
            member="Alpha",
            category="Meals/Food",
            amount="10.25",
        )
        ledger.add_expense(
            request_id="coffee-b",
            member="Beta",
            category="Meals/Coffee",
            amount="4.75",
        )
        client = FakeMQTTClient()
        publisher = BudgetMQTTPublisher(
            ledger=ledger,
            members=("Alpha", "Beta"),
            config=MQTTConfig(host="mqtt", port=1883, username="", password=""),
            client=client,
        )

        publisher.publish_current()
        messages = {topic: payload for topic, payload, _, _ in client.messages}
        self.assertEqual(
            messages["household_budget/state/category_1_month"],
            "0|Meals|600.00|10.25|4.75|15.00",
        )
        self.assertEqual(
            messages["household_budget/state/category_2_month"],
            "1|Food||10.25|0.00|10.25",
        )
        self.assertEqual(
            messages["household_budget/state/category_3_month"],
            "1|Coffee||0.00|4.75|4.75",
        )


if __name__ == "__main__":
    unittest.main()
