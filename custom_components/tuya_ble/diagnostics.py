"""Diagnostics support for Tuya BLE."""

from __future__ import annotations

import time
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_ADDRESS, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import CONF_ACCESS_ID, CONF_ACCESS_SECRET, CONF_LOCAL_KEY, CONF_UUID
from .devices import TuyaBLEConfigEntry

TO_REDACT = {
    CONF_ACCESS_ID,
    CONF_ACCESS_SECRET,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_LOCAL_KEY,
    CONF_UUID,
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TuyaBLEConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    address: str = entry.data[CONF_ADDRESS]
    result: dict[str, Any] = {
        "entry": {
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
    }

    # Bluetooth visibility, independent of whether the entry is loaded
    service_info = bluetooth.async_last_service_info(hass, address, connectable=True)
    result["bluetooth"] = {
        "seen": service_info is not None,
        "rssi": service_info.rssi if service_info else None,
        "source": service_info.source if service_info else None,
        "name": service_info.name if service_info else None,
        "connectable": service_info.connectable if service_info else None,
        "connectable_scanner_count": bluetooth.async_scanner_count(hass, connectable=True),
    }

    data = getattr(entry, "runtime_data", None)
    if data is None:
        result["device"] = {"loaded": False}
        return result

    device = data.device
    result["device"] = {
        "loaded": True,
        "address": device.address,
        "name": device.name,
        "product_id": device.product_id,
        "category": device.category,
        "product_name": device.product_name,
        "product_model": device.product_model,
        "device_version": device.device_version,
        "hardware_version": device.hardware_version,
        "protocol_version": device.protocol_version,
        "keep_connection": device.keep_connection,
        "connected": device.is_connected,
        "coordinator_connected": data.coordinator.connected,
        "seconds_since_last_connect": (
            round(time.monotonic() - device.last_connected_at, 1)
            if device.last_connected_at is not None
            else None
        ),
        "last_connect_error": device.last_connect_error,
        "rssi": device.rssi,
    }
    result["product_info"] = (
        {"manufacturer": data.product.manufacturer, "name": data.product.name}
        if data.product
        else None
    )
    result["datapoints"] = [
        {
            "id": dp.id,
            "type": dp.type.name if dp.type else None,
            "value": dp.value if not isinstance(dp.value, (bytes, bytearray)) else dp.value.hex(),
        }
        for dp in _iter_datapoints(device)
    ]
    return result


def _iter_datapoints(device) -> list:
    dps = device.datapoints
    inner = getattr(dps, "_datapoints", {})
    return [inner[k] for k in sorted(inner)]
