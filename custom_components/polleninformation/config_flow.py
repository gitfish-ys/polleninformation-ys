""" custom_components/polleninformation/config_flow.py """
"""Config flow for polleninformation.at integration."""

import json
import logging
import os

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import SOURCE_USER

from .api import async_get_pollenat_data
from .const import DEFAULT_LANG, DEFAULT_LANG_ID, DOMAIN
from .utils import (extract_place_slug, find_best_lang_key_for_locale,
                    get_language_options, load_available_languages, slugify,
                    split_location)

_LOGGER = logging.getLogger(__name__)
DEBUG = True

AVAILABLE_COUNTRIES_FILE = os.path.join(
    os.path.dirname(__file__), "available_countries.json"
)


async def async_load_available_countries(hass):
    def _load_sync():
        with open(AVAILABLE_COUNTRIES_FILE, encoding="utf-8") as f:
            return json.load(f)["countries"]

    return await hass.async_add_executor_job(_load_sync)


async def async_get_country_code_from_latlon(hass, lat, lon):
    url = f"https://nominatim.openstreetmap.org/reverse"
    params = {
        "lat": lat,
        "lon": lon,
        "format": "json",
        "zoom": 3,  # Country-level
        "addressdetails": 1,
    }
    headers = {"User-Agent": "Home Assistant Polleninformation Integration"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, headers=headers, timeout=5) as resp:
            if resp.status == 200:
                result = await resp.json()
                return result.get("address", {}).get("country_code", "").upper()
    return None


async def async_get_country_options(hass):
    countries = await async_load_available_countries(hass)
    return {
        c["code"]: c["name"] for c in sorted(countries, key=lambda c: c["name"].lower())
    }


class PolleninformationConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}

        country_options = await async_get_country_options(self.hass)
        default_lat = round(self.hass.config.latitude, 5)
        default_lon = round(self.hass.config.longitude, 5)

        # Försök hämta land från HA-konfig
        ha_country = getattr(self.hass.config, "country", None)
        default_country = None

        if ha_country and ha_country in country_options:
            default_country = ha_country
        else:
            country_code = await async_get_country_code_from_latlon(
                self.hass, default_lat, default_lon
            )
            if country_code and country_code in country_options:
                default_country = country_code
            elif "SE" in country_options:
                default_country = "SE"
            else:
                default_country = next(iter(country_options))

        # Hämta HA:s språk (locale/language)
        ha_locale = getattr(self.hass.config, "language", None)
        if not ha_locale and hasattr(self.hass, "locale"):
            ha_locale = getattr(self.hass.locale, "language", None)
        if not ha_locale:
            ha_locale = "en"  # fallback till engelska

        lang_options = get_language_options()
        default_lang_key = find_best_lang_key_for_locale(ha_locale)
        # Om default_lang_key inte finns, fallback till engelska ("1") eller första i listan
        if default_lang_key not in lang_options:
            default_lang_key = "1" if "1" in lang_options else next(iter(lang_options.keys()))

        if user_input is not None:
            country_code = user_input.get("country")
            lang_key = user_input.get("language")
            try:
                latitude = float(user_input.get("latitude"))
                longitude = float(user_input.get("longitude"))
            except Exception:
                errors["latitude"] = "invalid_latitude"
                errors["longitude"] = "invalid_longitude"
                latitude = longitude = None

            if country_code not in country_options:
                errors["country"] = "invalid_country"
            if lang_key not in lang_options:
                errors["language"] = "invalid_language"

            if not errors:
                countries = await async_load_available_countries(self.hass)
                countries_by_code = {c["code"]: c for c in countries}
                country_obj = countries_by_code.get(country_code)
                country_id = country_obj.get("country_id")
                if isinstance(country_id, list):
                    country_id = country_id[0]
                elif not isinstance(country_id, int):
                    errors["country"] = "invalid_country"
                    country_id = None

                langs = load_available_languages()
                selected_lang = next((l for l in langs if l["key"] == lang_key), None)
                if not selected_lang:
                    errors["language"] = "invalid_language"

                if not errors:
                    _LOGGER.debug(
                        "Kallar async_get_pollenat_data med: lat=%r, lon=%r, country=%r, country_id=%r, lang=%r, lang_id=%r",
                        latitude,
                        longitude,
                        country_code,
                        country_id,
                        selected_lang["lang_code"],
                        lang_key,
                    )

                    pollen_data = await async_get_pollenat_data(
                        self.hass,
                        latitude,
                        longitude,
                        country_code,
                        country_id,
                        selected_lang["lang_code"],
                        lang_key,
                    )

                    _LOGGER.debug("API-svar: %r", pollen_data)

                    if not pollen_data:
                        _LOGGER.debug("Inget pollen_data mottaget")
                    elif not pollen_data.get("contamination"):
                        _LOGGER.debug("Ingen 'contamination'-lista: %r", pollen_data)

                    result = None
                    if pollen_data and pollen_data.get("result"):
                        result = pollen_data["result"]
                    if not result or not result.get("contamination"):
                        _LOGGER.debug("Ingen 'contamination'-lista på rätt nivå: %r", pollen_data)
                    else:
                        contamination = result.get("contamination")
                        if not contamination:
                            errors["country"] = "no_sensors_for_country"
                        else:
                            location_title = result.get("locationtitle", country_options[country_code])
                            _zip, city = split_location(location_title)
                            entry_title = city if city else location_title
                            entry_data = {
                                "country": country_code,
                                "country_id": country_id,
                                "latitude": latitude,
                                "longitude": longitude,
                                "lang": selected_lang["lang_code"],
                                "lang_id": lang_key,
                            }
                            existing_entries = self._async_current_entries()
                            already_exists = any(
                                e.data.get("country") == country_code
                                and round(e.data.get("latitude", 0), 3) == round(latitude, 3)
                                and round(e.data.get("longitude", 0), 3) == round(longitude, 3)
                                for e in existing_entries
                            )
                            if already_exists:
                                return self.async_abort(reason="already_configured")
                            if DEBUG:
                                _LOGGER.debug(
                                    "Skapar polleninformation-entry med data: %s och title: %s",
                                    entry_data,
                                    entry_title,
                                )
                            return self.async_create_entry(
                                title=entry_title,
                                data=entry_data,
                            )

        data_schema = vol.Schema(
            {
                vol.Required("country", default=default_country): vol.In(country_options),
                vol.Required("latitude", default=default_lat): float,
                vol.Required("longitude", default=default_lon): float,
                vol.Required("language", default=default_lang_key): vol.In(lang_options),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
        )
