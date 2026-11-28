"""Sensor entities for Chzzk channels."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import (
    ChzzkCategoryCoordinator,
    ChzzkCategoryData,
    ChzzkCoordinator,
    ChzzkData,
)
from .entity import ChzzkCategoryEntity, ChzzkEntity


@dataclass(kw_only=True, frozen=True)
class ChzzkSensorEntityDescription(SensorEntityDescription):
    """Pairs a Home Assistant entity description with a value extractor."""

    value_fn: Callable[[ChzzkData], str | int | float | datetime | None]


SENSORS: tuple[ChzzkSensorEntityDescription, ...] = (
    ChzzkSensorEntityDescription(
        key="title",
        translation_key="title",
        icon="mdi:format-title",
        value_fn=lambda d: d.live.title,
    ),
    ChzzkSensorEntityDescription(
        key="category",
        translation_key="category",
        icon="mdi:tag",
        value_fn=lambda d: d.live.category_value,
    ),
    ChzzkSensorEntityDescription(
        key="viewers",
        translation_key="viewers",
        icon="mdi:account-eye",
        native_unit_of_measurement="viewers",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.live.concurrent_user_count if d.live.is_live else None,
    ),
    ChzzkSensorEntityDescription(
        key="started_at",
        translation_key="started_at",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d: d.live.open_date if d.live.is_live else None,
    ),
    ChzzkSensorEntityDescription(
        key="followers",
        translation_key="followers",
        icon="mdi:account-multiple",
        native_unit_of_measurement="followers",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.channel.follower_count,
    ),
)


@dataclass(kw_only=True, frozen=True)
class ChzzkCategorySensorEntityDescription(SensorEntityDescription):
    value_fn: Callable[[ChzzkCategoryData], int | None]
    attrs_fn: Callable[[ChzzkCategoryData], dict[str, Any]] | None = None


def _top_channels(data: ChzzkCategoryData) -> dict[str, Any]:
    return {
        "channels": [
            {
                "channel_id": live.channel_id,
                "channel_name": live.channel_name,
                "title": live.title,
                "viewers": live.viewers,
            }
            for live in data.top_lives
        ]
    }


CATEGORY_SENSORS: tuple[ChzzkCategorySensorEntityDescription, ...] = (
    ChzzkCategorySensorEntityDescription(
        key="live_channels",
        translation_key="live_channels",
        icon="mdi:broadcast",
        native_unit_of_measurement="channels",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.info.open_live_count,
        attrs_fn=_top_channels,
    ),
    ChzzkCategorySensorEntityDescription(
        key="viewers",
        translation_key="viewers",
        icon="mdi:account-eye",
        native_unit_of_measurement="viewers",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.info.concurrent_user_count,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coords: dict[str, ChzzkCoordinator] = hass.data[DOMAIN]["coordinators"]
    entities: list[ChzzkSensor] = []
    for coord in coords.values():
        entities.extend(ChzzkSensor(coord, desc) for desc in SENSORS)
    category_coords: dict[str, ChzzkCategoryCoordinator] = hass.data[DOMAIN][
        "category_coordinators"
    ]
    category_entities = [
        ChzzkCategorySensor(coord, desc)
        for coord in category_coords.values()
        for desc in CATEGORY_SENSORS
    ]
    async_add_entities([*entities, *category_entities])


class ChzzkSensor(ChzzkEntity, SensorEntity):
    entity_description: ChzzkSensorEntityDescription

    def __init__(
        self,
        coordinator: ChzzkCoordinator,
        description: ChzzkSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, description)

    @property
    def native_value(self) -> str | int | float | datetime | None:
        data = self.coordinator.data
        if data is None:
            return None
        return self.entity_description.value_fn(data)


class ChzzkCategorySensor(ChzzkCategoryEntity, SensorEntity):
    entity_description: ChzzkCategorySensorEntityDescription
    # Changes every poll and only matters live; keep it out of the database.
    _unrecorded_attributes = frozenset({"channels"})

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data
        if data is None:
            return None
        return self.entity_description.value_fn(data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        if data is None or self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(data)
