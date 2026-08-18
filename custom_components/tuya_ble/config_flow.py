"""Config flow for Tuya BLE integration."""

from __future__ import annotations

import logging
from typing import Any

import pycountry
import voluptuous as vol
from tuya_iot import AuthType

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import (
    CONF_ADDRESS,
    CONF_COUNTRY_CODE,
    CONF_DEVICE_ID,
    CONF_PASSWORD,
    CONF_USERNAME,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowHandler

from .cloud import HASSTuyaBLEDeviceManager
from .const import (
    CONF_ACCESS_ID,
    CONF_ACCESS_SECRET,
    CONF_AUTH_TYPE,
    CONF_CATEGORY,
    CONF_DEVICE_NAME,
    CONF_IDLE_DISCONNECT_DELAY,
    CONF_KEEP_CONNECTION,
    CONF_LOCAL_KEY,
    CONF_PRODUCT_ID,
    CONF_PRODUCT_MODEL,
    CONF_PRODUCT_NAME,
    CONF_UUID,
    DEFAULT_IDLE_DISCONNECT_DELAY,
    DEFAULT_KEEP_CONNECTION,
    DOMAIN,
    SMARTLIFE_APP,
    TUYA_COUNTRIES,
    TUYA_SMART_APP,
)
from .devices import get_device_readable_name
from .tuya_ble import SERVICE_UUID

# Defined locally: the core Tuya integration no longer exports these
# since its switch to QR-code login (HA >= 2025.12).
CONF_APP_TYPE = "tuya_app_type"
CONF_ENDPOINT = "endpoint"
TUYA_RESPONSE_CODE = "code"
TUYA_RESPONSE_MSG = "msg"
TUYA_RESPONSE_SUCCESS = "success"

_LOGGER = logging.getLogger(__name__)

# pycountry loads its database lazily from disk; trigger it once at import
# time (runs in the import executor) so it never happens in the event loop.
try:
    pycountry.countries.get(alpha_2="DE")
except Exception:  # pylint: disable=broad-except
    pass


# --------------------------------------------------------------------------- #
# Cloud login helpers                                                          #
# --------------------------------------------------------------------------- #


async def _try_login(
    manager: HASSTuyaBLEDeviceManager,
    user_input: dict[str, Any],
    errors: dict[str, str],
    placeholders: dict[str, Any],
) -> dict[str, Any] | None:
    response: dict[Any, Any] | None = None
    data: dict[str, Any]

    country = [
        country
        for country in TUYA_COUNTRIES
        if country.name == user_input[CONF_COUNTRY_CODE]
    ][0]

    data = {
        CONF_ENDPOINT: country.endpoint,
        CONF_AUTH_TYPE: AuthType.CUSTOM,
        CONF_ACCESS_ID: user_input[CONF_ACCESS_ID],
        CONF_ACCESS_SECRET: user_input[CONF_ACCESS_SECRET],
        CONF_USERNAME: user_input[CONF_USERNAME],
        CONF_PASSWORD: user_input[CONF_PASSWORD],
        CONF_COUNTRY_CODE: country.country_code,
    }

    for app_type in (TUYA_SMART_APP, SMARTLIFE_APP, ""):
        data[CONF_APP_TYPE] = app_type
        if app_type == "":
            data[CONF_AUTH_TYPE] = AuthType.CUSTOM
        else:
            data[CONF_AUTH_TYPE] = AuthType.SMART_HOME

        response = await manager._login(data, True)

        if response.get(TUYA_RESPONSE_SUCCESS, False):
            return data

    errors["base"] = "login_error"
    if response:
        placeholders.update(
            {
                TUYA_RESPONSE_CODE: response.get(TUYA_RESPONSE_CODE),
                TUYA_RESPONSE_MSG: response.get(TUYA_RESPONSE_MSG),
            }
        )

    return None


def _show_login_form(
    flow: FlowHandler,
    user_input: dict[str, Any],
    errors: dict[str, str],
    placeholders: dict[str, Any],
) -> ConfigFlowResult:
    """Shows the Tuya IOT platform login form."""
    if user_input is not None and user_input.get(CONF_COUNTRY_CODE) is not None:
        for country in TUYA_COUNTRIES:
            if country.country_code == user_input[CONF_COUNTRY_CODE]:
                user_input[CONF_COUNTRY_CODE] = country.name
                break

    def_country_name: str | None = None
    try:
        def_country = pycountry.countries.get(alpha_2=flow.hass.config.country)
        if def_country:
            def_country_name = def_country.name
    except Exception:  # pylint: disable=broad-except
        pass

    return flow.async_show_form(
        step_id="login",
        data_schema=vol.Schema(
            {
                vol.Required(
                    CONF_COUNTRY_CODE,
                    default=user_input.get(CONF_COUNTRY_CODE, def_country_name),
                ): vol.In(
                    # We don't pass a dict {code:name} because country codes can be duplicate.
                    [country.name for country in TUYA_COUNTRIES]
                ),
                vol.Required(
                    CONF_ACCESS_ID, default=user_input.get(CONF_ACCESS_ID, "")
                ): str,
                vol.Required(
                    CONF_ACCESS_SECRET,
                    default=user_input.get(CONF_ACCESS_SECRET, ""),
                ): str,
                vol.Required(
                    CONF_USERNAME, default=user_input.get(CONF_USERNAME, "")
                ): str,
                vol.Required(
                    CONF_PASSWORD, default=user_input.get(CONF_PASSWORD, "")
                ): str,
            }
        ),
        errors=errors,
        description_placeholders=placeholders,
    )


# --------------------------------------------------------------------------- #
# Manual credentials / settings helpers                                        #
# --------------------------------------------------------------------------- #


def _manual_schema(
    defaults: dict[str, Any], address_choices: dict[str, str] | None
) -> vol.Schema:
    fields: dict[Any, Any] = {}
    if address_choices:
        fields[
            vol.Required(
                CONF_ADDRESS,
                default=defaults.get(CONF_ADDRESS, next(iter(address_choices))),
            )
        ] = vol.In(address_choices)
    fields[vol.Required(CONF_UUID, default=defaults.get(CONF_UUID, ""))] = str
    fields[vol.Required(CONF_LOCAL_KEY, default=defaults.get(CONF_LOCAL_KEY, ""))] = str
    fields[vol.Required(CONF_DEVICE_ID, default=defaults.get(CONF_DEVICE_ID, ""))] = str
    fields[vol.Required(CONF_PRODUCT_ID, default=defaults.get(CONF_PRODUCT_ID, ""))] = str
    fields[vol.Required(CONF_CATEGORY, default=defaults.get(CONF_CATEGORY, "szjqr"))] = str
    fields[vol.Optional(CONF_DEVICE_NAME, default=defaults.get(CONF_DEVICE_NAME, ""))] = str
    return vol.Schema(fields)


def _validate_manual(
    user_input: dict[str, Any], errors: dict[str, str]
) -> dict[str, Any] | None:
    """Validate and normalise manual credentials; return options dict or None."""
    uuid = user_input[CONF_UUID].strip()
    local_key = user_input[CONF_LOCAL_KEY].strip()
    device_id = user_input[CONF_DEVICE_ID].strip()
    product_id = user_input[CONF_PRODUCT_ID].strip()
    category = user_input[CONF_CATEGORY].strip()

    if len(uuid) < 8:
        errors[CONF_UUID] = "invalid_uuid"
    if len(local_key) < 6:  # only the first 6 chars form the login key
        errors[CONF_LOCAL_KEY] = "invalid_local_key"
    if not device_id:
        errors[CONF_DEVICE_ID] = "invalid_device_id"
    if not product_id:
        errors[CONF_PRODUCT_ID] = "invalid_product_id"
    if not category:
        errors[CONF_CATEGORY] = "invalid_category"
    if errors:
        return None

    return {
        CONF_UUID: uuid,
        CONF_LOCAL_KEY: local_key,
        CONF_DEVICE_ID: device_id,
        CONF_PRODUCT_ID: product_id,
        CONF_CATEGORY: category,
        CONF_DEVICE_NAME: (user_input.get(CONF_DEVICE_NAME) or "").strip()
        or product_id,
        CONF_PRODUCT_NAME: "",
        CONF_PRODUCT_MODEL: "",
    }


def _settings_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_KEEP_CONNECTION,
                default=defaults.get(CONF_KEEP_CONNECTION, DEFAULT_KEEP_CONNECTION),
            ): bool,
            vol.Required(
                CONF_IDLE_DISCONNECT_DELAY,
                default=defaults.get(
                    CONF_IDLE_DISCONNECT_DELAY, DEFAULT_IDLE_DISCONNECT_DELAY
                ),
            ): vol.All(vol.Coerce(int), vol.Range(min=5, max=3600)),
        }
    )


# --------------------------------------------------------------------------- #
# Options flow                                                                 #
# --------------------------------------------------------------------------- #


class TuyaBLEOptionsFlow(OptionsFlow):
    """Handle a Tuya BLE options flow."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options menu."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["login", "manual", "settings"],
        )

    async def async_step_login(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Refresh credentials from the Tuya cloud."""
        errors: dict[str, str] = {}
        placeholders: dict[str, Any] = {}
        address: str | None = self.config_entry.data.get(CONF_ADDRESS)

        if user_input is not None:
            # Independent of the (possibly not loaded) runtime data
            manager = HASSTuyaBLEDeviceManager(
                self.hass, dict(self.config_entry.options)
            )
            login_data = await _try_login(manager, user_input, errors, placeholders)
            if login_data:
                credentials = await manager.get_device_credentials(
                    address, True, True
                )
                if credentials:
                    return self.async_create_entry(
                        title=self.config_entry.title,
                        data={**self.config_entry.options, **manager.data},
                    )
                errors["base"] = "device_not_registered"

        if user_input is None:
            user_input = dict(self.config_entry.options)

        return _show_login_form(self, user_input, errors, placeholders)

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit credentials manually."""
        errors: dict[str, str] = {}
        if user_input is not None:
            creds = _validate_manual(user_input, errors)
            if creds:
                return self.async_create_entry(
                    title=self.config_entry.title,
                    data={**self.config_entry.options, **creds},
                )
        defaults = user_input or dict(self.config_entry.options)
        return self.async_show_form(
            step_id="manual",
            data_schema=_manual_schema(defaults, None),
            errors=errors,
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Connection policy."""
        if user_input is not None:
            return self.async_create_entry(
                title=self.config_entry.title,
                data={**self.config_entry.options, **user_input},
            )
        return self.async_show_form(
            step_id="settings",
            data_schema=_settings_schema(dict(self.config_entry.options)),
        )


# --------------------------------------------------------------------------- #
# Config flow                                                                  #
# --------------------------------------------------------------------------- #


class TuyaBLEConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tuya BLE."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        super().__init__()
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}
        self._data: dict[str, Any] = {}
        self._manager: HASSTuyaBLEDeviceManager | None = None

    # ---- entry points ----

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle the bluetooth discovery step."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery_info = discovery_info
        await self._ensure_manager()
        self.context["title_placeholders"] = {
            "name": await get_device_readable_name(discovery_info, self._manager)
        }
        return await self.async_step_user()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose between cloud lookup and manual credentials."""
        await self._ensure_manager()
        return self.async_show_menu(
            step_id="user",
            menu_options=["login", "manual"],
        )

    async def _ensure_manager(self) -> None:
        if self._manager is None:
            self._manager = HASSTuyaBLEDeviceManager(self.hass, self._data)
            await self._manager.build_cache()

    # ---- cloud path ----

    async def async_step_login(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the Tuya IOT platform login step."""
        errors: dict[str, str] = {}
        placeholders: dict[str, Any] = {}

        if user_input is not None:
            data = await _try_login(self._manager, user_input, errors, placeholders)
            if data:
                self._data.update(data)
                return await self.async_step_device()

        if user_input is None:
            user_input = {}
            if self._discovery_info:
                await self._manager.get_device_credentials(
                    self._discovery_info.address, False, True
                )
            if not self._data:
                self._manager.get_login_from_cache()
            if self._data:
                user_input.update(self._data)

        return _show_login_form(self, user_input, errors, placeholders)

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a discovered device and fetch its credentials from the cloud."""
        errors: dict[str, str] = {}

        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            discovery_info = self._discovered_devices[address]
            local_name = await get_device_readable_name(discovery_info, self._manager)
            await self.async_set_unique_id(
                discovery_info.address, raise_on_progress=False
            )
            self._abort_if_unique_id_configured()
            # Always fetch fresh: a re-paired device has a new local_key
            credentials = await self._manager.get_device_credentials(
                discovery_info.address, True, True
            )
            self._data[CONF_ADDRESS] = discovery_info.address
            if credentials is None:
                errors["base"] = "device_not_registered"
            else:
                return self.async_create_entry(
                    title=local_name,
                    data={CONF_ADDRESS: discovery_info.address},
                    options=self._data,
                )

        self._collect_discovered_devices()
        if not self._discovered_devices:
            return self.async_abort(reason="no_unconfigured_devices")

        def_address = (
            user_input.get(CONF_ADDRESS)
            if user_input
            else list(self._discovered_devices)[0]
        )
        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS, default=def_address): vol.In(
                        {
                            service_info.address: await get_device_readable_name(
                                service_info, self._manager
                            )
                            for service_info in self._discovered_devices.values()
                        }
                    ),
                },
            ),
            errors=errors,
        )

    # ---- manual path ----

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Enter credentials manually (no cloud access needed)."""
        errors: dict[str, str] = {}

        self._collect_discovered_devices()
        if not self._discovered_devices:
            return self.async_abort(reason="no_unconfigured_devices")
        choices = {
            info.address: f"{info.name or 'Tuya BLE'} ({info.address})"
            for info in self._discovered_devices.values()
        }

        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            creds = _validate_manual(user_input, errors)
            if creds:
                await self.async_set_unique_id(address, raise_on_progress=False)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=creds[CONF_DEVICE_NAME],
                    data={CONF_ADDRESS: address},
                    options={CONF_ADDRESS: address, **creds},
                )

        defaults = dict(user_input or {})
        if self._discovery_info and CONF_ADDRESS not in defaults:
            defaults[CONF_ADDRESS] = self._discovery_info.address
        return self.async_show_form(
            step_id="manual",
            data_schema=_manual_schema(defaults, choices),
            errors=errors,
        )

    # ---- helpers ----

    def _collect_discovered_devices(self) -> None:
        if discovery := self._discovery_info:
            self._discovered_devices[discovery.address] = discovery
            return
        current_addresses = self._async_current_ids()
        for discovery in async_discovered_service_info(self.hass):
            if (
                discovery.address in current_addresses
                or discovery.address in self._discovered_devices
                or discovery.service_data is None
                or SERVICE_UUID not in discovery.service_data.keys()
            ):
                continue
            self._discovered_devices[discovery.address] = discovery

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> TuyaBLEOptionsFlow:
        """Get the options flow for this handler."""
        return TuyaBLEOptionsFlow()
