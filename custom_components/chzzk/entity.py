"""Shared base entities for Chzzk: device grouping, attribution, name composition."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import ATTRIBUTION, DOMAIN, MANUFACTURER
from .coordinator import ChzzkCategoryCoordinator, ChzzkCoordinator


def category_device_id(category_type: str, category_id: str) -> str:
    """Device identifier for a category, mirroring its chzzk.naver.com path."""
    return f"category/{category_type}/{category_id}"


class _ChzzkServiceEntity[_CoordinatorT: DataUpdateCoordinator](
    CoordinatorEntity[_CoordinatorT]
):
    """One service device per channel or category; device_id is also the
    chzzk.naver.com path of its page."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: _CoordinatorT,
        description: EntityDescription,
        device_id: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{device_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            entry_type=DeviceEntryType.SERVICE,
            manufacturer=MANUFACTURER,
            name=device_name,
            configuration_url=f"https://chzzk.naver.com/{device_id}",
        )

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None


class ChzzkEntity(_ChzzkServiceEntity[ChzzkCoordinator]):
    """Base class — all entities share one device per channel."""

    def __init__(
        self, coordinator: ChzzkCoordinator, description: EntityDescription
    ) -> None:
        super().__init__(
            coordinator, description, coordinator.channel_id, coordinator.channel_name
        )


class ChzzkCategoryEntity(_ChzzkServiceEntity[ChzzkCategoryCoordinator]):
    """Base class — all entities share one device per category."""

    def __init__(
        self, coordinator: ChzzkCategoryCoordinator, description: EntityDescription
    ) -> None:
        super().__init__(
            coordinator,
            description,
            category_device_id(coordinator.category_type, coordinator.category_id),
            coordinator.category_name,
        )
