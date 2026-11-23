# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared sending of the air conditioner's `settings` command.

Both the climate entity (mode/temp/fan/swing) and the AC switches modify the SAME
the `settings` command, which on send transmits ALL of its parameters. So every
send must apply the same sanitization of
`windDirectionVertical`/`windDirectionHorizontal`: the device may report them as 0
(a value not allowed by the enumValues) and the API would reject the command.
Centralizing this here avoids divergences between climate.py and switch.py.
"""
from __future__ import annotations

import logging
import weakref

from .hon_commands import async_send_command

_LOGGER = logging.getLogger(__name__)

# Wind-direction parameters that may be 0 (device off): to be sanitized.
AC_WIND_DIR_PARAMS = ("windDirectionVertical", "windDirectionHorizontal")
AC_SWING_V_PARAM = "windDirectionVertical"
AC_SWING_V_ON = "8"  # 8 = vertical oscillation


def settings_param(appliance, name):
    """Return the `name` parameter of the `settings` command, or None if absent."""
    commands = getattr(appliance, "commands", None)
    commands = commands if isinstance(commands, dict) else {}
    settings = commands.get("settings")
    params = getattr(settings, "parameters", None) if settings is not None else None
    if isinstance(params, dict):
        return params.get(name)
    return None


# Self-clean flags. On the settings write path these must be 0 whenever HA starts or
# resumes an operating mode: the Haier app's startProgram payload always carries
# selfCleaningStatus=0 for a normal mode, and on single-split (1to1) installs the
# fixed-0 programRule is inert, so a mode command could otherwise ship machMode together
# with a cached selfCleaningStatus=1 and the unit runs the fan with the compressor off.
AC_SELF_CLEAN_PARAMS = ("selfCleaningStatus", "selfCleaning56Status")


def with_self_clean_off(appliance, params: dict) -> dict:
    """Return a copy of `params` with the self-clean flags forced to '0', for the flags
    the device exposes on its settings command. Capability-gated (an absent flag is not
    injected). setdefault so a caller that set the flag itself is never overridden."""
    out = dict(params)
    for name in AC_SELF_CLEAN_PARAMS:
        if settings_param(appliance, name) is not None:
            out.setdefault(name, "0")
    return out


def param_allowed_values(param) -> list[str]:
    """Allowed values (as strings) of an enum parameter, or [] if not an enum."""
    values = getattr(param, "values", None)
    if not isinstance(values, list):
        return []
    return [str(v) for v in values]


def fixed_vertical_value(allowed: list[str]) -> str:
    """FIXED (non-swing) vertical position among the allowed ones; never 0."""
    fixed = [v for v in allowed if v != AC_SWING_V_ON]
    if "2" in fixed:
        return "2"
    return fixed[0] if fixed else AC_SWING_V_ON


def sanitize_wind_direction(command_params: dict) -> None:
    """Reset windDirectionVertical/Horizontal to an allowed value if the current
    one is not (e.g. 0 when off). Does not touch already-valid parameters."""
    for key in AC_WIND_DIR_PARAMS:
        param = command_params.get(key)
        if param is None:
            continue
        allowed = param_allowed_values(param)
        if not allowed:
            continue
        current = str(getattr(param, "value", ""))
        if current in allowed:
            continue
        safe = (
            fixed_vertical_value(allowed)
            if key == AC_SWING_V_PARAM
            else next((v for v in allowed if v != "0"), allowed[0])
        )
        try:
            param.value = safe
            _LOGGER.debug(
                "AC settings: sanitized %s from %r to %s (allowed=%s)", key, current, safe, allowed
            )
        except Exception as err:  # pragma: no cover - defensive
            _LOGGER.warning(
                "AC settings: unable to sanitize %s (value %r): %s", key, current, err
            )


# Settings parameters whose value read back from the cloud is NOT trusted once Home
# Assistant has written it. `echoStatus` (the key-press beep; 0 = beep, 1 = silent):
# the appliance applies the new value, but the cloud keeps reporting the old one
# (gvigroux/hon issue #268, seen on AS25/AS35 units). Since the shadow is synced into
# the `settings` command on every refresh and every send transmits the WHOLE group,
# the stale value would go out again with the next temperature or mode change and
# the beep would come back. For these parameters the value last written from Home
# Assistant is pinned per appliance and wins over the shadow in every settings send.
# Nothing is pinned until Home Assistant writes the parameter itself.
AC_PINNED_PARAMS: frozenset[str] = frozenset({"echoStatus"})

# appliance -> {param: value}. Weak keys: an appliance dropped by a reload takes its
# pins with it (the switch restores them on the next start, see switch.py).
_PINNED_SETTINGS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def pin_setting(appliance, name: str, value) -> None:
    """Pin `name` = `value` for `appliance`, if `name` is a pinned parameter."""
    if appliance is None or name not in AC_PINNED_PARAMS or value is None:
        return
    try:
        _PINNED_SETTINGS.setdefault(appliance, {})[name] = str(value)
    except TypeError:  # not weak-referenceable / hashable (e.g. a test double)
        _LOGGER.debug("AC settings: cannot pin %s on %r", name, type(appliance))


def pinned_setting(appliance, name: str) -> str | None:
    """The value pinned for `name` on `appliance`, or None if nothing is pinned."""
    if appliance is None:
        return None
    try:
        return _PINNED_SETTINGS.get(appliance, {}).get(name)
    except TypeError:  # not weak-referenceable / hashable (e.g. a test double)
        return None


def pinned_settings(appliance) -> dict[str, str]:
    """Every value pinned for `appliance` (a copy), or {} if nothing is pinned."""
    if appliance is None:
        return {}
    try:
        return dict(_PINNED_SETTINGS.get(appliance, {}))
    except TypeError:  # not weak-referenceable / hashable (e.g. a test double)
        return {}


def _pinned_settings(appliance) -> dict[str, str]:
    """Pinned values the appliance's `settings` command actually declares."""
    return {
        k: v
        for k, v in pinned_settings(appliance).items()
        if settings_param(appliance, k) is not None
    }


async def async_send_settings(hass, client, appliance, params: dict) -> None:
    """Apply `params` to the AC's `settings` command and send it.

    Sanitizes windDirection* before sending (never 0): the requested values win
    anyway. Delegates to the generic sender (hon_commands.async_send_command),
    which handles command/parameter lookup, rollback and execution on the client's
    dedicated loop; the AC sanitization is plugged in as a pre_send hook.

    Pinned parameters (AC_PINNED_PARAMS) are added to `params` so the whole-group
    send carries Home Assistant's value and not the stale shadow one; a value the
    caller passes explicitly still wins.
    """
    pinned = _pinned_settings(appliance)
    if pinned:
        params = {**pinned, **params}
    await async_send_command(
        hass, client, appliance, "settings", params, pre_send=sanitize_wind_direction
    )
