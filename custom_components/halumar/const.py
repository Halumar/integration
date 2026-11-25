"""Constants for the integration.

DOMAIN is the frozen technical namespace (level B of the naming model). Every
identifier derived from it is defined in this module; the display brand comes
from brand.py and never ends up in an identifier.
"""

from .brand import BRAND_NAME

DOMAIN = "halumar"

# Fixed config-entry unique id: exactly one hub per HA installation. Both the
# user and hassio flows set this, so Supervisor re-discovery can locate the
# entry and rotate its credentials in place.
ENTRY_UNIQUE_ID = DOMAIN

# Device registry identifiers (shared with the hub, GET /api/integration/devices).
HUB_DEVICE_ID = f"{DOMAIN}_hub"
CLIENT_DEVICE_ID_PREFIX = f"{DOMAIN}_client_"

# Entity object ids: always "<domain>_hub_<entity>" for hub entities and
# "<domain>_[guest_]<client name>_<entity>" for client entities, independent of
# the device (display) names. Distinct from the hub-pushed entity ids
# (sensor.<domain>_guests, sensor.<domain>_area_*, sensor.<domain>_client_*).
OBJECT_ID_PREFIX = f"{DOMAIN}_"
HUB_OBJECT_ID_PREFIX = f"{DOMAIN}_hub_"

# Unauthenticated compliance relay in HA Core (polled by the apps).
COMPLIANCE_VIEW_URL = f"/api/{DOMAIN}/compliance/{{client_id}}"
COMPLIANCE_VIEW_NAME = f"api:{DOMAIN}:compliance"


DEFAULT_HOST = "localhost"
DEFAULT_PORT = 8099

# Reserved area id of the start screen (contracts: client_status.current_area_id,
# navigate_to_area.area_id). Shown as the state "home" of the current-area sensor.
HOME_AREA_ID = "__home__"
HOME_STATE = "home"

CONF_API_TOKEN = "api_token"
CONF_USE_SSL = "use_ssl"
# Stable id of the hub this entry belongs to (status hub.instance_id, the
# Bonjour iid). Stored on first sight; a status with another id is another
# hub: nothing is reconciled until the admin confirms it in reconfigure or
# reauthenticate.
CONF_HUB_INSTANCE_ID = "hub_instance_id"
# True once the bundled blueprints were copied for this entry: they are
# copied at the first setup only, never again after a reload or restart.
CONF_BLUEPRINTS_INSTALLED = "blueprints_installed"

# Option: the "Start guest pairing" button pairs guests with all areas
# (marker "*") instead of none. Guests fail closed: without tags they see no
# area (contracts $defs/client_tags).
CONF_GUEST_BUTTON_ALL_AREAS = "guest_button_all_areas"
ALL_AREAS_TAG = "*"

# ── One HA user per paired device ───────────────────────────────
# Every paired device gets its own non-admin HA user with exactly one
# long-lived access token, created here and delivered to the hub
# (device_users.py). The integration holds no other HA credential.

# Device user name: "<BRAND_NAME> · [<guest label> ][<owner> ]<device>".
DEVICE_USER_NAME_SEPARATOR = " · "
DEVICE_USER_GUEST_LABEL = "Guest"
# Guest labels that are also recognised when a device user is adopted
# without the integration's store: guest users named with the German label.
DEVICE_USER_GUEST_LABEL_ALIASES = ("Gast",)

# client_name of a device user's long-lived token: "<domain>:<client_id>",
# after a rotation "<domain>:<client_id>/<hex6>" (HA allows only one LLAT
# per user and client_name, and the new token must exist before the old
# one is removed). The prefix marks integration-owned device users.
DEVICE_TOKEN_CLIENT_NAME_PREFIX = f"{DOMAIN}:"

# ── Enforcement in Home Assistant ─────────────────────
# halumar.control switches on behalf of device users after checking their
# rule snapshot; devices with the control path "checked" have read-only HA
# users. Events go to the hub log and onto the HA event bus.
SERVICE_CONTROL = "control"
SERVICE_REPORT_LOCATION = "report_location"
# Dispatcher signal: (client_id, fix or None) for the device trackers.
SIGNAL_LOCATION = f"{DOMAIN}_location"
SERVICE_APPLY_RULES = "apply_rules"
EVENT_EVENT = f"{DOMAIN}_event"
EVENT_ALARM = f"{DOMAIN}_alarm"
# A device resolved an NFC/QR tag (from the hub's ha_event tag_scanned).
EVENT_TAG_SCANNED = f"{DOMAIN}_tag_scanned"

# Whether to use HTTPS instead of HTTP. Defaults to False so existing
# LAN setups keep working over plain HTTP with no behavior change.
DEFAULT_USE_SSL = False

# Platforms to set up
PLATFORMS = [
    "binary_sensor",
    "button",
    "camera",
    "datetime",
    "device_tracker",
    "number",
    "select",
    "sensor",
    "switch",
]

# Coordinator update interval in seconds
UPDATE_INTERVAL = 30

# Relaxed safety-poll interval in seconds, used while the WebSocket push
# channel is connected: events arrive live over the WS, the REST poll only
# backstops missed events. On WS disconnect the coordinator drops back to
# UPDATE_INTERVAL.
SLOW_UPDATE_INTERVAL = 300

# Hub option pairing_qr_camera (status field of the same name): when the
# pairing camera may show the QR code with the PIN. off = never, guests =
# only during a guest pairing window, all = during every window.
QR_MODE_OFF = "off"
QR_MODE_GUESTS = "guests"
QR_MODE_ALL = "all"
QR_MODES = (QR_MODE_OFF, QR_MODE_GUESTS, QR_MODE_ALL)
# Request header telling the hub that this integration can show the QR.
QR_ACCEPT_HEADER = "X-Pairing-Qr"
QR_ACCEPT_VALUE = "accept"

# Hub device name (display only; entity ids come from HUB_OBJECT_ID_PREFIX).
HUB_DEVICE_NAME = f"{BRAND_NAME} Hub"

# Manufacturer info
MANUFACTURER = "Xonatec"
MODEL_HUB = f"{BRAND_NAME} Area Manager"
MODEL_CLIENT = f"{BRAND_NAME} Client"

# Minimum compatible hub version: first hub release with one HA user per
# device (POST /api/integration/client-token and the provisioning messages).
MIN_ADDON_VERSION = "2026.12.0"
