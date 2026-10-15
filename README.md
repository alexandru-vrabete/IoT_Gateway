# IoT Gateway

A small, modular telemetry pipeline for a factory line. An ESP32 edge node
reads a sensor and publishes over MQTT to a Mosquitto broker on a Raspberry Pi.
A Python gateway service on the Pi validates every message and forwards it to a
cloud MQTT broker. If the cloud link drops, messages are buffered in a local
SQLite database and replayed in order once the link comes back
(store-and-forward).

## Architecture

```
  +-------------+        WiFi / MQTT        +--------------------------+
  | ESP32 Node  |  factory/line1/node1/...  |  Raspberry Pi            |
  | (ESP-IDF, C)| ------------------------> |                          |
  |  SHT31/     |         tcp 1883          |  +--------------------+  |
  |  BME280     |                           |  | Mosquitto broker   |  |
  +-------------+                           |  | 1883 / 8883 (TLS)  |  |
                                            |  +---------+----------+  |
                                            |            | subscribe   |
                                            |            v             |
                                            |  +--------------------+  |
                                            |  | Python gateway     |  |
                                            |  | - validate JSON    |  |
                                            |  | - store & forward  |  |
                                            |  |   (SQLite queue)    |  |
                                            |  +---------+----------+  |
                                            +------------|-------------+
                                                         | MQTT/TLS 8883
                                                         v
                                               +--------------------+
                                               |  Cloud MQTT broker |
                                               +--------------------+
```

## Repository layout

```
esp32_node/      ESP-IDF firmware for the edge node
gateway/         Python gateway service (paho-mqtt + SQLite)
config/          Mosquitto config and systemd unit
dashboard/       Node-RED flow for a quick dashboard
```

## Message format

Topic: `factory/line1/node1/telemetry`

```json
{"node_id": "node1", "temp": 24.2, "hum": 55.1, "uptime": 120}
```

Any message that does not match this schema is logged and dropped by the
gateway.

---

## 1. ESP32 edge node

Built with ESP-IDF. The sensor read in `main.c` is faked (a random walk) so the
node works with no hardware attached; replace `read_sensor()` with a real
SHT31/BME280 I2C read when you wire up the part.

Edit the configuration block at the top of `esp32_node/main/main.c`:

```c
#define WIFI_SSID       "your-ssid"
#define WIFI_PASSWORD   "your-password"
#define MQTT_BROKER_URI "mqtt://192.168.1.10:1883"   /* Raspberry Pi LAN IP */
```

Build and flash:

```bash
cd esp32_node
idf.py set-target esp32
idf.py build
idf.py -p /dev/ttyUSB0 flash monitor
```

---

## 2. Raspberry Pi dependencies

On Raspberry Pi OS / Debian:

```bash
sudo apt update
sudo apt install -y mosquitto mosquitto-clients python3 python3-venv
```

Set up the gateway in `/opt/iot-gateway`:

```bash
sudo mkdir -p /opt/iot-gateway
sudo cp -r gateway /opt/iot-gateway/
cd /opt/iot-gateway
sudo python3 -m venv venv
sudo ./venv/bin/pip install -r gateway/requirements.txt
```

Edit the configuration block at the top of `gateway/app.py` with your local
topic and your cloud broker host, credentials, and CA certificate path.

---

## 3. Mosquitto: TLS and authentication

### Username / password

```bash
sudo mosquitto_passwd -c /etc/mosquitto/passwd gateway
# enter a password when prompted; add more users without -c
```

### Self-signed certificates

```bash
# Certificate authority
openssl req -new -x509 -days 3650 -extensions v3_ca \
  -keyout ca.key -out ca.crt -nodes -subj "/CN=iot-ca"

# Server key and signing request (CN must match the broker hostname/IP)
openssl req -new -nodes -keyout server.key -out server.csr \
  -subj "/CN=raspberrypi.local"

# Sign the server certificate with the CA
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key \
  -CAcreateserial -out server.crt -days 3650

sudo mkdir -p /etc/mosquitto/certs
sudo cp ca.crt server.crt server.key /etc/mosquitto/certs/
sudo chown mosquitto: /etc/mosquitto/certs/*
```

### Apply the config

```bash
sudo cp config/mosquitto.conf /etc/mosquitto/conf.d/iot-gateway.conf
sudo systemctl restart mosquitto
```

Copy `ca.crt` to the gateway host and point `CLOUD_CA_CERT` in `app.py` at it
(and distribute the CA to your cloud client as well). Test the TLS listener:

```bash
mosquitto_sub -h raspberrypi.local -p 8883 --cafile ca.crt \
  -u gateway -P yourpassword -t 'factory/#'
```

---

## 4. Run the gateway as a systemd service

```bash
sudo cp config/iot-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now iot-gateway
```

Check status and logs:

```bash
systemctl status iot-gateway
journalctl -u iot-gateway -f
```

The unit sets `Restart=always`, so the service comes back after a crash or a
reboot.

---

## 5. Dashboard

### Node-RED

```bash
sudo apt install -y nodejs npm
sudo npm install -g --unsafe-perm node-red node-red-dashboard
node-red
```

Open `http://raspberrypi.local:1880`, then import `dashboard/node-red-flow.json`
from the menu. The dashboard lives at `http://raspberrypi.local:1880/ui`.

### Grafana

Grafana does not read MQTT directly. The common path is MQTT -> InfluxDB ->
Grafana: run Telegraf with its `mqtt_consumer` input subscribed to
`factory/line1/node1/telemetry`, write into InfluxDB, and add InfluxDB as a
Grafana data source.
