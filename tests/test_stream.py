"""stream.py: the contract Protect Rules and Protect Detections rely on, and
how it holds up when Protect or uiprotect change.

The contract (do not change without changing the other companions):

  protect_api(hass)                 the UniFi Protect integration's client, or None
  async_get_stream(hass)            one ProtectStream per hass
  stream.connected                  bool
  stream.subscribe(on_packet, on_disconnect=None, on_connect=None) -> unsubscribe
      on_packet(action: dict, data) for every update
      on_disconnect()               when a live connection drops
      on_connect()                  each time it is (re)established; not replayed
                                    to someone who subscribes while connected
      unsubscribe()                 can be called twice
  The connection opens with the first subscriber and closes with the last.
  A listener that raises doesn't stop the stream or the other listeners.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import types
import unittest
from unittest import mock

from _load import ConfigEntryState, WSMsgType, load_stream

stream, issues = load_stream()
real_sleep = asyncio.sleep


class FakeWS:
    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self.closed = False

    def send(self, action, data=None, kind=WSMsgType.BINARY):
        self.queue.put_nowait(types.SimpleNamespace(type=kind, data=(action, data)))

    def send_raw(self, raw):
        self.queue.put_nowait(types.SimpleNamespace(type=WSMsgType.BINARY, data=raw))

    def drop(self):
        self.queue.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        msg = await self.queue.get()
        if msg is None:
            raise StopAsyncIteration
        return msg

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True


class FakeApi:
    headers = {"Cookie": "x"}
    base_url = "https://nvr"
    private_ws_path = "/proxy/protect/ws/updates"

    def __init__(self) -> None:
        self._ws_url = "wss://nvr/proxy/protect/ws/updates"
        self._verify_ssl = False
        self.sockets: list[FakeWS] = []
        self.connect_args: list[dict] = []
        self.fail = False

    async def ensure_authenticated(self):
        pass

    async def get_session(self):
        return self

    def ws_connect(self, url, **kwargs):
        if self.fail:
            raise OSError("no route")
        self.connect_args.append({"url": url, **kwargs})
        self.sockets.append(FakeWS())
        return self.sockets[-1]


class FakeHass:
    def __init__(self, api) -> None:
        self.data: dict = {}
        self.entries = [types.SimpleNamespace(state=ConfigEntryState.LOADED, runtime_data=types.SimpleNamespace(api=api))]
        self.config_entries = types.SimpleNamespace(
            async_entries=lambda domain: self.entries if domain == "unifiprotect" else []
        )

    def async_create_background_task(self, coro, name):
        return asyncio.get_running_loop().create_task(coro, name=name)


async def settle():
    for _ in range(20):
        await real_sleep(0)


class StreamCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        issues.clear()
        self.api = FakeApi()
        self.hass = FakeHass(self.api)
        self.stream = stream.async_get_stream(self.hass)
        self.sleeps: list[float] = []

        async def fast_sleep(delay, *args):
            self.sleeps.append(delay)
            await real_sleep(0)

        patcher = mock.patch.object(stream.asyncio, "sleep", fast_sleep)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.unsubs: list = []
        self.addAsyncCleanup(self._close)

    async def _close(self):
        for unsub in self.unsubs:
            unsub()
        await settle()

    def listen(self, name="a"):
        seen: list = []
        unsub = self.stream.subscribe(
            lambda action, data: seen.append((action, data)),
            lambda: seen.append("down"),
            lambda: seen.append("up"),
        )
        self.unsubs.append(unsub)
        return seen, unsub

    @property
    def ws(self) -> FakeWS:
        return self.api.sockets[-1]


class Contract(StreamCase):
    def test_signatures(self):
        self.assertEqual(list(inspect.signature(stream.protect_api).parameters), ["hass"])
        self.assertEqual(list(inspect.signature(stream.async_get_stream).parameters), ["hass"])
        params = inspect.signature(stream.ProtectStream.subscribe).parameters
        self.assertEqual(list(params), ["self", "on_packet", "on_disconnect", "on_connect"])
        self.assertIsNone(params["on_disconnect"].default)
        self.assertIsNone(params["on_connect"].default)

    def test_protect_api(self):
        self.assertIs(stream.protect_api(self.hass), self.api)
        self.hass.entries[0].state = ConfigEntryState.NOT_LOADED
        self.assertIsNone(stream.protect_api(self.hass))
        self.hass.entries.clear()
        self.assertIsNone(stream.protect_api(self.hass))

    def test_one_stream_per_hass(self):
        self.assertIs(stream.async_get_stream(self.hass), self.stream)
        self.assertIs(self.hass.data["protect_zones_stream"], self.stream)
        self.assertIs(self.stream.connected, False)

    async def test_packets_and_connection_changes(self):
        seen, _ = self.listen()
        await settle()
        self.assertIs(self.stream.connected, True)
        self.assertEqual(seen, ["up"])
        self.ws.send({"modelKey": "event", "id": "e1", "action": "add"}, {"type": "smartDetectZone"})
        self.ws.send({"modelKey": "camera", "id": "c1"}, None)
        await settle()
        self.assertEqual(
            seen[1:],
            [({"modelKey": "event", "id": "e1", "action": "add"}, {"type": "smartDetectZone"}), ({"modelKey": "camera", "id": "c1"}, None)],
        )
        self.ws.drop()
        await settle()
        self.assertEqual(seen[3:], ["down", "up"])  # dropped, then reconnected
        self.assertEqual(len(self.api.sockets), 2)
        self.assertIs(self.stream.connected, True)

    async def test_packet_only_subscriber(self):
        seen: list = []
        self.unsubs.append(self.stream.subscribe(lambda action, data: seen.append(action)))
        await settle()
        self.ws.send({"modelKey": "automation"}, {})
        self.ws.drop()
        await settle()
        self.assertEqual(seen, [{"modelKey": "automation"}])

    async def test_one_connection_for_all_and_no_replay_of_on_connect(self):
        first, _ = self.listen()
        await settle()
        second, _ = self.listen()
        await settle()
        self.assertEqual(len(self.api.sockets), 1)
        self.assertEqual(second, [])  # joined while connected: must read .connected
        self.ws.send({"modelKey": "event"}, {})
        await settle()
        self.assertEqual(first[1:], second)

    async def test_opens_with_the_first_and_closes_with_the_last(self):
        self.assertEqual(self.api.sockets, [])
        first, unsub_first = self.listen()
        second, unsub_second = self.listen()
        await settle()
        unsub_first()
        await settle()
        self.assertIs(self.stream.connected, True)
        self.assertIs(self.ws.closed, False)
        unsub_second()
        self.assertIs(self.stream.connected, False)
        await settle()
        self.assertIs(self.ws.closed, True)
        self.assertNotIn("down", first + second)  # unsubscribing is not a disconnect
        unsub_first()  # twice is fine
        unsub_second()
        third, _ = self.listen()  # and it opens again
        await settle()
        self.assertEqual(third, ["up"])
        self.assertEqual(len(self.api.sockets), 2)

    async def test_a_failing_listener_stops_nothing(self):
        def bad(*args):
            raise RuntimeError("boom")

        self.unsubs.append(self.stream.subscribe(bad, bad, bad))
        seen, _ = self.listen()
        with self.assertLogs(stream._LOGGER, "WARNING") as logs:
            await settle()
            for _ in range(5):
                self.ws.send({"modelKey": "event"}, {})
            await settle()
            self.ws.drop()
            await settle()
        self.assertEqual(seen.count(({"modelKey": "event"}, {})), 5)
        self.assertEqual([s for s in seen if isinstance(s, str)], ["up", "down", "up"])
        # warned, but not once per packet
        self.assertEqual(sum("failed on a Protect update" in line for line in logs.output), 1)

    async def test_non_binary_messages(self):
        seen, _ = self.listen()
        await settle()
        self.ws.send({"modelKey": "x"}, {}, kind=WSMsgType.TEXT)
        await settle()
        self.assertEqual(seen, ["up"])
        self.ws.send(None, kind=WSMsgType.CLOSED)
        await settle()
        self.assertEqual(seen, ["up", "down", "up"])


class Resilience(StreamCase):
    async def test_waits_for_the_protect_integration(self):
        self.hass.entries[0].state = ConfigEntryState.NOT_LOADED
        seen, _ = self.listen()
        await settle()
        self.assertEqual((seen, self.api.sockets), ([], []))
        self.assertEqual(set(self.sleeps), {10})
        self.hass.entries[0].state = ConfigEntryState.LOADED
        await settle()
        self.assertEqual(seen, ["up"])
        self.assertEqual(issues, [])

    async def test_backs_off_while_it_cannot_connect(self):
        self.api.fail = True
        seen, _ = self.listen()
        await settle()
        self.assertEqual(self.sleeps[:7], [2, 4, 8, 16, 32, 60, 60])
        self.assertEqual(seen, [])  # never connected: no "down" either
        self.api.fail = False
        await settle()
        self.sleeps.clear()
        self.ws.drop()
        await settle()
        self.assertEqual(self.sleeps, [2])  # starts over after a connection

    async def test_a_dead_task_is_restarted_by_the_next_subscriber(self):
        first, _ = self.listen()
        await settle()
        task = self.stream._task
        task.cancel()  # stands in for "died of something unforeseen"
        await settle()
        self.assertTrue(task.done())
        second, _ = self.listen()
        await settle()
        self.assertIsNot(self.stream._task, task)
        self.assertIs(self.stream.connected, True)
        self.assertEqual(second, ["up"])
        self.assertEqual(first[-1], "up")
        self.ws.send({"modelKey": "event"}, {})
        await settle()
        self.assertEqual(first[-1], second[-1])

    async def test_certificate_setting_is_passed_on(self):
        self.api._verify_ssl = True
        self.listen()
        await settle()
        self.assertIs(self.api.connect_args[0]["ssl"], True)
        self.assertEqual(self.api.connect_args[0]["headers"], {"Cookie": "x"})
        self.assertEqual(self.api.connect_args[0]["url"], "wss://nvr/proxy/protect/ws/updates")
        self.assertEqual(issues, [])

    async def test_certificate_not_checked_only_if_set_so(self):
        self.listen()
        await settle()
        self.assertIs(self.api.connect_args[0]["ssl"], False)

    async def test_certificate_is_checked_if_the_setting_is_gone(self):
        del self.api._verify_ssl
        seen, _ = self.listen()
        with self.assertLogs(stream._LOGGER, "WARNING"):
            await settle()
        self.assertIs(self.api.connect_args[0]["ssl"], True)
        self.assertEqual(issues, [("protect_zones", "stream_internals")])
        self.assertEqual(seen, ["up"])  # still works if the certificate is fine

    async def test_url_fallback(self):
        del self.api._ws_url
        self.listen()
        await settle()
        self.assertEqual(self.api.connect_args[0]["url"], "wss://nvr/proxy/protect/ws/updates")

    async def test_missing_client_internals_raise_an_issue_then_clear(self):
        get_session = FakeApi.get_session
        del FakeApi.get_session
        self.addCleanup(setattr, FakeApi, "get_session", get_session)
        seen, _ = self.listen()
        with self.assertLogs(stream._LOGGER, "WARNING") as logs:
            await settle()
        self.assertEqual(issues, [("protect_zones", "stream_internals")])
        self.assertEqual(len(logs.output), 1)  # said once, not on every retry
        self.assertEqual(seen, [])
        FakeApi.get_session = get_session
        await settle()
        self.assertEqual(seen, ["up"])
        self.assertEqual(issues, [])

    async def test_missing_wspacket_raises_an_issue_and_does_not_kill_the_task(self):
        ws_module = sys.modules["uiprotect.data.websocket"]
        packet_class = ws_module.WSPacket
        del ws_module.WSPacket
        self.addCleanup(setattr, ws_module, "WSPacket", packet_class)
        seen, _ = self.listen()
        with self.assertLogs(stream._LOGGER, "WARNING"):
            await settle()
        self.assertEqual(issues, [("protect_zones", "stream_internals")])
        self.assertFalse(self.stream._task.done())
        ws_module.WSPacket = packet_class
        await settle()
        self.assertEqual(seen, ["up"])
        self.assertEqual(issues, [])

    async def test_undecodable_updates_raise_an_issue_then_clear(self):
        seen, _ = self.listen()
        await settle()
        self.ws.send_raw(b"junk")
        self.ws.send({"modelKey": "event"}, {})
        for _ in range(stream.UNDECODABLE_AFTER - 1):
            self.ws.send_raw(b"junk")
        await settle()
        self.assertEqual(issues, [])  # a bad one now and then is not a problem
        self.ws.send_raw(b"junk")
        self.ws.send("not a dict", {})  # an action frame of the wrong shape counts too
        with self.assertLogs(stream._LOGGER, "WARNING"):
            await settle()
        self.assertEqual(issues, [("protect_zones", "stream_undecodable")])
        self.assertIs(self.stream.connected, True)
        self.assertEqual(seen, ["up", ({"modelKey": "event"}, {})])
        self.ws.send({"modelKey": "event"}, {})
        await settle()
        self.assertEqual(issues, [])
        self.assertEqual(len(seen), 3)


if __name__ == "__main__":
    unittest.main()
