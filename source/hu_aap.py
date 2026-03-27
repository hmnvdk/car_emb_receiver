"""
HUServer — direct Python port of gartnera/headunit hu_aap.cpp

Frame wire format (gartnera):
  [chan:1][flags:1][payload_len:2 BE][payload:N]
  If FIRST but not LAST: extra [total_len:4 BE] between header and payload.

Message payload:
  [msg_type:2 BE][protobuf bytes]

SSL: in-band TLS.  During STARTIN state only HU_INIT messages are processed.
     Version request is unencrypted; SSL handshake bytes go in SSLHandshake frames.
     Everything after AUTH_COMPLETE is encrypted.
"""
import logging
import math
import re
import struct
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

import usb.core

from hu_const import *
from hu_ssl   import HUSSLLayer
from hu_usb   import HUTransportUSB

log = logging.getLogger(__name__)
navi_log = logging.getLogger("navi")


def _sanitize_nav_text(s: object) -> str:
    """
    Phones sometimes send event_name containing embedded binary/control chars.
    Return a human-readable string (prefer last long readable segment).
    """
    try:
        t = "" if s is None else str(s)
    except Exception:
        return ""
    if not t:
        return ""
    # Strip control chars and collapse whitespace.
    t = "".join(ch if ch.isprintable() and ch not in "\r\n\t" else " " for ch in t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return ""
    # Prefer meaningful readable segments (Cyrillic/Latin text) if garbage was embedded.
    candidates = re.findall(r"[0-9A-Za-zА-Яа-яЁё][0-9A-Za-zА-Яа-яЁё .,:;()«»\"'/-]{6,}", t)
    if candidates:
        # Often the best one is the last (most recent road/step label).
        return candidates[-1].strip()
    return t

MAX_FRAME_SIZE = 16384   # max USB bulk read; big enough for any single transfer

# ── Разрешение видео в Android Auto (GAL / hu.proto), по публичным реверсам ─
#
# Полной открытой спецификации Google для бинарного протокола AA нет. Ориентиры:
#   • milek7 — обзор и ссылки на protobuf: https://milek7.pl/.stuff/galdocs/readme.md
#   • Кэш HU Integration Guide 1.3: https://milek7.pl/.stuff/galdocs/huig13_cache.html
#   • Репозитории gartnera/headunit, OpenAuto/aasdk — тот же семейство сообщений.
#
# В proto_gen/hu.proto в VideoConfig: resolution — enum VIDEO_RESOLUTION (как в реверсе
# Headunit Reloaded, gb.xxy.hr / VideoCodecResolutionType): альбом 1–5, портрет 6–9;
# margin_*, frame_rate, dpi; optional additional_depth.
# Произвольных WxH кроме этих пресетов по проводу нет — только enum + margins + touch.
#
# «Своё» логическое разрешение экрана задаётся не VideoConfig, а парой
# touch_screen_config.width / height в ServiceDiscovery — произвольные uint32 (в разумных
# пределах). Связь с выбранным пресетом (Rw,Rh) из enum — через margins: |Rw−touch_w|,
# |Rh−touch_h|, чтобы телефон согласовал разметку UI с опорным кадром.
#
# Произвольный WxH кодека вне таблицы пресетов отдельным полем в этом proto нельзя —
# только enum + margins + touch. Новые номера enum — из реверса актуальных APK (pbtk и т.д.).


def _video_mode_pixels_and_enum():
    """Все пресеты из hu.proto + пиксельные размеры кадра (как HR VideoCodecResolutionType)."""
    from proto_gen import hu_pb2 as HU

    V = HU.ChannelDescriptor.OutputStreamChannel.VideoConfig
    return (
        (800, 480, V.VIDEO_RESOLUTION_800x480),
        (1280, 720, V.VIDEO_RESOLUTION_1280x720),
        (1920, 1080, V.VIDEO_RESOLUTION_1920x1080),
        (2560, 1440, V.VIDEO_RESOLUTION_2560x1440),
        (3840, 2160, V.VIDEO_RESOLUTION_3840x2160),
        (720, 1280, V.VIDEO_RESOLUTION_720x1280),
        (1080, 1920, V.VIDEO_RESOLUTION_1080x1920),
        (1440, 2560, V.VIDEO_RESOLUTION_1440x2560),
        (2160, 3840, V.VIDEO_RESOLUTION_2160x3840),
    )


def _video_preset_lookup_dict() -> dict[str, tuple[int, int, int]]:
    """Строка 'WxH' → (Rw, Rh, enum) для явного выбора пресета."""
    return {f"{a}x{b}": (a, b, e) for a, b, e in _video_mode_pixels_and_enum()}


def resolve_video_preset(name: Optional[str]):
    """
    Явный пресет по имени из реверса (ровно одна из пар WxH из enum).
    None / 'auto' — не задано, использовать автоподбор под touch.
    """
    if name is None:
        return None
    s = str(name).strip().lower().replace("×", "x")
    if not s or s == "auto":
        return None
    lut = _video_preset_lookup_dict()
    if s in lut:
        return lut[s]
    log.warning(
        "Неизвестный video-пресет %r. Допустимо: auto или одна из пар %s",
        name,
        ", ".join(sorted(lut.keys())),
    )
    return None


def _pick_video_mode_for_touch_ui(w: int, h: int):
    """
    Выбор VIDEO_RESOLUTION под touch_screen (w×h): альбом (w≥h) — только режимы 1–5,
    портрет (h>w) — только 6–9, чтобы телефон не получал несогласованную пару
    «ландшафтный поток + портретный touch» из enum.

    Далее — точное совпадение или минимум L1 по пикселям (как в первой стабильной версии
    после реверса HR; без скоринга по aspect — он добавлялся позже для ультрашироких панелей).

    Свой экран (touch) остаётся произвольным WxH; «привязка» к таблице пресетов —
    только через этот выбор enum + margin_*. Явный пресет: resolve_video_preset + HUServer.
    """
    modes = _video_mode_pixels_and_enum()
    landscape = w >= h
    filtered = [m for m in modes if ((m[0] >= m[1]) == landscape)]
    if not filtered:
        filtered = list(modes)

    for Rw, Rh, ev in filtered:
        if Rw == w and Rh == h:
            return (Rw, Rh, ev)

    def score(m):
        Rw, Rh, _ = m
        l1 = abs(Rw - w) + abs(Rh - h)
        return (l1, Rw * Rh)

    Rw, Rh, enumv = min(filtered, key=score)
    return (Rw, Rh, enumv)


def _video_margins_for_mode(Rw: int, Rh: int, touch_w: int, touch_h: int) -> tuple[int, int]:
    """Сообщаем телефону расхождение между кадром выбранного режима и touch UI."""
    return (abs(Rw - touch_w), abs(Rh - touch_h))


# Значение по умолчанию для CLI `--dpi-scale` и для HUServer.video_dpi_scale.
DEFAULT_VIDEO_DPI_SCALE = 0.8

# Явный VideoConfig.dpi (proto: uint32) — разумные границы для CLI `--dpi`.
# Типовые значения из реверсов/обсуждений AA (не спецификация Google):
#   140 — частая база для 800×480 (gartnera/аналоги);
#   160 — опорный «mdpi» Android;
#   170–213 — попадаются в примерах VideoConfiguration / форумах;
#   213, 238 — крупные/Hi-DPI ГУ (в т.ч. обсуждения на XDA).
#
# Портретное окно 1080×1920 (native touch): в AA2 на практике стабильная работа
# при VideoConfig.dpi не выше этого значения; выше — типично сбои/некорректный UI.
VIDEO_DPI_PORTRAIT_1080X1920_MAX = 240

VIDEO_DPI_EXPLICIT_MIN = 40
VIDEO_DPI_EXPLICIT_MAX = 400


def _clamp_video_dpi_explicit(d: int) -> int:
    return max(VIDEO_DPI_EXPLICIT_MIN, min(VIDEO_DPI_EXPLICIT_MAX, int(d)))


def _video_dpi_for_touch(touch_w: int, touch_h: int, dpi_scale: float) -> int:
    """База 140 dpi при 800×480; масштабируем по площади, ×dpi_scale, пределы 80–260."""
    base = 800 * 480
    area = max(1, touch_w * touch_h)
    s = max(0.05, min(4.0, float(dpi_scale)))
    d = int(140 * math.sqrt(area / base) * s)
    return max(80, min(260, d))


# ── helpers ───────────────────────────────────────────────────────────────

def chan_name(c: int) -> str:
    # Must match hu_const.CHAN_NAME (was wrong before — ch3 is VID, not MIC)
    return CHAN_NAME.get(c, f"CH{c}")


class _FrameDecoder:
    """
    Splits a raw byte stream (or single USB transfer) into complete frames.

    Wire format:
      [chan:1][flags:1][payload_len:2 BE]
      If FIRST but not LAST: [total_len:4 BE]  (skip — only used for pre-sizing)
      [payload:payload_len bytes]
    """

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data: (bytes, bytearray)):
        """Append data, yield (chan, flags, payload) for every complete frame."""
        self._buf.extend(data)
        frames = []
        while True:
            if len(self._buf) < 4:
                break
            flags       = self._buf[1]
            payload_len = struct.unpack_from(">H", self._buf, 2)[0]

            # Multi-frame: FIRST but not LAST carries 4-byte total_len
            extra = 4 if (flags & HU_FRAME_FIRST_FRAME) and not (flags & HU_FRAME_LAST_FRAME) else 0
            need  = 4 + extra + payload_len

            if len(self._buf) < need:
                break

            chan    = self._buf[0]
            payload = bytes(self._buf[4 + extra : 4 + extra + payload_len])
            self._buf = self._buf[need:]
            frames.append((chan, flags, payload))
        return frames


# ── Application callback interface ────────────────────────────────────────

class IHUCallbacks(ABC):
    """Mirror of gartnera IHUConnectionThreadEventCallbacks."""

    @abstractmethod
    def media_packet(self, chan: int, timestamp: int, data: bytes) -> int:
        """Called with decoded video/audio NAL unit.  Return 0 or -1."""

    @abstractmethod
    def media_start(self, chan: int) -> int: ...

    @abstractmethod
    def media_stop(self, chan: int) -> int: ...

    @abstractmethod
    def media_setup_complete(self, chan: int) -> None: ...

    @abstractmethod
    def disconnection_or_error(self) -> None: ...

    def audio_focus_request(self, chan: int, request) -> None:
        """Default: grant full media audio focus immediately."""
        pass

    def video_focus_request(self, chan: int, request) -> None:
        pass

    def customize_car_info(self, car_info) -> None:
        pass

    def customize_input_config(self, inner) -> None:
        pass

    def customize_sensor_config(self, inner) -> None:
        pass

    def navigation_turn_image(self, png_bytes: bytes) -> None:
        """Optional: turn-by-turn arrow PNG from phone (AA_CH_NAVI / NAVTurnMessage.image)."""
        pass

    def navigation_status(self, status: int) -> None:
        """Optional: NAV start/stop event."""
        pass

    def navigation_turn(self, turn_msg) -> None:
        """Optional: full NAVTurnMessage for app-side scenarios."""
        pass

    def navigation_distance(self, distance_msg) -> None:
        """Optional: full NAVDistanceMessage for app-side scenarios."""
        pass


# ── HUServer ──────────────────────────────────────────────────────────────

class HUServer:
    """
    Python port of gartnera HUServer.

    Call hu_aap_start() to connect and run the protocol loop.
    All message handlers mirror the gartnera C++ handlers 1:1.
    """

    HU_STATE_INITIAL  = 0
    HU_STATE_STARTIN  = 1
    HU_STATE_STARTED  = 2
    HU_STATE_STOPPIN  = 3
    HU_STATE_STOPPED  = 4

    def __init__(
        self,
        callbacks: IHUCallbacks,
        cert_type: str = "jaguar",
        video_width: int = 800,
        video_height: int = 480,
        proto_major: Optional[int] = None,
        proto_minor: Optional[int] = None,
        sw_version: Optional[str] = None,
        sw_build: Optional[str] = None,
        video_preset: Optional[str] = None,
        video_dpi_scale: float = DEFAULT_VIDEO_DPI_SCALE,
        video_dpi: Optional[int] = None,
        driver_pos: bool = False,
    ):
        self._cb         = callbacks
        self._cert_type  = cert_type
        self._video_w    = max(1, int(video_width))
        self._video_h    = max(1, int(video_height))
        self._video_dpi_scale = max(0.05, min(4.0, float(video_dpi_scale)))
        self._video_dpi_explicit: Optional[int] = (
            None if video_dpi is None else _clamp_video_dpi_explicit(video_dpi)
        )
        # ServiceDiscoveryResponse.driver_pos: на телефоне в AA2 эмпирически False=LHD, True=RHD.
        self._driver_pos = bool(driver_pos)
        self._video_preset_resolved = resolve_video_preset(video_preset)
        self._vr_major   = AA_VERSION_MAJOR if proto_major is None else int(proto_major)
        self._vr_minor   = AA_VERSION_MINOR if proto_minor is None else int(proto_minor)
        self._sw_version = AA_SW_VERSION if sw_version is None else str(sw_version)
        self._sw_build   = AA_SW_BUILD if sw_build is None else str(sw_build)
        self._state      = self.HU_STATE_INITIAL
        self._transport: Optional[HUTransportUSB] = None
        self._ssl: Optional[HUSSLLayer] = None
        self._channel_session_id = [0] * 256
        # Media ACK value per channel (must be monotonic for flow control)
        self._channel_ack_value = [0] * 256
        # How often to send MediaAck (batching reduces USB OUT pressure)
        self._channel_ack_every = [1] * 256
        self._asm_bufs   = {}   # chan → bytearray (multi-frame reassembly)
        self._decoder    = _FrameDecoder()
        self._stop_event = threading.Event()
        self._reader_thread: Optional[threading.Thread] = None
        # Версия протокола после VersionResponse (для логов); sw_version/sw_build к ней не привязываем — см. gartnera.
        self._remote_proto_major = self._vr_major
        self._remote_proto_minor = self._vr_minor

    # ── Public API ─────────────────────────────────────────────────────────

    def hu_aap_start(self) -> int:
        """
        Start USB transport, send VersionRequest, run SSL handshake,
        then spin up the background reader thread.
        Mirrors gartnera hu_aap_start().
        """
        if self._state in (self.HU_STATE_STARTED, self.HU_STATE_STARTIN):
            return 0

        self._state = self.HU_STATE_STARTIN
        self._remote_proto_major = self._vr_major
        self._remote_proto_minor = self._vr_minor
        self._ssl   = HUSSLLayer(cert_type=self._cert_type)

        self._transport = HUTransportUSB()
        try:
            self._transport.start()
        except Exception as e:
            log.error("Transport start failed: %s", e)
            self._state = self.HU_STATE_STOPPED
            return -1

        # Send unencrypted VersionRequest (gartnera: vr_buf = {0,1,0,1})
        if self._send_version_request() < 0:
            log.error("Не удалось отправить VersionRequest — отключите USB и подключите снова.")
            self.hu_aap_shutdown()
            return -1

        # Blocking startup loop: process until state transitions to STARTED
        log.info("Ожидаю версию и SSL handshake от телефона…")
        try:
            while self._state == self.HU_STATE_STARTIN:
                ret = self._recv_process(tmo=2000)
                if ret < 0:
                    self.hu_aap_shutdown()
                    return ret
        except Exception as e:
            log.error("Startup loop error: %s", e)
            self.hu_aap_shutdown()
            return -1

        # State is now STARTED: launch background reader thread
        log.info("AA соединение установлено — запускаю reader thread.")
        self._stop_event.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name="hu-reader")
        self._reader_thread.start()
        return 0

    def hu_aap_shutdown(self) -> None:
        if self._state == self.HU_STATE_STARTED:
            try:
                self._send_shutdown_request()
            except Exception:
                pass
        self._state = self.HU_STATE_STOPPIN
        self._stop_event.set()
        if self._reader_thread:
            self._reader_thread.join(timeout=3)
        if self._transport:
            self._transport.stop()
            self._transport = None
        self._state = self.HU_STATE_STOPPED
        log.info("HU остановлен.")

    def hu_aap_stop(self) -> int:
        """Called from protocol handlers on fatal error (gartnera style)."""
        if self._state != self.HU_STATE_STARTED:
            return 0
        try:
            self._send_shutdown_request()
        except Exception:
            pass
        self._stop_event.set()
        self._cb.disconnection_or_error()
        return 0

    def send_input_event(self, chan: int, timestamp: int, data: bytes) -> None:
        """Send touch/key event to phone (HU → phone, unencrypted msg_type prefix)."""
        payload = struct.pack(">H", HU_INPUT_InputEvent) + data
        self._enc_send(0, chan, payload)

    # ── Frame encoding / decoding ─────────────────────────────────────────

    def _build_frame(self, chan: int, flags: int, payload: bytes) -> bytes:
        """Build a single frame.  Multi-frame splitting is done by _enc_send."""
        hdr = struct.pack(">BBH", chan, flags, len(payload))
        return hdr + payload

    def _unenc_send(self, retry: int, chan: int, payload: bytes) -> int:
        """Send plaintext frame(s).  Mirrors hu_aap_unenc_send()."""
        msg_type = struct.unpack_from(">H", payload, 0)[0]
        base_flags = 0
        if (chan != AA_CH_CTR and 2 <= msg_type < 0x8000):
            base_flags |= HU_FRAME_CONTROL_MESSAGE

        total = len(payload)
        sent  = 0
        first = True
        while sent < total:
            chunk = payload[sent:sent + MAX_FRAME_PAYLOAD_SIZE]
            flags = base_flags
            if first:
                flags |= HU_FRAME_FIRST_FRAME
            if sent + len(chunk) >= total:
                flags |= HU_FRAME_LAST_FRAME

            header = struct.pack(">BBH", chan, flags, len(chunk))
            if first and not (flags & HU_FRAME_LAST_FRAME):
                header += struct.pack(">I", total)

            frame = header + chunk
            try:
                self._transport.write(frame)
            except Exception as e:
                log.error("USB write error: %s", e)
                return -1

            sent  += len(chunk)
            first  = False
        return 0

    def _enc_send(self, retry: int, chan: int, payload: bytes) -> int:
        """Encrypt payload and send as ENCRYPTED frame(s).  Mirrors hu_aap_enc_send()."""
        if self._state != self.HU_STATE_STARTED:
            # Normal during hu_aap_shutdown() while reader drains last USB packet
            return -1

        msg_type = struct.unpack_from(">H", payload, 0)[0]
        base_flags = HU_FRAME_ENCRYPTED
        if (chan != AA_CH_CTR and 2 <= msg_type < 0x8000):
            base_flags |= HU_FRAME_CONTROL_MESSAGE

        total = len(payload)
        sent  = 0
        first = True
        while sent < total:
            chunk = payload[sent:sent + MAX_FRAME_PAYLOAD_SIZE]
            flags = base_flags
            if first:
                flags |= HU_FRAME_FIRST_FRAME
            if sent + len(chunk) >= total:
                flags |= HU_FRAME_LAST_FRAME

            ciphertext = self._ssl.encrypt(bytes(chunk))
            header = struct.pack(">BBH", chan, flags, len(ciphertext))
            if first and not (flags & HU_FRAME_LAST_FRAME):
                header += struct.pack(">I", total)

            frame = header + ciphertext
            try:
                # Input events must never block the whole link. Use short timeout and drop on stall.
                if chan == AA_CH_TOU and msg_type == HU_INPUT_InputEvent:
                    self._transport.write(frame, tmo=250)
                else:
                    self._transport.write(frame)
            except Exception as e:
                if chan == AA_CH_TOU and msg_type == HU_INPUT_InputEvent:
                    # Drop the input event if phone is temporarily not reading.
                    log.debug(
                        "InputEvent dropped due to USB write stall: %s (ch=%s cipher=%d)",
                        e,
                        chan_name(chan),
                        len(ciphertext),
                    )
                    return 0
                log.error(
                    "USB write error (enc): %s (ch=%s type=0x%04X plaintext=%d cipher=%d flags=0x%02X)",
                    e,
                    chan_name(chan),
                    msg_type,
                    len(chunk),
                    len(ciphertext),
                    flags,
                )
                return -1

            sent  += len(chunk)
            first  = False
        return 0

    def _enc_send_message(self, retry: int, chan: int,
                          msg_type: int, proto_msg) -> int:
        """Serialize proto_msg + msg_type prefix and send encrypted.
        Mirrors hu_aap_enc_send_message()."""
        body = proto_msg.SerializeToString()
        payload = struct.pack(">H", msg_type) + body
        return self._enc_send(retry, chan, payload)

    def _unenc_send_blob(self, retry: int, chan: int,
                         msg_type: int, raw: bytes) -> int:
        """Send raw bytes with msg_type prefix, unencrypted.
        Mirrors hu_aap_unenc_send_blob()."""
        payload = struct.pack(">H", msg_type) + raw
        return self._unenc_send(retry, chan, payload)

    def _unenc_send_message(self, retry: int, chan: int,
                            msg_type: int, proto_msg) -> int:
        payload = struct.pack(">H", msg_type) + proto_msg.SerializeToString()
        return self._unenc_send(retry, chan, payload)

    # ── Reader / receive loop ─────────────────────────────────────────────

    def _reader_loop(self) -> None:
        """Background thread: continuously call _recv_process().
        Mirrors gartnera hu_thread_main()."""
        log.info("Reader thread started.")
        silent = 0
        while not self._stop_event.is_set() and self._state == self.HU_STATE_STARTED:
            try:
                ret = self._recv_process(tmo=2000)
                if ret == 0 and self._state == self.HU_STATE_STARTED:
                    # timeout (no data)
                    silent += 1
                    if silent % 5 == 0:
                        log.debug("USB: нет данных %d с…", silent * 2)
                    continue
                silent = 0
                if ret < 0:
                    if self._state == self.HU_STATE_STARTED:
                        log.error("recv_process error: %d", ret)
                        self.hu_aap_stop()
                    break
            except usb.core.USBTimeoutError:
                silent += 1
                if silent % 5 == 0:
                    log.debug("USB: нет данных %d с…", silent * 2)
            except Exception as e:
                if self._state == self.HU_STATE_STARTED:
                    log.error("Reader error: %s", e)
                    self.hu_aap_stop()
                break
        log.info("Reader thread exited.")

    def _recv_process(self, tmo: int) -> int:
        """
        Read one USB bulk transfer, feed it to the frame decoder, and process
        any complete messages found.  Returns 0 on timeout, 1 on data, -1 on error.

        USB bulk transfers deliver the complete packet at once — we cannot read
        partial bytes.  We use _FrameDecoder to split the stream into frames.
        """
        try:
            raw = self._transport.read(MAX_FRAME_SIZE, tmo)
        except usb.core.USBTimeoutError:
            return 0
        except usb.core.USBError as e:
            if e.errno in (110, None) and "timed out" in str(e).lower():
                return 0
            raise
        if not raw:
            return 0

        log.debug("USB ← %d байт: %s…", len(raw), raw[:16].hex())

        frames = self._decoder.feed(raw)
        for chan, flags, payload in frames:
            ret = self._process_frame(chan, flags, payload)
            if ret < 0:
                return ret
        return 1

    def _process_frame(self, chan: int, flags: int, payload: bytes) -> int:
        """Process one decoded frame.  Decrypt if needed, reassemble multi-frame messages."""
        log.debug("FRAME ch=%s flags=0x%02X payload=%d",
                  chan_name(chan), flags, len(payload))

        if flags & HU_FRAME_ENCRYPTED:
            if not self._ssl or not self._ssl.is_ready:
                # SSL handshake frame during STARTIN
                out = self._ssl.feed(payload) if self._ssl else b""
                if out:
                    log.debug("SSL TX: %d байт", len(out))
                    self._send_ssl_handshake(out)
                if self._ssl and self._ssl.is_ready:
                    self._on_ssl_complete()
                return 0
            else:
                data = self._ssl.decrypt(payload)
        else:
            data = payload

        # Reassemble multi-frame messages per channel
        if flags & HU_FRAME_FIRST_FRAME:
            self._asm_bufs[chan] = bytearray(data)
        else:
            self._asm_bufs.setdefault(chan, bytearray()).extend(data)

        if not (flags & HU_FRAME_LAST_FRAME):
            return 0   # still accumulating

        asm = bytes(self._asm_bufs.pop(chan, b""))
        if len(asm) < 2:
            return 0

        msg_type = struct.unpack_from(">H", asm, 0)[0]
        body     = asm[2:]
        return self._iaap_msg_process(chan, msg_type, body)

    # ── Message dispatch (gartnera iaap_msg_process) ──────────────────────

    def _iaap_msg_process(self, chan: int, msg_type: int, body: bytes) -> int:
        log.debug("MSG ch=%s type=0x%04X len=%d",
                  chan_name(chan), msg_type, len(body))

        if self._state == self.HU_STATE_STARTIN:
            if msg_type == HU_INIT_VersionResponse:
                return self._handle_version_response(chan, body)
            elif msg_type == HU_INIT_SSLHandshake:
                return self._handle_ssl_handshake(chan, body)
            else:
                log.warning("STARTIN: unknown msg_type=0x%04X (dropping)", msg_type)
                return 0

        # STARTED state
        is_control = msg_type < 0x8000
        if is_control:
            if msg_type == HU_MSG_MediaDataWithTimestamp:
                return self._handle_media_data_ts(chan, body)
            elif msg_type == HU_MSG_MediaData:
                return self._handle_media_data(chan, body)
            elif msg_type == HU_MSG_ServiceDiscoveryRequest:
                return self._handle_service_discovery(chan, body)
            elif msg_type == HU_MSG_ChannelOpenRequest:
                return self._handle_channel_open(chan, body)
            elif msg_type == HU_MSG_PingRequest:
                return self._handle_ping(chan, body)
            elif msg_type == HU_MSG_NavigationFocusRequest:
                return self._handle_nav_focus(chan, body)
            elif msg_type == HU_MSG_ShutdownRequest:
                return self._handle_shutdown(chan, body)
            elif msg_type == HU_MSG_ShutdownResponse:
                return self._handle_shutdown_response(chan, body)
            elif msg_type == HU_MSG_VoiceSessionRequest:
                return self._handle_voice_session(chan, body)
            elif msg_type == HU_MSG_AudioFocusRequest:
                return self._handle_audio_focus(chan, body)
            else:
                log.warning("Unknown control msg_type=0x%04X ch=%s", msg_type, chan_name(chan))
                return 0

        # Non-control: route by channel
        if chan == AA_CH_SEN:
            if msg_type == HU_SENSOR_SensorStartRequest:
                return self._handle_sensor_start(chan, body)
        elif chan == AA_CH_TOU:
            if msg_type == HU_INPUT_BindingRequest:
                return self._handle_binding(chan, body)
        elif chan == AA_CH_BT:
            if msg_type == HU_BT_PairingRequest:
                return self._handle_bt_pairing(chan, body)
            elif msg_type == HU_BT_AuthData:
                return self._handle_bt_auth(chan, body)
        elif chan == AA_CH_PSTAT:
            if msg_type == HU_PSTAT_PhoneStatus:
                return self._handle_phone_status(chan, body)
        elif chan == AA_CH_NAVI:
            if msg_type == HU_NAVI_Status:
                return self._handle_navi_status(chan, body)
            # Some phones/AA versions use shifted NAV msg_type values (observed 0x8006/0x8007).
            elif msg_type in (HU_NAVI_Turn, 0x8006):
                return self._handle_navi_turn(chan, body)
            elif msg_type in (HU_NAVI_TurnDistance, 0x8007):
                return self._handle_navi_distance(chan, body)
        elif chan in (AA_CH_AUD, AA_CH_AU1, AA_CH_AU2, AA_CH_VID, AA_CH_MIC):
            if msg_type == HU_MEDIA_MediaSetupRequest:
                return self._handle_media_setup(chan, body)
            elif msg_type == HU_MEDIA_MediaStartRequest:
                return self._handle_media_start(chan, body)
            elif msg_type == HU_MEDIA_MediaStopRequest:
                return self._handle_media_stop(chan, body)
            elif msg_type == HU_MEDIA_MediaAck:
                return self._handle_media_ack(chan, body)
            elif msg_type == HU_MEDIA_MicRequest:
                return self._handle_mic_request(chan, body)
            elif msg_type == HU_MEDIA_VideoFocusRequest:
                return self._handle_video_focus(chan, body)

        log.warning("Unhandled ch=%s msg_type=0x%04X", chan_name(chan), msg_type)
        return 0

    # ── Init handlers ─────────────────────────────────────────────────────

    def _send_version_request(self) -> int:
        """Send VersionRequest (unencrypted).  gartnera: vr_buf={0,1,0,1} → протокол 1.1"""
        # Payload after msg_type: major(2B BE) + minor(2B BE)
        vr_payload = struct.pack(">HH", self._vr_major, self._vr_minor)
        ret = self._unenc_send_blob(0, AA_CH_CTR, HU_INIT_VersionRequest, vr_payload)
        if ret < 0:
            return -1
        log.info("VersionRequest %d.%d → телефон", self._vr_major, self._vr_minor)
        return 0

    def _handle_version_response(self, chan: int, body: bytes) -> int:
        if len(body) >= 4:
            major, minor = struct.unpack_from(">HH", body, 0)
            extra = len(body) - 4
            log.info(
                "VersionResponse от телефона: %d.%d (%d байт%s)",
                major, minor, len(body), f", +{extra} доп." if extra else "",
            )
            if extra:
                log.debug("VersionResponse после major/minor: %s", body[4:].hex())
            # Только «разумные» значения; иначе возможна другая кодировка тела — не затираем _remote_proto мусором.
            if major == 1 and 1 <= minor <= 48:
                self._remote_proto_major = major
                self._remote_proto_minor = minor
            else:
                log.warning(
                    "VersionResponse необычные major/minor %d.%d — оставляю %d.%d (hex=%s)",
                    major, minor,
                    self._remote_proto_major, self._remote_proto_minor,
                    body[: min(48, len(body))].hex(),
                )
        else:
            log.warning(
                "VersionResponse короткий (%d байт), оставляю %d.%d",
                len(body), self._remote_proto_major, self._remote_proto_minor,
            )
        # Begin SSL handshake (gartnera hu_ssl_begin_handshake)
        client_hello = self._ssl.begin_handshake()
        if client_hello:
            self._send_ssl_handshake(client_hello)
            log.info("TLS ClientHello → телефон (%d байт)", len(client_hello))
        return 0

    def _handle_ssl_handshake(self, chan: int, body: bytes) -> int:
        """Process incoming SSL handshake bytes.  Mirrors hu_handle_SSLHandshake()."""
        log.debug("SSL RX: %d байт TLS", len(body))
        out = self._ssl.feed(body)
        if out:
            self._send_ssl_handshake(out)
            log.debug("SSL TX: %d байт TLS", len(out))
        if self._ssl.is_ready:
            self._on_ssl_complete()
        return 0

    def _send_ssl_handshake(self, tls_bytes: bytes) -> None:
        """Send raw TLS bytes as an unencrypted SSLHandshake frame."""
        self._unenc_send_blob(0, AA_CH_CTR, HU_INIT_SSLHandshake, tls_bytes)

    def _on_ssl_complete(self) -> None:
        """Called when TLS handshake completes.  Send AuthComplete and enter STARTED."""
        log.info("SSL активен — отправляю AUTH_COMPLETE.")
        from proto_gen import hu_pb2 as HU
        auth = HU.AuthCompleteResponse()
        auth.status = HU.STATUS_OK
        # gartnera sends AuthComplete as UNENCRYPTED (during STARTIN)
        self._unenc_send_message(0, AA_CH_CTR, HU_INIT_AuthComplete, auth)
        # Transition immediately: phone does NOT send AUTH_COMPLETE back.
        # It goes directly to ServiceDiscoveryRequest in encrypted frames.
        self._state = self.HU_STATE_STARTED
        log.info("Состояние → STARTED")

    # ── Control channel handlers ─────────────────────────────────────────

    def _handle_service_discovery(self, chan: int, body: bytes) -> int:
        from proto_gen import hu_pb2 as HU
        req = HU.ServiceDiscoveryRequest()
        try:
            req.ParseFromString(body)
            log.info("ServiceDiscoveryRequest: phone_name=%r", req.phone_name)
        except Exception as e:
            log.info("ServiceDiscoveryRequest parse issue (continuing): %s", e)

        car = HU.ServiceDiscoveryResponse()
        car.head_unit_name = "AA Receiver"
        car.car_model      = "Raspberry"
        car.car_year       = str(datetime.now().year)
        car.car_serial     = "0001"
        car.driver_pos     = self._driver_pos
        # Как в gartnera: sw_version / sw_build не привязаны к номеру протокола (там "SWV1"/"SWB1").
        car.sw_version = self._sw_version
        car.sw_build   = self._sw_build
        if self._cert_type in ("jaguar", "lr"):
            car.headunit_make  = "Jaguar Land Rover"
            car.headunit_model = "InControl Touch Pro"
        else:
            car.headunit_make  = "OpenSource"
            car.headunit_model = "AAReceiver"
        car.can_play_native_media_during_vr = False
        car.hide_clock     = False

        # Input channel (AA_CH_TOU = 1)
        ch = car.channels.add()
        ch.channel_id = AA_CH_TOU
        inner = ch.input_event_channel
        ts = inner.touch_screen_config
        ts.width  = self._video_w
        ts.height = self._video_h
        for kc in (HUIB_MENU, HUIB_MIC1, HUIB_HOME, HUIB_BACK, HUIB_PHONE, HUIB_CALLEND,
                   HUIB_UP, HUIB_DOWN, HUIB_LEFT, HUIB_RIGHT, HUIB_ENTER,
                   HUIB_MIC, HUIB_PLAYPAUSE, HUIB_NEXT, HUIB_PREV,
                   HUIB_MUSIC, HUIB_SCROLLWHEEL, HUIB_TEL, HUIB_NAVIGATION, HUIB_MEDIA,
                   HUIB_RADIO, HUIB_PRIMARY_BUTTON, HUIB_SECONDARY_BUTTON,
                   HUIB_TERTIARY_BUTTON, HUIB_START, HUIB_STOP):
            inner.keycodes_supported.append(kc)
        self._cb.customize_input_config(inner)

        # Sensor channel (AA_CH_SEN = 2)
        ch = car.channels.add()
        ch.channel_id = AA_CH_SEN
        inner = ch.sensor_channel
        inner.sensor_list.add().type = HU.SENSOR_TYPE_DRIVING_STATUS
        inner.sensor_list.add().type = HU.SENSOR_TYPE_NIGHT_DATA
        inner.sensor_list.add().type = HU.SENSOR_TYPE_LOCATION
        self._cb.customize_sensor_config(inner)

        # Navigation status channel (AA_CH_NAVI = 11)
        # Phone → HU: NAVMessagesStatus, NAVTurnMessage, NAVDistanceMessage.
        ch = car.channels.add()
        ch.channel_id = AA_CH_NAVI
        nav = ch.navigation_status_service
        # Conservative defaults: some phones show "head unit software version" warnings
        # when advertising custom images. Start with codes-only, then upgrade later.
        nav.minimum_interval_ms = 1000
        nav.type = HU.ChannelDescriptor.NavigationStatusService.IMAGE_CODES_ONLY

        # Video channel (AA_CH_VID = 3)
        ch = car.channels.add()
        ch.channel_id = AA_CH_VID
        inner = ch.output_stream_channel
        inner.type = HU.STREAM_TYPE_VIDEO
        vc = inner.video_configs.add()
        if self._video_preset_resolved is not None:
            Rw, Rh, vc.resolution = self._video_preset_resolved
            preset_note = " (пресет задан явно)"
        else:
            Rw, Rh, vc.resolution = _pick_video_mode_for_touch_ui(self._video_w, self._video_h)
            preset_note = ""
        vc.frame_rate = HU.ChannelDescriptor.OutputStreamChannel.VideoConfig.VIDEO_FPS_30
        vc.margin_width, vc.margin_height = _video_margins_for_mode(
            Rw, Rh, self._video_w, self._video_h
        )
        if self._video_dpi_explicit is not None:
            vc.dpi = self._video_dpi_explicit
            dpi_note = " (dpi задан явно)"
        else:
            vc.dpi = _video_dpi_for_touch(self._video_w, self._video_h, self._video_dpi_scale)
            dpi_note = ""
        inner.available_while_in_call = True
        log.info(
            "Видео: touch UI %dx%d → кадр режима %dx%d, margin_width=%d margin_height=%d dpi=%d%s%s",
            self._video_w,
            self._video_h,
            Rw,
            Rh,
            vc.margin_width,
            vc.margin_height,
            vc.dpi,
            preset_note,
            dpi_note,
        )

        # Media audio channel (AA_CH_AUD = 4, stereo 48 kHz)
        ch = car.channels.add()
        ch.channel_id = AA_CH_AUD
        inner = ch.output_stream_channel
        inner.type       = HU.STREAM_TYPE_AUDIO
        inner.audio_type = HU.AUDIO_TYPE_MEDIA
        ac = inner.audio_configs.add()
        ac.sample_rate   = 48000
        ac.bit_depth     = 16
        ac.channel_count = 2
        inner.available_while_in_call = True

        # Speech audio channel (AA_CH_AU1 = 5, mono 16 kHz)
        ch = car.channels.add()
        ch.channel_id = AA_CH_AU1
        inner = ch.output_stream_channel
        inner.type       = HU.STREAM_TYPE_AUDIO
        inner.audio_type = HU.AUDIO_TYPE_SPEECH
        ac = inner.audio_configs.add()
        ac.sample_rate   = 16000
        ac.bit_depth     = 16
        ac.channel_count = 1

        # Microphone channel (AA_CH_MIC = 7, mono 16 kHz, INPUT)
        ch = car.channels.add()
        ch.channel_id = AA_CH_MIC
        inner = ch.input_stream_channel
        inner.type = HU.STREAM_TYPE_AUDIO
        ac = inner.audio_config
        ac.sample_rate   = 16000
        ac.bit_depth     = 16
        ac.channel_count = 1

        self._cb.customize_car_info(car)

        ret = self._enc_send_message(0, chan, HU_MSG_ServiceDiscoveryResponse, car)
        log.info(
            "ServiceDiscoveryResponse → телефон (%d каналов), driver_pos=%s (%s)",
            len(car.channels),
            self._driver_pos,
            "RHD" if self._driver_pos else "LHD",
        )
        return ret

    def _handle_channel_open(self, chan: int, body: bytes) -> int:
        """
        Mirrors hu_handle_ChannelOpenRequest().
        After opening sensor channel, immediately sends initial SensorEvent.
        """
        from proto_gen import hu_pb2 as HU
        req = HU.ChannelOpenRequest()
        try:
            req.ParseFromString(body)
            log.info("ChannelOpenRequest ch=%s id=%d priority=%d",
                     chan_name(chan), req.id, req.priority)
        except Exception:
            log.info("ChannelOpenRequest ch=%s", chan_name(chan))

        resp = HU.ChannelOpenResponse()
        resp.status = HU.STATUS_OK
        ret = self._enc_send_message(0, chan, HU_MSG_ChannelOpenResponse, resp)
        if ret:
            return ret

        # After sensor channel open: send initial driving status (gartnera does this here)
        if chan == AA_CH_SEN:
            time.sleep(0.002)
            event = HU.SensorEvent()
            event.driving_status.add().status = \
                HU.SensorEvent.DrivingStatus.DRIVE_STATUS_UNRESTRICTED
            self._enc_send_message(0, AA_CH_SEN, HU_SENSOR_SensorEvent, event)
            log.info("SensorEvent(DRIVE_STATUS_UNRESTRICTED) → телефон")
        return ret

    def _handle_ping(self, chan: int, body: bytes) -> int:
        from proto_gen import hu_pb2 as HU
        req = HU.PingRequest()
        try:
            req.ParseFromString(body)
        except Exception:
            pass
        log.debug("PingRequest ch=%s ts=%s", chan_name(chan), getattr(req, "timestamp", "?"))
        resp = HU.PingResponse()
        resp.timestamp = req.timestamp
        return self._enc_send_message(0, chan, HU_MSG_PingResponse, resp)

    def _handle_nav_focus(self, chan: int, body: bytes) -> int:
        from proto_gen import hu_pb2 as HU
        resp = HU.NavigationFocusResponse()
        resp.focus_type = 2  # gained
        return self._enc_send_message(0, chan, HU_MSG_NavigationFocusResponse, resp)

    # ── Navigation channel (AA_CH_NAVI) ───────────────────────────────────

    def _handle_navi_status(self, chan: int, body: bytes) -> int:
        from proto_gen import hu_pb2 as HU
        msg = HU.NAVMessagesStatus()
        try:
            msg.ParseFromString(body)
        except Exception:
            pass
        st = int(getattr(msg, "status", 0) or 0)
        st_name = HU.NAVMessagesStatus.STATUS.Name(st) if st else "UNKNOWN"
        navi_log.info("navi status=%s(%d)", st_name, st)
        try:
            self._cb.navigation_status(st)
        except Exception:
            pass
        return 0

    def _handle_navi_turn(self, chan: int, body: bytes) -> int:
        from proto_gen import hu_pb2 as HU
        msg = HU.NAVTurnMessage()
        try:
            msg.ParseFromString(body)
        except Exception:
            pass
        img = bytes(getattr(msg, "image", b"") or b"")
        side = int(getattr(msg, "turn_side", 0) or 0)
        ev = int(getattr(msg, "turn_event", 0) or 0)
        side_name = HU.NAVTurnMessage.TURN_SIDE.Name(side) if side else "UNKNOWN"
        ev_name = HU.NAVTurnMessage.TURN_EVENT.Name(ev) if ev or ev == 0 else "UNKNOWN"
        road = _sanitize_nav_text(getattr(msg, "event_name", ""))
        navi_log.info(
            "navi turn road=%r side=%s(%d) event=%s(%d) exit=%s angle=%s image=%dB",
            road,
            side_name,
            side,
            ev_name,
            ev,
            getattr(msg, "turn_number", None),
            getattr(msg, "turn_angle", None),
            len(img),
        )
        if img:
            # Make it easy to spot changes in arrow art.
            navi_log.debug("navi turn png_head=%s", img[:16].hex())
            self._cb.navigation_turn_image(img)
        try:
            self._cb.navigation_turn(msg)
        except Exception:
            pass
        return 0

    def _handle_navi_distance(self, chan: int, body: bytes) -> int:
        from proto_gen import hu_pb2 as HU
        msg = HU.NAVDistanceMessage()
        try:
            msg.ParseFromString(body)
        except Exception:
            pass
        unit = int(getattr(msg, "display_distance_unit", 0) or 0)
        unit_name = HU.NAVDistanceMessage.DISPLAY_DISTANCE_UNIT.Name(unit) if unit else "UNKNOWN"
        disp_raw = int(getattr(msg, "display_distance", 0) or 0)
        if unit == HU.NAVDistanceMessage.METERS:
            disp_h = f"{disp_raw / 1000.0:.0f} m"
        elif unit in (HU.NAVDistanceMessage.KILOMETERS, HU.NAVDistanceMessage.KILOMETERS10):
            disp_h = f"{disp_raw / 1000.0:.1f} km"
        elif unit in (HU.NAVDistanceMessage.MILES, HU.NAVDistanceMessage.MILES10):
            disp_h = f"{disp_raw / 1000.0:.1f} mi"
        elif unit == HU.NAVDistanceMessage.FEET:
            disp_h = f"{disp_raw / 1000.0:.0f} ft"
        else:
            disp_h = str(disp_raw)
        navi_log.info(
            "navi distance meters=%s eta_s=%s display=%s unit=%s(%d)",
            getattr(msg, "distance", None),
            getattr(msg, "time_until", None),
            disp_h,
            unit_name,
            unit,
        )
        try:
            self._cb.navigation_distance(msg)
        except Exception:
            pass
        return 0

    def _handle_shutdown(self, chan: int, body: bytes) -> int:
        from proto_gen import hu_pb2 as HU
        req = HU.ShutdownRequest()
        try:
            req.ParseFromString(body)
            log.warning("ShutdownRequest reason=%d", req.reason)
        except Exception:
            log.warning("ShutdownRequest (parse error)")
        resp = HU.ShutdownResponse()
        self._enc_send_message(0, chan, HU_MSG_ShutdownResponse, resp)
        time.sleep(0.1)
        self.hu_aap_stop()
        return -1

    def _handle_shutdown_response(self, chan: int, body: bytes) -> int:
        """Phone acknowledges our ShutdownRequest — no reply needed."""
        log.info("ShutdownResponse от телефона (канал %s)", chan_name(chan))
        return 0

    def _handle_voice_session(self, chan: int, body: bytes) -> int:
        log.debug("VoiceSessionRequest (ignored)")
        return 0

    def _handle_audio_focus(self, chan: int, body: bytes) -> int:
        """
        Mirrors hu_handle_AudioFocusRequest().
        Delegates to callback which must call send_audio_focus_response().
        """
        from proto_gen import hu_pb2 as HU
        req = HU.AudioFocusRequest()
        try:
            req.ParseFromString(body)
            log.info("AudioFocusRequest ch=%s focus_type=%d",
                     chan_name(chan), req.focus_type)
        except Exception:
            log.info("AudioFocusRequest (parse error)")
        self._cb.audio_focus_request(chan, req)
        return 0

    def send_audio_focus_response(self, chan: int, focus_state: int) -> int:
        """Called by app layer to grant audio focus.  gartnera callback style."""
        from proto_gen import hu_pb2 as HU
        resp = HU.AudioFocusResponse()
        resp.focus_type = focus_state
        log.info("AudioFocusResponse(state=%d) → телефон", focus_state)
        return self._enc_send_message(0, chan, HU_MSG_AudioFocusResponse, resp)

    def _send_shutdown_request(self) -> None:
        from proto_gen import hu_pb2 as HU
        req = HU.ShutdownRequest()
        req.reason = HU.ShutdownRequest.REASON_QUIT
        self._enc_send_message(0, AA_CH_CTR, HU_MSG_ShutdownRequest, req)

    # ── Sensor channel ────────────────────────────────────────────────────

    def _handle_sensor_start(self, chan: int, body: bytes) -> int:
        from proto_gen import hu_pb2 as HU
        req = HU.SensorStartRequest()
        try:
            req.ParseFromString(body)
            log.info("SensorStartRequest type=%d", req.type)
        except Exception:
            pass
        resp = HU.SensorStartResponse()
        resp.status = HU.STATUS_OK
        return self._enc_send_message(0, chan, HU_SENSOR_SensorStartResponse, resp)

    # ── Input channel ─────────────────────────────────────────────────────

    def _handle_binding(self, chan: int, body: bytes) -> int:
        from proto_gen import hu_pb2 as HU
        req = HU.BindingRequest()
        try:
            req.ParseFromString(body)
            log.info("BindingRequest scan_codes=%d", req.scan_codes_size())
        except Exception:
            pass
        resp = HU.BindingResponse()
        resp.status = HU.STATUS_OK
        return self._enc_send_message(0, chan, HU_INPUT_BindingResponse, resp)

    # ── Media/video/audio channels ────────────────────────────────────────

    def _handle_media_setup(self, chan: int, body: bytes) -> int:
        from proto_gen import hu_pb2 as HU
        req = HU.MediaSetupRequest()
        try:
            req.ParseFromString(body)
            log.info("MediaSetupRequest ch=%s type=%d", chan_name(chan), req.type)
        except Exception:
            pass
        resp = HU.MediaSetupResponse()
        resp.media_status = HU.MediaSetupResponse.MEDIA_STATUS_2
        # Lower ACK rate to avoid clogging USB OUT (touch/input shares same pipe).
        # Values chosen empirically; phone should honor this window.
        if chan == AA_CH_VID:
            resp.max_unacked = 32
            self._channel_ack_every[chan] = 8
        elif chan in (AA_CH_AUD, AA_CH_AU1, AA_CH_AU2):
            resp.max_unacked = 16
            self._channel_ack_every[chan] = 4
        else:
            resp.max_unacked = 8
            self._channel_ack_every[chan] = 2
        resp.configs.append(0)
        ret = self._enc_send_message(0, chan, HU_MEDIA_MediaSetupResponse, resp)
        if not ret:
            self._cb.media_setup_complete(chan)
        return ret

    def _handle_media_start(self, chan: int, body: bytes) -> int:
        from proto_gen import hu_pb2 as HU
        req = HU.MediaStartRequest()
        try:
            req.ParseFromString(body)
            self._channel_session_id[chan] = req.session
            self._channel_ack_value[chan] = 0
            log.info("MediaStartRequest ch=%s session=%d", chan_name(chan), req.session)
        except Exception:
            pass
        return self._cb.media_start(chan)

    def _handle_media_stop(self, chan: int, body: bytes) -> int:
        log.info("MediaStopRequest ch=%s", chan_name(chan))
        self._channel_session_id[chan] = 0
        self._channel_ack_value[chan] = 0
        return self._cb.media_stop(chan)

    def _send_media_ack(self, chan: int) -> int:
        """Send MediaAck; return 0 if shutting down (no ACK expected)."""
        from proto_gen import hu_pb2 as HU
        # Always count every packet, but ACK less frequently.
        self._channel_ack_value[chan] = (self._channel_ack_value[chan] + 1) & 0xFFFFFFFF
        v = self._channel_ack_value[chan]
        every = max(1, int(self._channel_ack_every[chan] or 1))
        if v != 1 and (v % every) != 0:
            return 0

        ack = HU.MediaAck()
        ack.session = self._channel_session_id[chan]
        ack.value = v
        r = self._enc_send_message(0, chan, HU_MEDIA_MediaAck, ack)
        if r < 0 and self._state != self.HU_STATE_STARTED:
            return 0
        return r

    def _handle_media_data_ts(self, chan: int, body: bytes) -> int:
        if len(body) < 8:
            return -1
        ts  = struct.unpack_from(">Q", body, 0)[0]
        ret = self._cb.media_packet(chan, ts, body[8:])
        if ret < 0:
            return ret
        return self._send_media_ack(chan)

    def _handle_media_data(self, chan: int, body: bytes) -> int:
        ret = self._cb.media_packet(chan, 0, body)
        if ret < 0:
            return ret
        return self._send_media_ack(chan)

    def _handle_media_ack(self, chan: int, body: bytes) -> int:
        return 0

    def _handle_mic_request(self, chan: int, body: bytes) -> int:
        from proto_gen import hu_pb2 as HU
        req = HU.MicRequest()
        try:
            req.ParseFromString(body)
        except Exception:
            pass
        if not req.open:
            log.info("MicRequest STOP ch=%s", chan_name(chan))
            return self._cb.media_stop(chan)
        else:
            log.info("MicRequest START ch=%s", chan_name(chan))
            return self._cb.media_start(chan)

    def _handle_video_focus(self, chan: int, body: bytes) -> int:
        from proto_gen import hu_pb2 as HU
        req = HU.VideoFocusRequest()
        try:
            req.ParseFromString(body)
        except Exception:
            pass
        self._cb.video_focus_request(chan, req)
        return 0

    def send_video_focus(self, chan: int, focused: bool) -> int:
        from proto_gen import hu_pb2 as HU
        vf = HU.VideoFocus()
        vf.mode         = HU.VIDEO_FOCUS_MODE_FOCUSED if focused else HU.VIDEO_FOCUS_MODE_UNFOCUSED
        vf.unrequested  = False
        return self._enc_send_message(0, chan, HU_MEDIA_VideoFocus, vf)

    # ── Bluetooth / Phone-status ──────────────────────────────────────────

    def _handle_bt_pairing(self, chan: int, body: bytes) -> int:
        from proto_gen import hu_pb2 as HU
        resp = HU.BluetoothPairingResponse()
        resp.already_paired = True
        resp.status = HU.BluetoothPairingResponse.PAIRING_STATUS_1
        return self._enc_send_message(0, chan, HU_BT_PairingResponse, resp)

    def _handle_bt_auth(self, chan: int, body: bytes) -> int:
        log.debug("BluetoothAuthData received")
        return 0

    def _handle_phone_status(self, chan: int, body: bytes) -> int:
        log.debug("PhoneStatus received")
        return 0
