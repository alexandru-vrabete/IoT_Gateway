"""IoT gateway service.

Subscribes to the local Mosquitto broker on the Raspberry Pi, validates every
incoming telemetry message, and forwards it to a cloud MQTT broker.

Store-and-forward:
  * cloud up   -> publish straight away, nothing touches the database
  * cloud down -> save the message to SQLite as 'pending'
  * cloud back -> drain the pending rows in FIFO order, then resume live

Two MQTT clients are used: one connected to the local broker (subscriber) and
one connected to the cloud broker (publisher).
"""

import json
import logging
import time

import paho.mqtt.client as mqtt

from db_handler import DBHandler

# ---- configuration ---------------------------------------------------------

LOCAL_HOST = "localhost"
LOCAL_PORT = 1883
LOCAL_TOPIC = "factory/line1/node1/telemetry"

CLOUD_HOST = "your-cloud-broker.example.com"
CLOUD_PORT = 8883
CLOUD_USERNAME = "gateway"
CLOUD_PASSWORD = "change-me"
CLOUD_CA_CERT = "/etc/iot-gateway/ca.crt"   # set to None to disable TLS
CLOUD_TOPIC = "factory/line1/node1/telemetry"

DB_PATH = "gateway.db"

# ----------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("gateway")

db = DBHandler(DB_PATH)

cloud_connected = False
cloud_client = None


def validate(payload):
    """Return a parsed dict if the payload matches our schema, else None."""
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None

    if not isinstance(data, dict):
        return None

    required = {
        "node_id": str,
        "temp": (int, float),
        "hum": (int, float),
        "uptime": (int, float),
    }
    for key, types in required.items():
        if key not in data or not isinstance(data[key], types):
            return None

    # bool is a subclass of int; reject it so True/False never counts as a value
    if isinstance(data["uptime"], bool):
        return None

    return data


def flush_pending():
    """Send everything queued in SQLite, oldest first."""
    sent = 0
    while cloud_connected:
        rows = db.fetch_pending(limit=100)
        if not rows:
            break
        for row in rows:
            if not cloud_connected:
                break
            result = cloud_client.publish(row["topic"], row["payload"], qos=1)
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                log.warning("flush stopped, cloud publish failed")
                return
            db.mark_sent(row["id"])
            sent += 1
    if sent:
        log.info("flushed %d queued message(s) to cloud", sent)


# ---- cloud client callbacks ------------------------------------------------

def on_cloud_connect(client, userdata, flags, rc):
    global cloud_connected
    if rc == 0:
        cloud_connected = True
        log.info("cloud connected")
        flush_pending()
    else:
        log.error("cloud connect failed (rc=%s)", rc)


def on_cloud_disconnect(client, userdata, rc):
    global cloud_connected
    cloud_connected = False
    log.warning("cloud disconnected (rc=%s)", rc)


# ---- local client callbacks ------------------------------------------------

def on_local_connect(client, userdata, flags, rc):
    if rc == 0:
        log.info("local broker connected, subscribing to %s", LOCAL_TOPIC)
        client.subscribe(LOCAL_TOPIC, qos=1)
    else:
        log.error("local connect failed (rc=%s)", rc)


def on_local_message(client, userdata, msg):
    payload = msg.payload.decode("utf-8", errors="replace")
    data = validate(payload)
    if data is None:
        log.warning("dropping malformed message on %s: %s", msg.topic, payload)
        return

    # Re-serialize from the validated dict so we forward clean JSON.
    clean = json.dumps(data)

    if cloud_connected:
        result = cloud_client.publish(CLOUD_TOPIC, clean, qos=1)
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            log.info("forwarded to cloud: %s", clean)
            return
        log.warning("cloud publish failed, queueing instead")

    db.enqueue(CLOUD_TOPIC, clean)
    log.info("cloud down, queued message (%d pending)", db.pending_count())


def build_cloud_client():
    client = mqtt.Client(client_id="iot-gateway-cloud")
    client.on_connect = on_cloud_connect
    client.on_disconnect = on_cloud_disconnect

    if CLOUD_USERNAME:
        client.username_pw_set(CLOUD_USERNAME, CLOUD_PASSWORD)
    if CLOUD_CA_CERT:
        client.tls_set(ca_certs=CLOUD_CA_CERT)

    # Keep retrying the cloud connection in the background forever.
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    return client


def build_local_client():
    client = mqtt.Client(client_id="iot-gateway-local")
    client.on_connect = on_local_connect
    client.on_message = on_local_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    return client


def main():
    global cloud_client

    cloud_client = build_cloud_client()
    local_client = build_local_client()

    # connect_async + loop_start so a broker that is down at boot does not
    # stop the service from starting.
    cloud_client.connect_async(CLOUD_HOST, CLOUD_PORT, keepalive=60)
    cloud_client.loop_start()

    local_client.connect_async(LOCAL_HOST, LOCAL_PORT, keepalive=60)
    local_client.loop_start()

    log.info("gateway running")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        local_client.loop_stop()
        cloud_client.loop_stop()
        db.close()


if __name__ == "__main__":
    main()
