# ONVIF Exporter

![image](ONVIF%20Exporter%20Banner.png)

## 📖 项目描述 (Description)

**ONVIF Exporter** 是一个专为安防监控系统和企业级网络设施设计的高性能 Prometheus Exporter。

它采用类似 `blackbox-exporter` 的动态探测（Probe）模式，不仅能够通过 ONVIF 协议毫秒级获取摄像头的硬件状态（固件版本、云台 PTZ 坐标、系统时间漂移），还能利用 **OpenCV** 和 **FFmpeg** 异步拉取并分析 RTSP 音视频流的画面质量。

通过将计算机视觉技术与传统 IT 监控结合，本工具可精准捕捉安防网络中的“哑设备故障”（如：画面黑屏、镜头被遮挡、起雾跑焦、IR-Cut 滤光片卡死导致偏色等），极大提升了弱电与安防网络的运维效率。

---

## ✨ 核心特性 (Features)

* **🚀 双轨制解耦架构**: ONVIF 轻量网络请求与重度 CV 视频流解码分离。即便视频流完全卡死，也能保证监控设备在线状态及基础参数（如 PTZ）的实时返回，绝不超时假死。
* **🛡️ 进程级内存防爆 (OOM Protection)**: 针对 4K 级高分辨率视频监控，彻底抛弃线程池解码。将底层 C++ 库（OpenCV/FFmpeg）封装进独立进程池，物理级隔绝内存碎片泄漏，系统常年运行稳如泰山。
* **🎯 动态期望阈值巡检**: 独创的 Label 映射魔法。支持为成百上千个摄像头单独下发 `expected_pan/tilt/zoom` 期望坐标，一旦设备被人为恶意扭动导致偏离，即可触发精准告警。
* **👁️ CV 画面与硬件故障分析**:
* **黑屏/遮挡**: 基于灰度计算的图像异常告警。
* **跑焦/起雾**: 引入拉普拉斯算子 (Laplacian) 计算画面锐度，精准抓取失焦镜头。
* **红外机械故障**: 计算红蓝通道比值，发现硬件级“红眼”偏色故障。
* **PTZ 伪造拦截**: 针对劣质定焦摄像头谎报 PTZ 坐标（0,0,0）的情况，提供严格的拦截机制，保证数据纯洁。


* **🎛️ 摄像机反向控制 (Actuator)**: 提供安全的 `/control` 端点，支持通过 API 一键呼叫云台移动或触发预置位，为自动化修复纠偏提供可能。

---

## 🛠️ 安装与部署 (Installation & Deployment)

我们提供了预构建的开箱即用 Docker 镜像，内置了完整的 Python 环境及 FFmpeg/OpenCV 底层依赖，推荐使用 Docker 部署。

### 方式一：Docker 一键运行

你可以使用 Docker Hub 或 GitHub Container Registry：

```bash
docker run -d \
  --name onvif-exporter \
  -p 9121:9121 \
  --restart unless-stopped \
  -e MAX_CONCURRENCY=3 \
  -e CACHE_TTL=90 \
  -e REFRESH_THRESHOLD=50 \
  -e STRICT_ZERO_PTZ_CHECK=true \
  sqkkyzx/onvif_exporter:latest
  
# 备用镜像源 (GHCR):
# ghcr.io/sqkkyzx/onvif_exporter:latest

```

### 方式二：Docker Compose (推荐)

创建 `docker-compose.yml` 文件：

```yaml
services:
  onvif-exporter:
    image: sqkkyzx/onvif_exporter:latest
    container_name: onvif-exporter
    ports:
      - "9121:9121"
    restart: unless-stopped
    environment:
      # --- 核心性能调优 ---
      - MAX_CONCURRENCY=3          # 视频流解码最大并发进程数 (按内存大小调整)
      - CACHE_TTL=90               # 视频分析缓存最大寿命 (秒)
      - REFRESH_THRESHOLD=50       # 触发后台静默刷新的临界时间 (秒)
      - BLACK_THRESHOLD=15         # 黑屏判定阈值 (0-255)
      - STRICT_ZERO_PTZ_CHECK=true # 开启严格 PTZ 零值拦截 (过滤假 PTZ 设备)
      # --- 安全控制 (可选) ---
      # - EXPORTER_AUTH_USERNAME=admin
      # - EXPORTER_AUTH_PASSWORD=secret

```

---

## ⚙️ Prometheus 配置指南

请在 `prometheus.yml` 中添加抓取任务。我们提供了两种配置范例：

### 1. 基础配置 (仅收集状态)

```yaml
scrape_configs:
  - job_name: 'onvif_cameras'
    metrics_path: /probe
    scrape_interval: 60s
    scrape_timeout: 10s # 由于采用了双轨制架构，超时时间设为10秒即可极速返回
    static_configs:
      - targets:
        - 192.168.1.3
        - 192.168.1.4
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      # 指向你的 ONVIF Exporter 部署地址
      - target_label: __address__
        replacement: 192.168.1.2:9121
    params:
      user: ['admin']
      password: ['secret']
      port: ['2000']

```

### 2. 高阶配置：千机千面 PTZ 偏离告警 🎯

利用 `__` 双下划线魔法，为每个摄像头配置期望坐标，并将其映射为内部参数，查询后自动丢弃标签（防止时序数据断裂）。

```yaml
scrape_configs:
  - job_name: 'onvif_cameras_strict'
    metrics_path: /probe
    scrape_interval: 60s
    static_configs:
      - targets: ["192.168.1.3"]
        labels:
          __expected_pan__: "0.5"
          __expected_tilt__: "0.1"
          __expected_zoom__: "0.7"
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      # 将期望值传递给 Exporter 进行动态指标生成
      - source_labels: [__expected_pan__]
        target_label: __param_expected_pan
      - source_labels: [__expected_tilt__]
        target_label: __param_expected_tilt
      - source_labels: [__expected_zoom__]
        target_label: __param_expected_zoom
      - target_label: __address__
        replacement: 192.168.1.2:9121

```

---

## 📊 核心指标字典 (Metrics Dictionary)

除了标准网络连通性，本 Exporter 会暴露以下高价值安防指标：

| Metric Name | Type | 描述 (Description) |
| --- | --- | --- |
| `probe_success` | Gauge | 探测是否整体成功 (反映 ONVIF 网络连通鉴权是否正常) |
| `onvif_device_info` | Gauge | 静态设备指纹 (包含厂商、型号、固件版本、MAC、视频编码) |
| `onvif_video_stream_exists` | Gauge | 是否成功从 RTSP 流中解码出首帧图像 |
| `onvif_system_time_drift_seconds` | Gauge | ⚠️ 摄像头系统时间与服务器时间的漂移误差（秒） |
| `onvif_video_is_black_screen` | Gauge | 画面黑屏判定 (1=黑屏/被遮挡, 0=正常) |
| `onvif_video_cv_sharpness` | Gauge | 画面清晰度/锐度(Laplacian方差)。数值大幅下降提示跑焦或脏污 |
| `onvif_video_cv_red_blue_ratio` | Gauge | 红蓝色彩通道比值。如大幅偏离 1.0 说明 IR-Cut 滤光片物理卡死 |
| `onvif_imaging_autofocus_enabled` | Gauge | 自动对焦状态 (1=开启自动, 0=手动)。监控自动对焦被意外关闭 |
| `onvif_ptz_pan` / `tilt` / `zoom` | Gauge | 云台当前实际坐标 |
| `onvif_ptz_expected_pan`等 | Gauge | 云台期望坐标 (通过 Prometheus 配置文件注入并生成) |

---

## 🚨 告警规则最佳实践 (Alerting Rules)

基于 Exporter 暴露的高价值指标，可配置以下黄金告警规则：

**1. 录像取证时间失效告警（NTP 异常）**

```promql
abs(onvif_system_time_drift_seconds) > 300

```

**2. 摄像头被人为转动/云台位置异常偏离（机械容差 0.001 度）**

```promql
abs(onvif_ptz_pan - onvif_ptz_expected_pan) > 0.001

```

**3. 镜头严重跑焦 / 画面模糊（基于基线下降）**

```promql
# 当前清晰度低于过去一小时平均水平的 30% 时触发告警
onvif_video_cv_sharpness < (avg_over_time(onvif_video_cv_sharpness[1h]) * 0.3)

```

**4. 硬件滤光片卡死 / 画面呈现红眼伪影**

```promql
onvif_video_cv_red_blue_ratio > 1.8 or onvif_video_cv_red_blue_ratio < 0.5

```