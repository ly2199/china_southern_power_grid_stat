"""Sensors for the China Southern Power Grid Statistics integration."""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
import traceback
from datetime import timedelta
from typing import Any

import async_timeout
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_USERNAME,
    STATE_UNAVAILABLE,
    UnitOfEnergy,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from . import CONF_UPDATED_AT
from .const import (
    ATTR_KEY_ACCOUNT_INFO,
    ATTR_KEY_BILL_DETAILS,
    ATTR_KEY_BILL_HISTORY,
    ATTR_KEY_CURRENT_LADDER_START_DATE,
    ATTR_KEY_LAST_MONTH_BY_DAY,
    ATTR_KEY_LAST_YEAR_BY_MONTH,
    ATTR_KEY_LATEST_DAY_DATE,
    ATTR_KEY_OUTAGE_EVENTS,
    ATTR_KEY_OUTAGE_NOTICES,
    ATTR_KEY_PAYMENT_HISTORY,
    ATTR_KEY_THIS_MONTH_BY_DAY,
    ATTR_KEY_THIS_MONTH_TEMPERATURE,
    ATTR_KEY_THIS_YEAR_BY_MONTH,
    CONF_AUTH_TOKEN,
    CONF_ELE_ACCOUNTS,
    CONF_FEATURES,
    CONF_HAS_DAILY_CHARGE,
    CONF_HAS_LADDER,
    CONF_SETTINGS,
    CONF_UPDATE_INTERVAL,
    DATA_KEY_LAST_UPDATE_DAY,
    DOMAIN,
    SETTING_LAST_MONTH_UPDATE_DAY_THRESHOLD,
    SETTING_LAST_YEAR_UPDATE_DAY_THRESHOLD,
    SETTING_UPDATE_TIMEOUT,
    STATE_UPDATE_UNCHANGED,
    SUFFIX_ARR,
    SUFFIX_BAL,
    SUFFIX_CURRENT_LADDER,
    SUFFIX_CURRENT_LADDER_REMAINING_KWH,
    SUFFIX_CURRENT_LADDER_TARIFF,
    SUFFIX_LAST_MONTH_COST,
    SUFFIX_LAST_MONTH_KWH,
    SUFFIX_LAST_YEAR_COST,
    SUFFIX_LAST_YEAR_KWH,
    SUFFIX_LATEST_DAY_COST,
    SUFFIX_LATEST_DAY_KWH,
    SUFFIX_OUTAGE_EVENT,
    SUFFIX_OUTAGE_NOTICE,
    SUFFIX_THIS_MONTH_AVG_TEMP,
    SUFFIX_THIS_MONTH_COST,
    SUFFIX_THIS_MONTH_KWH,
    SUFFIX_THIS_YEAR_COST,
    SUFFIX_THIS_YEAR_KWH,
    SUFFIX_YESTERDAY_KWH,
)
from .csg_client import (
    JSON_KEY_METERING_POINT_NUMBER,
    WF_ATTR_AVG_TEMP,
    WF_ATTR_CHARGE,
    WF_ATTR_DATE,
    WF_ATTR_KWH,
    WF_ATTR_LADDER,
    WF_ATTR_LADDER_REMAINING_KWH,
    WF_ATTR_LADDER_START_DATE,
    WF_ATTR_LADDER_TARIFF,
    WF_ATTR_MAX_TEMP,
    WF_ATTR_MIN_TEMP,
    CSGAPIError,
    CSGClient,
    CSGElectricityAccount,
    NotLoggedIn,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    """Setup sensors from a config entry created in the integrations UI."""
    if not config_entry.data[CONF_ELE_ACCOUNTS]:
        _LOGGER.info("No ele accounts in config, exit entry setup")
        return
    coordinator = CSGCoordinator(hass, config_entry.entry_id)

    # features may not be cached yet (e.g. right after upgrade); in that case
    # create all sensors and let the coordinator trim the unsupported ones
    features = config_entry.data.get(CONF_FEATURES, {})
    has_daily_charge = features.get(CONF_HAS_DAILY_CHARGE, True)
    has_ladder = features.get(CONF_HAS_LADDER, True)

    all_sensors = []
    for ele_account_number, _ in config_entry.data[CONF_ELE_ACCOUNTS].items():
        sensors = [
            # balance, with extra attributes about the account
            CSGCostSensor(
                coordinator,
                ele_account_number,
                SUFFIX_BAL,
                extra_state_attributes_key=ATTR_KEY_ACCOUNT_INFO,
            ),
            # arrears
            CSGCostSensor(coordinator, ele_account_number, SUFFIX_ARR),
            # yesterday kwh
            CSGEnergySensor(
                coordinator,
                ele_account_number,
                SUFFIX_YESTERDAY_KWH,
            ),
            # latest day usage that is available, with extra attributes about the date
            CSGEnergySensor(
                coordinator,
                ele_account_number,
                SUFFIX_LATEST_DAY_KWH,
                extra_state_attributes_key=ATTR_KEY_LATEST_DAY_DATE,
            ),
            # this year's total energy, with extra attributes about monthly usage
            CSGEnergySensor(
                coordinator,
                ele_account_number,
                SUFFIX_THIS_YEAR_KWH,
                extra_state_attributes_key=ATTR_KEY_THIS_YEAR_BY_MONTH,
            ),
            # this year's total cost, with extra attributes about monthly bills
            CSGCostSensor(
                coordinator,
                ele_account_number,
                SUFFIX_THIS_YEAR_COST,
                extra_state_attributes_key=ATTR_KEY_BILL_HISTORY,
            ),
            # this month's total energy, with extra attributes about daily usage
            CSGEnergySensor(
                coordinator,
                ele_account_number,
                SUFFIX_THIS_MONTH_KWH,
                extra_state_attributes_key=ATTR_KEY_THIS_MONTH_BY_DAY,
            ),
            # this month's average temperature, with extra attributes about
            # max/min temperature and daily temperature (electricity calendar)
            CSGTemperatureSensor(
                coordinator,
                ele_account_number,
                SUFFIX_THIS_MONTH_AVG_TEMP,
                extra_state_attributes_key=ATTR_KEY_THIS_MONTH_TEMPERATURE,
            ),
            # latest outage/maintenance notice of the region
            CSGOutageNoticeSensor(
                coordinator,
                ele_account_number,
                SUFFIX_OUTAGE_NOTICE,
                extra_state_attributes_key=ATTR_KEY_OUTAGE_NOTICES,
            ),
            # ongoing/upcoming power outages near the home
            CSGOutageEventSensor(
                coordinator,
                ele_account_number,
                SUFFIX_OUTAGE_EVENT,
                extra_state_attributes_key=ATTR_KEY_OUTAGE_EVENTS,
            ),
            # last year's total energy, with extra attributes about monthly usage
            CSGEnergySensor(
                coordinator,
                ele_account_number,
                SUFFIX_LAST_YEAR_KWH,
                extra_state_attributes_key=ATTR_KEY_LAST_YEAR_BY_MONTH,
            ),
            # last year's total cost
            CSGCostSensor(
                coordinator,
                ele_account_number,
                SUFFIX_LAST_YEAR_COST,
            ),
            # last month's total energy, with extra attributes about daily usage
            CSGEnergySensor(
                coordinator,
                ele_account_number,
                SUFFIX_LAST_MONTH_KWH,
                extra_state_attributes_key=ATTR_KEY_LAST_MONTH_BY_DAY,
            ),
            # last month's total cost, with extra attributes about the bill
            CSGCostSensor(
                coordinator,
                ele_account_number,
                SUFFIX_LAST_MONTH_COST,
                extra_state_attributes_key=ATTR_KEY_BILL_DETAILS,
            ),
        ]

        if has_daily_charge:
            sensors.extend(
                [
                    # latest day cost that is available, with extra attributes about the date
                    CSGCostSensor(
                        coordinator,
                        ele_account_number,
                        SUFFIX_LATEST_DAY_COST,
                        extra_state_attributes_key=ATTR_KEY_LATEST_DAY_DATE,
                    ),
                    # this month's total cost, with extra attributes about daily usage
                    CSGCostSensor(
                        coordinator,
                        ele_account_number,
                        SUFFIX_THIS_MONTH_COST,
                        extra_state_attributes_key=ATTR_KEY_THIS_MONTH_BY_DAY,
                    ),
                ]
            )

        if has_ladder:
            sensors.extend(
                [
                    # current ladder, with extra attributes about start date
                    CSGLadderStageSensor(
                        coordinator,
                        ele_account_number,
                        SUFFIX_CURRENT_LADDER,
                        extra_state_attributes_key=ATTR_KEY_CURRENT_LADDER_START_DATE,
                    ),
                    # current ladder remaining kwh
                    CSGEnergySensor(
                        coordinator,
                        ele_account_number,
                        SUFFIX_CURRENT_LADDER_REMAINING_KWH,
                    ),
                    # current ladder tariff
                    CSGCostSensor(
                        coordinator,
                        ele_account_number,
                        SUFFIX_CURRENT_LADDER_TARIFF,
                    ),
                ]
            )

        all_sensors.extend(sensors)

    async_add_entities(all_sensors)
    _LOGGER.debug(f"created {len(all_sensors)} sensors for config {config_entry.title}")
    # Schedule the first update to run in the background
    config_entry.async_create_task(
        hass,
        coordinator.async_config_entry_first_refresh(),
        f"{config_entry.title}_first_update",
    )


class CSGBaseSensor(
    CoordinatorEntity,
    SensorEntity,
):
    """Base CSG sensor"""

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        account_number: str,
        entity_suffix: str,
        extra_state_attributes_key: str | None = None,
    ) -> None:
        SensorEntity.__init__(self)
        CoordinatorEntity.__init__(self, coordinator)
        self._coordinator = coordinator
        self._account_number = account_number

        self._entity_suffix = entity_suffix
        self._attr_extra_state_attributes = {}
        self._extra_state_attributes_key = extra_state_attributes_key

    @property
    def unique_id(self) -> str | None:
        return f"{DOMAIN}.{self._account_number}.{self._entity_suffix}"

    @property
    def name(self) -> str | None:
        return f"{self._account_number}-{self._entity_suffix}"

    @property
    def should_poll(self) -> bool:
        return False

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._account_number)},
            name=f"CSGAccount-{self._account_number}",
            manufacturer="CSG",
            model="CSG Virtual Electricity Meter",
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # _LOGGER.debug(
        #     "%s coordinator update triggered",
        #     self.unique_id,
        # )

        if not self._coordinator.data:
            _LOGGER.error(
                "%s coordinator has no data",
                self.unique_id,
            )
            self._attr_available = False
            self.async_write_ha_state()
            return

        account_data = self._coordinator.data.get(self._account_number)
        if account_data is None:
            _LOGGER.warning("%s not found in coordinator data", self.unique_id)
            self._attr_available = False
            self.async_write_ha_state()
            return

        new_native_value = account_data.get(self._entity_suffix)
        if new_native_value is None:
            _LOGGER.warning("%s data not found in coordinator data", self.unique_id)
            self._attr_available = False
            self.async_write_ha_state()
            return

        if new_native_value == STATE_UNAVAILABLE:
            _LOGGER.debug("%s data is unavailable", self.unique_id)
            self._attr_available = False
            self.async_write_ha_state()
            return

        # from this point the value is available
        self._attr_available = True

        if new_native_value == STATE_UPDATE_UNCHANGED:
            # no update for this sensor, skip
            _LOGGER.debug("%s doesn't need to be updated, skip", self.unique_id)
            return

        # from this point, `new_native_value` is a true value
        self._attr_native_value = new_native_value

        if self._extra_state_attributes_key:
            new_attributes = account_data.get(self._extra_state_attributes_key)
            if new_attributes is None:
                new_attributes = {}
                _LOGGER.warning(
                    "%s attribute %s not found in coordinator data",
                    self.unique_id,
                    self._extra_state_attributes_key,
                )
            self._attr_extra_state_attributes = new_attributes
        _LOGGER.debug("%s state update done!", self.unique_id)
        self.async_write_ha_state()


class CSGEnergySensor(CSGBaseSensor):
    """Representation of a CSG Energy Sensor."""

    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:lightning-bolt"


class CSGCostSensor(CSGBaseSensor):
    """Representation of a CSG Cost Sensor."""

    _attr_native_unit_of_measurement = "CNY"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:currency-cny"


class CSGTemperatureSensor(CSGBaseSensor):
    """Representation of a CSG Temperature Sensor."""

    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:thermometer"


class CSGOutageNoticeSensor(CSGBaseSensor):
    """Representation of the latest outage/maintenance notice."""

    _attr_icon = "mdi:power-plug-off"


class CSGOutageEventSensor(CSGBaseSensor):
    """Representation of ongoing/upcoming power outages near the home."""

    _attr_icon = "mdi:flash-alert"


class CSGLadderStageSensor(CSGBaseSensor):
    """Representation of a CSG Ladder Stage Sensor."""

    _attr_icon = "mdi:stairs"


class CSGCoordinator(DataUpdateCoordinator):
    """CSG custom coordinator."""

    def __init__(self, hass: HomeAssistant, config_entry_id: str) -> None:
        """Initialize coordinator."""
        self._config_entry_id = config_entry_id
        self._config = hass.config_entries.async_get_entry(self._config_entry_id).data
        super().__init__(
            hass,
            _LOGGER,
            # Name of the data. For logging purposes.
            name=f"CSG Account {self._config[CONF_USERNAME]}",
            # Polling interval. Will only be polled if there are subscribers.
            update_interval=timedelta(
                seconds=self._config[CONF_SETTINGS][CONF_UPDATE_INTERVAL]
            ),
        )
        self._client: CSGClient | None = None
        self._if_update_last_month = True
        self._if_update_last_year = True
        self._this_day = None
        self._this_year = None
        self._this_month_ym = None
        self._last_year = None
        self._last_month_ym = None
        self._this_month_update_completed_flag = asyncio.Event()
        self._gathered_data = {}

    async def _async_refresh_client(self):
        """Refresh the client, update the user data.
        It cannot re-login if the session is invalidated.
        """
        _LOGGER.debug("Refreshing client")
        self._client = await self.hass.async_add_executor_job(
            CSGClient.load,
            {
                CONF_AUTH_TOKEN: self._config[CONF_AUTH_TOKEN],
            },
        )
        logged_in = await self.hass.async_add_executor_job(
            self._client.verify_login,
        )
        if not logged_in:
            _LOGGER.warning(f"{self._config[CONF_USERNAME]}: Login expired")
            raise ConfigEntryAuthFailed("Login expired")

        _LOGGER.debug(f"{self._config[CONF_USERNAME]}: Session still valid")
        await self.hass.async_add_executor_job(self._client.initialize)

    async def _async_fetch(self, func: callable, *args, **kwargs) -> (bool, tuple):
        """Wrapper to fetch data from API. Return (success, result) with timeout.
        Also handle all exceptions here to avoid task group being cancelled.
        """
        try:
            async with async_timeout.timeout(SETTING_UPDATE_TIMEOUT):
                return True, await self.hass.async_add_executor_job(
                    func, *args, **kwargs
                )

        except asyncio.TimeoutError as err:
            _LOGGER.error("Timeout fetching data in function: %s", func.__name__)
            return False, (func.__name__, err)
        except NotLoggedIn as err:
            _LOGGER.error(
                "Session invalidated unexpectedly in function: %s", func.__name__
            )
            return False, (func.__name__, err)
        except CSGAPIError as err:
            _LOGGER.error(
                "Error fetching data in coordinator: API error, function %s, %s",
                func.__name__,
                err,
            )
            return False, (func.__name__, err)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.error("Unexpected exception: %s", err)
            _LOGGER.error(traceback.format_exc())
            return False, (func.__name__, err)

    async def _async_update_bal_arr(self, account: CSGElectricityAccount):
        """Update balance and arrears"""
        success, result = await self._async_fetch(
            self._client.get_balance_and_arrears, account
        )
        if success:
            balance, arrears = result
            _LOGGER.debug(
                "Updated balance and arrears for account %s: %s",
                account.account_number,
                result,
            )
        else:
            balance, arrears = STATE_UNAVAILABLE, STATE_UNAVAILABLE
            _LOGGER.error(
                "Error updating balance and arrears for account %s: %s",
                account.account_number,
                result,
            )
        self._gathered_data[account.account_number][SUFFIX_BAL] = balance
        self._gathered_data[account.account_number][SUFFIX_ARR] = arrears

    async def _async_update_account_info(self, account: CSGElectricityAccount):
        """Update account info (user name, address, arrears status)"""
        success, result = await self._async_fetch(
            self._client.get_account_info, account
        )
        if success:
            account_info = result
            _LOGGER.debug(
                "Updated account info for account %s: %s",
                account.account_number,
                result,
            )
        else:
            account_info = STATE_UNAVAILABLE
            _LOGGER.error(
                "Error updating account info for account %s: %s",
                account.account_number,
                result,
            )
        self._gathered_data[account.account_number][ATTR_KEY_ACCOUNT_INFO] = {
            ATTR_KEY_ACCOUNT_INFO: account_info
        }

    async def _async_update_bill_history(self, account: CSGElectricityAccount):
        """Update monthly bill history (recent 12 months)"""
        success, result = await self._async_fetch(
            self._client.get_bill_history, account
        )
        if success:
            bill_history = result
            _LOGGER.debug(
                "Updated bill history for account %s: %s",
                account.account_number,
                result,
            )
        else:
            bill_history = STATE_UNAVAILABLE
            _LOGGER.error(
                "Error updating bill history for account %s: %s",
                account.account_number,
                result,
            )
        self._gathered_data[account.account_number][ATTR_KEY_BILL_HISTORY] = {
            ATTR_KEY_BILL_HISTORY: bill_history
        }

    async def _async_update_bill_details(self, account: CSGElectricityAccount):
        """Update the latest available bill details (attached to last month cost)"""
        success, result = await self._async_fetch(
            self._client.get_bill_details, account, self._last_month_ym
        )
        if success:
            bill_details = self._filter_bill_details(result)
            _LOGGER.debug(
                "Updated bill details for account %s: %s",
                account.account_number,
                result,
            )
        else:
            bill_details = STATE_UNAVAILABLE
            _LOGGER.error(
                "Error updating bill details for account %s: %s",
                account.account_number,
                result,
            )
        self._gathered_data[account.account_number][ATTR_KEY_BILL_DETAILS] = {
            ATTR_KEY_BILL_DETAILS: bill_details
        }

    @staticmethod
    def _filter_bill_details(bill_detail: dict) -> dict:
        """Extract the useful fields from the raw bill detail response"""
        mapping = {
            "electricityBillYearMonth": "bill_year_month",
            "meteringPointNumber": "metering_point_number",
            "meterReadingDate": "meter_read_date",
            "lastIndicators": "last_indicators",
            "theIndicators": "the_indicators",
            "useElectricDay": "use_electric_day",
            "meterReadCycle": "meter_read_cycle",
            "billingCycle": "billing_cycle",
            "shoPaymentDate": "payment_due_date",
            "paymentWay": "payment_way",
            "supplyUnitName": "supply_unit",
            "tariffCodeName": "tariff_type",
            "settleAcctName": "settle_acct_name",
            "receieElectricity": "bill_amount",
            "curReceieElectricity": "current_bill_amount",
            "receivablePenalty": "penalty",
            "penaltyDate": "penalty_date",
            "historyTotalArrears": "history_total_arrears",
            "averageElectricityPrice": "average_price",
            "peakPower": "peak_kwh",
            "flatPower": "flat_kwh",
            "valleyPower": "valley_kwh",
            "peakElectricityBills": "peak_bill",
            "flatElectricityBills": "flat_bill",
            "valleyElectricityBills": "valley_bill",
            "basicElectricity": "basic_electricity",
            "demandTotalElectricity": "demand_electricity",
            "oneUsed": "ladder_1_used",
            "twoUsed": "ladder_2_used",
            "oneSurplus": "ladder_1_surplus",
            "twoSurplus": "ladder_2_surplus",
            "firstUseSituation": "ladder_1_situation",
            "secondUseSituation": "ladder_2_situation",
            "remarks": "ladder_remarks",
        }
        result = {}
        for src_key, dst_key in mapping.items():
            value = bill_detail.get(src_key)
            if value is not None:
                result[dst_key] = value
        return result

    async def _async_update_this_month_temperature(
        self, account: CSGElectricityAccount
    ):
        """Update this month's temperature from the electricity calendar"""
        success, result = await self._async_fetch(
            self._client.get_month_daily_calendar, account, self._this_month_ym
        )
        if success and result is not None:
            avg_temp, max_temp, min_temp, temp_by_day, summary = result
            _LOGGER.debug(
                "Updated this month's temperature for account %s: %s",
                account.account_number,
                result,
            )
        else:
            avg_temp, max_temp, min_temp, temp_by_day, summary = (
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
            )
            _LOGGER.error(
                "Error updating this month's temperature for account %s: %s",
                account.account_number,
                result,
            )
        self._gathered_data[account.account_number][
            SUFFIX_THIS_MONTH_AVG_TEMP
        ] = avg_temp
        self._gathered_data[account.account_number][ATTR_KEY_THIS_MONTH_TEMPERATURE] = {
            ATTR_KEY_THIS_MONTH_TEMPERATURE: {
                "avg": avg_temp,
                "max": max_temp,
                "min": min_temp,
                "by_day": temp_by_day,
                "summary": summary,
            }
        }

    def _merge_temperature_into_month_by_day(self, account: CSGElectricityAccount):
        """Attach the daily temperature to the this_month_by_day attribute"""
        temp_attr = self._gathered_data[account.account_number].get(
            ATTR_KEY_THIS_MONTH_TEMPERATURE, {}
        ).get(ATTR_KEY_THIS_MONTH_TEMPERATURE, {})
        temp_by_day = temp_attr.get("by_day")
        month_by_day = self._gathered_data[account.account_number].get(
            ATTR_KEY_THIS_MONTH_BY_DAY, {}
        ).get(ATTR_KEY_THIS_MONTH_BY_DAY)
        if not isinstance(temp_by_day, list) or not isinstance(month_by_day, list):
            return
        temp_by_date = {d[WF_ATTR_DATE]: d for d in temp_by_day}
        for item in month_by_day:
            temp = temp_by_date.get(item.get(WF_ATTR_DATE))
            if not temp:
                continue
            for key in (WF_ATTR_MAX_TEMP, WF_ATTR_MIN_TEMP, WF_ATTR_AVG_TEMP):
                if key in temp:
                    item[key] = temp[key]

    async def _async_update_payment_history(self, account: CSGElectricityAccount):
        """Update the recent payment/recharge history"""
        success, result = await self._async_fetch(
            self._client.get_payment_history, account
        )
        if success:
            payment_history = result
            _LOGGER.debug(
                "Updated payment history for account %s: %s",
                account.account_number,
                result,
            )
        else:
            payment_history = STATE_UNAVAILABLE
            _LOGGER.error(
                "Error updating payment history for account %s: %s",
                account.account_number,
                result,
            )
        self._gathered_data[account.account_number][ATTR_KEY_PAYMENT_HISTORY] = {
            ATTR_KEY_PAYMENT_HISTORY: payment_history
        }

    def _merge_payment_history_into_account_info(
        self, account: CSGElectricityAccount
    ):
        """Attach the payment history to the account info attribute"""
        payment_attr = self._gathered_data[account.account_number].get(
            ATTR_KEY_PAYMENT_HISTORY, {}
        ).get(ATTR_KEY_PAYMENT_HISTORY)
        if payment_attr is None:
            return
        account_info = self._gathered_data[account.account_number].get(
            ATTR_KEY_ACCOUNT_INFO, {}
        )
        info = account_info.get(ATTR_KEY_ACCOUNT_INFO)
        if not isinstance(info, dict):
            info = {}
        if payment_attr != STATE_UNAVAILABLE:
            info["payment_history"] = payment_attr
        account_info[ATTR_KEY_ACCOUNT_INFO] = info
        self._gathered_data[account.account_number][
            ATTR_KEY_ACCOUNT_INFO
        ] = account_info

    async def _async_update_outage_notice(self, account: CSGElectricityAccount):
        """Update the outage/maintenance notices of the region"""
        success, result = await self._async_fetch(
            self._client.get_outage_notices, account
        )
        if success:
            notices = result
            _LOGGER.debug(
                "Updated outage notices for account %s: %s",
                account.account_number,
                result,
            )
            latest = notices[0] if notices else STATE_UNAVAILABLE
        else:
            notices = STATE_UNAVAILABLE
            latest = STATE_UNAVAILABLE
            _LOGGER.error(
                "Error updating outage notices for account %s: %s",
                account.account_number,
                result,
            )
        self._gathered_data[account.account_number][SUFFIX_OUTAGE_NOTICE] = latest
        self._gathered_data[account.account_number][ATTR_KEY_OUTAGE_NOTICES] = {
            ATTR_KEY_OUTAGE_NOTICES: notices
        }

    async def _async_update_outage_event(self, account: CSGElectricityAccount):
        """Update the power outages near the home location"""
        latitude = self.hass.config.latitude
        longitude = self.hass.config.longitude
        if not latitude or not longitude:
            _LOGGER.warning(
                "Home location is not set in Home Assistant, "
                "cannot query power outages"
            )
            self._gathered_data[account.account_number][SUFFIX_OUTAGE_EVENT] = (
                STATE_UNAVAILABLE
            )
            self._gathered_data[account.account_number][ATTR_KEY_OUTAGE_EVENTS] = {
                ATTR_KEY_OUTAGE_EVENTS: STATE_UNAVAILABLE
            }
            return
        success, result = await self._async_fetch(
            self._client.get_power_outages,
            account,
            latitude,
            longitude,
        )
        if success:
            data = result
            _LOGGER.debug(
                "Updated power outages for account %s: %s",
                account.account_number,
                result,
            )
            ongoing = data["ongoing"]
        else:
            data = STATE_UNAVAILABLE
            ongoing = STATE_UNAVAILABLE
            _LOGGER.error(
                "Error updating power outages for account %s: %s",
                account.account_number,
                result,
            )
        self._gathered_data[account.account_number][SUFFIX_OUTAGE_EVENT] = ongoing
        self._gathered_data[account.account_number][ATTR_KEY_OUTAGE_EVENTS] = {
            ATTR_KEY_OUTAGE_EVENTS: data
        }

    async def _async_update_yesterday_kwh(self, account: CSGElectricityAccount):
        """Update yesterday's kwh"""
        success, result = await self._async_fetch(
            self._client.get_yesterday_kwh,
            account,
        )
        if success and result is not None:
            yesterday_kwh = result
            _LOGGER.debug(
                "Updated yesterday's kwh for account %s: %s",
                account.account_number,
                result,
            )
        elif success:
            # the api may legitimately return null when yesterday's data
            # has not been generated yet
            _LOGGER.debug(
                "Yesterday's kwh for account %s: no data yet",
                account.account_number,
            )
            yesterday_kwh = STATE_UNAVAILABLE
        else:
            yesterday_kwh = STATE_UNAVAILABLE
            _LOGGER.error(
                "Error updating yesterday's kwh for account %s: %s",
                account.account_number,
                result,
            )
        self._gathered_data[account.account_number][
            SUFFIX_YESTERDAY_KWH
        ] = yesterday_kwh

    async def _async_update_this_year_stats(self, account: CSGElectricityAccount):
        """Update this year's data"""
        success, result = await self._async_fetch(
            self._client.get_year_month_stats, account, self._this_year
        )
        if success:
            (
                this_year_cost,
                this_year_kwh,
                this_year_by_month,
            ) = result

            _LOGGER.debug(
                "Updated this year's data for account %s: %s",
                account.account_number,
                result,
            )
        else:
            _LOGGER.error(
                "Error updating this year's data for account %s: %s",
                account.account_number,
                result,
            )
            this_year_cost, this_year_kwh, this_year_by_month = (
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
            )
        self._gathered_data[account.account_number][
            SUFFIX_THIS_YEAR_KWH
        ] = this_year_kwh
        self._gathered_data[account.account_number][
            SUFFIX_THIS_YEAR_COST
        ] = this_year_cost
        self._gathered_data[account.account_number][ATTR_KEY_THIS_YEAR_BY_MONTH] = {
            ATTR_KEY_THIS_YEAR_BY_MONTH: this_year_by_month
        }

    async def _async_update_last_year_stats(self, account: CSGElectricityAccount):
        """Update last year's data"""
        if not self._if_update_last_year:
            self._gathered_data[account.account_number][
                SUFFIX_LAST_YEAR_KWH
            ] = STATE_UPDATE_UNCHANGED
            self._gathered_data[account.account_number][
                SUFFIX_LAST_YEAR_COST
            ] = STATE_UPDATE_UNCHANGED
            self._gathered_data[account.account_number][ATTR_KEY_LAST_YEAR_BY_MONTH] = {
                ATTR_KEY_LAST_YEAR_BY_MONTH: STATE_UPDATE_UNCHANGED
            }
            _LOGGER.debug(
                "Last year's data for account %s: no need to update",
                account.account_number,
            )
            return
        success, result = await self._async_fetch(
            self._client.get_year_month_stats, account, self._last_year
        )
        if success:
            (
                last_year_cost,
                last_year_kwh,
                last_year_by_month,
            ) = result

            _LOGGER.debug(
                "Updated last year's data for account %s: %s",
                account.account_number,
                result,
            )
        else:
            _LOGGER.error(
                "Error updating last year's data for account %s: %s",
                account.account_number,
                result,
            )
            last_year_cost, last_year_kwh, last_year_by_month = (
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
            )
        self._gathered_data[account.account_number][
            SUFFIX_LAST_YEAR_KWH
        ] = last_year_kwh
        self._gathered_data[account.account_number][
            SUFFIX_LAST_YEAR_COST
        ] = last_year_cost
        self._gathered_data[account.account_number][ATTR_KEY_LAST_YEAR_BY_MONTH] = {
            ATTR_KEY_LAST_YEAR_BY_MONTH: last_year_by_month
        }

    @staticmethod
    def merge_by_day_data(
        by_day_from_cost: list | str | None,
        kwh_from_cost: float | str | None,
        by_day_from_usage: list | str | None,
        kwh_from_usage: float | str | None,
    ) -> (list | str, float | str):
        """Merge by_day_from_usage and by_day_from_cost data"""
        if by_day_from_cost is None:
            by_day_from_cost = STATE_UNAVAILABLE
        if by_day_from_usage is None:
            by_day_from_usage = STATE_UNAVAILABLE
        if kwh_from_cost is None:
            kwh_from_cost = STATE_UNAVAILABLE
        if kwh_from_usage is None:
            kwh_from_usage = STATE_UNAVAILABLE
        # merge by_day
        # determine which is the latest
        if (
            by_day_from_cost == STATE_UNAVAILABLE
            and by_day_from_usage == STATE_UNAVAILABLE
        ):
            by_day = STATE_UNAVAILABLE
        elif by_day_from_cost == STATE_UNAVAILABLE:
            by_day = by_day_from_usage
        elif by_day_from_usage == STATE_UNAVAILABLE:
            by_day = by_day_from_cost
        else:
            # both are available
            if len(by_day_from_cost) >= len(by_day_from_usage):
                # the result from daily cost is newer
                by_day = by_day_from_cost
            else:
                # the result from daily usage is newer
                # but since the result from daily cost contains cost data, need to merge them
                by_day = by_day_from_usage
                cost_by_date = {
                    d[WF_ATTR_DATE]: d[WF_ATTR_CHARGE]
                    for d in by_day_from_cost
                    if WF_ATTR_DATE in d
                }
                for item in by_day:
                    charge = cost_by_date.get(item.get(WF_ATTR_DATE))
                    if charge is not None:
                        item[WF_ATTR_CHARGE] = charge

        # determine which one to use as kwh
        if kwh_from_cost == STATE_UNAVAILABLE and kwh_from_usage == STATE_UNAVAILABLE:
            kwh = STATE_UNAVAILABLE
        elif kwh_from_cost == STATE_UNAVAILABLE:
            kwh = kwh_from_usage
        elif kwh_from_usage == STATE_UNAVAILABLE:
            kwh = kwh_from_cost
        else:
            # determine which kwh is the latest
            # get the larger one
            kwh = max(kwh_from_cost, kwh_from_usage)
        return by_day, kwh

    async def _async_update_this_month_stats_and_ladder(
        self, account: CSGElectricityAccount
    ):
        """Update this month's usage, cost and ladder"""
        self._this_month_update_completed_flag.clear()
        try:
            await self._async_update_this_month_stats_and_ladder_inner(account)
        finally:
            self._this_month_update_completed_flag.set()

    async def _async_update_this_month_stats_and_ladder_inner(
        self, account: CSGElectricityAccount
    ):
        """Fetch usage and cost in parallel, then merge and store the results"""
        # fetch usage and cost in parallel
        task_fetch_usage = asyncio.create_task(
            self._async_fetch(
                self._client.get_month_daily_usage_detail, account, self._this_month_ym
            )
        )
        task_fetch_cost = asyncio.create_task(
            self._async_fetch(
                self._client.get_month_daily_cost_detail, account, self._this_month_ym
            )
        )

        results = await asyncio.gather(task_fetch_usage, task_fetch_cost)

        (success_usage, result_usage), (success_cost, result_cost) = results

        if success_usage:
            this_month_kwh_from_usage, this_month_by_day_from_usage = result_usage
        else:
            this_month_kwh_from_usage = STATE_UNAVAILABLE
            this_month_by_day_from_usage = STATE_UNAVAILABLE

        if success_cost:
            (
                this_month_cost,
                this_month_kwh_from_cost,
                ladder,
                this_month_by_day_from_cost,
            ) = result_cost
            # special processing
            if this_month_cost is None:
                this_month_cost = STATE_UNAVAILABLE
            if this_month_kwh_from_cost is None:
                this_month_kwh_from_cost = STATE_UNAVAILABLE
            ladder_stage = (
                ladder[WF_ATTR_LADDER]
                if ladder[WF_ATTR_LADDER] is not None
                else STATE_UNAVAILABLE
            )
            ladder_remaining_kwh = (
                ladder[WF_ATTR_LADDER_REMAINING_KWH]
                if ladder[WF_ATTR_LADDER_REMAINING_KWH] is not None
                else STATE_UNAVAILABLE
            )
            ladder_tariff = (
                ladder[WF_ATTR_LADDER_TARIFF]
                if ladder[WF_ATTR_LADDER_TARIFF] is not None
                else STATE_UNAVAILABLE
            )
            ladder_start_date = (
                ladder[WF_ATTR_LADDER_START_DATE]
                if ladder[WF_ATTR_LADDER_START_DATE] is not None
                else STATE_UNAVAILABLE
            )
        else:
            (
                this_month_cost,
                this_month_kwh_from_cost,
                this_month_by_day_from_cost,
                ladder_stage,
                ladder_remaining_kwh,
                ladder_tariff,
                ladder_start_date,
            ) = (
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
            )
        this_month_by_day, this_month_kwh = self.merge_by_day_data(
            by_day_from_usage=this_month_by_day_from_usage,
            kwh_from_usage=this_month_kwh_from_usage,
            by_day_from_cost=this_month_by_day_from_cost,
            kwh_from_cost=this_month_kwh_from_cost,
        )

        if this_month_by_day == STATE_UNAVAILABLE:
            # need last month's data to update `latest_day` entity
            self._if_update_last_month = True

        self._gathered_data[account.account_number][
            SUFFIX_THIS_MONTH_KWH
        ] = this_month_kwh
        self._gathered_data[account.account_number][
            SUFFIX_THIS_MONTH_COST
        ] = this_month_cost
        self._gathered_data[account.account_number][ATTR_KEY_THIS_MONTH_BY_DAY] = {
            ATTR_KEY_THIS_MONTH_BY_DAY: this_month_by_day
        }
        self._gathered_data[account.account_number][
            SUFFIX_CURRENT_LADDER
        ] = ladder_stage
        self._gathered_data[account.account_number][
            SUFFIX_CURRENT_LADDER_REMAINING_KWH
        ] = ladder_remaining_kwh
        self._gathered_data[account.account_number][
            SUFFIX_CURRENT_LADDER_TARIFF
        ] = ladder_tariff
        self._gathered_data[account.account_number][
            ATTR_KEY_CURRENT_LADDER_START_DATE
        ] = {ATTR_KEY_CURRENT_LADDER_START_DATE: ladder_start_date}

    async def _async_update_last_month_stats(self, account: CSGElectricityAccount):
        """Update last month's usage and cost"""
        if not self._if_update_last_month:
            # original condition, don't need to update last month's data

            # wait for this month's data to be updated to see if last month's data is needed
            try:
                await asyncio.wait_for(
                    self._this_month_update_completed_flag.wait(),
                    timeout=SETTING_UPDATE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                _LOGGER.warning(
                    "Ele account %s: timeout waiting for this month's update, "
                    "skipping last month's update",
                    account.account_number,
                )
                self._gathered_data[account.account_number][
                    SUFFIX_LAST_MONTH_KWH
                ] = STATE_UPDATE_UNCHANGED
                self._gathered_data[account.account_number][
                    SUFFIX_LAST_MONTH_COST
                ] = STATE_UPDATE_UNCHANGED
                self._gathered_data[account.account_number][
                    ATTR_KEY_LAST_MONTH_BY_DAY
                ] = {ATTR_KEY_LAST_MONTH_BY_DAY: STATE_UPDATE_UNCHANGED}
                return

            if not self._if_update_last_month:
                # don't need last month's data for latest day
                _LOGGER.debug(
                    "Last month's data for account %s: no need to update",
                    account.account_number,
                )
                self._gathered_data[account.account_number][
                    SUFFIX_LAST_MONTH_KWH
                ] = STATE_UPDATE_UNCHANGED
                self._gathered_data[account.account_number][
                    SUFFIX_LAST_MONTH_COST
                ] = STATE_UPDATE_UNCHANGED
                self._gathered_data[account.account_number][
                    ATTR_KEY_LAST_MONTH_BY_DAY
                ] = {ATTR_KEY_LAST_MONTH_BY_DAY: STATE_UPDATE_UNCHANGED}
                return

        # continue to update last month's data
        # fetch usage and cost in parallel
        task_fetch_usage = asyncio.create_task(
            self._async_fetch(
                self._client.get_month_daily_usage_detail, account, self._last_month_ym
            )
        )
        task_fetch_cost = asyncio.create_task(
            self._async_fetch(
                self._client.get_month_daily_cost_detail, account, self._last_month_ym
            )
        )

        results = await asyncio.gather(task_fetch_usage, task_fetch_cost)

        (success_usage, result_usage), (success_cost, result_cost) = results

        if success_usage:
            last_month_kwh_from_usage, last_month_by_day_from_usage = result_usage
        else:
            last_month_kwh_from_usage = STATE_UNAVAILABLE
            last_month_by_day_from_usage = STATE_UNAVAILABLE

        if success_cost:
            (
                last_month_cost,
                last_month_kwh_from_cost,
                _,  # ladder is discarded
                last_month_by_day_from_cost,
            ) = result_cost

            # for last month, it's safe to calculate total kwh from cost
            if not last_month_cost:
                last_month_cost = sum(
                    d[WF_ATTR_CHARGE] for d in last_month_by_day_from_cost
                )
            if not last_month_kwh_from_cost:
                last_month_kwh_from_cost = sum(
                    d[WF_ATTR_KWH] for d in last_month_by_day_from_cost
                )
        else:
            (
                last_month_cost,
                last_month_kwh_from_cost,
                last_month_by_day_from_cost,
            ) = (
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
            )
        last_month_by_day, last_month_kwh = self.merge_by_day_data(
            by_day_from_usage=last_month_by_day_from_usage,
            kwh_from_usage=last_month_kwh_from_usage,
            by_day_from_cost=last_month_by_day_from_cost,
            kwh_from_cost=last_month_kwh_from_cost,
        )

        self._gathered_data[account.account_number][
            SUFFIX_LAST_MONTH_KWH
        ] = last_month_kwh
        self._gathered_data[account.account_number][
            SUFFIX_LAST_MONTH_COST
        ] = last_month_cost
        self._gathered_data[account.account_number][ATTR_KEY_LAST_MONTH_BY_DAY] = {
            ATTR_KEY_LAST_MONTH_BY_DAY: last_month_by_day
        }

    def _update_latest_day(self, account: CSGElectricityAccount):
        this_month_by_day = self._gathered_data[account.account_number][
            ATTR_KEY_THIS_MONTH_BY_DAY
        ][ATTR_KEY_THIS_MONTH_BY_DAY]
        last_month_by_day = self._gathered_data[account.account_number][
            ATTR_KEY_LAST_MONTH_BY_DAY
        ][ATTR_KEY_LAST_MONTH_BY_DAY]

        if (
            this_month_by_day == STATE_UNAVAILABLE
            and last_month_by_day == STATE_UNAVAILABLE
        ):
            latest_day_kwh = STATE_UNAVAILABLE
            latest_day_cost = STATE_UNAVAILABLE
            latest_day_date = STATE_UNAVAILABLE
        else:
            if this_month_by_day != STATE_UNAVAILABLE and len(this_month_by_day) >= 1:
                # we have this month's data, use the latest day
                latest_day_kwh = this_month_by_day[-1][WF_ATTR_KWH]
                latest_day_cost = this_month_by_day[-1].get(WF_ATTR_CHARGE)
                if latest_day_cost is None:
                    latest_day_cost = STATE_UNAVAILABLE
                latest_day_date = this_month_by_day[-1][WF_ATTR_DATE]
            else:
                # this month isn't available yet (typically during the first 3 days)
                # let's try last month
                if (
                    last_month_by_day
                    not in [
                        STATE_UNAVAILABLE,
                        STATE_UPDATE_UNCHANGED,
                    ]
                    and len(last_month_by_day) >= 1
                ):
                    latest_day_kwh = last_month_by_day[-1][WF_ATTR_KWH]
                    latest_day_cost = STATE_UNAVAILABLE
                    latest_day_date = last_month_by_day[-1][WF_ATTR_DATE]
                else:
                    _LOGGER.error(
                        "Ele account %s, no latest day data available",
                        account.account_number,
                    )
                    latest_day_kwh = STATE_UNAVAILABLE
                    latest_day_cost = STATE_UNAVAILABLE
                    latest_day_date = STATE_UNAVAILABLE
        self._gathered_data[account.account_number][
            SUFFIX_LATEST_DAY_KWH
        ] = latest_day_kwh
        self._gathered_data[account.account_number][
            SUFFIX_LATEST_DAY_COST
        ] = latest_day_cost
        self._gathered_data[account.account_number][ATTR_KEY_LATEST_DAY_DATE] = {
            ATTR_KEY_LATEST_DAY_DATE: latest_day_date
        }

    def _update_states(self):
        current_dt = datetime.datetime.now()
        this_year, this_month, this_day = (
            current_dt.year,
            current_dt.month,
            current_dt.day,
        )
        last_year, last_month = this_year - 1, this_month - 1
        if last_month == 0:
            last_month_ym = (last_year, 12)
        else:
            last_month_ym = (this_year, last_month)
        self._this_day = this_day
        self._this_year = this_year
        self._this_month_ym = (this_year, this_month)
        self._last_year = last_year
        self._last_month_ym = last_month_ym

        # for last month and last year data, they won't change over a long period of time
        # so we could use cache
        #
        # update policy for last month:
        # for the first <LAST_MONTH_UPDATE_DAY_THRESHOLD> days of a month,
        # update every `update_interval`.
        # for the rest of the time, do not update.

        # update policy for last year:
        # for the first <LAST_YEAR_UPDATE_DAY_THRESHOLD> days of Jan, update daily at first update
        # for the rest of the time, do not update
        #
        # when integration is reloaded, all updates will be triggered
        # so user could just reload the integration to refresh the data if needed

        if (
            self.hass.data[DOMAIN][self._config_entry_id].get(DATA_KEY_LAST_UPDATE_DAY)
            is None
        ):
            # first update
            update_last_month = True
            update_last_year = True
            _LOGGER.debug(
                "First update for account %s, getting all past data",
                self._config[CONF_USERNAME],
            )
        else:
            update_last_month = False
            update_last_year = False

            if this_day <= SETTING_LAST_MONTH_UPDATE_DAY_THRESHOLD:
                update_last_month = True
            today_first_update_triggered = (
                self.hass.data[DOMAIN][self._config_entry_id][DATA_KEY_LAST_UPDATE_DAY]
                == this_day
            )
            if this_month == 1 and this_day <= SETTING_LAST_YEAR_UPDATE_DAY_THRESHOLD:
                if not today_first_update_triggered:
                    update_last_year = True
        self._if_update_last_month = update_last_month
        self._if_update_last_year = update_last_year

    async def _async_update_account_data(self, account: CSGElectricityAccount):
        start_time = time.time()
        # TODO use asyncio.TaskGroup() in 3.11

        # async with asyncio.TaskGroup() as task_group:
        #     task_group.create_task(self._async_update_bal_arr(account))
        #     task_group.create_task(self._async_update_yesterday_kwh(account))
        #     task_group.create_task(self._async_update_this_year_stats(account))
        #     task_group.create_task(self._async_update_last_year_stats(account))
        #     task_group.create_task(
        #         self._async_update_this_month_stats_and_ladder(account)
        #     )
        #     task_group.create_task(self._async_update_last_month_stats(account))
        await asyncio.gather(
            self._async_update_bal_arr(account),
            self._async_update_account_info(account),
            self._async_update_yesterday_kwh(account),
            self._async_update_this_year_stats(account),
            self._async_update_last_year_stats(account),
            self._async_update_this_month_stats_and_ladder(account),
            self._async_update_last_month_stats(account),
            self._async_update_bill_history(account),
            self._async_update_bill_details(account),
            self._async_update_this_month_temperature(account),
            self._async_update_payment_history(account),
            self._async_update_outage_notice(account),
            self._async_update_outage_event(account),
            return_exceptions=True,
        )
        try:
            self._update_latest_day(account)
            self._merge_temperature_into_month_by_day(account)
            self._merge_payment_history_into_account_info(account)
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.error(
                "Ele account %s, update latest day data failed: %s",
                account.account_number,
                exc,
            )

        _LOGGER.debug(
            "Ele account %s, update took %s seconds",
            account.account_number,
            time.time() - start_time,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from API endpoint.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """
        self.update_interval = timedelta(
            seconds=self._config[CONF_SETTINGS][CONF_UPDATE_INTERVAL]
        )
        self._update_states()
        # _LOGGER.debug("Coordinator update interval: %d", self.update_interval.seconds)
        _LOGGER.debug("Coordinator update started")
        start_time = time.time()

        metering_point_data = {}
        config_entry_need_update = False
        await self._async_refresh_client()
        new_config = self._config.copy()

        # detect which features the region supports (daily charge / ladder),
        # cache the result in the config entry and remove unsupported entities
        features = self._config.get(CONF_FEATURES)
        if features is None and self._config[CONF_ELE_ACCOUNTS]:
            first_account_data = next(iter(self._config[CONF_ELE_ACCOUNTS].values()))
            first_account = CSGElectricityAccount.load(first_account_data)
            try:
                features = await self.hass.async_add_executor_job(
                    self._client.probe_supported_features, first_account
                )
            except NotLoggedIn:
                raise ConfigEntryAuthFailed("Login expired")
            except Exception:  # pylint: disable=broad-except
                _LOGGER.warning(
                    "Failed to probe supported features, keeping all sensors",
                    exc_info=True,
                )
                features = None
            if features is not None:
                new_config[CONF_FEATURES] = features
                config_entry_need_update = True

        for account_number, account_data in self._config[CONF_ELE_ACCOUNTS].items():
            self._gathered_data[account_number] = {}
            account = CSGElectricityAccount.load(account_data)
            # handling the addition of metering point number
            if not account.metering_point_number:
                if not metering_point_data:
                    ok, data = await self._async_fetch(
                        self._client.api_get_metering_point,
                        account.area_code,
                        account.ele_customer_id,
                    )
                    if ok:
                        metering_point_data = data
                if metering_point_data:
                    for mp in metering_point_data:
                        if mp["eleCustNumber"] == account.account_number:
                            config_entry_need_update = True
                            account.metering_point_number = mp[
                                JSON_KEY_METERING_POINT_NUMBER
                            ]
                            new_config[CONF_ELE_ACCOUNTS][
                                account_number
                            ] = account.dump()
                            break

            await self._async_update_account_data(account)

        if features is not None:
            self._remove_unsupported_entities(features)
        if config_entry_need_update:
            new_config[CONF_UPDATED_AT] = str(int(time.time() * 1000))
            self.hass.config_entries.async_update_entry(
                self.hass.config_entries.async_get_entry(self._config_entry_id),
                data=new_config,
            )
            _LOGGER.debug("Updated accounts with metering point number")
        _LOGGER.debug("Coordinator update took %s seconds", time.time() - start_time)
        self.hass.data[DOMAIN][self._config_entry_id][
            DATA_KEY_LAST_UPDATE_DAY
        ] = self._this_day
        return self._gathered_data

    def _remove_unsupported_entities(self, features: dict) -> None:
        """Remove entities that the account's region does not support"""
        ladder_suffixes = {
            SUFFIX_CURRENT_LADDER,
            SUFFIX_CURRENT_LADDER_REMAINING_KWH,
            SUFFIX_CURRENT_LADDER_TARIFF,
        }
        daily_cost_suffixes = {SUFFIX_LATEST_DAY_COST, SUFFIX_THIS_MONTH_COST}

        def unsupported(suffix: str) -> bool:
            if suffix in ladder_suffixes and not features.get(CONF_HAS_LADDER, True):
                return True
            if (
                suffix in daily_cost_suffixes
                and not features.get(CONF_HAS_DAILY_CHARGE, True)
            ):
                return True
            return False

        entity_reg = entity_registry.async_get(self.hass)
        removed = False
        for ent in entity_registry.async_entries_for_config_entry(
            entity_reg, self._config_entry_id
        ):
            if not ent.unique_id or not ent.unique_id.startswith(f"{DOMAIN}."):
                continue
            suffix = ent.unique_id.rsplit(".", 1)[-1]
            if unsupported(suffix):
                entity_reg.async_remove(ent.entity_id)
                removed = True
                _LOGGER.info(
                    "Removed entity %s because the region does not support it",
                    ent.entity_id,
                )
        if removed:
            _LOGGER.debug(
                "Removed unsupported entities for entry %s", self._config_entry_id
            )
