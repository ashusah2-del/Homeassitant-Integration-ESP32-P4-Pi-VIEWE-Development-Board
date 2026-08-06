// TEMPORARY hardware-isolation test (2026-08-06). Reads the MSM261D PDM mic
// with RAW ESP-IDF calls — byte-for-byte the vendor BSP config
// (esp32_p4_ultra / examples/esp-idf/04-mic_msm261d) — bypassing ESPHome's
// i2s_audio component entirely. If this reads real audio, the ESPHome
// component is at fault; if it also reads zeros, the mic hardware on this
// unit is dead. Cycles LEFT/RIGHT/BOTH PDM slot masks so one flash tests
// every possibility. Delete this file and its config once done.
#pragma once
#include "esphome/core/log.h"
#include <driver/i2s_pdm.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <math.h>

namespace mic_hw_test {

static const char *const TAG = "mic_hw";

inline void read_slot(const char *name, i2s_pdm_slot_mask_t mask) {
  i2s_chan_handle_t rx = nullptr;
  i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
  if (i2s_new_channel(&chan_cfg, nullptr, &rx) != ESP_OK) {
    ESP_LOGE(TAG, "%s: i2s_new_channel failed (I2S0 busy?)", name);
    return;
  }
  i2s_pdm_rx_slot_config_t slot_cfg =
      I2S_PDM_RX_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO);
  slot_cfg.slot_mask = mask;
  i2s_pdm_rx_config_t pdm_cfg = {
      .clk_cfg = I2S_PDM_RX_CLK_DEFAULT_CONFIG(16000),
      .slot_cfg = slot_cfg,
      .gpio_cfg =
          {
              .clk = GPIO_NUM_26,
              .din = GPIO_NUM_27,
              .invert_flags = {.clk_inv = false},
          },
  };
  if (i2s_channel_init_pdm_rx_mode(rx, &pdm_cfg) != ESP_OK) {
    ESP_LOGE(TAG, "%s: init_pdm_rx_mode failed", name);
    i2s_del_channel(rx);
    return;
  }
  i2s_channel_enable(rx);
  static int16_t buf[1024];
  int32_t max_peak = 0;
  double rms_sum = 0;
  int iters = 0;
  for (int t = 0; t < 6; t++) {  // ~3 s per slot
    size_t br = 0;
    if (i2s_channel_read(rx, buf, sizeof(buf), &br, pdMS_TO_TICKS(500)) != ESP_OK)
      continue;
    size_t n = br / 2;
    int32_t peak = 0;
    int64_t sq = 0;
    for (size_t i = 0; i < n; i++) {
      int16_t s = buf[i];
      int32_t a = s < 0 ? -s : s;
      if (a > peak)
        peak = a;
      sq += (int64_t) s * s;
    }
    double rms = n ? sqrt((double) sq / (double) n) : 0.0;
    if (peak > max_peak)
      max_peak = peak;
    rms_sum += rms;
    iters++;
  }
  i2s_channel_disable(rx);
  i2s_del_channel(rx);
  ESP_LOGW(TAG, "SLOT %-5s -> max_peak=%d  avg_rms=%.1f   %s", name, (int) max_peak,
           iters ? rms_sum / iters : 0.0,
           max_peak > 20 ? "*** SIGNAL ***" : "(silent)");
}

inline void task(void *arg) {
  vTaskDelay(pdMS_TO_TICKS(4000));
  ESP_LOGW(TAG, "==== RAW BSP PDM mic test: I2S0, GPIO26/27, bclk_div=8. TALK/TAP THE MIC NOW ====");
  while (true) {
    read_slot("LEFT", I2S_PDM_SLOT_LEFT);
    read_slot("RIGHT", I2S_PDM_SLOT_RIGHT);
    read_slot("BOTH", I2S_PDM_SLOT_BOTH);
    ESP_LOGW(TAG, "---- cycle done, repeating (speak to see peak/rms rise) ----");
  }
}

}  // namespace mic_hw_test

inline void mic_hw_test_start() {
  static bool started = false;
  if (started)
    return;
  started = true;
  xTaskCreatePinnedToCore(mic_hw_test::task, "mic_hw", 8192, nullptr, 5, nullptr, 1);
}
