"""ConfigFlow, OptionsFlow și ReconfigureFlow pentru CNAIR eRovinieta.

Flow-uri disponibile:
- ConfigFlow (async_step_user): configurare inițială cu credentiale
- ReauthFlow (async_step_reauth): re-autentificare automată (HA trigger)
- ReconfigureFlow (async_step_reconfigure): schimbare parolă (din meniu)
- OptionsFlow (async_step_init): interval actualizare + istoric tranzacții
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import ErovinietaAPI
from .const import (
    CONF_ISTORIC_TRANZACTII,
    CONF_LICENSE_KEY,
    CONF_PASSWORD,
    CONF_UPDATE_INTERVAL,
    CONF_USERNAME,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    ISTORIC_TRANZACTII_DEFAULT,
    LICENSE_DATA_KEY,
    LICENSE_PURCHASE_URL,
    MAX_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
)
from .exceptions import ErovinietaAuthError, ErovinietaConnectionError

_LOGGER = logging.getLogger(__name__)

# ------------------------------------------------------------------
#  Scheme reutilizabile (selectoare HA moderne)
# ------------------------------------------------------------------

SELECTOR_USERNAME = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))
SELECTOR_PASSWORD = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD)
)
SELECTOR_INTERVAL = NumberSelector(
    NumberSelectorConfig(
        min=MIN_UPDATE_INTERVAL,
        max=MAX_UPDATE_INTERVAL,
        step=60,
        unit_of_measurement="secunde",
        mode=NumberSelectorMode.BOX,
    )
)
SELECTOR_ISTORIC = NumberSelector(
    NumberSelectorConfig(
        min=1,
        max=10,
        step=1,
        unit_of_measurement="ani",
        mode=NumberSelectorMode.SLIDER,
    )
)


# =====================================================================
#  ConfigFlow
# =====================================================================


class ErovinietaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """ConfigFlow pentru configurarea integrării eRovinieta."""

    VERSION = 1

    def __init__(self) -> None:
        """Inițializare."""
        self._reauth_entry: ConfigEntry | None = None

    # ------------------------------------------------------------------
    #  Configurare inițială
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configurare inițială — credentiale + setări."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # NumberSelector returnează float — convertim la int
            update_interval = int(
                user_input.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
            )
            istoric = int(
                user_input.get(CONF_ISTORIC_TRANZACTII, ISTORIC_TRANZACTII_DEFAULT)
            )

            # Testăm credentialele
            errors = await self._test_credentials(
                user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
            )

            if not errors:
                # Prevenim duplicate pe același username
                await self.async_set_unique_id(user_input[CONF_USERNAME])
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=f"CNAIR eRovinieta ({user_input[CONF_USERNAME]})",
                    data={
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                    options={
                        CONF_UPDATE_INTERVAL: update_interval,
                        CONF_ISTORIC_TRANZACTII: istoric,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): SELECTOR_USERNAME,
                vol.Required(CONF_PASSWORD): SELECTOR_PASSWORD,
                vol.Required(
                    CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL
                ): SELECTOR_INTERVAL,
                vol.Required(
                    CONF_ISTORIC_TRANZACTII, default=ISTORIC_TRANZACTII_DEFAULT
                ): SELECTOR_ISTORIC,
            }
        )

        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    # ------------------------------------------------------------------
    #  Re-autentificare (declanșată automat de HA la ConfigEntryAuthFailed)
    # ------------------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Punct de intrare pentru re-autentificare."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Formular re-autentificare — doar parola."""
        errors: dict[str, str] = {}

        if user_input is not None and self._reauth_entry is not None:
            username = self._reauth_entry.data[CONF_USERNAME]
            errors = await self._test_credentials(
                username, user_input[CONF_PASSWORD]
            )

            if not errors:
                self.hass.config_entries.async_update_entry(
                    self._reauth_entry,
                    data={
                        **self._reauth_entry.data,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
                await self.hass.config_entries.async_reload(
                    self._reauth_entry.entry_id
                )
                return self.async_abort(reason="reauth_successful")

        schema = vol.Schema(
            {vol.Required(CONF_PASSWORD): SELECTOR_PASSWORD}
        )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "username": (
                    self._reauth_entry.data.get(CONF_USERNAME, "")
                    if self._reauth_entry
                    else ""
                )
            },
        )

    # ------------------------------------------------------------------
    #  Reconfigurare (declanșată manual de utilizator din meniu)
    # ------------------------------------------------------------------

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Permite schimbarea parolei din meniul integrării."""
        entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        errors: dict[str, str] = {}

        if user_input is not None and entry is not None:
            username = entry.data[CONF_USERNAME]
            errors = await self._test_credentials(
                username, user_input[CONF_PASSWORD]
            )

            if not errors:
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **entry.data,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reconfigure_successful")

        schema = vol.Schema(
            {vol.Required(CONF_PASSWORD): SELECTOR_PASSWORD}
        )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "username": (
                    entry.data.get(CONF_USERNAME, "") if entry else ""
                )
            },
        )

    # ------------------------------------------------------------------
    #  Helper: testare credentiale
    # ------------------------------------------------------------------

    async def _test_credentials(
        self, username: str, password: str
    ) -> dict[str, str]:
        """Testează credentialele fără a păstra sesiunea."""
        errors: dict[str, str] = {}
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        )
        try:
            api = ErovinietaAPI(session, username, password)
            await api.authenticate()
        except ErovinietaAuthError:
            errors["base"] = "authentication_failed"
        except ErovinietaConnectionError:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Eroare neașteptată la testarea credentialelor")
            errors["base"] = "unknown"
        finally:
            await session.close()
        return errors

    # ------------------------------------------------------------------
    #  Options flow
    # ------------------------------------------------------------------

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Returnează OptionsFlow-ul pentru integrare."""
        return ErovinietaOptionsFlow()


# =====================================================================
#  OptionsFlow
# =====================================================================


class ErovinietaOptionsFlow(config_entries.OptionsFlow):
    """OptionsFlow pentru setările integrării eRovinieta.

    self.config_entry este injectat automat de Home Assistant.
    Meniu principal cu două opțiuni: Setări și Licență.
    """

    # ─────────────────────────────────────────
    # Meniu principal
    # ─────────────────────────────────────────
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Afișează meniul principal."""
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "settings",
                "licenta",
            ],
        )

    # ─────────────────────────────────────────
    # Setări (interval + istoric)
    # ─────────────────────────────────────────
    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configurare interval actualizare și istoric tranzacții."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # NumberSelector returnează float — convertim la int
            update_interval = int(
                user_input.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
            )
            istoric = int(
                user_input.get(
                    CONF_ISTORIC_TRANZACTII, ISTORIC_TRANZACTII_DEFAULT
                )
            )

            if (
                update_interval < MIN_UPDATE_INTERVAL
                or update_interval > MAX_UPDATE_INTERVAL
            ):
                errors[CONF_UPDATE_INTERVAL] = "invalid_update_interval"

            if not errors:
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_UPDATE_INTERVAL: update_interval,
                        CONF_ISTORIC_TRANZACTII: istoric,
                    },
                )

        # vol.Required + default = câmp cu valoare pre-completată, FĂRĂ checkbox
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_UPDATE_INTERVAL,
                    default=self.config_entry.options.get(
                        CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
                    ),
                ): SELECTOR_INTERVAL,
                vol.Required(
                    CONF_ISTORIC_TRANZACTII,
                    default=self.config_entry.options.get(
                        CONF_ISTORIC_TRANZACTII, ISTORIC_TRANZACTII_DEFAULT
                    ),
                ): SELECTOR_ISTORIC,
            }
        )

        return self.async_show_form(
            step_id="settings", data_schema=schema, errors=errors
        )

    # ─────────────────────────────────────────
    # Licențiere
    # ─────────────────────────────────────────
    async def async_step_licenta(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Formular pentru activarea / vizualizarea licenței eRovinieta."""
        from .license import LicenseManager

        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] = {}

        # Obține LicenseManager
        mgr: LicenseManager | None = self.hass.data.get(DOMAIN, {}).get(
            LICENSE_DATA_KEY
        )
        if mgr is None:
            mgr = LicenseManager(self.hass)
            await mgr.async_load()

        # Detect language
        is_ro = self.hass.config.language == "ro"

        # Informații pentru descrierea formularului
        server_status = mgr.status  # 'licensed', 'trial', 'expired', 'unlicensed'

        if server_status == "licensed":
            from datetime import datetime

            tip = mgr.license_type or "necunoscut"
            status_lines = [f"✅ Licență activă ({tip})"]

            if mgr.license_key_masked:
                status_lines[0] += f" — {mgr.license_key_masked}"

            # Data activării
            if mgr.activated_at:
                act_date = datetime.fromtimestamp(
                    mgr.activated_at
                ).strftime("%d.%m.%Y %H:%M")
                status_lines.append(f"Activată la: {act_date}")

            # Data expirării
            if mgr.license_expires_at:
                exp_date = datetime.fromtimestamp(
                    mgr.license_expires_at
                ).strftime("%d.%m.%Y %H:%M")
                status_lines.append(f"📅 Expiră la: {exp_date}")
            elif tip == "perpetual":
                status_lines.append("Valabilitate: nelimitată (perpetuă)")

            description_placeholders["license_status"] = "\n".join(
                status_lines
            )

        elif server_status == "trial":
            days = mgr.trial_days_remaining
            if is_ro:
                status_lines = [
                    f"⏳ Evaluare — {days} zile rămase",
                    "",
                    f"🛒 Obține licență: {LICENSE_PURCHASE_URL}",
                ]
            else:
                status_lines = [
                    f"⏳ Trial — {days} days remaining",
                    "",
                    f"🛒 Get a license: {LICENSE_PURCHASE_URL}",
                ]
            description_placeholders["license_status"] = "\n".join(status_lines)
        elif server_status == "expired":
            from datetime import datetime

            status_lines = []
            if is_ro:
                status_lines.append("❌ Licență expirată")
            else:
                status_lines.append("❌ License expired")

            if mgr.activated_at:
                act_date = datetime.fromtimestamp(
                    mgr.activated_at
                ).strftime("%d.%m.%Y")
                if is_ro:
                    status_lines.append(f"Activată la: {act_date}")
                else:
                    status_lines.append(f"Activated on: {act_date}")
            if mgr.license_expires_at:
                exp_date = datetime.fromtimestamp(
                    mgr.license_expires_at
                ).strftime("%d.%m.%Y")
                if is_ro:
                    status_lines.append(f"Expirată la: {exp_date}")
                else:
                    status_lines.append(f"Expired on: {exp_date}")

            status_lines.append("")
            if is_ro:
                status_lines.append(f"🛒 Obține licență: {LICENSE_PURCHASE_URL}")
            else:
                status_lines.append(f"🛒 Get a license: {LICENSE_PURCHASE_URL}")

            description_placeholders["license_status"] = "\n".join(
                status_lines
            )
        else:
            if is_ro:
                status_lines = [
                    "❌ Fără licență — funcționalitate blocată",
                    "",
                    f"🛒 Obține licență: {LICENSE_PURCHASE_URL}",
                ]
            else:
                status_lines = [
                    "❌ No license — functionality blocked",
                    "",
                    f"🛒 Get a license: {LICENSE_PURCHASE_URL}",
                ]
            description_placeholders["license_status"] = "\n".join(status_lines)

        if user_input is not None:
            cheie = user_input.get(CONF_LICENSE_KEY, "").strip()

            if not cheie:
                errors["base"] = "license_key_empty"
            elif len(cheie) < 10:
                errors["base"] = "license_key_invalid"
            else:
                # Activare prin API
                result = await mgr.async_activate(cheie)

                if result.get("success"):
                    # Notificare de succes
                    from homeassistant.components import (
                        persistent_notification,
                    )

                    _LICENSE_TYPE_RO = {
                        "monthly": "lunară",
                        "yearly": "anuală",
                        "perpetual": "perpetuă",
                        "trial": "evaluare",
                    }
                    tip_ro = _LICENSE_TYPE_RO.get(
                        mgr.license_type, mgr.license_type or "necunoscut"
                    )

                    persistent_notification.async_create(
                        self.hass,
                        f"Licența eRovinieta a fost activată cu succes! "
                        f"Tip: {tip_ro}.",
                        title="Licență activată",
                        notification_id="erovinieta_license_activated",
                    )
                    return self.async_create_entry(
                        data=self.config_entry.options
                    )

                # Mapare erori API
                api_error = result.get("error", "unknown_error")
                error_map = {
                    "invalid_key": "license_key_invalid",
                    "already_used": "license_already_used",
                    "expired_key": "license_key_expired",
                    "fingerprint_mismatch": "license_fingerprint_mismatch",
                    "invalid_signature": "license_server_error",
                    "network_error": "license_network_error",
                    "server_error": "license_server_error",
                }
                errors["base"] = error_map.get(api_error, "license_server_error")

        schema = vol.Schema(
            {
                vol.Optional(CONF_LICENSE_KEY): TextSelector(
                    TextSelectorConfig(
                        type=TextSelectorType.TEXT,
                        suffix="EROV-XXXX-XXXX-XXXX-XXXX",
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="licenta",
            data_schema=schema,
            errors=errors,
            description_placeholders=description_placeholders,
        )
