"""
Constants — direct port of gartnera/headunit hu_aap.h
"""

# ── Channels (Service IDs) ─────────────────────────────────────────────────
AA_CH_CTR   = 0
AA_CH_TOU   = 1
AA_CH_SEN   = 2
AA_CH_VID   = 3
AA_CH_AUD   = 4
AA_CH_AU1   = 5
AA_CH_AU2   = 6
AA_CH_MIC   = 7
AA_CH_BT    = 8
AA_CH_PSTAT = 9
AA_CH_NOT   = 10
AA_CH_NAVI  = 11

CHAN_NAME = {
    AA_CH_CTR:   "AA_CH_CTR",
    AA_CH_TOU:   "AA_CH_TOU",
    AA_CH_SEN:   "AA_CH_SEN",
    AA_CH_VID:   "AA_CH_VID",
    AA_CH_AUD:   "AA_CH_AUD",
    AA_CH_AU1:   "AA_CH_AU1",
    AA_CH_AU2:   "AA_CH_AU2",
    AA_CH_MIC:   "AA_CH_MIC",
    AA_CH_BT:    "AA_CH_BT",
    AA_CH_PSTAT: "AA_CH_PSTAT",
    AA_CH_NOT:   "AA_CH_NOT",
    AA_CH_NAVI:  "AA_CH_NAVI",
}


def chan_name(chan: int) -> str:
    return CHAN_NAME.get(chan, f"CH{chan}")


# ── Frame flags ────────────────────────────────────────────────────────────
HU_FRAME_FIRST_FRAME     = 1 << 0   # 0x01
HU_FRAME_LAST_FRAME      = 1 << 1   # 0x02
HU_FRAME_CONTROL_MESSAGE = 1 << 2   # 0x04  (non-CTR channels, msg_type 2..0x7FFF)
HU_FRAME_ENCRYPTED       = 1 << 3   # 0x08

MAX_FRAME_PAYLOAD_SIZE = 0x4000      # 16384
MAX_FRAME_SIZE         = 0x4100      # 16640 (payload + header)

# ── HU_INIT_MESSAGE (version / SSL) ───────────────────────────────────────
HU_INIT_VersionRequest  = 0x0001
HU_INIT_VersionResponse = 0x0002
HU_INIT_SSLHandshake    = 0x0003
HU_INIT_AuthComplete    = 0x0004

# ── HU_PROTOCOL_MESSAGE (control channel, msg_type < 0x8000) ─────────────
HU_MSG_MediaDataWithTimestamp = 0x0000
HU_MSG_MediaData              = 0x0001
HU_MSG_ServiceDiscoveryRequest  = 0x0005
HU_MSG_ServiceDiscoveryResponse = 0x0006
HU_MSG_ChannelOpenRequest       = 0x0007
HU_MSG_ChannelOpenResponse      = 0x0008
HU_MSG_PingRequest              = 0x000B
HU_MSG_PingResponse             = 0x000C
HU_MSG_NavigationFocusRequest   = 0x000D
HU_MSG_NavigationFocusResponse  = 0x000E
HU_MSG_ShutdownRequest          = 0x000F
HU_MSG_ShutdownResponse         = 0x0010
HU_MSG_VoiceSessionRequest      = 0x0011
HU_MSG_AudioFocusRequest        = 0x0012
HU_MSG_AudioFocusResponse       = 0x0013

# ── HU_MEDIA_CHANNEL_MESSAGE (AUD/VID/MIC channels) ──────────────────────
HU_MEDIA_MediaSetupRequest  = 0x8000
HU_MEDIA_MediaStartRequest  = 0x8001
HU_MEDIA_MediaStopRequest   = 0x8002
HU_MEDIA_MediaSetupResponse = 0x8003
HU_MEDIA_MediaAck           = 0x8004
HU_MEDIA_MicRequest         = 0x8005
HU_MEDIA_MicResponse        = 0x8006
HU_MEDIA_VideoFocusRequest  = 0x8007
HU_MEDIA_VideoFocus         = 0x8008

# ── HU_SENSOR_CHANNEL_MESSAGE ─────────────────────────────────────────────
HU_SENSOR_SensorStartRequest  = 0x8001
HU_SENSOR_SensorStartResponse = 0x8002
HU_SENSOR_SensorEvent         = 0x8003

# ── HU_INPUT_CHANNEL_MESSAGE ──────────────────────────────────────────────
HU_INPUT_InputEvent      = 0x8001
HU_INPUT_BindingRequest  = 0x8002
HU_INPUT_BindingResponse = 0x8003

# ── HU_BLUETOOTH_CHANNEL_MESSAGE ──────────────────────────────────────────
HU_BT_PairingRequest  = 0x8001
HU_BT_PairingResponse = 0x8002
HU_BT_AuthData        = 0x8003

# ── HU_PHONE_STATUS_CHANNEL_MESSAGE ───────────────────────────────────────
HU_PSTAT_PhoneStatus      = 0x8001
HU_PSTAT_PhoneStatusInput = 0x8002

# ── HU_GENERIC_NOTIFICATIONS_CHANNEL_MESSAGE ─────────────────────────────
HU_NOT_Start    = 0x8001
HU_NOT_Stop     = 0x8002
HU_NOT_Request  = 0x8003
HU_NOT_Response = 0x8004

# ── HU_NAVI_CHANNEL_MESSAGE ───────────────────────────────────────────────
HU_NAVI_Status       = 0x8003
HU_NAVI_Turn         = 0x8004
HU_NAVI_TurnDistance = 0x8005

# ── Input buttons (keycodes_supported in ServiceDiscovery) ────────────────
HUIB_MIC1            = 0x01
HUIB_MENU            = 0x02
HUIB_HOME            = 0x03
HUIB_BACK            = 0x04
HUIB_PHONE           = 0x05
HUIB_CALLEND         = 0x06
HUIB_UP              = 0x13
HUIB_DOWN            = 0x14
HUIB_LEFT            = 0x15
HUIB_RIGHT           = 0x16
HUIB_ENTER           = 0x17
HUIB_MIC             = 0x54
HUIB_PLAYPAUSE       = 0x55
HUIB_NEXT            = 0x57
HUIB_PREV            = 0x58
HUIB_START           = 0x7E
HUIB_STOP            = 0x7F
HUIB_MUSIC           = 0xD1
HUIB_SCROLLWHEEL     = 65536
HUIB_MEDIA           = 65537
HUIB_NAVIGATION      = 65538
HUIB_RADIO           = 65539
HUIB_TEL             = 65540
HUIB_PRIMARY_BUTTON  = 65541
HUIB_SECONDARY_BUTTON = 65542
HUIB_TERTIARY_BUTTON = 65543

# ── USB / AOA ─────────────────────────────────────────────────────────────
GOOGLE_VID       = 0x18D1
AOA_PRODUCT_IDS  = [0x2D00, 0x2D01, 0x2D04, 0x2D05]
ANDROID_VIDS     = [0x18D1, 0x04E8, 0x22B8, 0x0BB4, 0x054C, 0x2717, 0x12D1, 0x0FCE]
AOA_USB_GET_PROTOCOL = 51
AOA_USB_SEND_STRING  = 52
AOA_USB_START        = 53
AOA_STRINGS = [
    (0, "Android"),
    (1, "Android Auto"),
    (2, "Android Auto"),
    (3, "2"),
    (4, "https://www.android.com/auto/"),
    (5, "HU0001"),
]

# ── Version ───────────────────────────────────────────────────────────────
# Версия протокола HU в VersionRequest (TLS). gartnera по умолчанию шлёт 1.1 (vr_buf 00 01 00 01);
# многие телефоны отвечают 1.7 — см. --proto-minor при предупреждениях о версии.
AA_VERSION_MAJOR = 1
AA_VERSION_MINOR = 7

# Строки ПО в ServiceDiscoveryResponse — в gartnera это литералы "SWV1" / "SWB1", они не равны номеру протокола.
AA_SW_VERSION = "SWV1"
AA_SW_BUILD = "SWB1"
