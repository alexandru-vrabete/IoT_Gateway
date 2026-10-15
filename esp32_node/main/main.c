/*
 * Edge node: reads a temperature/humidity sensor and publishes the values
 * over MQTT to the local Mosquitto broker running on the Raspberry Pi.
 *
 * The sensor read is faked here (random walk) so the node works without any
 * hardware wired up. Replace read_sensor() with a real SHT31 / BME280 driver
 * call when you have the part connected to the I2C bus.
 */

#include <stdio.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"

#include "esp_system.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_random.h"
#include "nvs_flash.h"

#include "mqtt_client.h"

/* ---- user configuration -------------------------------------------------- */

#define WIFI_SSID          "your-ssid"
#define WIFI_PASSWORD      "your-password"

/* Local IP of the Raspberry Pi running Mosquitto. Use mqtt:// on the LAN. */
#define MQTT_BROKER_URI    "mqtt://192.168.1.10:1883"

#define MQTT_TOPIC         "factory/line1/node1/telemetry"
#define NODE_ID            "node1"
#define PUBLISH_PERIOD_MS  5000

/* -------------------------------------------------------------------------- */

static const char *TAG = "node1";

static EventGroupHandle_t s_wifi_events;
#define WIFI_CONNECTED_BIT BIT0

static esp_mqtt_client_handle_t s_mqtt_client = NULL;
static bool s_mqtt_connected = false;

/* Fake sensor state so the random values drift instead of jumping around. */
static float s_temp = 24.0f;
static float s_hum  = 55.0f;

static void read_sensor(float *temp, float *hum)
{
    /* Random walk in a small band. Swap this out for the real driver read. */
    s_temp += ((float)(esp_random() % 100) - 50) / 100.0f;
    s_hum  += ((float)(esp_random() % 100) - 50) / 100.0f;

    if (s_temp < 18.0f) s_temp = 18.0f;
    if (s_temp > 30.0f) s_temp = 30.0f;
    if (s_hum < 30.0f)  s_hum = 30.0f;
    if (s_hum > 80.0f)  s_hum = 80.0f;

    *temp = s_temp;
    *hum = s_hum;
}

static uint32_t uptime_seconds(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000000);
}

/* ---- WiFi ---------------------------------------------------------------- */

static void wifi_event_handler(void *arg, esp_event_base_t base,
                               int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        ESP_LOGW(TAG, "wifi disconnected, retrying");
        xEventGroupClearBits(s_wifi_events, WIFI_CONNECTED_BIT);
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "got ip " IPSTR, IP2STR(&event->ip_info.ip));
        xEventGroupSetBits(s_wifi_events, WIFI_CONNECTED_BIT);
    }
}

static void wifi_init(void)
{
    s_wifi_events = xEventGroupCreate();

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                               &wifi_event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                               &wifi_event_handler, NULL));

    wifi_config_t wifi_config = {
        .sta = {
            .ssid = WIFI_SSID,
            .password = WIFI_PASSWORD,
        },
    };

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_LOGI(TAG, "connecting to %s", WIFI_SSID);
    xEventGroupWaitBits(s_wifi_events, WIFI_CONNECTED_BIT,
                        pdFALSE, pdTRUE, portMAX_DELAY);
}

/* ---- MQTT ---------------------------------------------------------------- */

static void mqtt_event_handler(void *arg, esp_event_base_t base,
                               int32_t id, void *data)
{
    switch ((esp_mqtt_event_id_t)id) {
    case MQTT_EVENT_CONNECTED:
        ESP_LOGI(TAG, "mqtt connected");
        s_mqtt_connected = true;
        break;
    case MQTT_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "mqtt disconnected");
        s_mqtt_connected = false;
        break;
    case MQTT_EVENT_ERROR:
        ESP_LOGE(TAG, "mqtt error");
        break;
    default:
        break;
    }
}

static void mqtt_init(void)
{
    esp_mqtt_client_config_t cfg = {
        .broker.address.uri = MQTT_BROKER_URI,
    };

    s_mqtt_client = esp_mqtt_client_init(&cfg);
    esp_mqtt_client_register_event(s_mqtt_client, ESP_EVENT_ANY_ID,
                                   mqtt_event_handler, NULL);
    esp_mqtt_client_start(s_mqtt_client);
}

/* ---- main ---------------------------------------------------------------- */

static void publish_task(void *arg)
{
    char payload[128];
    float temp, hum;

    while (1) {
        if (s_mqtt_connected) {
            read_sensor(&temp, &hum);

            snprintf(payload, sizeof(payload),
                     "{\"node_id\":\"%s\",\"temp\":%.1f,\"hum\":%.1f,\"uptime\":%lu}",
                     NODE_ID, temp, hum, (unsigned long)uptime_seconds());

            int msg_id = esp_mqtt_client_publish(s_mqtt_client, MQTT_TOPIC,
                                                 payload, 0, 1, 0);
            ESP_LOGI(TAG, "published (id=%d): %s", msg_id, payload);
        } else {
            ESP_LOGW(TAG, "mqtt down, skipping publish");
        }

        vTaskDelay(pdMS_TO_TICKS(PUBLISH_PERIOD_MS));
    }
}

void app_main(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    wifi_init();
    mqtt_init();

    xTaskCreate(publish_task, "publish_task", 4096, NULL, 5, NULL);
}
