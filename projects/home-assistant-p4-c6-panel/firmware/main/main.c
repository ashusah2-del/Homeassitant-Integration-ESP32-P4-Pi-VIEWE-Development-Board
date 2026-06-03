#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "mqtt_client.h"
#include "nvs_flash.h"

static const char *TAG = "ha_panel";
static EventGroupHandle_t s_wifi_event_group;
static esp_mqtt_client_handle_t s_mqtt_client;

#define WIFI_CONNECTED_BIT BIT0
#define WIFI_MAX_RETRY 10

static int s_retry_num;
static uint8_t s_mac[6];

static void mqtt_publish_json(const char *topic, const char *payload, bool retained)
{
    if (s_mqtt_client == NULL) {
        return;
    }
    esp_mqtt_client_publish(s_mqtt_client, topic, payload, 0, 1, retained ? 1 : 0);
}

static void publish_ha_discovery(void)
{
    char topic[256];
    char payload[1024];

    const char *dev_name = CONFIG_HA_PANEL_DEVICE_NAME;
    const char *prefix = CONFIG_HA_PANEL_TOPIC_PREFIX;

    snprintf(topic, sizeof(topic), "homeassistant/sensor/%s/status/config", dev_name);
    snprintf(payload, sizeof(payload),
             "{"
             "\"name\":\"%s Status\","
             "\"uniq_id\":\"%s_status\","
             "\"stat_t\":\"%s/status\","
             "\"dev_cla\":\"connectivity\","
             "\"pl_on\":\"online\","
             "\"pl_off\":\"offline\","
             "\"dev\":{\"ids\":[\"%s\"],\"name\":\"%s\",\"mf\":\"VIEWE\",\"mdl\":\"ESP32-P4-C6 7inch Panel\"}"
             "}",
             dev_name, dev_name, prefix, dev_name, dev_name);
    mqtt_publish_json(topic, payload, true);

    snprintf(topic, sizeof(topic), "homeassistant/sensor/%s/uptime/config", dev_name);
    snprintf(payload, sizeof(payload),
             "{"
             "\"name\":\"%s Uptime\","
             "\"uniq_id\":\"%s_uptime\","
             "\"stat_t\":\"%s/uptime_s\","
             "\"unit_of_meas\":\"s\","
             "\"stat_cla\":\"measurement\","
             "\"dev\":{\"ids\":[\"%s\"]}"
             "}",
             dev_name, dev_name, prefix, dev_name);
    mqtt_publish_json(topic, payload, true);

    snprintf(topic, sizeof(topic), "homeassistant/sensor/%s/camera_url/config", dev_name);
    snprintf(payload, sizeof(payload),
             "{"
             "\"name\":\"%s Camera Stream URL\","
             "\"uniq_id\":\"%s_camera_url\","
             "\"stat_t\":\"%s/camera/stream_url\","
             "\"icon\":\"mdi:cctv\","
             "\"dev\":{\"ids\":[\"%s\"]}"
             "}",
             dev_name, dev_name, prefix, dev_name);
    mqtt_publish_json(topic, payload, true);

    snprintf(topic, sizeof(topic), "homeassistant/event/%s/touch/config", dev_name);
    snprintf(payload, sizeof(payload),
             "{"
             "\"name\":\"%s Touch Event\","
             "\"uniq_id\":\"%s_touch_event\","
             "\"state_topic\":\"%s/touch\","
             "\"event_types\":[\"tap\",\"swipe\"],"
             "\"dev\":{\"ids\":[\"%s\"]}"
             "}",
             dev_name, dev_name, prefix, dev_name);
    mqtt_publish_json(topic, payload, true);
}

static void publish_runtime_state(void)
{
    char topic[256];
    char payload[128];
    const char *prefix = CONFIG_HA_PANEL_TOPIC_PREFIX;

    snprintf(topic, sizeof(topic), "%s/status", prefix);
    mqtt_publish_json(topic, "online", true);

    snprintf(topic, sizeof(topic), "%s/uptime_s", prefix);
    snprintf(payload, sizeof(payload), "%lu", (unsigned long)(esp_log_timestamp() / 1000));
    mqtt_publish_json(topic, payload, false);

    snprintf(topic, sizeof(topic), "%s/camera/stream_url", prefix);
    mqtt_publish_json(topic, CONFIG_HA_PANEL_CAMERA_STREAM_URL, true);
}

static void mqtt_event_handler(void *handler_args,
                               esp_event_base_t base,
                               int32_t event_id,
                               void *event_data)
{
    (void)handler_args;
    (void)base;
    esp_mqtt_event_handle_t event = event_data;

    if (event_id == MQTT_EVENT_CONNECTED) {
        ESP_LOGI(TAG, "MQTT connected");
        publish_ha_discovery();
        publish_runtime_state();
    }
}

static void wifi_event_handler(void *arg,
                               esp_event_base_t event_base,
                               int32_t event_id,
                               void *event_data)
{
    (void)arg;
    (void)event_data;

    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        if (s_retry_num < WIFI_MAX_RETRY) {
            esp_wifi_connect();
            s_retry_num++;
            ESP_LOGW(TAG, "retry to connect to AP");
        } else {
            xEventGroupClearBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
        }
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        s_retry_num = 0;
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

static esp_err_t wifi_init_sta(void)
{
    s_wifi_event_group = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    esp_event_handler_instance_t instance_any_id;
    esp_event_handler_instance_t instance_got_ip;
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL, &instance_any_id));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL, &instance_got_ip));

    wifi_config_t wifi_config = {0};
    strlcpy((char *)wifi_config.sta.ssid, CONFIG_HA_PANEL_WIFI_SSID, sizeof(wifi_config.sta.ssid));
    strlcpy((char *)wifi_config.sta.password, CONFIG_HA_PANEL_WIFI_PASSWORD, sizeof(wifi_config.sta.password));
    wifi_config.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());

    EventBits_t bits = xEventGroupWaitBits(
        s_wifi_event_group,
        WIFI_CONNECTED_BIT,
        pdFALSE,
        pdFALSE,
        pdMS_TO_TICKS(30000));

    if ((bits & WIFI_CONNECTED_BIT) == 0) {
        return ESP_FAIL;
    }
    return ESP_OK;
}

static void mqtt_start(void)
{
    esp_mqtt_client_config_t mqtt_cfg = {
        .broker.address.uri = CONFIG_HA_PANEL_MQTT_URI,
    };

    s_mqtt_client = esp_mqtt_client_init(&mqtt_cfg);
    esp_mqtt_client_register_event(s_mqtt_client, ESP_EVENT_ANY_ID, mqtt_event_handler, NULL);
    esp_mqtt_client_start(s_mqtt_client);
}

static void heartbeat_task(void *arg)
{
    (void)arg;
    while (1) {
        publish_runtime_state();

        // Placeholder touch event for HA event entity wiring.
        char topic[256];
        const char *prefix = CONFIG_HA_PANEL_TOPIC_PREFIX;
        snprintf(topic, sizeof(topic), "%s/touch", prefix);
        mqtt_publish_json(topic, "tap", false);

        vTaskDelay(pdMS_TO_TICKS(15000));
    }
}

void app_main(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    esp_read_mac(s_mac, ESP_MAC_WIFI_STA);
    ESP_LOGI(TAG, "Device MAC: %02X:%02X:%02X:%02X:%02X:%02X",
             s_mac[0], s_mac[1], s_mac[2], s_mac[3], s_mac[4], s_mac[5]);

    ESP_ERROR_CHECK(wifi_init_sta());
    mqtt_start();

    xTaskCreate(heartbeat_task, "ha_heartbeat", 4096, NULL, 5, NULL);
}
