"""
Modernized Disney Infinity USB base driver.

This is a rewrite of the chrisbergeron/di-usb-library code to use
a modern hidapi wrapper that works on Linux/macOS/Windows.

It supports both common Python bindings:
- `hid`    (pip install hid)    [preferred]
- `hidapi` (pip install hidapi) [fallback]

Public API:
    from infinity import InfinityBase

    base = InfinityBase()
    base.connect()
    print(base.get_all_tags())

Notes:
- On Linux you may need a udev rule for /dev/hidraw access.
- This targets the Wii/PlayStation base (VID 0x0E6F, PID 0x0129). Xbox bases differ.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

DEFAULT_VENDOR_ID = 0x0E6F
DEFAULT_PRODUCT_ID = 0x0129

# The base uses 32-byte input reports and 33-byte output reports (report_id + 32 bytes).
IN_REPORT_SIZE = 32
OUT_REPORT_SIZE = 33


class InfinityError(RuntimeError):
    """Base class for Infinity USB errors."""


class DeviceNotFoundError(InfinityError):
    """No compatible base found."""


class DeviceOpenError(InfinityError):
    """Base found but couldn't be opened (permissions/driver)."""


def _load_hid_backend():
    """
    Try to import a hidapi wrapper module.

    Returns a module-like object exposing Device and (optionally) enumerate().
    """
    try:
        import hid  # type: ignore
        return hid
    except Exception:
        try:
            import hidapi as hid  # type: ignore
            return hid
        except Exception as e:
            raise ImportError(
                "Couldn't import a HID backend. Try:\n"
                "  pip install hid\n"
                "or:\n"
                "  pip install hidapi\n"
            ) from e


_HID = _load_hid_backend()


@dataclass(frozen=True)
class HidDeviceInfo:
    vendor_id: int
    product_id: int
    path: Optional[Union[str, bytes]] = None
    product_string: Optional[str] = None
    manufacturer_string: Optional[str] = None
    serial_number: Optional[str] = None

    @staticmethod
    def from_enumerate_dict(d: Dict[str, Any]) -> "HidDeviceInfo":
        return HidDeviceInfo(
            vendor_id=int(d.get("vendor_id") or d.get("vendorId") or 0),
            product_id=int(d.get("product_id") or d.get("productId") or 0),
            path=d.get("path"),
            product_string=d.get("product_string") or d.get("product"),
            manufacturer_string=d.get("manufacturer_string") or d.get("manufacturer"),
            serial_number=d.get("serial_number") or d.get("serial"),
        )


def discover_bases(
    vendor_id: int = DEFAULT_VENDOR_ID,
    product_id: int = DEFAULT_PRODUCT_ID,
) -> List[HidDeviceInfo]:
    """List connected bases matching VID/PID (best effort)."""
    enum = getattr(_HID, "enumerate", None)
    if not callable(enum):
        return []
    try:
        devices = enum()
    except TypeError:
        try:
            devices = enum(vendor_id, product_id)
        except Exception:
            return []

    infos: List[HidDeviceInfo] = []
    for d in devices:
        info = HidDeviceInfo.from_enumerate_dict(d)
        if info.vendor_id == vendor_id and info.product_id == product_id:
            infos.append(info)
    return infos


class _HidDevice:
    """Compatibility wrapper around different hidapi Python bindings."""

    def __init__(self, vendor_id: int, product_id: int, path: Optional[Union[str, bytes]] = None):
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.path = path
        self._dev = self._open()

    def _open(self):
        Device = getattr(_HID, "Device", None)
        if Device is None:
            # Old-style module with hid.device()
            device_ctor = getattr(_HID, "device", None)
            if device_ctor is None:
                raise ImportError("HID backend doesn't expose Device or device()")
            dev = device_ctor()
            if self.path is not None and hasattr(dev, "open_path"):
                dev.open_path(self.path)
            else:
                try:
                    dev.open(vendor_id=self.vendor_id, product_id=self.product_id)
                except TypeError:
                    dev.open(self.vendor_id, self.product_id)
            return dev

        # New-style Device class
        if self.path is not None:
            for args, kwargs in (
                ((), {"path": self.path}),
                ((self.path,), {}),
            ):
                try:
                    return Device(*args, **kwargs)
                except TypeError:
                    pass

        for args, kwargs in (
            ((), {"vendor_id": self.vendor_id, "product_id": self.product_id}),
            ((self.vendor_id, self.product_id), {}),
            ((), {}),
        ):
            try:
                dev = Device(*args, **kwargs)
                if args == () and kwargs == {} and hasattr(dev, "open"):
                    try:
                        dev.open(vendor_id=self.vendor_id, product_id=self.product_id)
                    except TypeError:
                        dev.open(self.vendor_id, self.product_id)
                return dev
            except TypeError:
                continue

        raise DeviceOpenError("HID backend Device() signature not supported; try a different backend")

    def close(self) -> None:
        if hasattr(self._dev, "close"):
            try:
                self._dev.close()
            except Exception:
                pass

    def set_blocking(self, blocking: bool) -> None:
        if hasattr(self._dev, "set_nonblocking"):
            try:
                self._dev.set_nonblocking(not blocking)
                return
            except TypeError:
                self._dev.set_nonblocking(0 if blocking else 1)
                return
        if hasattr(self._dev, "nonblocking"):
            try:
                setattr(self._dev, "nonblocking", not blocking)
            except Exception:
                pass

    def read(self, size: int, timeout_ms: int) -> List[int]:
        dev = self._dev
        for call in (
            lambda: dev.read(size, timeout_ms),
            lambda: dev.read(size, timeout_ms=timeout_ms),
            lambda: dev.read(size, timeout=timeout_ms),
            lambda: dev.read(size),
        ):
            try:
                data = call()
                break
            except TypeError:
                continue
        else:
            raise InfinityError("Device.read() signature not supported by backend")

        if data is None:
            return []
        if isinstance(data, (bytes, bytearray)):
            return list(data)
        return list(data)

    def write(self, data: Union[bytes, bytearray, List[int]]) -> int:
        dev = self._dev
        b = bytes(data) if not isinstance(data, list) else bytes(data)
        try:
            return int(dev.write(b))
        except TypeError:
            return int(dev.write(list(b)))


class InfinityComms(threading.Thread):
    """Background reader thread + request/response correlation."""

    def __init__(
        self,
        vendor_id: int = DEFAULT_VENDOR_ID,
        product_id: int = DEFAULT_PRODUCT_ID,
        path: Optional[Union[str, bytes]] = None,
        read_timeout_ms: int = 3000,
        debug: bool = False,
    ):
        super().__init__(daemon=True)
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.path = path
        self.read_timeout_ms = int(read_timeout_ms)
        self.debug = debug

        try:
            self.device = _HidDevice(vendor_id, product_id, path=path)
        except Exception as e:
            infos = discover_bases(vendor_id, product_id)
            if infos:
                raise DeviceOpenError(
                    "Base found but couldn't be opened. On Linux this is usually /dev/hidraw* permissions. "
                    "Try sudo once, then add a udev rule."
                ) from e
            raise DeviceNotFoundError(
                f"No Disney Infinity base found (VID={vendor_id:04x} PID={product_id:04x})."
            ) from e

        self.device.set_blocking(True)

        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._pending: Dict[int, Future[List[int]]] = {}
        self._message_number = 0
        self._observers: List[Callable[[], None]] = []

        if self.debug:
            print(f"Connected to Disney Infinity base (VID={vendor_id:04x} PID={product_id:04x})")

    def add_observer(self, cb: Callable[[], None]) -> None:
        self._observers.append(cb)

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        self.stop()
        self.device.close()

    def run(self) -> None:
        while not self._stop.is_set():
            line = self.device.read(IN_REPORT_SIZE, self.read_timeout_ms)
            if not line:
                continue

            if line[0] == 0xAA and len(line) >= 3:
                length = line[1]
                message_id = line[2]
                payload = line[3 : length + 2]  # preserve original slicing semantics
                with self._lock:
                    fut = self._pending.pop(message_id, None)
                if fut is not None and not fut.done():
                    fut.set_result(payload)
                elif self.debug:
                    print("Response with unknown message_id:", message_id, line)

            elif line[0] == 0xAB:
                for cb in list(self._observers):
                    try:
                        cb()
                    except Exception as e:
                        if self.debug:
                            print("Observer error:", e)

            elif self.debug:
                print("Unknown message:", line)

        # Reject pending futures on shutdown
        with self._lock:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(InfinityError("Reader thread stopped before response arrived"))
            self._pending.clear()

    def _next_message_number(self) -> int:
        with self._lock:
            self._message_number = (self._message_number + 1) % 256
            return self._message_number

    @staticmethod
    def _build_message(command: int, message_id: int, data: List[int]) -> bytes:
        command_body = [command & 0xFF, message_id & 0xFF] + [b & 0xFF for b in data]
        command_length = len(command_body)
        command_bytes = [0x00, 0xFF, command_length] + command_body  # 0x00 = report id
        msg = [0x00] * OUT_REPORT_SIZE
        checksum = 0
        for i, b in enumerate(command_bytes):
            msg[i] = b
            checksum = (checksum + b) & 0xFF
        msg[len(command_bytes)] = checksum
        return bytes(msg)

    def send(self, command: int, data: Optional[List[int]] = None, *, expect_reply: bool = True) -> Future[List[int]]:
        if data is None:
            data = []
        mid = self._next_message_number()
        msg = self._build_message(command, mid, data)

        fut: Future[List[int]] = Future()
        if expect_reply:
            with self._lock:
                self._pending[mid] = fut

        try:
            self.device.write(msg)
        except Exception as e:
            if expect_reply:
                with self._lock:
                    self._pending.pop(mid, None)
            fut.set_exception(DeviceOpenError(f"Write failed: {e}"))
            return fut

        if not expect_reply:
            fut.set_result([])
        return fut


class InfinityBase:
    """
    High-level API for the Disney Infinity base.

    Modern (blocking):
        base = InfinityBase().connect()
        tags = base.get_all_tags()

    Legacy (callbacks) is also supported:
        base.getAllTags(print)
    """

    def __init__(
        self,
        vendor_id: int = DEFAULT_VENDOR_ID,
        product_id: int = DEFAULT_PRODUCT_ID,
        path: Optional[Union[str, bytes]] = None,
        debug: bool = False,
    ):
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.path = path
        self.debug = debug

        self.onTagsChanged: Optional[Callable[[], None]] = None  # legacy name
        self._comms: Optional[InfinityComms] = None

    def connect(self) -> "InfinityBase":
        if self._comms is not None:
            return self
        self._comms = InfinityComms(
            vendor_id=self.vendor_id,
            product_id=self.product_id,
            path=self.path,
            debug=self.debug,
        )
        self._comms.add_observer(self._handle_tags_updated)
        self._comms.start()
        self.activate()
        return self

    def disconnect(self) -> None:
        if self._comms is None:
            return
        self._comms.close()
        self._comms = None

    def __enter__(self) -> "InfinityBase":
        return self.connect()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.disconnect()

    def _handle_tags_updated(self) -> None:
        if self.onTagsChanged:
            try:
                self.onTagsChanged()
            except Exception:
                pass

    def _ensure_connected(self) -> InfinityComms:
        if self._comms is None:
            raise InfinityError("Not connected. Call base.connect() first.")
        return self._comms

    def _call(self, cmd: int, data: Optional[List[int]] = None, *, timeout: float = 3.5) -> List[int]:
        comms = self._ensure_connected()
        fut = comms.send(cmd, data or [], expect_reply=True)
        return fut.result(timeout=timeout)

    def _send(self, cmd: int, data: Optional[List[int]] = None) -> None:
        comms = self._ensure_connected()
        comms.send(cmd, data or [], expect_reply=False)

    # --- protocol commands ---
    def activate(self) -> None:
        activate_message = [
            0x28, 0x63, 0x29, 0x20, 0x44,
            0x69, 0x73, 0x6E, 0x65, 0x79,
            0x20, 0x32, 0x30, 0x31, 0x33,
        ]
        self._send(0x80, activate_message)

    def get_tag_index(self) -> List[Tuple[int, int]]:
        data = self._call(0xA1)
        return [((b & 0xF0) >> 4, b & 0x0F) for b in data if b != 0x09]

    def get_tag(self, idx: int) -> List[int]:
        return self._call(0xB4, [idx & 0xFF])

    def get_all_tags(self) -> Dict[int, List[List[int]]]:
        idx = self.get_tag_index()
        tag_by_platform: Dict[int, List[List[int]]] = defaultdict(list)
        for platform, tag_idx in idx:
            tag_by_platform[platform].append(self.get_tag(tag_idx))
        return dict(tag_by_platform)

    def set_color(self, platform: int, r: int, g: int, b: int) -> None:
        self._send(0x90, [platform & 0xFF, r & 0xFF, g & 0xFF, b & 0xFF])

    def fade_color(self, platform: int, r: int, g: int, b: int) -> None:
        self._send(0x92, [platform & 0xFF, 0x10, 0x02, r & 0xFF, g & 0xFF, b & 0xFF])

    def flash_color(self, platform: int, r: int, g: int, b: int) -> None:
        self._send(0x93, [platform & 0xFF, 0x02, 0x02, 0x06, r & 0xFF, g & 0xFF, b & 0xFF])

    # --- legacy names / callback wrappers ---
    def tagsUpdated(self) -> None:
        self._handle_tags_updated()

    def getTagIdx(self, then: Callable[[List[Tuple[int, int]]], None]) -> None:
        threading.Thread(target=lambda: then(self.get_tag_index()), daemon=True).start()

    def getTag(self, idx: int, then: Callable[[List[int]], None]) -> None:
        threading.Thread(target=lambda: then(self.get_tag(idx)), daemon=True).start()

    def getAllTags(self, then: Callable[[Dict[int, List[List[int]]]], None]) -> None:
        threading.Thread(target=lambda: then(self.get_all_tags()), daemon=True).start()

    def setColor(self, platform: int, r: int, g: int, b: int) -> None:
        self.set_color(platform, r, g, b)

    def fadeColor(self, platform: int, r: int, g: int, b: int) -> None:
        self.fade_color(platform, r, g, b)

    def flashColor(self, platform: int, r: int, g: int, b: int) -> None:
        self.flash_color(platform, r, g, b)


# Handy Linux udev rule snippet
LINUX_UDEV_RULE_SNIPPET = (
    'SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0e6f", ATTRS{idProduct}=="0129", MODE="0666"\n'
)
