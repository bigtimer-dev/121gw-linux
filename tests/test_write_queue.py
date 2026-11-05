import tempfile
import unittest
from pathlib import Path

from gw121.bluez import BluezLink
from gw121.settings import Settings


class FakeBus(BluezLink):
    """BluezLink whose D-Bus calls are held until the test finishes them."""

    def __init__(self):
        super().__init__(Settings(Path(tempfile.mkdtemp()) / "s.json"))
        self.calls = []
        self.is_connected = True
        self.write_char = "/char"

    def _call(self, path, iface, method, params, cb=None, timeout=10_000):
        self.calls.append((method, bytes(params.unpack()[0]) if method == "WriteValue" else None, cb))

    def finish(self, i=0, err=None):
        method, payload, cb = self.calls.pop(i)
        cb(None, err)
        return payload


class WriteQueueTests(unittest.TestCase):
    def test_one_write_at_a_time_in_order(self):
        link, done = FakeBus(), []
        for p in (b"a", b"b", b"c"):
            link.write_frame(p, lambda e, p=p: done.append((p, e)))
        self.assertEqual([c[1] for c in link.calls], [b"a"])        # only the first is with BlueZ
        self.assertEqual(link.finish(), b"a")
        self.assertEqual([c[1] for c in link.calls], [b"b"])
        link.finish()
        link.finish()
        self.assertEqual(done, [(b"a", None), (b"b", None), (b"c", None)])
        self.assertEqual(link.calls, [])

    def test_a_write_from_a_callback_waits_its_turn(self):
        link, order = FakeBus(), []
        link.write_frame(b"1", lambda e: (order.append(1), link.write_frame(b"3", lambda e: order.append(3))))
        link.write_frame(b"2", lambda e: order.append(2))
        while link.calls:
            link.finish()
        self.assertEqual(order, [1, 2, 3])

    def test_disconnect_fails_the_waiting_writes(self):
        link, done = FakeBus(), []
        for p in (b"a", b"b"):
            link.write_frame(p, lambda e, p=p: done.append((p, e)))
        link._flush_writes("Not connected")
        self.assertEqual(done, [(b"b", "Not connected")])          # "a" is with BlueZ; its own answer reports it
        link.write_frame(b"c", lambda e: done.append((b"c", e)))
        self.assertEqual([c[1] for c in link.calls], [b"a", b"c"])


if __name__ == "__main__":
    unittest.main()
