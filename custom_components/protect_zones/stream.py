"""One shared connection to Protect's live update stream.

The UniFi Protect companions (Protect Zones, Protect Rules, Protect
Detections) all listen to the stream the Protect app uses. This keeps a
single connection for all of them:

    from custom_components.protect_zones.stream import async_get_stream

    unsubscribe = async_get_stream(hass).subscribe(on_packet, on_disconnect, on_connect)

on_packet(action, data) is called for every update (both are the decoded
frames of Protect's packet); on_disconnect() when the connection drops, so a
listener can forget what it can no longer keep up to date; on_connect() each
time it is (re)established, so a listener can catch up on what it missed. The connection is
opened with the first subscriber and closed with the last.

It reuses the logged-in client of HA's own UniFi Protect integration
(protect_api), so it holds no credentials of its own. The stream is Protect's
internal one, which is undocumented: a Protect update could change it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
import time
from typing import Any

from aiohttp import WSMsgType

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

_LOGGER = logging.getLogger(__name__)

_KEY = "protect_zones_stream"
_DOMAIN = "protect_zones"  # the repair issues below are raised under this integration

# Repair issues (texts in translations/en.json)
ISSUE_INTERNALS = "stream_internals"  # something this relies on in uiprotect is gone
ISSUE_UNDECODABLE = "stream_undecodable"  # connected, but the updates can't be read
# this many updates in a row that can't be read -> ISSUE_UNDECODABLE
UNDECODABLE_AFTER = 20
# the same warning is logged at most once in this many seconds
WARN_EVERY = 300


class _InternalsChanged(Exception):
    """Something this relies on in uiprotect / the UniFi Protect integration is gone."""

type PacketListener = Callable[[dict[str, Any], Any], None]
type ConnectionListener = Callable[[], None]


def protect_api(hass: HomeAssistant):
    """The UniFi Protect integration's logged-in client, or None if it isn't loaded."""
    for entry in hass.config_entries.async_entries("unifiprotect"):
        if entry.state is ConfigEntryState.LOADED and getattr(entry, "runtime_data", None):
            return entry.runtime_data.api
    return None


@callback
def async_get_stream(hass: HomeAssistant) -> ProtectStream:
    if _KEY not in hass.data:
        hass.data[_KEY] = ProtectStream(hass)
    return hass.data[_KEY]


class ProtectStream:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.connected = False
        self._listeners: list[tuple[PacketListener, ConnectionListener | None, ConnectionListener | None]] = []
        self._task: asyncio.Task | None = None
        self._warned: dict[str, float] = {}  # warning key -> when last logged
        self._skipped: dict[str, int] = {}  # warning key -> how many were held back since
        self._issues: set[str] = set()  # repair issues now raised

    @callback
    def subscribe(
        self,
        on_packet: PacketListener,
        on_disconnect: ConnectionListener | None = None,
        on_connect: ConnectionListener | None = None,
    ) -> Callable[[], None]:
        item = (on_packet, on_disconnect, on_connect)
        self._listeners.append(item)
        if self._task is None or self._task.done():  # done: it died, start it again
            self.connected = False
            self._task = self.hass.async_create_background_task(self._listen(), "protect live stream")

        @callback
        def unsubscribe() -> None:
            if item in self._listeners:
                self._listeners.remove(item)
            if not self._listeners and self._task:
                self._task.cancel()
                self._task = None
                self.connected = False

        return unsubscribe

    async def _listen(self) -> None:
        backoff = 2
        while self._listeners:
            wait = backoff
            backoff = min(backoff * 2, 60)
            try:
                api = protect_api(self.hass)
                if api is None:
                    wait = 10  # the UniFi Protect integration isn't loaded (yet)
                    backoff = 2
                else:
                    await self._connection(api)
                    wait = backoff = 2  # it was connected: start over
            except asyncio.CancelledError:
                raise
            except _InternalsChanged as err:
                self._raise_issue(ISSUE_INTERNALS, str(err))
            except Exception as err:  # noqa: BLE001
                if self.connected:
                    wait = backoff = 2
                _LOGGER.debug("Protect updates disconnected: %s", err)
            was_connected, self.connected = self.connected, False
            if was_connected:
                self._tell(1)
            await asyncio.sleep(wait)

    async def _connection(self, api: Any) -> None:
        """Connect and pass updates on until the connection ends."""
        try:
            from uiprotect.data.websocket import WSPacket  # noqa: PLC0415
        except Exception as err:  # noqa: BLE001
            raise _InternalsChanged(f"uiprotect.data.websocket.WSPacket ({err})") from err
        try:
            ensure_authenticated, get_session, headers = api.ensure_authenticated, api.get_session, api.headers
            url = getattr(api, "_ws_url", None) or (
                api.base_url.replace("https://", "wss://", 1) + api.private_ws_path
            )
        except AttributeError as err:
            raise _InternalsChanged(str(err)) from err
        # Check the certificate if the UniFi Protect integration is set to. If
        # we can no longer tell, check it: never silently stop checking.
        verify_known = hasattr(api, "_verify_ssl")
        verify = bool(api._verify_ssl) if verify_known else True  # noqa: SLF001
        if not verify_known:
            self._raise_issue(
                ISSUE_INTERNALS,
                "the client's _verify_ssl setting (the certificate is now always checked, "
                "which fails if your Protect console uses a self-signed one)",
            )

        await ensure_authenticated()
        session = await get_session()
        async with session.ws_connect(str(url), headers=headers or {}, ssl=verify, heartbeat=30) as ws:
            self.connected = True
            _LOGGER.debug("Connected to Protect updates")
            if verify_known:
                self._clear_issue(ISSUE_INTERNALS)
            self._tell(2)
            unreadable = 0  # updates in a row that couldn't be decoded
            async for msg in ws:
                if msg.type != WSMsgType.BINARY:
                    if msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                        break
                    continue
                try:
                    packet = WSPacket(msg.data)
                    action, data = packet.action_frame.data, packet.data_frame.data
                    if not isinstance(action, dict):
                        raise TypeError(f"action frame is a {type(action).__name__}, not a dict")  # noqa: TRY301
                except Exception as err:  # noqa: BLE001 - one bad packet shouldn't kill the stream
                    _LOGGER.debug("Skipped a Protect update", exc_info=True)
                    unreadable += 1
                    if unreadable == UNDECODABLE_AFTER:
                        self._raise_issue(ISSUE_UNDECODABLE, f"{type(err).__name__}: {err}")
                    continue
                unreadable = 0
                self._clear_issue(ISSUE_UNDECODABLE)
                for on_packet, _, _ in list(self._listeners):
                    try:
                        on_packet(action, data)
                    except Exception:  # noqa: BLE001 - nor should one listener
                        self._warn(
                            f"packet:{_name(on_packet)}",
                            "%s failed on a Protect update (%s)",
                            _name(on_packet),
                            action.get("modelKey"),
                        )

    def _tell(self, which: int) -> None:
        """Call every listener's on_disconnect (1) or on_connect (2)."""
        for item in list(self._listeners):
            if item[which]:
                try:
                    item[which]()
                except Exception:  # noqa: BLE001
                    self._warn(
                        f"connection:{_name(item[which])}",
                        "%s failed on a Protect connection change",
                        _name(item[which]),
                    )

    def _warn(self, key: str, message: str, *args: Any) -> None:
        """Log a warning with the current exception, but the same one only every WARN_EVERY seconds."""
        now = time.monotonic()
        if key in self._warned and now - self._warned[key] < WARN_EVERY:
            self._skipped[key] = self._skipped.get(key, 0) + 1
            return
        self._warned[key] = now
        if skipped := self._skipped.pop(key, 0):
            message += f" ({skipped} more since the last warning)"
        _LOGGER.warning(message, *args, exc_info=True)

    def _raise_issue(self, issue_id: str, detail: str) -> None:
        if issue_id in self._issues:
            return
        self._issues.add(issue_id)
        _LOGGER.warning("Protect live updates: %s: %s", issue_id, detail)
        try:
            ir.async_create_issue(
                self.hass,
                _DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=issue_id,
                translation_placeholders={"detail": detail},
            )
        except Exception:  # noqa: BLE001 - reporting a problem must not become one
            _LOGGER.debug("Couldn't raise the repair issue %s", issue_id, exc_info=True)

    def _clear_issue(self, issue_id: str) -> None:
        if issue_id not in self._issues:
            return
        self._issues.discard(issue_id)
        try:
            ir.async_delete_issue(self.hass, _DOMAIN, issue_id)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Couldn't clear the repair issue %s", issue_id, exc_info=True)


def _name(func: Any) -> str:
    return getattr(func, "__qualname__", None) or repr(func)
