import os
import json
import logging
from datetime import timedelta, datetime
from typing import Any, Dict, Optional, Tuple
import aiohttp
from dateutil import tz


from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.typing import ConfigType
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.components.sensor import RestoreSensor

from .const import (
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    _LOGGER.info("Called async setup entry from __init__.py")

    hass.data.setdefault(DOMAIN, {})

    # here we store the coordinator for future access
    coordinator = EcoGazAPICoordinator(hass, dict(entry.data))
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # will make sure async_setup_entry from sensor.py is called
    await hass.config_entries.async_forward_entry_setups(entry, [Platform.SENSOR])

    # subscribe to config updates
    entry.async_on_unload(entry.add_update_listener(update_entry))

    return True


async def update_entry(hass, entry):
    """
    This method is called when options are updated
    We trigger the reloading of entry (that will eventually call async_unload_entry)
    """
    _LOGGER.debug("update_entry method called")
    # will make sure async_setup_entry from sensor.py is called
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """This method is called to clean all sensors before re-adding them"""
    _LOGGER.debug("async_unload_entry method called")
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, [Platform.SENSOR]
    )
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


class EcoGazAPICoordinator(DataUpdateCoordinator):
    """A coordinator to fetch data from the api only once"""

    def __init__(self, hass, config: ConfigType):
        super().__init__(
            hass,
            _LOGGER,
            name="ecogaz api",  # for logging purpose
            update_method=self.update_method,
        )
        self.config = config
        self.hass = hass

    async def update_method(self):
        """Fetch data from API endpoint.

        This could be the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """
        try:
            _LOGGER.debug(
                f"Calling update method, {len(self._listeners)} listeners subscribed"
            )
            if "ECOGAZ_APIFAIL" in os.environ:
                raise UpdateFailed(
                    "Failing update on purpose to test state restoration"
                )
            _LOGGER.debug("Starting collecting data")
            async with aiohttp.ClientSession() as session:
                async with session.get('https://odre.opendatasoft.com/api/records/1.0/search/?dataset=signal-ecogaz&q=&facet=gas_day&sort=gas_day&rows=30') as api_result:

                    _LOGGER.debug(f"data received, status code: {api_result.status}")
                    if api_result.status != 200:
                        raise UpdateFailed(
                            f"Error communicating with RTE API: status code was {api_result.status}"
                                )

                    response = await api_result.json()
            _LOGGER.debug(f"api response body: {response}")
            signals = response['records']
            for day_data in signals:
                parsed_date = datetime.strptime(
                    day_data["fields"]["gas_day"], "%Y-%m-%d"
                ).date()
                day_data["fields"]["date"] = parsed_date

            _LOGGER.debug(f"data parsed: {signals}")
            return signals
        except Exception as err:
            raise UpdateFailed(f"Error communicating with API: {err}")


class RestorableCoordinatedSensor(RestoreSensor):
    @property
    def restored(self):
        return self._restored

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        _LOGGER.debug("starting to restore sensor from previous data")
        
        if (extra_stored_data := await self.async_get_last_sensor_data()) is not None:
            if (
                extra_stored_data.native_value != "unknown"
                or self.restore_even_if_unknown()
            ):
                _LOGGER.debug(f"Restoring state for {self.unique_id}")
                self._attr_native_value = extra_stored_data.native_value
                self._attr_native_unit_of_measurement = extra_stored_data.native_unit_of_measurement
                # sadly it seems as of 2023.6 we don't have any way to get back the additional attributes
                self.coordinator.last_update_success = True
            else:
                # by not restoring state, we allow the Coordinator to fetch data again and fill
                # data as soon as possible
                _LOGGER.debug(f"Stored state was 'unknown', starting from scratch")
        # signal restoration happened
        self._restored = True



class EcogazLevel(CoordinatorEntity, RestorableCoordinatedSensor):
    """Representation of ecogaz level for a given day"""

    @property
    def unique_id(self) -> str:
        return f"ecogaz-level-in-{self.shift}-days"

    def __init__(
        self, coordinator: EcoGazAPICoordinator, shift: int, hass: HomeAssistant
    ):
        self._attr_name = f"Ecogaz level {self._day_string(shift)}"
        super().__init__(coordinator)
        self._restored = False
        self.hass = hass
        self._attr_extra_state_attributes: Dict[str, Any] = {}
        _LOGGER.info(f"Creating an ecogaz sensor, named {self.name}")
        self._state = None
        self.shift = shift
        self.happening_now = False
        # prevision in the past might not be available yet
        self.swallow_errors = shift > 2
        self.options = {
                1: "Situation normale",
                2: "Consommation élevée",
                3: "Situation tendue",
                4: "Situation très tendue",
                None: "Unknown",
                }
        self._attr_extra_state_attributes["options"] = list(self.options.values())

    def _timezone(self):
        timezone = self.hass.config.as_dict()["time_zone"]
        return tz.gettz(timezone)

    @callback
    def _handle_coordinator_update(self) -> None:
        if not self.coordinator.last_update_success:
            _LOGGER.debug("Last coordinator failed, assuming state has not changed")
            return
        try:
            ecogaz_level = self._find_ecogaz_level()
        except UnknownDayError:
            if self.swallow_errors:
                ecogaz_level = None
            else:
                raise
        previous_level = self._attr_extra_state_attributes.get("indice_de_couleur", None)
        self._attr_extra_state_attributes["indice_de_couleur"] = ecogaz_level
        self._state = self._level2string(ecogaz_level)
        self._attr_icon = self._level2icon(ecogaz_level)
        if previous_level != self._attr_extra_state_attributes["indice_de_couleur"]:
            _LOGGER.info(f"updated '{self.name}' with level {self._state}")
        self.async_write_ha_state()


    def _level2string(self, level):
        if self.happening_now and level == 4:
            return "Coupure de gaz possible"
        return self.options[level]

    def _level2icon(self, level):
        return {
            1: "mdi:check-circle",
            2: "mdi:alert",
            3: "mdi:power-plug-off",
            4: "mdi:power-plug-off",
            None: "mdi:progress-question",
        }[level]

    @property
    def state(self) -> Optional[str]:
        return self._state

    def _day_string(self, day_shift):
        if day_shift == 0:
            return "aujourd'hui"
        elif day_shift == 1:
            return "demain"
        else:
            return f"dans {day_shift} jours"

    @property
    def native_value(self):
        return self._state

    def _find_ecogaz_level(self) -> int:
        now = datetime.now(self._timezone())
        relevant_date = now + timedelta(days=self.shift)
        try:
            ecogaz_data = next(
                filter(
                    lambda e: e["fields"]["date"] == relevant_date.date(), self.coordinator.data
                )
            )
            self._attr_extra_state_attributes["timestamp"] = ecogaz_data["record_timestamp"]
            return int(ecogaz_data["fields"]["indice_de_couleur"])
        except StopIteration:
            raise UnknownDayError(
                f"Unable to find ecogaz level for {relevant_date.date()}"
            )

class UnknownDayError(RuntimeError):
    pass
