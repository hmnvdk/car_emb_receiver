"""
Android Auto Head Unit — AA2
Python port of gartnera/headunit.

Usage:
    ./run.sh [--cert jaguar|lr] [--debug] [--video-debug] [--no-audio] [-W W] [-H H] [-r WxH]
             [--video-scale stretch|letterbox] [--video-preset WxH|auto] [--touch-mode auto|native]
             [--dpi N] [--dpi-scale F] [--driver-position lhd|rhd] [--proto-major N] [--proto-minor N]
             [--sw-version S] [--sw-build S] [--nav-only]

Роли флагов (коротко):
  -r / -W -H     — размер окна pygame и база координат тача на экране.
  --touch-mode  — какие размеры touch_screen слать в ServiceDiscovery (native = как окно; auto = в
                  портрете часто max×min «альбом» для совместимости телефона).
  --video-preset — только enum разрешения потока H.264 в ServiceDiscovery; окно не меняет.
                  Если совпадает с -r, можно не указывать — автоподбор часто выберет то же.
  --video-scale — только отрисовка: letterbox или stretch кадра в окне.
  --dpi         — явный VideoConfig.dpi (если не задан — расчёт по площади × --dpi-scale).
  --dpi-scale   — множитель к формуле dpi (по умолчанию 0.8); при --dpi не используется.
  --driver-position — lhd/rhd; в proto driver_pos: False=LHD, True=RHD.
  Портрет 1080×1920 — на практике стабильно до 240 dpi (см. README, hu_aap.VIDEO_DPI_PORTRAIT_1080X1920_MAX).
"""
import argparse
import logging
import math
import queue
import signal
import struct
import sys
import threading
import time
from dataclasses import dataclass
import re

import pygame

try:
    import av
except ImportError:
    av = None  # type: ignore

from proto_gen import hu_pb2 as HU

from hu_aap import (
    DEFAULT_VIDEO_DPI_SCALE,
    HUServer,
    IHUCallbacks,
    VIDEO_DPI_EXPLICIT_MAX,
    VIDEO_DPI_EXPLICIT_MIN,
    VIDEO_DPI_PORTRAIT_1080X1920_MAX,
    resolve_video_preset,
)
from hu_const import (
    AA_CH_VID,
    AA_CH_AUD,
    AA_CH_AU1,
    AA_CH_MIC,
    AA_CH_TOU,
    HUIB_BACK,
    HUIB_HOME,
    HUIB_ENTER,
    HUIB_UP,
    HUIB_DOWN,
    HUIB_LEFT,
    HUIB_RIGHT,
)

log = logging.getLogger("main")
log_video = logging.getLogger("video")


def _configure_video_logging(video_debug: bool, nav_only: bool, root_debug: bool) -> None:
    """
    Логгер video: при --nav-only корень WARNING; без --debug корень INFO — в обоих случаях
    DEBUG от дочернего логгера до root не доходит. Если нужен --video-debug, вешаем свой handler.
    """
    vlog = log_video
    vlog.setLevel(logging.DEBUG if video_debug else logging.INFO)
    need_own_handler = video_debug and (nav_only or not root_debug)
    if need_own_handler:
        if not vlog.handlers:
            h = logging.StreamHandler(sys.stderr)
            h.setFormatter(
                logging.Formatter(
                    "%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%H:%M:%S",
                )
            )
            vlog.addHandler(h)
        vlog.propagate = False
    else:
        vlog.propagate = True

# ── Display (по умолчанию; фактические W/H задаются флагами --width/--height) ─

DEFAULT_VIDEO_W = 800
DEFAULT_VIDEO_H = 480

_TOUCH_PRESS = HU.TouchInfo.TOUCH_ACTION.TOUCH_ACTION_PRESS
_TOUCH_RELEASE = HU.TouchInfo.TOUCH_ACTION.TOUCH_ACTION_RELEASE
_TOUCH_DRAG = HU.TouchInfo.TOUCH_ACTION.TOUCH_ACTION_DRAG

_CLIMATE_LONG_TAP_S = 0.75
_CLIMATE_TOUCH_MOVE_TOL_PX = 36
_CLIMATE_ALPHA = 232

_AA_KEYMAP = {
    pygame.K_ESCAPE: HUIB_BACK,
    pygame.K_BACKSPACE: HUIB_BACK,
    pygame.K_HOME: HUIB_HOME,
    pygame.K_RETURN: HUIB_ENTER,
    pygame.K_UP: HUIB_UP,
    pygame.K_DOWN: HUIB_DOWN,
    pygame.K_LEFT: HUIB_LEFT,
    pygame.K_RIGHT: HUIB_RIGHT,
}


def _avcc_to_annex_b(avcc: bytes):
    """
    ISO/IEC 14496-15 AVCDecoderConfigurationRecord → Annex B SPS/PPS NALs.
    First video chunk from the phone is often this blob (e.g. 29 bytes), not a NAL.
    """
    if len(avcc) < 7 or avcc[0] != 1:
        return None
    out = bytearray()
    pos = 6
    num_sps = avcc[5] & 0x1F
    for _ in range(num_sps):
        if pos + 2 > len(avcc):
            return None
        ln = struct.unpack_from(">H", avcc, pos)[0]
        pos += 2
        if pos + ln > len(avcc) or ln > 65535:
            return None
        out.extend(b"\x00\x00\x00\x01")
        out.extend(avcc[pos : pos + ln])
        pos += ln
    if pos >= len(avcc):
        return bytes(out) if out else None
    num_pps = avcc[pos]
    pos += 1
    for _ in range(num_pps):
        if pos + 2 > len(avcc):
            return None
        ln = struct.unpack_from(">H", avcc, pos)[0]
        pos += 2
        if pos + ln > len(avcc):
            return None
        out.extend(b"\x00\x00\x00\x01")
        out.extend(avcc[pos : pos + ln])
        pos += ln
    return bytes(out) if out else None


def h264_to_annex_b(data: bytes) -> bytes:
    """
    FFmpeg's h264 demuxer expects Annex B (start codes).

    Payloads may be: Annex B already, AVCDecoderConfigurationRecord (avcC),
    or repeated [4-byte BE length][NAL] (AVCC length-prefixed access units).
    """
    if not data:
        return data
    if data.startswith(b"\x00\x00\x00\x01") or data.startswith(b"\x00\x00\x01"):
        return data
    avcc = _avcc_to_annex_b(data)
    if avcc:
        return avcc
    # Repeated [4-byte BE length][nal] (AVCC sample format)
    pos = 0
    out = bytearray()
    while pos + 4 <= len(data):
        ln = struct.unpack_from(">I", data, pos)[0]
        if ln == 0 or ln > len(data) - pos - 4 or ln > 4 * 1024 * 1024:
            break
        out.extend(b"\x00\x00\x00\x01")
        out.extend(data[pos + 4 : pos + 4 + ln])
        pos += 4 + ln
    if out and pos == len(data):
        return bytes(out)
    # Some stacks use 16-bit BE length + NAL (not 32-bit)
    pos = 0
    out2 = bytearray()
    while pos + 2 <= len(data):
        ln = struct.unpack_from(">H", data, pos)[0]
        if ln < 1 or pos + 2 + ln > len(data) or ln > 65534:
            break
        out2.extend(b"\x00\x00\x00\x01")
        out2.extend(data[pos + 2 : pos + 2 + ln])
        pos += 2 + ln
    if out2 and pos == len(data):
        return bytes(out2)
    return b"\x00\x00\x00\x01" + data


def _letterbox_to_window(src: pygame.Surface, win_w: int, win_h: int):
    """
    Вписать кадр в окно без искажения (альбом остаётся альбомом).
    Возвращает (surface win_w×win_h, (ox, oy, dw, dh) для маппинга тача).
    """
    fw, fh = src.get_size()
    if fw <= 0 or fh <= 0 or win_w <= 0 or win_h <= 0:
        return src, (0, 0, max(1, win_w), max(1, win_h))
    scale = min(win_w / fw, win_h / fh)
    dw = max(1, int(round(fw * scale)))
    dh = max(1, int(round(fh * scale)))
    ox = (win_w - dw) // 2
    oy = (win_h - dh) // 2
    if fw == dw and fh == dh and ox == 0 and oy == 0:
        return src, (ox, oy, dw, dh)
    dst = pygame.Surface((win_w, win_h))
    dst.fill((12, 12, 24))
    scaler = getattr(pygame.transform, "smoothscale", pygame.transform.scale)
    scaled = scaler(src, (dw, dh)) if (dw, dh) != (fw, fh) else src
    dst.blit(scaled, (ox, oy))
    return dst, (ox, oy, dw, dh)


def _stretch_to_window(src: pygame.Surface, win_w: int, win_h: int):
    """
    Растянуть кадр на всё окно (как многие head unit, в т.ч. ультраширокий альбом).
    Без полей; пропорции могут исказиться. Тач маппится на весь прямоугольник окна.
    """
    fw, fh = src.get_size()
    if fw <= 0 or fh <= 0 or win_w <= 0 or win_h <= 0:
        return src, (0, 0, max(1, win_w), max(1, win_h))
    if fw == win_w and fh == win_h:
        return src, (0, 0, win_w, win_h)
    scaler = getattr(pygame.transform, "smoothscale", pygame.transform.scale)
    scaled = scaler(src, (win_w, win_h))
    return scaled, (0, 0, win_w, win_h)


def _fit_video_to_window(
    src: pygame.Surface, win_w: int, win_h: int, mode: str
):
    """
    mode: letterbox — вписать с полями; stretch — на весь экран (по умолчанию для широких панелей).
    """
    if mode == "stretch":
        return _stretch_to_window(src, win_w, win_h)
    return _letterbox_to_window(src, win_w, win_h)


class AADisplay:
    """
    input_sink — объект с методами send_touch(x,y,action), send_key(code) (как AppCallbacks);
    action для тача — значения HU.TouchInfo.TOUCH_ACTION (0 press, 1 release, 2 drag).
    """

    def __init__(
        self,
        frame_queue: queue.Queue,
        stop_event: threading.Event,
        input_sink=None,
        width: int = DEFAULT_VIDEO_W,
        height: int = DEFAULT_VIDEO_H,
        decode_width: int | None = None,
        decode_height: int | None = None,
        proto_w: int | None = None,
        proto_h: int | None = None,
        video_scale: str = "stretch",
    ):
        self._q           = frame_queue
        self._stop        = stop_event
        self._input       = input_sink
        self._mouse_down  = False
        self._w           = int(width)
        self._h           = int(height)
        self._dec_w       = int(decode_width) if decode_width else self._w
        self._dec_h       = int(decode_height) if decode_height else self._h
        self._proto_w     = int(proto_w) if proto_w is not None else self._dec_w
        self._proto_h     = int(proto_h) if proto_h is not None else self._dec_h
        self._video_scale = video_scale if video_scale in ("stretch", "letterbox") else "stretch"
        self._vid_rect    = (0, 0, self._w, self._h)
        self._climate_visible = False
        self._climate_rect = pygame.Rect(0, 0, 0, 0)
        self._climate_close_rect = pygame.Rect(0, 0, 0, 0)
        self._climate_button_rects: dict[str, pygame.Rect] = {}
        self._climate_temp_c = 22
        self._climate_fan = 3
        self._climate_ac_on = True
        self._climate_recirc = False
        self._climate_mode_idx = 0
        self._climate_modes = ["AUTO", "FACE", "FEET", "DEFROST"]
        self._finger_down: dict[int, tuple[float, float]] = {}
        self._finger_pressed: set[int] = set()
        # Map pygame finger_id -> small pointer_id (0..9) like Android MotionEvent semantics.
        self._finger_pid: dict[int, int] = {}
        self._pid_free: list[int] = list(range(10))
        self._finger_last_seen_s: dict[int, float] = {}
        self._two_finger_since: float | None = None
        self._two_finger_start: dict[int, tuple[float, float]] = {}
        self._two_finger_latched = False

    def run(self) -> None:
        try:
            self._run_inner()
        except Exception:
            log.exception("Display thread crashed")
        finally:
            try:
                pygame.quit()
            except Exception:
                pass
        log.info("Display closed.")

    def _run_inner(self) -> None:
        pygame.init()
        # SDL2: окно и flip() должны быть в главном потоке — иначе на Linux/X11
        # часто пустое окно при том, что декодер отдаёт кадры.
        screen = pygame.display.set_mode((self._w, self._h), pygame.DOUBLEBUF)
        pygame.display.set_caption("Android Auto")
        clock   = pygame.time.Clock()
        if av is None:
            log.error("Нужен PyAV для видео: pip install av")
            return
        try:
            import numpy  # noqa: F401 — PyAV frame.to_ndarray() требует numpy
        except ImportError:
            log.error(
                "Для декодирования H.264 нужен numpy (frame.to_ndarray). "
                "Установите: pip install numpy  (или из каталога AA2: pip install -r requirements.txt)"
            )
            return
        decoder = _CodecH264Decoder(self._dec_w, self._dec_h)
        frames_shown = 0
        last_frame = None  # последний кадр — иначе при пустой очереди один fill → мерцание
        nals_consumed = 0
        last_vid_warn_t = time.monotonic()
        display_loop_start = time.monotonic()
        last_empty_usb_warn = 0.0
        log_video.info(
            "дисплей: окно %dx%d decode %dx%d proto %dx%d scale=%s",
            self._w,
            self._h,
            self._dec_w,
            self._dec_h,
            self._proto_w,
            self._proto_h,
            self._video_scale,
        )
        try:
            running = True
            while running:
                if self._stop.is_set():
                    running = False
                    break
                try:
                    for ev in pygame.event.get():
                        if ev.type == pygame.QUIT:
                            self._stop.set()
                            running = False
                        elif ev.type == pygame.FINGERDOWN:
                            sx = int(ev.x * self._w)
                            sy = int(ev.y * self._h)
                            fid = int(ev.finger_id)
                            self._finger_down[fid] = (float(sx), float(sy))
                            self._finger_last_seen_s[fid] = time.monotonic()
                            if fid not in self._finger_pid:
                                self._finger_pid[fid] = (self._pid_free.pop(0) if self._pid_free else 0)
                            pid = self._finger_pid[fid]
                            if self._climate_visible and self._handle_climate_pointer_down(sx, sy):
                                continue
                            if self._input is not None and hasattr(self._input, "_touch_log"):
                                try:
                                    self._input._touch_log.debug(
                                        "touch←screen DOWN fid=%d pid=%d sx=%d sy=%d fingers=%d",
                                        fid, pid, sx, sy, len(self._finger_down)
                                    )
                                except Exception:
                                    pass
                            # Second+ finger (ладонь, шум тачскрина, pinch): не трогаем основной палец.
                            # Раньше здесь слали RELEASE всем — на портретном высоком окне второй контакт
                            # случается чаще, чем в альбоме, и тапы в навигатор срывались.
                            if len(self._finger_down) > 1:
                                continue

                            # Forward single-finger press to phone (skip if MOVE already synthesized PRESS).
                            if self._input is not None and len(self._finger_down) == 1 and not self._climate_visible:
                                if fid not in self._finger_pressed:
                                    self._finger_pressed.add(fid)
                                    x, y = self._display_to_proto(sx, sy)
                                    self._input.send_touch(x, y, _TOUCH_PRESS, pointer_id=pid)
                        elif ev.type == pygame.FINGERUP:
                            sx = int(ev.x * self._w)
                            sy = int(ev.y * self._h)
                            fid = int(ev.finger_id)
                            self._finger_down.pop(fid, None)
                            pid = self._finger_pid.pop(fid, 0)
                            self._finger_last_seen_s.pop(fid, None)
                            if pid in range(10) and pid not in self._pid_free:
                                self._pid_free.append(pid)
                                self._pid_free.sort()
                            if self._input is not None and hasattr(self._input, "_touch_log"):
                                try:
                                    self._input._touch_log.debug(
                                        "touch←screen UP fid=%d pid=%d sx=%d sy=%d fingers=%d",
                                        fid, pid, sx, sy, len(self._finger_down)
                                    )
                                except Exception:
                                    pass
                            if self._input is not None and fid in self._finger_pressed and not self._climate_visible:
                                x, y = self._display_to_proto(sx, sy)
                                self._input.send_touch(x, y, _TOUCH_RELEASE, pointer_id=pid)
                            self._finger_pressed.discard(fid)
                        elif ev.type == pygame.FINGERMOTION:
                            sx = int(ev.x * self._w)
                            sy = int(ev.y * self._h)
                            fid = int(ev.finger_id)
                            if fid not in self._finger_down:
                                self._finger_down[fid] = (float(sx), float(sy))
                            else:
                                self._finger_down[fid] = (float(sx), float(sy))
                            self._finger_last_seen_s[fid] = time.monotonic()
                            if fid not in self._finger_pid:
                                self._finger_pid[fid] = (self._pid_free.pop(0) if self._pid_free else 0)
                            # Forward single-finger drag to phone; ignore multi-touch (reserved for climate long-tap / future pinch).
                            if self._input is not None and len(self._finger_down) == 1 and not self._climate_visible:
                                pid = self._finger_pid.get(fid, 0)
                                # Some touch stacks emit MOVE without DOWN/UP; synthesize DOWN on first MOVE.
                                if fid not in self._finger_pressed:
                                    self._finger_pressed.add(fid)
                                    x0, y0 = self._display_to_proto(sx, sy)
                                    self._input.send_touch(x0, y0, _TOUCH_PRESS, pointer_id=pid)
                                if hasattr(self._input, "_touch_log"):
                                    try:
                                        self._input._touch_log.debug(
                                            "touch←screen MOVE fid=%d pid=%d sx=%d sy=%d",
                                            fid, pid, sx, sy
                                        )
                                    except Exception:
                                        pass
                                x, y = self._display_to_proto(sx, sy)
                                self._input.send_touch(x, y, _TOUCH_DRAG, pointer_id=pid)
                        elif self._input is not None:
                            if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                                if self._climate_visible and self._handle_climate_pointer_down(*ev.pos):
                                    self._mouse_down = False
                                    continue
                                self._mouse_down = True
                                x, y = self._display_to_proto(*ev.pos)
                                self._input.send_touch(x, y, _TOUCH_PRESS)
                            elif ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
                                if self._climate_visible:
                                    self._mouse_down = False
                                    continue
                                x, y = self._display_to_proto(*ev.pos)
                                self._input.send_touch(x, y, _TOUCH_RELEASE)
                                self._mouse_down = False
                            elif (
                                ev.type == pygame.MOUSEMOTION
                                and self._mouse_down
                                and ev.buttons[0]
                            ):
                                if self._climate_visible:
                                    continue
                                x, y = self._display_to_proto(*ev.pos)
                                self._input.send_touch(x, y, _TOUCH_DRAG)
                            elif ev.type == pygame.KEYDOWN:
                                code = _AA_KEYMAP.get(ev.key)
                                if code is not None:
                                    self._input.send_key(code)
                except pygame.error:
                    break

                if not running:
                    break

                # If touch stack didn't send FINGERUP, release stale touches.
                if self._input is not None and self._finger_pressed and not self._climate_visible:
                    now = time.monotonic()
                    for fid in list(self._finger_pressed):
                        last = self._finger_last_seen_s.get(fid, now)
                        if (now - last) > 0.35 and fid not in self._finger_down:
                            pid = self._finger_pid.get(fid, 0)
                            sx, sy = self._finger_down.get(fid, (0.0, 0.0))
                            x, y = self._display_to_proto(int(sx), int(sy))
                            self._input.send_touch(x, y, _TOUCH_RELEASE, pointer_id=pid)
                            self._finger_pressed.discard(fid)

                screen.fill((12, 12, 24))
                if last_frame is not None:
                    screen.blit(last_frame, (0, 0))
                self._update_two_finger_long_tap()

                # Как в ~/Desktop/AA/ui/display.py: stateful CodecContext, без av.open
                try:
                    while True:
                        _ts, nal = self._q.get_nowait()
                        nals_consumed += 1
                        if log_video.isEnabledFor(logging.DEBUG) and nals_consumed <= 6:
                            log_video.debug(
                                "очередь→NAL #%d %d байт ts=%s",
                                nals_consumed,
                                len(nal),
                                _ts,
                            )
                        annex = h264_to_annex_b(nal)
                        if not annex and log_video.isEnabledFor(logging.DEBUG):
                            log_video.debug(
                                "h264_to_annex_b: пусто для входа %d байт (первые 16: %s)",
                                len(nal),
                                nal[:16].hex() if nal else "",
                            )
                        for surf in decoder.feed(annex):
                            framed, rect = _fit_video_to_window(
                                surf, self._w, self._h, self._video_scale
                            )
                            self._vid_rect = rect
                            last_frame = framed
                            screen.blit(framed, (0, 0))
                            frames_shown += 1
                            if frames_shown == 1:
                                log.info("Первый декодированный кадр — вывод на экран")
                                log_video.info(
                                    "vid_rect после fit: %s (кадр %dx%d)",
                                    rect,
                                    framed.get_width(),
                                    framed.get_height(),
                                )
                except queue.Empty:
                    pass

                now = time.monotonic()
                if frames_shown == 0 and (now - last_vid_warn_t) >= 3.0:
                    last_vid_warn_t = now
                    if nals_consumed > 0:
                        log_video.warning(
                            "есть NAL (%d чанков), но кадра на экране всё ещё нет — очередь=%d, "
                            "окно %dx%d decode %dx%d; смотрите --video-debug и ошибки декодера",
                            nals_consumed,
                            self._q.qsize(),
                            self._w,
                            self._h,
                            self._dec_w,
                            self._dec_h,
                        )
                    elif (now - display_loop_start) >= 8.0 and (
                        (now - last_empty_usb_warn) >= 15.0 or last_empty_usb_warn == 0.0
                    ):
                        last_empty_usb_warn = now
                        log_video.warning(
                            "за %.0f с не пришло ни одного видеочанка в очередь — USB/сессия/видеофокус?",
                            now - display_loop_start,
                        )
                self._draw_climate_overlay(screen)

                try:
                    pygame.display.flip()
                except pygame.error:
                    break

                clock.tick(60)
        finally:
            decoder.close()

    def _handle_climate_pointer_down(self, sx: int, sy: int) -> bool:
        """Handle local overlay taps. Return True if consumed."""
        if not self._climate_visible:
            return False
        if self._climate_close_rect.collidepoint(sx, sy):
            self._climate_visible = False
            return True
        if not self._climate_rect.collidepoint(sx, sy):
            return False

        for key, rect in self._climate_button_rects.items():
            if not rect.collidepoint(sx, sy):
                continue
            if key == "fan_minus":
                self._climate_fan = max(1, self._climate_fan - 1)
            elif key == "fan_plus":
                self._climate_fan = min(7, self._climate_fan + 1)
            elif key == "temp_minus":
                self._climate_temp_c = max(16, self._climate_temp_c - 1)
            elif key == "temp_plus":
                self._climate_temp_c = min(30, self._climate_temp_c + 1)
            elif key == "ac_toggle":
                self._climate_ac_on = not self._climate_ac_on
            elif key == "recirc_toggle":
                self._climate_recirc = not self._climate_recirc
            elif key == "mode_cycle":
                self._climate_mode_idx = (self._climate_mode_idx + 1) % len(self._climate_modes)
            return True
        return True

    def _update_two_finger_long_tap(self) -> None:
        ids = list(self._finger_down.keys())
        if len(ids) != 2:
            self._two_finger_since = None
            self._two_finger_start = {}
            self._two_finger_latched = False
            return

        if self._two_finger_since is None:
            self._two_finger_since = time.monotonic()
            self._two_finger_start = {fid: self._finger_down[fid] for fid in ids}
            return

        for fid in ids:
            sx0, sy0 = self._two_finger_start.get(fid, self._finger_down[fid])
            sx, sy = self._finger_down[fid]
            if abs(sx - sx0) > _CLIMATE_TOUCH_MOVE_TOL_PX or abs(sy - sy0) > _CLIMATE_TOUCH_MOVE_TOL_PX:
                self._two_finger_since = time.monotonic()
                self._two_finger_start = {fid2: self._finger_down[fid2] for fid2 in ids}
                return

        if not self._two_finger_latched and (time.monotonic() - self._two_finger_since) >= _CLIMATE_LONG_TAP_S:
            self._climate_visible = True
            self._two_finger_latched = True

    def _draw_climate_overlay(self, screen: pygame.Surface) -> None:
        if not self._climate_visible:
            return
        ow = int(self._w * 0.78)
        oh = int(self._h * 0.62)
        ox = (self._w - ow) // 2
        oy = (self._h - oh) // 2
        self._climate_rect = pygame.Rect(ox, oy, ow, oh)

        # Pseudo blur underlay: cheap downscale/upscale of current frame.
        underlay = screen.subsurface(self._climate_rect).copy()
        blur_w = max(24, ow // 10)
        blur_h = max(24, oh // 10)
        underlay = pygame.transform.smoothscale(underlay, (blur_w, blur_h))
        underlay = pygame.transform.smoothscale(underlay, (ow, oh))
        underlay_dark = pygame.Surface((ow, oh), pygame.SRCALPHA)
        underlay_dark.fill((10, 14, 18, 88))
        underlay.blit(underlay_dark, (0, 0))
        screen.blit(underlay, (ox, oy))

        overlay = pygame.Surface((ow, oh), pygame.SRCALPHA)
        overlay.fill((18, 24, 30, _CLIMATE_ALPHA))
        pygame.draw.rect(overlay, (78, 110, 138, 235), overlay.get_rect(), width=2, border_radius=14)

        font_title = pygame.font.SysFont(None, max(24, ow // 18))
        title = font_title.render("Climate Control", True, (220, 236, 250))
        overlay.blit(title, (18, 12))

        cb_size = max(26, ow // 16)
        cbr = pygame.Rect(ow - cb_size - 12, 10, cb_size, cb_size)
        pygame.draw.rect(overlay, (120, 55, 55, 230), cbr, border_radius=7)
        font_x = pygame.font.SysFont(None, int(cb_size * 0.9))
        x_txt = font_x.render("X", True, (255, 232, 232))
        overlay.blit(x_txt, (cbr.x + (cb_size - x_txt.get_width()) // 2, cbr.y + (cb_size - x_txt.get_height()) // 2 - 1))
        self._climate_close_rect = pygame.Rect(ox + cbr.x, oy + cbr.y, cbr.w, cbr.h)

        cx = int(ow * 0.28)
        cy = int(oh * 0.56)
        r_outer = max(38, min(ow, oh) // 6)
        r_inner = max(12, r_outer // 4)
        pygame.draw.circle(overlay, (30, 43, 58, 220), (cx, cy), r_outer + 12)
        pygame.draw.circle(overlay, (90, 128, 164, 230), (cx, cy), r_outer, width=3)
        t = time.monotonic() * 3.2
        for i in range(6):
            a = t + (i * (2.0 * math.pi / 6.0))
            tip = (int(cx + math.cos(a) * (r_outer - 6)), int(cy + math.sin(a) * (r_outer - 6)))
            left = (int(cx + math.cos(a + 1.8) * r_inner), int(cy + math.sin(a + 1.8) * r_inner))
            right = (int(cx + math.cos(a - 1.8) * r_inner), int(cy + math.sin(a - 1.8) * r_inner))
            pygame.draw.polygon(overlay, (136, 193, 236, 210), [left, tip, right])
        pygame.draw.circle(overlay, (180, 220, 245, 230), (cx, cy), r_inner)

        self._climate_button_rects = {}
        font_value = pygame.font.SysFont(None, max(26, ow // 20))
        font_btn = pygame.font.SysFont(None, max(22, ow // 28))
        font_small = pygame.font.SysFont(None, max(18, ow // 34))

        def draw_button(name: str, rx: int, ry: int, rw: int, rh: int, label: str, active: bool = False) -> None:
            rect = pygame.Rect(rx, ry, rw, rh)
            self._climate_button_rects[name] = pygame.Rect(ox + rx, oy + ry, rw, rh)
            if active:
                fill = (59, 143, 201, 235)
                border = (150, 218, 255, 245)
                txt = (235, 248, 255)
            else:
                fill = (36, 52, 70, 220)
                border = (92, 128, 158, 235)
                txt = (202, 224, 242)
            pygame.draw.rect(overlay, fill, rect, border_radius=11)
            pygame.draw.rect(overlay, border, rect, width=2, border_radius=11)
            t_s = font_btn.render(label, True, txt)
            overlay.blit(t_s, (rx + (rw - t_s.get_width()) // 2, ry + (rh - t_s.get_height()) // 2))

        col_x = int(ow * 0.52)
        row_top = int(oh * 0.20)
        row_step = max(54, oh // 7)
        btn_h = max(40, oh // 10)
        btn_w = max(84, ow // 6)

        temp_txt = font_value.render(f"{self._climate_temp_c}C", True, (214, 240, 255))
        fan_txt = font_value.render(f"Fan {self._climate_fan}", True, (214, 240, 255))
        mode_txt = font_small.render(f"Mode: {self._climate_modes[self._climate_mode_idx]}", True, (186, 214, 234))
        overlay.blit(temp_txt, (col_x, row_top - 34))
        overlay.blit(fan_txt, (col_x, row_top + row_step - 34))
        overlay.blit(mode_txt, (col_x, row_top + 2 * row_step - 30))

        draw_button("temp_minus", col_x, row_top, btn_w, btn_h, "TEMP -")
        draw_button("temp_plus", col_x + btn_w + 12, row_top, btn_w, btn_h, "TEMP +")
        draw_button("fan_minus", col_x, row_top + row_step, btn_w, btn_h, "FAN -")
        draw_button("fan_plus", col_x + btn_w + 12, row_top + row_step, btn_w, btn_h, "FAN +")
        draw_button("mode_cycle", col_x, row_top + 2 * row_step, btn_w * 2 + 12, btn_h, "MODE")
        draw_button("ac_toggle", col_x, row_top + 3 * row_step, btn_w, btn_h, "A/C", active=self._climate_ac_on)
        draw_button("recirc_toggle", col_x + btn_w + 12, row_top + 3 * row_step, btn_w, btn_h, "RECIRC", active=self._climate_recirc)

        for i in range(7):
            bar_w = 8
            bar_h = 14 + i * 3
            bx = int(ow * 0.17) + i * 12
            by = int(oh * 0.80) - bar_h
            col = (122, 195, 240, 220) if i < self._climate_fan else (67, 94, 118, 190)
            pygame.draw.rect(overlay, col, pygame.Rect(bx, by, bar_w, bar_h), border_radius=3)

        font_note = pygame.font.SysFont(None, max(18, ow // 32))
        note = font_note.render("Android-style mock climate panel", True, (175, 206, 228))
        overlay.blit(note, (18, oh - note.get_height() - 12))
        screen.blit(overlay, (ox, oy))

    # Navigation HUD disabled (console logs only).

    def _display_to_proto(self, sx: int, sy: int) -> tuple[int, int]:
        """Координаты окна → 0…proto_w−1 / 0…proto_h−1 (учёт letterbox)."""
        ox, oy, dw, dh = self._vid_rect
        if dw <= 0 or dh <= 0:
            return sx, sy
        u = (sx - ox) / float(dw)
        v = (sy - oy) / float(dh)
        u = min(max(u, 0.0), 1.0)
        v = min(max(v, 0.0), 1.0)
        tx = int(round(u * (self._proto_w - 1))) if self._proto_w > 1 else 0
        ty = int(round(v * (self._proto_h - 1))) if self._proto_h > 1 else 0
        return tx, ty


class _CodecH264Decoder:
    """
    H.264 → pygame Surface через один av.CodecContext (как ~/Desktop/AA/ui/display.py).
    Каждый USB-чанк после h264_to_annex_b подаётся как av.Packet — без растущего
    буфера и без повторного demux всего потока (там был O(n²) и «моргание»).
    """

    def __init__(self, w: int, h: int):
        self._w = w
        self._h = h
        self._codec = av.CodecContext.create("h264", "r")
        self._codec.open()
        self._decode_err_n = 0
        self._decode_ok_n = 0

    def feed(self, annex_b: bytes) -> list:
        """Декодирует один буфер Annex B; список pygame.Surface (может быть пустым)."""
        if not annex_b:
            return []
        out = []
        try:
            packet = av.Packet(annex_b)
            for frame in self._codec.decode(packet):
                arr = frame.to_ndarray(format="rgb24")
                if not arr.flags["C_CONTIGUOUS"]:
                    arr = arr.copy()
                w0, h0 = frame.width, frame.height
                surf = pygame.image.frombytes(
                    arr.tobytes(), (w0, h0), "RGB"
                )
                if w0 != self._w or h0 != self._h:
                    surf = pygame.transform.scale(surf, (self._w, self._h))
                out.append(surf)
                self._decode_ok_n += 1
                if log_video.isEnabledFor(logging.DEBUG) and self._decode_ok_n <= 5:
                    log_video.debug(
                        "декодер: кадр #%d %dx%d → surface %dx%d (целевой %dx%d)",
                        self._decode_ok_n,
                        w0,
                        h0,
                        surf.get_width(),
                        surf.get_height(),
                        self._w,
                        self._h,
                    )
        except av.error.FFmpegError as e:
            self._decode_err_n += 1
            if self._decode_err_n <= 12 or self._decode_err_n % 400 == 0:
                log_video.debug("H264 FFmpegError #%d: %s", self._decode_err_n, e)
        except Exception as e:
            self._decode_err_n += 1
            if self._decode_err_n <= 12 or self._decode_err_n % 400 == 0:
                log_video.debug("H264 decode #%d: %s", self._decode_err_n, e)
        return out

    def close(self) -> None:
        try:
            self._codec = None
        except Exception:
            pass


# ── Audio output ──────────────────────────────────────────────────────────

class PcmAudioSink:
    """Simple PCM audio sink using PyAudio."""

    def __init__(self, sample_rate: int, channels: int, bits: int):
        self._sr    = sample_rate
        self._ch    = channels
        self._bits  = bits
        self._stream = None
        self._pa     = None

    def open(self):
        try:
            import pyaudio
            self._pa = pyaudio.PyAudio()
            fmt = pyaudio.paInt16 if self._bits == 16 else pyaudio.paInt8
            self._stream = self._pa.open(
                format=fmt, channels=self._ch,
                rate=self._sr, output=True,
                frames_per_buffer=1024,
            )
        except Exception as e:
            log.warning("Audio open failed: %s", e)

    def write(self, data: bytes):
        if self._stream:
            try:
                self._stream.write(data)
            except Exception:
                pass

    def close(self):
        if self._stream:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
        if self._pa:
            try:
                self._pa.terminate()
            except Exception:
                pass


# ── Application callbacks (gartnera IHUConnectionThreadEventCallbacks) ────

class AppCallbacks(IHUCallbacks):

    def __init__(
        self,
        video_queue: queue.Queue,
        no_audio: bool,
        hu_server: "HUServer",
        display_w: int = DEFAULT_VIDEO_W,
        display_h: int = DEFAULT_VIDEO_H,
        proto_touch_w: int | None = None,
        proto_touch_h: int | None = None,
    ):
        self._vq       = video_queue
        self._no_audio = no_audio
        self._hu       = hu_server
        self._disp_w   = int(display_w)
        self._disp_h   = int(display_h)
        self._proto_w  = int(proto_touch_w) if proto_touch_w is not None else self._disp_w
        self._proto_h  = int(proto_touch_h) if proto_touch_h is not None else self._disp_h
        self._media_sink  = PcmAudioSink(48000, 2, 16)
        self._speech_sink = PcmAudioSink(16000, 1, 16)
        self._stop_event  = threading.Event()
        self._last_drag_send_s = 0.0
        self._drag_min_interval_s = 1.0 / 60.0  # throttle DRAG to 60 Hz
        self._navi_log = logging.getLogger("navi")
        self._touch_log = logging.getLogger("touch")
        self._navi = _NavState()
        self._navi_last_line: str | None = None
        self._navi_last_line_at_s: float = 0.0
        self._vid_pkt_n = 0
        self._vid_pkt_log_t = 0.0

        if not no_audio:
            self._media_sink.open()
            self._speech_sink.open()

    def media_packet(self, chan: int, timestamp: int, data: bytes) -> int:
        if chan == AA_CH_VID:
            if not hasattr(self, "_vid_logged"):
                self._vid_logged = True
                log.info("Первый видеопакет: %d байт", len(data))
            self._vid_pkt_n += 1
            now = time.monotonic()
            if log_video.isEnabledFor(logging.DEBUG):
                if self._vid_pkt_n <= 8 or self._vid_pkt_n % 80 == 0 or (now - self._vid_pkt_log_t) >= 2.0:
                    self._vid_pkt_log_t = now
                    try:
                        qs = self._vq.qsize()
                    except Exception:
                        qs = -1
                    log_video.debug(
                        "видео USB→очередь pkt#%d ts=%s %dB очередь=%s",
                        self._vid_pkt_n,
                        timestamp,
                        len(data),
                        qs,
                    )
            if self._vq.full():
                try:
                    self._vq.get_nowait()
                    if log_video.isEnabledFor(logging.DEBUG):
                        log_video.debug("очередь видео была полна — выкинул старый чанк")
                except queue.Empty:
                    pass
            try:
                self._vq.put_nowait((timestamp, data))
            except queue.Full:
                log_video.warning("очередь видео полна, чанк потерян (увеличьте maxsize?)")
        elif chan == AA_CH_AUD and not self._no_audio:
            self._media_sink.write(data)
        elif chan == AA_CH_AU1 and not self._no_audio:
            self._speech_sink.write(data)
        return 0

    def media_start(self, chan: int) -> int:
        log.info("MediaStart ch=%d", chan)
        return 0

    def media_stop(self, chan: int) -> int:
        log.info("MediaStop ch=%d", chan)
        return 0

    def media_setup_complete(self, chan: int) -> None:
        log.info("MediaSetupComplete ch=%d — requesting video focus", chan)
        if chan == AA_CH_VID:
            # Grant video focus after setup
            self._hu.send_video_focus(chan, focused=True)

    def disconnection_or_error(self) -> None:
        log.warning("Disconnection or error — stopping.")
        self._stop_event.set()

    def audio_focus_request(self, chan: int, request) -> None:
        """Grant AUDIO_FOCUS_STATE_GAIN_MEDIA_ONLY immediately."""
        from proto_gen import hu_pb2 as HU
        self._hu.send_audio_focus_response(
            chan,
            HU.AudioFocusResponse.AUDIO_FOCUS_STATE_GAIN_MEDIA_ONLY
        )

    def video_focus_request(self, chan: int, request) -> None:
        log.info("VideoFocusRequest ch=%d disp_index=%s",
                 chan, getattr(request, "disp_index", "?"))
        self._hu.send_video_focus(chan, focused=True)

    def send_touch(self, x: int, y: int, action: int, pointer_id: int = 0) -> None:
        """
        Координаты уже в системе протокола (как touch_screen в ServiceDiscovery): 0…proto_w−1 / 0…proto_h−1.
        action — HU.TouchInfo.TOUCH_ACTION: PRESS=0, RELEASE=1, DRAG=2.
        """
        # Coalesce/throttle drag events: phone can stop reading BULK OUT
        # if we flood input while video is running.
        if action == _TOUCH_DRAG:
            now = time.monotonic()
            if (now - self._last_drag_send_s) < self._drag_min_interval_s:
                return
            self._last_drag_send_s = now

        ie = HU.InputEvent()
        ie.timestamp = int(time.monotonic() * 1_000_000)
        # Some AA stacks expect disp_channel present (0 = main display).
        ie.disp_channel = 0
        loc = ie.touch.location.add()
        loc.x = max(0, min(self._proto_w - 1, int(x)))
        loc.y = max(0, min(self._proto_h - 1, int(y)))
        loc.pointer_id = pointer_id
        # In many AA headunit implementations, action_index is always set (single-touch = 0).
        # Some phone stacks appear to rely on this being present.
        ie.touch.action_index = 0
        ie.touch.action = action
        if self._touch_log.isEnabledFor(logging.DEBUG):
            an = {0: "PRESS", 1: "RELEASE", 2: "DRAG"}.get(int(action), str(action))
            self._touch_log.debug(
                "touch→phone %s x=%d y=%d pid=%d",
                an, int(loc.x), int(loc.y), int(pointer_id),
            )
        self._hu.send_input_event(AA_CH_TOU, 0, ie.SerializeToString())

    def send_key(self, scan_code: int) -> None:
        """Один «нажатый» сканкод (HUIB_*), см. keycodes_supported в ServiceDiscovery."""
        ie = HU.InputEvent()
        ie.timestamp = int(time.monotonic() * 1_000_000)
        bi = ie.button.button.add()
        bi.scan_code = scan_code
        bi.is_pressed = True
        bi.meta = 0
        bi.long_press = False
        self._hu.send_input_event(AA_CH_TOU, 0, ie.SerializeToString())

    def wait(self):
        self._stop_event.wait()

    def close(self):
        if not self._no_audio:
            self._media_sink.close()
            self._speech_sink.close()

    def navigation_status(self, status: int) -> None:
        # 1 = START, 2 = STOP (per hu.proto)
        self._navi.active = (status == 1)
        if not self._navi.active:
            self._navi.turn = None
            self._navi.dist = None
            self._navi.turn_png_len = None

    def navigation_turn_image(self, png_bytes: bytes) -> None:
        self._navi.turn_png_len = len(png_bytes) if png_bytes else 0
        self._emit_nav_event_if_ready()

    def navigation_turn(self, turn_msg) -> None:
        self._navi.turn = turn_msg
        self._emit_nav_event_if_ready()

    def navigation_distance(self, distance_msg) -> None:
        self._navi.dist = distance_msg
        self._emit_nav_event_if_ready()

    def _sanitize_nav_text(self, s: object) -> str:
        try:
            t = "" if s is None else str(s)
        except Exception:
            return ""
        if not t:
            return ""
        t = "".join(ch if ch.isprintable() and ch not in "\r\n\t" else " " for ch in t)
        t = re.sub(r"\s+", " ", t).strip()
        if not t:
            return ""
        candidates = re.findall(r"[0-9A-Za-zА-Яа-яЁё][0-9A-Za-zА-Яа-яЁё .,:;()«»\"'/-]{6,}", t)
        if candidates:
            return candidates[-1].strip()
        return t

    def _emit_nav_event_if_ready(self) -> None:
        if not self._navi.active:
            return
        if self._navi.turn is None and self._navi.dist is None:
            return

        try:
            from proto_gen import hu_pb2 as HU
        except Exception:
            HU = None  # type: ignore

        road = None
        side = None
        event = None
        exit_num = None
        angle = None
        if self._navi.turn is not None:
            road = self._sanitize_nav_text(getattr(self._navi.turn, "event_name", None))
            side = getattr(self._navi.turn, "turn_side", None)
            event = getattr(self._navi.turn, "turn_event", None)
            exit_num = getattr(self._navi.turn, "turn_number", None)
            angle = getattr(self._navi.turn, "turn_angle", None)

        meters = None
        eta_s = None
        disp = None
        unit = None
        if self._navi.dist is not None:
            meters = getattr(self._navi.dist, "distance", None)
            eta_s = getattr(self._navi.dist, "time_until", None)
            disp_raw = int(getattr(self._navi.dist, "display_distance", 0) or 0)
            unit = int(getattr(self._navi.dist, "display_distance_unit", 0) or 0)
            if HU is not None:
                try:
                    u = HU.NAVDistanceMessage.DISPLAY_DISTANCE_UNIT
                    if unit == u.METERS:
                        disp = f"{disp_raw / 1000.0:.0f} m"
                    elif unit in (u.KILOMETERS, u.KILOMETERS10):
                        disp = f"{disp_raw / 1000.0:.1f} km"
                    elif unit in (u.MILES, u.MILES10):
                        disp = f"{disp_raw / 1000.0:.1f} mi"
                    elif unit == u.FEET:
                        disp = f"{disp_raw / 1000.0:.0f} ft"
                    else:
                        disp = str(disp_raw)
                except Exception:
                    disp = str(disp_raw)
            else:
                disp = str(disp_raw)

        # Human readable enum names when possible.
        side_name = None
        event_name = None
        if HU is not None and self._navi.turn is not None:
            try:
                side_name = HU.NAVTurnMessage.TURN_SIDE.Name(int(side))
            except Exception:
                pass
            try:
                event_name = HU.NAVTurnMessage.TURN_EVENT.Name(int(event))
            except Exception:
                pass

        def fmt_eta(s: int | None) -> str | None:
            if s is None:
                return None
            try:
                s = int(s)
            except Exception:
                return None
            if s < 0:
                return None
            mm = s // 60
            ss = s % 60
            return f"{mm:02d}:{ss:02d}"

        def fmt_distance_m(m: int | None) -> str | None:
            if m is None:
                return None
            try:
                m = int(m)
            except Exception:
                return None
            if m < 0:
                return None
            if m >= 1000:
                return f"{m/1000.0:.1f} км"
            return f"{m} м"

        def fmt_turn_ru(ev_name: str | None, side_name: str | None) -> str | None:
            if not ev_name and not side_name:
                return None
            side_ru = None
            if side_name == "TURN_LEFT":
                side_ru = "налево"
            elif side_name == "TURN_RIGHT":
                side_ru = "направо"
            elif side_name == "TURN_UNSPECIFIED":
                side_ru = None

            ev_ru = None
            if ev_name in ("TURN_TURN", "TURN_SLIGHT_TURN", "TURN_SHARP_TURN", "TURN_U_TURN"):
                ev_ru = "поворот"
            elif ev_name == "TURN_DEPART":
                ev_ru = "старт"
            elif ev_name == "TURN_STRAIGHT":
                ev_ru = "прямо"
            elif ev_name == "TURN_MERGE":
                ev_ru = "съезд/слияние"
            elif ev_name == "TURN_FORK":
                ev_ru = "развилка"
            elif ev_name == "TURN_ON_RAMP":
                ev_ru = "на съезд (ramp)"
            elif ev_name == "TURN_OFF_RAMP":
                ev_ru = "съезд"
            elif ev_name in ("TURN_ROUNDABOUT_ENTER", "TURN_ROUNDABOUT_EXIT", "TURN_ROUNDABOUT_ENTER_AND_EXIT"):
                ev_ru = "круговое"
            elif ev_name == "TURN_NAME_CHANGE":
                ev_ru = "смена улицы"
            elif ev_name == "TURN_DESTINATION":
                ev_ru = "прибытие"

            if ev_ru and side_ru:
                return f"{ev_ru} {side_ru}"
            return ev_ru or side_ru or None

        eta_h = fmt_eta(eta_s)
        meters_h = fmt_distance_m(meters)
        in_h = disp or meters_h
        turn_h = fmt_turn_ru(event_name, side_name)

        # Canonical key for scenario matching / dedupe.
        def norm_text(s: str | None) -> str | None:
            if s is None:
                return None
            t = str(s).strip()
            if not t:
                return None
            return t

        def dist_bucket(m: int | None) -> str | None:
            if m is None:
                return None
            try:
                m = int(m)
            except Exception:
                return None
            if m < 0:
                return None
            # Bucket to reduce churn: 0-200m step 10m, then 50m up to 1km, then 0.1km.
            if m <= 200:
                b = int(round(m / 10.0) * 10)
                return f"{b}m"
            if m <= 1000:
                b = int(round(m / 50.0) * 50)
                return f"{b}m"
            km10 = int(round((m / 100.0)))  # 0.1km units
            return f"{km10/10.0:.1f}km"

        road_key = norm_text(road)
        ev_key = norm_text(event_name)
        side_key = norm_text(side_name)
        dist_key = dist_bucket(meters)
        # Prefer stable, machine-friendly key. Keep it short.
        key_parts = []
        if ev_key:
            key_parts.append(ev_key)
        if side_key and side_key != "TURN_UNSPECIFIED":
            key_parts.append(side_key)
        if dist_key:
            key_parts.append(dist_key)
        if road_key:
            key_parts.append(road_key)
        event_key = "|".join(key_parts) if key_parts else "NAV|UNKNOWN"

        parts = []
        if turn_h:
            parts.append(turn_h)
        if in_h:
            parts.append(f"через {in_h}")
        if eta_h:
            parts.append(f"({eta_h})")
        if road:
            parts.append(f"по {road!r}")
        if exit_num:
            parts.append(f"выезд {exit_num}")
        if self._navi.turn_png_len is not None and (self._navi.turn_png_len or 0) > 0:
            parts.append(f"иконка {self._navi.turn_png_len}B")

        line = "navi " + event_key + " " + (" ".join(parts) if parts else "update")

        now = time.monotonic()
        if line != self._navi_last_line or (now - self._navi_last_line_at_s) >= 2.0:
            self._navi_last_line = line
            self._navi_last_line_at_s = now
            self._navi_log.info("%s", line)



@dataclass
class _NavState:
    active: bool = False
    turn: object | None = None
    dist: object | None = None
    turn_png_len: int | None = None




# ── Main ──────────────────────────────────────────────────────────────────

def _parse_resolution_string(s: str) -> tuple[int, int]:
    """Строка вида 800x480, 1280*720, 1920:1080 → (ширина, высота)."""
    t = s.strip().lower().replace("*", "x").replace(":", "x").replace("×", "x")
    if "x" not in t:
        raise ValueError("ожидается WxH")
    a, b = t.split("x", 1)
    return int(a.strip()), int(b.strip())


def _protocol_touch_dims(win_w: int, win_h: int, touch_mode: str = "native") -> tuple[int, int, bool]:
    """
    Возвращает размеры touch_screen для ServiceDiscovery.

    touch_mode:
      - auto   : если окно портретное, объявляем touch как альбом (max×min).
                Это повышает шанс успешного handshake на телефонах, которые
                не принимают портретный touch_screen.
      - native : объявляем touch ровно как окно (win_w×win_h), включая портрет.
                Это нужно, чтобы попытаться запросить портретный видеопресет
                (например 720x1280) без каких-либо поворотов рендера.

    Возвращает: (w_proto, h_proto, did_swap_to_landscape).
    """
    if touch_mode == "native":
        return win_w, win_h, False
    if win_h > win_w:
        return max(win_w, win_h), min(win_w, win_h), True
    return win_w, win_h, False


def _log_cli_config_and_check(args, proto_w: int, proto_h: int, swapped: bool) -> None:
    """
    Сводка: кто из флагов за что отвечает, и предупреждение если -r и --video-preset расходятся.
    """
    tm = "auto" if swapped else "native"
    touch_note = (
        "портретное окно → touch в протоколе как альбом max×min (обход частых ограничений телефона)"
        if swapped
        else "touch в протоколе = размер окна"
    )
    lines = [
        "——— эффективная конфигурация CLI ———",
        f"  окно pygame: {args.width}×{args.height}  (-r или -W/-H)",
        f"  touch_screen (ServiceDiscovery): {proto_w}×{proto_h}  (--touch-mode {tm}: {touch_note})",
        f"  отрисовка кадра в окне: {args.video_scale}  (--video-scale; только letterbox/stretch)",
    ]
    pr = resolve_video_preset(args.video_preset)
    if pr is not None:
        rw, rh, _en = pr
        lines.append(f"  видео enum (H.264 в SD): {rw}×{rh}  (--video-preset)")
        if (rw, rh) != (args.width, args.height):
            log.warning(
                "Окно %d×%d ≠ --video-preset %d×%d: в ServiceDiscovery уйдут margin_* между кадром и touch; "
                "координаты тача по-прежнему 0…%d × 0…%d. Проверьте, что так задумано.",
                args.width,
                args.height,
                rw,
                rh,
                proto_w - 1,
                proto_h - 1,
            )
        elif args.video_preset and str(args.video_preset).strip().lower() not in ("", "auto"):
            lines.append(
                "    (пресет совпадает с окном — --video-preset избыточен; без него hu_aap обычно "
                "выберет тот же enum от touch UI)"
            )
    else:
        lines.append(
            "  видео enum: автоподбор в hu_aap по touch UI  (--video-preset не задан или auto)"
        )
    lines.append(
        f"  декодер/маппинг тача: целевой кадр {proto_w}×{proto_h} (= размер touch в протоколе)"
    )
    lines.append(
        f"  driver_pos (положение руля): {args.driver_position}  (--driver-position; lhd=левый, rhd=правый)"
    )
    if args.dpi is not None:
        lines.append(
            f"  VideoConfig.dpi: {args.dpi}  (--dpi; расчёт и --dpi-scale не используются)"
        )
    else:
        lines.append(
            f"  VideoConfig.dpi: формула по площади × {args.dpi_scale}  (--dpi-scale; явный --dpi отключает)"
        )
    log.info("\n".join(lines))


def main():
    ap = argparse.ArgumentParser(description="Android Auto Head Unit (gartnera port)")
    ap.add_argument("--cert",     choices=["jaguar", "lr"], default="jaguar",
                    help="OEM certificate to use for TLS auth")
    ap.add_argument("--no-audio", action="store_true",
                    help="Disable audio output")
    ap.add_argument("--debug",    action="store_true",
                    help="Enable debug logging")
    ap.add_argument(
        "--video-debug",
        action="store_true",
        help="Подробный лог цепочки видео (логгер video): пакеты, очередь, NAL, декодер. "
        "Если нет картинки — запускайте с этим флагом (и при --nav-only логи video всё равно видны).",
    )
    ap.add_argument(
        "--nav-only",
        action="store_true",
        help="Логировать почти только навигацию (NAVI>>). Полезно для grep-less режима.",
    )
    ap.add_argument(
        "-W",
        "--width",
        type=int,
        default=DEFAULT_VIDEO_W,
        metavar="W",
        help="Ширина окна и touch/video для телефона (по умолчанию %d)"
        % DEFAULT_VIDEO_W,
    )
    ap.add_argument(
        "-H",
        "--height",
        type=int,
        default=DEFAULT_VIDEO_H,
        metavar="H",
        help="Высота окна и touch/video для телефона (по умолчанию %d)"
        % DEFAULT_VIDEO_H,
    )
    ap.add_argument(
        "-r",
        "--resolution",
        default=None,
        metavar="WxH",
        help="Размер окна pygame одной строкой (перекрывает -W/-H), напр. 800x480, 720x1280. "
        "Задаёт базу координат тача на экране; не путать с --video-preset (enum потока H.264).",
    )
    ap.add_argument(
        "--proto-major",
        type=int,
        default=None,
        metavar="N",
        help="Major версии протокола в VersionRequest (по умолчанию из hu_const)",
    )
    ap.add_argument(
        "--proto-minor",
        type=int,
        default=None,
        metavar="N",
        help="Minor в VersionRequest (gartnera: 1; часто пробуют 6/7/8 при предупреждениях)",
    )
    ap.add_argument(
        "--sw-version",
        default=None,
        metavar="S",
        help="Строка sw_version в ServiceDiscovery (по умолчанию SWV1, как в gartnera)",
    )
    ap.add_argument(
        "--sw-build",
        default=None,
        metavar="S",
        help="Строка sw_build (по умолчанию SWB1)",
    )
    ap.add_argument(
        "--video-scale",
        choices=("stretch", "letterbox"),
        default="stretch",
        help="Только отрисовка: как вписать декодированный кадр в окно pygame — stretch (на весь экран) "
        "или letterbox (поля, без искажения). Не меняет протокол и размер touch_screen.",
    )
    ap.add_argument(
        "--dpi",
        type=int,
        default=None,
        metavar="N",
        help="Явный dpi в VideoConfig (ServiceDiscovery). Диапазон %d…%d. "
        "Если задан — не используется формула по площади и флаг --dpi-scale. "
        "Портрет 1080×1920: на практике не выше %d dpi. "
        "Ориентиры: 140 (800×480), 160 (mdpi), 170–213, 213/238 для крупных HU — см. README и hu_aap.py."
        % (VIDEO_DPI_EXPLICIT_MIN, VIDEO_DPI_EXPLICIT_MAX, VIDEO_DPI_PORTRAIT_1080X1920_MAX),
    )
    ap.add_argument(
        "--dpi-scale",
        type=float,
        default=DEFAULT_VIDEO_DPI_SCALE,
        metavar="F",
        help="Множитель к расчётному dpi в VideoConfig (база 140 при 800×480, дальше √площадь). "
        "По умолчанию %(default)s (~на 20%% ниже «сырой» формулы; в портрете часто лучше совпадение UI/тач). "
        "Игнорируется при --dpi. Допустимо примерно 0.05…4.0.",
    )
    ap.add_argument(
        "--driver-position",
        choices=("lhd", "rhd"),
        default="lhd",
        help="Положение руля для ServiceDiscovery.driver_pos: lhd — левый руль (LHD, как в РФ/США/ЕС), "
        "rhd — правый руль (RHD, UK/Япония и т.д.). В protobuf для телефона: False=LHD, True=RHD.",
    )
    ap.add_argument(
        "--video-preset",
        default=None,
        metavar="WxH",
        help="Принудительный enum разрешения потока H.264 в ServiceDiscovery (таблица из реверса: "
        "1280x720, 720x1280, …). Размер окна по-прежнему задаёт только -r/-W/-H; при несовпадении с окном "
        "hu_aap шлёт margin_*. Если пресет совпадает с окном, флаг часто не нужен — сработает автоподбор. "
        "Список допустимых пар: см. resolve_video_preset в hu_aap.py.",
    )
    ap.add_argument(
        "--touch-mode",
        choices=("auto", "native"),
        default="native",
        help="Как объявлять touch_screen в ServiceDiscovery: native — как окно (в т.ч. книжный), чтобы "
        "согласовать портретный видеопресет (720x1280 и т.д.); auto — для handshake развернуть портрет "
        "окна в альбом max×min (часто ландшафтный поток на вертикальном дисплее). По умолчанию native. "
        "Если в портрете навигатор не реагирует на тапы, попробуйте --touch-mode auto.",
    )
    args = ap.parse_args()

    if args.resolution is not None:
        try:
            args.width, args.height = _parse_resolution_string(args.resolution)
        except ValueError as e:
            ap.error("неверный --resolution %r: %s (нужно, например, 800x480)" % (args.resolution, e))

    if not (320 <= args.width <= 4096 and 240 <= args.height <= 4096):
        ap.error("width/height вне допустимого диапазона (320…4096 × 240…4096)")

    if not (0.05 <= args.dpi_scale <= 4.0):
        ap.error("--dpi-scale должен быть в диапазоне 0.05…4.0")

    if args.dpi is not None and not (VIDEO_DPI_EXPLICIT_MIN <= args.dpi <= VIDEO_DPI_EXPLICIT_MAX):
        ap.error(
            "--dpi должен быть в диапазоне %d…%d"
            % (VIDEO_DPI_EXPLICIT_MIN, VIDEO_DPI_EXPLICIT_MAX)
        )

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.nav_only:
        # Reduce noise: keep only dedicated 'navi' plus selected debug channels.
        logging.getLogger().setLevel(logging.WARNING)
        logging.getLogger("navi").setLevel(logging.INFO)
        if args.debug:
            # Touch debugging is critical when nav apps ignore taps.
            logging.getLogger("touch").setLevel(logging.DEBUG)
            # Protocol-level debug (still much less noisy than full root DEBUG).
            logging.getLogger("hu_aap").setLevel(logging.DEBUG)

    # После --nav-only: иначе DEBUG логгера video отфильтруется корнем WARNING/INFO.
    _configure_video_logging(args.video_debug, args.nav_only, args.debug)

    video_queue = queue.Queue(maxsize=10)

    proto_w, proto_h, swapped = _protocol_touch_dims(args.width, args.height, args.touch_mode)
    log.info(
        "Окно %dx%d; touch для телефона: %dx%d; видео в окне: %s%s",
        args.width,
        args.height,
        proto_w,
        proto_h,
        args.video_scale,
        " — touch для handshake развёрнут из портретного окна" if swapped else "",
    )
    _log_cli_config_and_check(args, proto_w, proto_h, swapped)
    if args.dpi is not None and args.dpi_scale != DEFAULT_VIDEO_DPI_SCALE:
        log.info(
            "Задан --dpi: множитель --dpi-scale=%s для VideoConfig не применяется.",
            args.dpi_scale,
        )
    if not args.debug and args.video_debug:
        log.info("Для логов touch→phone добавьте также --debug (логгер touch в DEBUG).")

    # Create HUServer first so AppCallbacks can reference it
    hu = HUServer(
        callbacks=None,
        cert_type=args.cert,
        video_width=proto_w,
        video_height=proto_h,
        proto_major=args.proto_major,
        proto_minor=args.proto_minor,
        sw_version=args.sw_version,
        sw_build=args.sw_build,
        video_preset=args.video_preset,
        video_dpi_scale=args.dpi_scale,
        video_dpi=args.dpi,
        driver_pos=(args.driver_position == "rhd"),
    )
    cb = AppCallbacks(
        video_queue=video_queue,
        no_audio=args.no_audio,
        hu_server=hu,
        display_w=args.width,
        display_h=args.height,
        proto_touch_w=proto_w,
        proto_touch_h=proto_h,
    )
    hu._cb = cb  # inject callbacks

    # Handle Ctrl+C
    def _sigint(sig, frame):
        log.info("Прерывание — останавливаюсь…")
        cb._stop_event.set()
    signal.signal(signal.SIGINT, _sigint)

    # Start the AA protocol in a background thread
    def _run_hu():
        ret = hu.hu_aap_start()
        if ret < 0:
            log.error("hu_aap_start() вернул %d", ret)
            cb._stop_event.set()

    hu_thread = threading.Thread(target=_run_hu, daemon=True, name="hu-main")
    hu_thread.start()

    # Видео — в главном потоке (требование SDL для корректного вывода на Linux).
    display = AADisplay(
        video_queue,
        cb._stop_event,
        input_sink=cb,
        width=args.width,
        height=args.height,
        decode_width=proto_w,
        decode_height=proto_h,
        proto_w=proto_w,
        proto_h=proto_h,
        video_scale=args.video_scale,
    )
    display.run()

    log.info("Останавливаю head unit…")
    hu.hu_aap_shutdown()
    cb.close()
    hu_thread.join(timeout=15)
    if hu_thread.is_alive():
        log.warning("Поток hu-main не завершился за 15 с")
    log.info("До свидания.")


if __name__ == "__main__":
    main()
