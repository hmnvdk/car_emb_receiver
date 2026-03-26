"""
USB transport — port of gartnera/headunit hu_usb.cpp

Handles AOA (Android Open Accessory) device discovery and switching,
then exposes synchronous read/write on bulk endpoints.
"""
import errno
import logging
import time
import threading
import usb.core
import usb.util

from hu_const import (
    GOOGLE_VID, AOA_PRODUCT_IDS, ANDROID_VIDS,
    AOA_USB_GET_PROTOCOL, AOA_USB_SEND_STRING, AOA_USB_START,
    AOA_STRINGS,
)

log = logging.getLogger(__name__)

READ_BUF    = 16384
# 2s иногда мало: при тяжёлом видео/нагрузке телефон может подвисать на приёме BULK OUT.
# Держим побольше, чтобы не рвать сессию по ложному timeout.
USB_TIMEOUT = 12000   # ms


class HUTransportUSB:
    """USB AOA 2.0 transport.  Exposes read() / write() / start() / stop()."""

    def __init__(self):
        self._dev    = None
        self._ep_in  = None
        self._ep_out = None
        self._lock   = threading.Lock()

    # ── Public API ─────────────────────────────────────────────────────────

    def start(self, wait_for_device: bool = True) -> None:
        """Find phone, switch to AOA if needed, open endpoints."""
        dev, is_aoa = self._find_device()
        if dev is None:
            raise RuntimeError("No Android device found. Connect phone via USB and unlock screen.")

        if not is_aoa:
            log.info("Phone found (normal mode). Switching to AOA…")
            self._switch_to_aoa(dev)
            time.sleep(2.5)
            dev, is_aoa = self._find_device()
            if not is_aoa:
                raise RuntimeError("Phone did not reconnect in AOA mode.")

        log.info("Phone in AOA mode (VID=%04X PID=%04X)", dev.idVendor, dev.idProduct)
        self._open_endpoints(dev)

    def stop(self) -> None:
        if self._dev:
            try:
                usb.util.release_interface(self._dev, 0)
                usb.util.dispose_resources(self._dev)
            except Exception:
                pass
            self._dev = None

    def read(self, length: int, tmo: int = USB_TIMEOUT) -> bytes:
        """Blocking read from the IN endpoint."""
        data = self._ep_in.read(length, timeout=tmo)
        return bytes(data)

    def write(self, data: bytes, tmo: int = USB_TIMEOUT) -> int:
        log.debug("USB → %d байт: %s…", len(data), data[:16].hex())
        for attempt in range(4):
            try:
                with self._lock:
                    return self._ep_out.write(data, timeout=tmo)
            except usb.core.USBError as e:
                en = e.errno
                retryable = en in (errno.EIO, errno.ENODEV, 5, 19) or (
                    en is None and "Input/output" in str(e)
                )
                if retryable and attempt < 3:
                    try:
                        self._ep_out.clear_halt()
                    except Exception:
                        pass
                    time.sleep(0.08 * (attempt + 1))
                    continue
                raise

    # ── Internals ──────────────────────────────────────────────────────────

    def _find_device(self):
        for pid in AOA_PRODUCT_IDS:
            dev = usb.core.find(idVendor=GOOGLE_VID, idProduct=pid)
            if dev:
                return dev, True
        for vid in ANDROID_VIDS:
            dev = usb.core.find(idVendor=vid)
            if dev:
                return dev, False
        return None, False

    def _switch_to_aoa(self, dev) -> None:
        try:
            dev.set_configuration()
        except Exception:
            pass
        try:
            buf = dev.ctrl_transfer(0xC0, AOA_USB_GET_PROTOCOL, 0, 0, 2, timeout=2000)
            version = buf[0] | (buf[1] << 8)
            log.info("AOA protocol version: %d", version)
            if version < 1:
                raise RuntimeError("Device does not support AOA")
        except usb.core.USBError as e:
            raise RuntimeError(f"AOA query failed: {e}") from e
        for idx, string in AOA_STRINGS:
            dev.ctrl_transfer(0x40, AOA_USB_SEND_STRING, 0, idx,
                              string.encode() + b"\x00", timeout=2000)
        dev.ctrl_transfer(0x40, AOA_USB_START, 0, 0, None, timeout=2000)
        log.info("AOA start command sent.")

    def _open_endpoints(self, dev) -> None:
        INTF = 0
        try:
            if dev.is_kernel_driver_active(INTF):
                dev.detach_kernel_driver(INTF)
        except Exception:
            pass
        try:
            dev.set_configuration()
        except Exception:
            pass
        cfg  = dev.get_active_configuration()
        intf = cfg[(INTF, 0)]
        try:
            usb.util.claim_interface(dev, INTF)
        except Exception:
            pass

        self._ep_out = usb.util.find_descriptor(intf, custom_match=lambda e:
            usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
            and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK)
        self._ep_in = usb.util.find_descriptor(intf, custom_match=lambda e:
            usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN
            and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK)

        if not self._ep_in or not self._ep_out:
            raise RuntimeError("Could not find USB bulk endpoints.")

        for ep in (self._ep_in, self._ep_out):
            try:
                ep.clear_halt()
            except Exception:
                pass

        self._dev = dev
        log.info("USB endpoints: IN=%02X OUT=%02X",
                 self._ep_in.bEndpointAddress, self._ep_out.bEndpointAddress)
        # Brief settle after claim — helps some kernels after AOA re-enumeration
        time.sleep(0.05)
