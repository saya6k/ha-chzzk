"""Per-channel polling coordinator.

To keep Chzzk's API happy we split the per-poll work in two:

* ``live-status`` is fetched on every tick (default 60 s). This is what
  changes minute-to-minute: `is_live`, viewer count, title, thumbnail URL.
* ``channel`` (name, avatar, follower count) is refreshed only every Nth tick
  (default 10 → every ~10 minutes). It barely changes and re-fetching it on
  every poll is what tends to trip Chzzk's "too many requests from one
  session" heuristic and invalidate the NID cookies.

If a channel-info refresh fails we keep using the cached value rather than
marking the whole coordinator as failed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import timedelta

from aiohttp import ClientConnectionError
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    CategoryInfo,
    CategoryLive,
    ChannelInfo,
    ChzzkApiError,
    ChzzkChannelNotFound,
    ChzzkClient,
    LiveStatus,
)
from .const import (
    CATEGORY_SCAN_INTERVAL,
    CATEGORY_TOP_LIVES,
    CHANNEL_INFO_EVERY_N_POLLS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

try:  # HA 2026.12+: logs the first failure once at info instead of error on every poll
    from homeassistant.helpers.update_coordinator import UpdateFailedUnreachable
except ImportError:  # older cores
    UpdateFailedUnreachable = UpdateFailed

_LOGGER = logging.getLogger(__name__)


def _update_failed(exc: ChzzkApiError) -> UpdateFailed:
    """UpdateFailedUnreachable when Chzzk could not be reached at all (a
    connection error or timeout), plain UpdateFailed for an API answer."""
    unreachable = isinstance(exc.__cause__, (ClientConnectionError, TimeoutError))
    return (UpdateFailedUnreachable if unreachable else UpdateFailed)(str(exc))


@dataclass
class ChzzkData:
    """Snapshot returned from each refresh."""

    channel: ChannelInfo
    live: LiveStatus


class ChzzkCoordinator(DataUpdateCoordinator[ChzzkData]):
    """Polls Chzzk once per channel."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: ChzzkClient,
        channel_id: str,
        channel_name: str,
        update_interval: timedelta | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{channel_id}",
            update_interval=update_interval or DEFAULT_SCAN_INTERVAL,
        )
        self._client = client
        self.channel_id = channel_id
        self.channel_name = channel_name
        self._cached_channel: ChannelInfo | None = None
        self._tick = 0  # incremented each successful poll

    async def _async_update_data(self) -> ChzzkData:
        # live-status: every tick. Cheap polling endpoint; lacks the thumbnail.
        try:
            live = await self._client.get_live_status(self.channel_id)
        except ChzzkChannelNotFound as exc:
            raise UpdateFailed(f"Channel {self.channel_id} not found: {exc}") from exc
        except ChzzkApiError as exc:
            raise _update_failed(exc) from exc

        # If the channel is live, fetch the thumbnail (only available on the
        # heavier live-detail endpoint). One extra call/min/live-channel.
        if live.is_live:
            try:
                thumb = await self._client.get_live_thumbnail(self.channel_id)
            except ChzzkApiError as exc:
                _LOGGER.debug("Live thumbnail fetch failed (non-fatal): %s", exc)
                thumb = None
            if thumb:
                live = replace(live, live_image_url=thumb)

        # channel info: first tick, then every Nth tick. Fall back to the cache
        # on failure so a transient blip doesn't blank out the entity device
        # info.
        if (
            self._cached_channel is None
            or self._tick % CHANNEL_INFO_EVERY_N_POLLS == 0
        ):
            try:
                self._cached_channel = await self._client.get_channel(self.channel_id)
            except ChzzkChannelNotFound as exc:
                if self._cached_channel is None:
                    raise UpdateFailed(
                        f"Channel {self.channel_id} not found: {exc}"
                    ) from exc
                _LOGGER.warning(
                    "Channel info disappeared, keeping cache: %s", exc
                )
            except ChzzkApiError as exc:
                if self._cached_channel is None:
                    raise _update_failed(exc) from exc
                _LOGGER.warning(
                    "Channel info refresh failed, keeping cache: %s", exc
                )

        self._tick += 1
        assert self._cached_channel is not None
        if self._cached_channel.channel_name:
            self.channel_name = self._cached_channel.channel_name
        return ChzzkData(channel=self._cached_channel, live=live)


@dataclass
class ChzzkCategoryData:
    info: CategoryInfo
    top_lives: list[CategoryLive]


class ChzzkCategoryCoordinator(DataUpdateCoordinator[ChzzkCategoryData]):
    """Polls one category's totals and its most-watched streams."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: ChzzkClient,
        category_type: str,
        category_id: str,
        category_name: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_category_{category_type}_{category_id}",
            update_interval=CATEGORY_SCAN_INTERVAL,
        )
        self._client = client
        self.category_type = category_type
        self.category_id = category_id
        self.category_name = category_name

    async def _async_update_data(self) -> ChzzkCategoryData:
        try:
            info = await self._client.get_category(
                self.category_type, self.category_id
            )
        except ChzzkChannelNotFound as exc:
            raise UpdateFailed(
                f"Category {self.category_type}/{self.category_id} not found: {exc}"
            ) from exc
        except ChzzkApiError as exc:
            raise _update_failed(exc) from exc

        # The top list only feeds an attribute; keep the last one rather than
        # blanking the counts the info call already returned.
        try:
            top_lives = await self._client.get_category_lives(
                self.category_type, self.category_id, CATEGORY_TOP_LIVES
            )
        except ChzzkApiError as exc:
            _LOGGER.warning("Category lives refresh failed, keeping last list: %s", exc)
            top_lives = self.data.top_lives if self.data else []
        self.category_name = info.category_value
        return ChzzkCategoryData(info=info, top_lives=top_lives)
