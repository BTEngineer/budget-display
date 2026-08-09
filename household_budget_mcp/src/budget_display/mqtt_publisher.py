"""Publish read-only monthly budget totals through Home Assistant MQTT discovery."""

from __future__ import annotations

import json
import logging
import os
import threading
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import paho.mqtt.client as mqtt

from .ledger import BudgetLedger, BudgetValidationError


LOGGER = logging.getLogger(__name__)
DEVICE_ID = "household_budget"
DISCOVERY_PREFIX = "homeassistant"
STATE_PREFIX = "household_budget"
MAX_DISPLAY_CATEGORY_ROWS = 6


@dataclass(frozen=True)
class MQTTConfig:
    host: str
    port: int
    username: str
    password: str
    publish_interval_seconds: int = 30

    @classmethod
    def from_environment(cls) -> "MQTTConfig | None":
        host = os.environ.get("BUDGET_MQTT_HOST", "").strip()
        if not host:
            return None
        try:
            port = int(os.environ.get("BUDGET_MQTT_PORT", "1883"))
            interval = int(os.environ.get("BUDGET_MQTT_INTERVAL", "30"))
        except ValueError as exc:
            raise BudgetValidationError(
                "BUDGET_MQTT_PORT and BUDGET_MQTT_INTERVAL must be integers"
            ) from exc
        if not 1 <= port <= 65535:
            raise BudgetValidationError("BUDGET_MQTT_PORT must be between 1 and 65535")
        if not 10 <= interval <= 3600:
            raise BudgetValidationError(
                "BUDGET_MQTT_INTERVAL must be between 10 and 3600 seconds"
            )
        return cls(
            host=host,
            port=port,
            username=os.environ.get("BUDGET_MQTT_USERNAME", ""),
            password=os.environ.get("BUDGET_MQTT_PASSWORD", ""),
            publish_interval_seconds=interval,
        )


class PublisherClient(Protocol):
    def publish(
        self, topic: str, payload: str, qos: int = 0, retain: bool = False
    ) -> object: ...


class BudgetMQTTPublisher:
    """Maintain retained Home Assistant sensor discovery and current states."""

    def __init__(
        self,
        *,
        ledger: BudgetLedger,
        members: tuple[str, ...],
        config: MQTTConfig,
        client: mqtt.Client | None = None,
    ) -> None:
        self.ledger = ledger
        self.members = members
        self.config = config
        self.client = client or mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="household-budget-ha",
            protocol=mqtt.MQTTv5,
        )
        self.client.on_connect = self._on_connect
        if config.username:
            self.client.username_pw_set(config.username, config.password)
        self.client.will_set(
            f"{STATE_PREFIX}/status", "offline", qos=1, retain=True
        )

    def start(self) -> None:
        self.client.connect_async(self.config.host, self.config.port, keepalive=60)
        self.client.loop_start()
        threading.Thread(
            target=self._publish_loop,
            name="budget-mqtt-publisher",
            daemon=True,
        ).start()

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code != 0:
            LOGGER.error("MQTT connection rejected: %s", reason_code)
            return
        LOGGER.info("Connected to Home Assistant MQTT service")
        self.publish_current()

    def _publish_loop(self) -> None:
        while True:
            threading.Event().wait(self.config.publish_interval_seconds)
            try:
                self.publish_current()
            except Exception:
                LOGGER.exception("Unable to publish household budget sensor state")

    def publish_current(self) -> None:
        month = datetime.now(self.ledger.household_timezone).strftime("%Y-%m")
        summary = self.ledger.list_spending(month=month)
        self.client.publish(
            f"{STATE_PREFIX}/status", "online", qos=1, retain=True
        )
        self._publish_money_sensor(
            object_id="spent_month",
            display_name="Spent this month",
            cents=int(summary["spent_cents"]),
        )
        remaining = summary["remaining_cents"]
        budget = summary["budget_cents"]
        if budget is not None:
            self._publish_money_sensor(
                object_id="budget_month",
                display_name="Budget this month",
                cents=int(budget),
            )
            percent = round(int(summary["spent_cents"]) * 100 / int(budget), 1) if int(budget) else 0
            self._publish_number_sensor(
                object_id="percent_used_month",
                display_name="Budget used this month",
                value=f"{percent:.1f}",
                unit="%",
                state_class="measurement",
            )
        if remaining is not None:
            self._publish_money_sensor(
                object_id="remaining_month",
                display_name="Remaining this month",
                cents=int(remaining),
            )
        by_member = summary["by_member_cents"]
        for index, member in enumerate(self.members, start=1):
            self._publish_money_sensor(
                object_id=f"person_{index}_month",
                display_name=f"{member} this month",
                cents=int(by_member.get(member, 0)),
            )
        category_rows = summary["category_rows"]
        for row in category_rows:
            if row["parent"] is not None:
                continue
            category_key = self._category_key(str(row["name"]))
            spent_cents = int(row["spent_cents"])
            budget_cents = row["budget_cents"]
            self._publish_money_sensor(
                object_id=f"category_{category_key}_spent_month",
                display_name=f"{row['name']} spent this month",
                cents=spent_cents,
            )
            if budget_cents is not None:
                numeric_budget = int(budget_cents)
                self._publish_money_sensor(
                    object_id=f"category_{category_key}_budget_month",
                    display_name=f"{row['name']} budget this month",
                    cents=numeric_budget,
                )
                self._publish_money_sensor(
                    object_id=f"category_{category_key}_remaining_month",
                    display_name=f"{row['name']} remaining this month",
                    cents=numeric_budget - spent_cents,
                )
                percent = round(spent_cents * 100 / numeric_budget, 1) if numeric_budget else 0
                self._publish_number_sensor(
                    object_id=f"category_{category_key}_percent_month",
                    display_name=f"{row['name']} budget used this month",
                    value=f"{percent:.1f}",
                    unit="%",
                    state_class="measurement",
                )
        if len(category_rows) > MAX_DISPLAY_CATEGORY_ROWS:
            LOGGER.warning(
                "Only the first %s budget categories fit on the E1001 display",
                MAX_DISPLAY_CATEGORY_ROWS,
            )
        for index in range(1, MAX_DISPLAY_CATEGORY_ROWS + 1):
            if index <= len(category_rows):
                row = category_rows[index - 1]
                member_totals = row["by_member_cents"]
                budget_cents = row["budget_cents"]
                safe_name = str(row["name"]).replace("|", " ")
                payload = "|".join(
                    (
                        "1" if row["parent"] is not None else "0",
                        safe_name,
                        "" if budget_cents is None else f"{int(budget_cents) / 100:.2f}",
                        f"{int(member_totals.get(self.members[0], 0)) / 100:.2f}",
                        f"{int(member_totals.get(self.members[1], 0)) / 100:.2f}"
                        if len(self.members) > 1
                        else "0.00",
                        f"{int(row['spent_cents']) / 100:.2f}",
                    )
                )
            else:
                payload = "|||||"
            self._publish_text_sensor(
                object_id=f"category_{index}_month",
                display_name=f"Category row {index} this month",
                value=payload,
            )
        self._publish_text_sensor(
            object_id="last_update",
            display_name="Last budget update",
            value=datetime.now(self.ledger.household_timezone).isoformat(timespec="seconds"),
            device_class="timestamp",
        )

    @staticmethod
    def _category_key(name: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_") or "category"
        digest = hashlib.sha256(name.casefold().encode("utf-8")).hexdigest()[:8]
        return f"{slug[:40]}_{digest}"

    def _publish_money_sensor(
        self, *, object_id: str, display_name: str, cents: int
    ) -> None:
        self._publish_number_sensor(
            object_id=object_id,
            display_name=display_name,
            value=f"{cents / 100:.2f}",
            unit="$",
            device_class="monetary",
        )

    def _publish_number_sensor(
        self,
        *,
        object_id: str,
        display_name: str,
        value: str,
        unit: str,
        device_class: str | None = None,
        state_class: str | None = None,
    ) -> None:
        unique_id = f"household_budget_{object_id}"
        state_topic = f"{STATE_PREFIX}/state/{object_id}"
        discovery = {
            "name": display_name,
            "unique_id": unique_id,
            "default_entity_id": f"sensor.{unique_id}",
            "state_topic": state_topic,
            "availability_topic": f"{STATE_PREFIX}/status",
            "payload_available": "online",
            "payload_not_available": "offline",
            "unit_of_measurement": unit,
            "device": {
                "identifiers": [DEVICE_ID],
                "name": "Household Budget",
                "manufacturer": "BTEngineer",
                "model": "Home Assistant Budget MCP",
            },
        }
        if device_class:
            discovery["device_class"] = device_class
        if state_class:
            discovery["state_class"] = state_class
        self.client.publish(
            f"{DISCOVERY_PREFIX}/sensor/{unique_id}/config",
            json.dumps(discovery, separators=(",", ":"), sort_keys=True),
            qos=1,
            retain=True,
        )
        self.client.publish(
            state_topic, value, qos=1, retain=True
        )

    def _publish_text_sensor(
        self,
        *,
        object_id: str,
        display_name: str,
        value: str,
        device_class: str | None = None,
    ) -> None:
        unique_id = f"household_budget_{object_id}"
        state_topic = f"{STATE_PREFIX}/state/{object_id}"
        discovery = {
            "name": display_name,
            "unique_id": unique_id,
            "default_entity_id": f"sensor.{unique_id}",
            "state_topic": state_topic,
            "availability_topic": f"{STATE_PREFIX}/status",
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": {
                "identifiers": [DEVICE_ID],
                "name": "Household Budget",
                "manufacturer": "BTEngineer",
                "model": "Home Assistant Budget MCP",
            },
        }
        if device_class:
            discovery["device_class"] = device_class
        self.client.publish(
            f"{DISCOVERY_PREFIX}/sensor/{unique_id}/config",
            json.dumps(discovery, separators=(",", ":"), sort_keys=True),
            qos=1,
            retain=True,
        )
        self.client.publish(state_topic, value, qos=1, retain=True)
