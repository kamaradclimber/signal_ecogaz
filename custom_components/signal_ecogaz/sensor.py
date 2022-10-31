import voluptuous as vol
from datetime import timedelta, datetime
from typing import Optional
import logging
import asyncio

from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.config_entries import ConfigEntry

from . import (
    EcoGazAPICoordinator,
    EcogazLevel,
)
from .const import (
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    _LOGGER.info("Called async setup entry")
    coordinator = hass.data[DOMAIN][entry.entry_id]
    sensors = []
    for i in range(5):
        sensors.append(EcogazLevel(coordinator, i, hass))

    async_add_entities(sensors)
    while not all(s.restored for s in sensors):
        _LOGGER.debug("Wait for all sensors to have been restored")
        await asyncio.sleep(0.2)
    _LOGGER.debug("All sensors have been restored properly")

    # we declare update_interval after initialization to avoid a first refresh before we setup entities
    coordinator.update_interval = timedelta(minutes=10)
    # force a first refresh immediately to avoid waiting for several minutes
    if any(s.state is None or s.state == 'unknown' for s in sensors):  # one sensor needs immediate refresh
        _LOGGER.debug("Triggering a first refresh")
        await coordinator.async_config_entry_first_refresh()
    else:
        # it means we might not get up to date info if HA was stopped for a while. TODO: detect last refresh for each sensor to take the best decision
        _LOGGER.info(
            "All sensors have already a known state, we'll wait next refresh to avoid hitting API limit after a restart"
        )
        coordinator._schedule_refresh()
    _LOGGER.info("We finished the setup of ecogaz *entity*")
