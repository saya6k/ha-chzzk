"""Config flow: one shared entry, multiple channels under it.

User adds the integration once. The first step asks for optional Naver session
cookies (shared across all channels). The second step takes the first channel
URL/ID. After that, channels and categories are added/removed and cookies
refreshed via the options flow.

Notes for HA ≥ 2024.12 / 2026.x:
* No ``__init__`` override on either flow class. HA's flow manager injects
  ``self.hass``, ``self.context``, and (for OptionsFlow) ``self.config_entry``.
* ``async_get_options_flow`` returns ``ChzzkOptionsFlow()`` with no args.
* Inter-step state is stashed under ``self.context`` so the framework owns the
  lifecycle and we don't fight stricter class-init checks.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
)

from .api import (
    ChzzkApiError,
    ChzzkChannelNotFound,
    ChzzkClient,
    extract_category,
    extract_channel_id,
)
from .const import (
    CONF_CATEGORIES,
    CONF_CATEGORY_ID,
    CONF_CATEGORY_NAME,
    CONF_CATEGORY_TYPE,
    CONF_CHANNEL_ID,
    CONF_CHANNEL_NAME,
    CONF_CHANNELS,
    CONF_NID_AUT,
    CONF_NID_SES,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

_COOKIES_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_NID_AUT, default=""): str,
        vol.Optional(CONF_NID_SES, default=""): str,
    }
)


def _cookies_dict(form: dict[str, Any]) -> dict[str, str]:
    """Return ``{NID_AUT: ..., NID_SES: ...}`` keeping only non-empty entries."""
    out: dict[str, str] = {}
    for key in (CONF_NID_AUT, CONF_NID_SES):
        val = (form.get(key) or "").strip()
        if val:
            out[key.upper()] = val
    return out


async def _validate_channel(
    hass, channel_id: str, cookies: dict[str, str] | None
) -> tuple[str | None, str]:
    """Return ``(channel_name, error_code)``. ``channel_name`` is ``None`` on error."""
    session = async_get_clientsession(hass)
    client = ChzzkClient(session, cookies=cookies or None)
    try:
        channel = await client.get_channel(channel_id)
    except ChzzkChannelNotFound:
        return None, "not_found"
    except ChzzkApiError as exc:
        _LOGGER.warning("Chzzk API error: %s", exc)
        return None, "cannot_connect"
    return channel.channel_name or channel_id, ""


async def _validate_category(
    hass, category_type: str, category_id: str, cookies: dict[str, str] | None
) -> tuple[str | None, str]:
    """Return (category_name, error_code). category_name is None on error."""
    session = async_get_clientsession(hass)
    client = ChzzkClient(session, cookies=cookies or None)
    try:
        info = await client.get_category(category_type, category_id)
    except ChzzkChannelNotFound:
        return None, "category_not_found"
    except ChzzkApiError as exc:
        _LOGGER.warning("Chzzk API error: %s", exc)
        return None, "cannot_connect"
    return info.category_value, ""


def _category_key(category: dict[str, str]) -> str:
    return f"{category[CONF_CATEGORY_TYPE]}/{category[CONF_CATEGORY_ID]}"


async def _validate_cookies(
    hass, channel_id: str, cookies: dict[str, str]
) -> str:
    session = async_get_clientsession(hass)
    client = ChzzkClient(session, cookies=cookies)
    try:
        await client.get_live_status(channel_id)
    except ChzzkApiError as exc:
        _LOGGER.warning("Chzzk auth check failed: %s", exc)
        return "invalid_cookies"
    return ""


class ChzzkConfigFlow(ConfigFlow, domain=DOMAIN):
    """Two-step setup: cookies (optional), then first channel."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        # Single-entry enforcement lives in the manifest
        # (``single_config_entry``); HA aborts with ``single_instance_allowed``
        # before this step runs.
        if user_input is not None:
            # Stash cookies in self.context for the next step (framework-owned).
            self.context["chzzk_cookies"] = {
                CONF_NID_AUT: user_input.get(CONF_NID_AUT, "").strip(),
                CONF_NID_SES: user_input.get(CONF_NID_SES, "").strip(),
            }
            return await self.async_step_channel()

        return self.async_show_form(step_id="user", data_schema=_COOKIES_SCHEMA)

    async def async_step_channel(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        pending: dict[str, str] = self.context.get("chzzk_cookies", {})

        if user_input is not None:
            channel_id = extract_channel_id(user_input["channel"])
            if channel_id is None:
                errors["channel"] = "invalid_channel"
            else:
                cookies = _cookies_dict(pending)
                if cookies:
                    err = await _validate_cookies(self.hass, channel_id, cookies)
                    if err:
                        errors["base"] = err
                if not errors:
                    name, err = await _validate_channel(self.hass, channel_id, cookies)
                    if err:
                        errors["channel"] = err
                    else:
                        return self.async_create_entry(
                            title="Chzzk",
                            data=pending,
                            options={
                                CONF_CHANNELS: [
                                    {
                                        CONF_CHANNEL_ID: channel_id,
                                        CONF_CHANNEL_NAME: name or "",
                                    }
                                ]
                            },
                        )

        return self.async_show_form(
            step_id="channel",
            data_schema=vol.Schema({vol.Required("channel"): str}),
            errors=errors,
            last_step=True,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        # ``config_entry`` is auto-injected as ``self.config_entry`` by the
        # flow manager — don't pass it through the constructor.
        return ChzzkOptionsFlow()


class ChzzkOptionsFlow(OptionsFlow):
    """Add/remove channels and refresh shared cookies after initial setup."""

    # No __init__ — the flow manager assigns ``self.hass`` and
    # ``self.config_entry`` after instantiation.

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "add_channel",
                "remove_channel",
                "add_category",
                "remove_category",
                "update_cookies",
            ],
        )

    # ------------------------------------------------------------- helpers
    def _channels(self) -> list[dict[str, str]]:
        return list(self.config_entry.options.get(CONF_CHANNELS, []))

    def _categories(self) -> list[dict[str, str]]:
        return list(self.config_entry.options.get(CONF_CATEGORIES, []))

    def _finish_with_options(
        self,
        channels: list[dict[str, str]] | None = None,
        categories: list[dict[str, str]] | None = None,
    ) -> ConfigFlowResult:
        """Persist updated options without losing siblings.

        OptionsFlow.async_create_entry(data=X) REPLACES entry.options with X.
        We must pass the full options dict back to keep anything else intact.
        """
        new_options = dict(self.config_entry.options)
        if channels is not None:
            new_options[CONF_CHANNELS] = channels
        if categories is not None:
            new_options[CONF_CATEGORIES] = categories
        return self.async_create_entry(title="", data=new_options)

    def _cookies(self) -> dict[str, str]:
        return _cookies_dict(
            {
                CONF_NID_AUT: self.config_entry.data.get(CONF_NID_AUT, ""),
                CONF_NID_SES: self.config_entry.data.get(CONF_NID_SES, ""),
            }
        )

    # ------------------------------------------------------------- add channel
    async def async_step_add_channel(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            channel_id = extract_channel_id(user_input["channel"])
            if channel_id is None:
                errors["channel"] = "invalid_channel"
            else:
                channels = self._channels()
                if any(c[CONF_CHANNEL_ID] == channel_id for c in channels):
                    errors["channel"] = "already_added"
                else:
                    name, err = await _validate_channel(
                        self.hass, channel_id, self._cookies()
                    )
                    if err:
                        errors["channel"] = err
                    else:
                        channels.append(
                            {
                                CONF_CHANNEL_ID: channel_id,
                                CONF_CHANNEL_NAME: name or "",
                            }
                        )
                        return self._finish_with_options(channels)

        return self.async_show_form(
            step_id="add_channel",
            data_schema=vol.Schema({vol.Required("channel"): str}),
            errors=errors,
        )

    # ------------------------------------------------------------- remove channel
    async def async_step_remove_channel(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        channels = self._channels()
        if not channels:
            return self.async_abort(reason="no_channels")

        if user_input is not None:
            target = user_input["channel"]
            channels = [c for c in channels if c[CONF_CHANNEL_ID] != target]
            return self._finish_with_options(channels)

        select = SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(
                        value=c[CONF_CHANNEL_ID],
                        label=c.get(CONF_CHANNEL_NAME) or c[CONF_CHANNEL_ID],
                    )
                    for c in channels
                ],
                multiple=False,
            )
        )
        return self.async_show_form(
            step_id="remove_channel",
            data_schema=vol.Schema({vol.Required("channel"): select}),
        )

    # ------------------------------------------------------------- add category
    async def async_step_add_category(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            parsed = extract_category(user_input["category"])
            if parsed is None:
                errors["category"] = "invalid_category"
            else:
                category_type, category_id = parsed
                categories = self._categories()
                if any(
                    _category_key(c) == f"{category_type}/{category_id}"
                    for c in categories
                ):
                    errors["category"] = "category_already_added"
                else:
                    name, err = await _validate_category(
                        self.hass, category_type, category_id, self._cookies()
                    )
                    if err:
                        errors["category"] = err
                    else:
                        categories.append(
                            {
                                CONF_CATEGORY_TYPE: category_type,
                                CONF_CATEGORY_ID: category_id,
                                CONF_CATEGORY_NAME: name or "",
                            }
                        )
                        return self._finish_with_options(categories=categories)

        return self.async_show_form(
            step_id="add_category",
            data_schema=vol.Schema({vol.Required("category"): str}),
            errors=errors,
        )

    # ------------------------------------------------------------- remove category
    async def async_step_remove_category(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        categories = self._categories()
        if not categories:
            return self.async_abort(reason="no_categories")

        if user_input is not None:
            target = user_input["category"]
            categories = [c for c in categories if _category_key(c) != target]
            return self._finish_with_options(categories=categories)

        select = SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(
                        value=_category_key(c),
                        label=c.get(CONF_CATEGORY_NAME) or _category_key(c),
                    )
                    for c in categories
                ],
                multiple=False,
            )
        )
        return self.async_show_form(
            step_id="remove_category",
            data_schema=vol.Schema({vol.Required("category"): select}),
        )

    # ------------------------------------------------------------- update cookies
    async def async_step_update_cookies(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        defaults = {
            CONF_NID_AUT: self.config_entry.data.get(CONF_NID_AUT, ""),
            CONF_NID_SES: self.config_entry.data.get(CONF_NID_SES, ""),
        }

        if user_input is not None:
            cookies = _cookies_dict(user_input)
            if cookies:
                channels = self._channels()
                if channels:
                    err = await _validate_cookies(
                        self.hass, channels[0][CONF_CHANNEL_ID], cookies
                    )
                    if err:
                        errors["base"] = err
            if not errors:
                # Cookies live in entry.data — update those out-of-band, then
                # let OptionsFlow.async_create_entry round-trip options as-is.
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data={
                        **self.config_entry.data,
                        CONF_NID_AUT: user_input.get(CONF_NID_AUT, "").strip(),
                        CONF_NID_SES: user_input.get(CONF_NID_SES, "").strip(),
                    },
                )
                return self._finish_with_options()

        schema = vol.Schema(
            {
                vol.Optional(CONF_NID_AUT, default=defaults[CONF_NID_AUT]): str,
                vol.Optional(CONF_NID_SES, default=defaults[CONF_NID_SES]): str,
            }
        )
        return self.async_show_form(
            step_id="update_cookies", data_schema=schema, errors=errors
        )
