# ONVIF Exporter

![image](ONVIF%20Exporter%20Banner.png)

## 📖 项目描述 (Description)

**ONVIF Exporter** 是一个专为安防监控系统和企业级网络设施设计的高性能 Prometheus Exporter。

它采用类似 `blackbox-exporter` 的动态探测（Probe）模式，不仅能够通过 ONVIF 协议毫秒级获取摄像头的硬件状态（固件版本、云台 PTZ 坐标、系统时间漂移），还能利用 **OpenCV** 和 **FFmpeg** 异步拉取并分析 RTSP 音视频流的画面质量。

通过将计算机视觉技术与传统 IT 监控结合，本工具可精准捕捉安防网络中的“哑设备故障”（如：画面黑屏、镜头被遮挡、起雾跑焦、IR-Cut 滤光片卡死导致偏色等），极大提升了弱电与安防网络的运维效率。

---

## ✨ 核心特性 (Features)

* **🚀 双轨制解耦架构**: ONVIF 轻量网络请求与重度 CV 视频流解码分离。即便视频流完全卡死，也能保证监控设备在线状态及基础参数（如 PTZ）的实时返回，绝不超时假死。
* **🛡️ 进程级内存隔离与回收 (OOM Protection)**: 针对 4K 级高分辨率视频监控，彻底抛弃线程池解码。将底层 C++ 库（OpenCV/FFmpeg）封装进独立进程池，并定期重建 CV 子进程，降低 native 内存长期累积导致卡顿的风险。
* **📉 自动低分辨率取流**: 通过 ONVIF `GetProfiles()` 读取所有媒体 Profile，默认选择分辨率最小的一路 RTSP 流进行 CV 分析，降低边缘节点 CPU 与内存压力。
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
  -e CV_MAX_CONCURRENCY=1 \
  -e ONVIF_MAX_CONCURRENCY=4 \
  -e CACHE_TTL=180 \
  -e REFRESH_THRESHOLD=120 \
  -e CV_PROCESS_MAX_TASKS=25 \
  -e CV_QUEUE_MAXSIZE=2 \
  -e CV_CACHE_MAX_ENTRIES=256 \
  -e CV_CACHE_CLEAN_INTERVAL=120 \
  -e BLACK_THRESHOLD=15 \
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
      - CV_MAX_CONCURRENCY=1        # 视频流解码最大并发进程数，2C2G/低内存设备建议固定 1
      - ONVIF_MAX_CONCURRENCY=4     # ONVIF 网络请求线程并发，独立于重内存 CV 分析
      - CACHE_TTL=180               # 视频分析缓存最大寿命 (秒)
      - REFRESH_THRESHOLD=120       # 触发后台静默刷新的临界时间 (秒)
      - CV_PROCESS_MAX_TASKS=25     # 每个 CV 子进程处理多少个任务后重建
      - CV_QUEUE_MAXSIZE=2          # CV 后台队列上限，默认 CV_MAX_CONCURRENCY * 2
      - CV_CACHE_MAX_ENTRIES=256    # CV 缓存最多保留多少个 target
      - CV_CACHE_CLEAN_INTERVAL=120 # CV 过期缓存清理间隔 (秒)
      - BLACK_THRESHOLD=15         # 黑屏判定阈值 (0-255)
      - STRICT_ZERO_PTZ_CHECK=true # 开启严格 PTZ 零值拦截 (过滤假 PTZ 设备)
      - EXPORTER_ACCESS_LOG=false  # 是否输出正常 HTTP 请求访问日志
      - EXPORTER_FFMPEG_VERBOSE_ERROR=false # 是否输出完整 FFmpeg stderr
      - FFMPEG_AUDIO_SAMPLE_SECONDS=2 # 音频音量检测采样时长
      - FFMPEG_AUDIO_TIMEOUT_SECONDS=8 # 音频检测 FFmpeg 子进程超时
      # --- 安全控制 (可选) ---
      # - EXPORTER_AUTH_USERNAME=admin
      # - EXPORTER_AUTH_PASSWORD=secret

```

### 方式三：Debian LXC 二进制运行

每次推送新版后，GitHub Release 会附带 Linux x86_64 二进制压缩包：

```text
onvif-exporter-linux-x86_64-vX.Y.Z.tar.gz
```

在 Debian LXC 里安装运行时依赖并启动：

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg

tar -xzf onvif-exporter-linux-x86_64-vX.Y.Z.tar.gz
cd onvif-exporter-linux-x86_64-vX.Y.Z
chmod +x ./onvif-exporter

CV_MAX_CONCURRENCY=1 ONVIF_MAX_CONCURRENCY=4 ./onvif-exporter
```

二进制内置 Python 运行时和 Python 依赖；`ffmpeg` 仍使用系统命令，用于音频音量检测。服务默认监听 `0.0.0.0:9121`。

---

## ⚙️ 环境变量说明

以下环境变量均为可选；不设置时使用默认值。默认值按低内存边缘节点、跨公网拉取 RTSP 画面、20-100ms 延迟的保守场景设计，优先限制内存峰值并保证长期运行稳定。

| 环境变量 | 默认值 | 用法 |
| --- | --- | --- |
| `CV_MAX_CONCURRENCY` | `1` | CV 视频流分析的最大并发数，同时也是 CV worker 和 CV 子进程数量。2C2G/低内存设备建议保持 `1`；值越大，并发越高，但 CPU、内存、公网带宽和摄像机 RTSP 连接压力也越大。 |
| `MAX_CONCURRENCY` | 未设置 | 旧版兼容别名。未设置 `CV_MAX_CONCURRENCY` 时才会读取它；新部署建议使用 `CV_MAX_CONCURRENCY`。 |
| `ONVIF_MAX_CONCURRENCY` | `4` | ONVIF 协议交互线程并发数。ONVIF 请求主要是网络 I/O，和重内存 CV 分析分开控制，避免降低视频分析并发后基础探测也被过度限制。 |
| `CACHE_TTL` | `180` | CV 分析结果最大可复用时间，单位秒。超过该时间后 `/probe` 会返回默认 CV 指标，直到后台刷新完成。 |
| `REFRESH_THRESHOLD` | `120` | CV 缓存达到多少秒后触发后台静默刷新，单位秒。建议小于 `CACHE_TTL`，这样可以在缓存彻底过期前提前刷新。 |
| `BLACK_THRESHOLD` | `15` | 黑屏判定亮度阈值，范围 `0-255`。平均灰度低于该值时 `onvif_video_is_black_screen=1`。夜间红外场景可适当调低。 |
| `STRICT_ZERO_PTZ_CHECK` | `False` | 是否启用严格 PTZ 全零拦截。设置为 `true`、`1` 或 `yes` 时，如果设备返回 `pan=0, tilt=0, zoom=0`，会判定为 PTZ 异常/不支持。 |
| `CV_PROCESS_MAX_TASKS` | `25` | 每个 CV 子进程最多处理多少次视频分析任务后自动重建。用于释放 OpenCV/FFmpeg 可能累积的 native 内存。设置太小会增加进程重建开销，设置太大则内存回收变慢。 |
| `CV_QUEUE_MAXSIZE` | `CV_MAX_CONCURRENCY * 2` | CV 后台刷新队列最大长度。队列满时会跳过本次后台刷新，避免请求堆积导致内存上涨。 |
| `CV_CACHE_MAX_ENTRIES` | `256` | CV 缓存最多保留多少个 target。适合限制动态 target 或异常请求造成的缓存字典增长。 |
| `CV_CACHE_CLEAN_INTERVAL` | `120` | 清理过期 CV 缓存的间隔，单位秒。清理对象是超过 `CACHE_TTL` 的缓存项。 |
| `EXPORTER_ACCESS_LOG` | `False` | 是否输出正常 HTTP 请求访问日志。默认关闭，避免 Prometheus 高频抓取时日志刷屏。设置为 `true`、`1` 或 `yes` 可打开。 |
| `EXPORTER_FFMPEG_VERBOSE_ERROR` | `False` | 是否输出完整 FFmpeg stderr。默认只输出最后几行关键错误，避免日志过长；排查 RTSP 鉴权、超时、编码兼容问题时可设为 `true`。 |
| `FFMPEG_AUDIO_SAMPLE_SECONDS` | `2` | FFmpeg `volumedetect` 音频采样时长，单位秒。采样越长越稳定，但 CV 后台刷新耗时也会增加。 |
| `FFMPEG_AUDIO_TIMEOUT_SECONDS` | `8` | Python 侧强制终止音频检测 FFmpeg 子进程的超时时间，单位秒。用于避免异常 RTSP 流卡住 CV 子进程。 |
| `EXPORTER_AUTH_USERNAME` | 未设置 | HTTP Basic Auth 用户名。只有同时设置用户名和密码时才开启鉴权。 |
| `EXPORTER_AUTH_PASSWORD` | 未设置 | HTTP Basic Auth 密码。开启后 `/probe`、`/control`、`/metrics` 都需要认证，且自动隐藏 `/docs`。 |

### 调参建议

* 2C2G、Wyse 3040 这类边缘节点建议 `CV_MAX_CONCURRENCY=1`、`CV_QUEUE_MAXSIZE=2`。程序会默认从 ONVIF 媒体 Profile 中选择分辨率最小的一路 RTSP 流用于 CV 分析。
* 4G 内存机器可以按现场情况把 `CV_MAX_CONCURRENCY` 调到 `2`。如果摄像头是 4K、高码率或公网质量不稳，仍然先用 `1`。
* 默认缓存策略是 stale-while-revalidate：`REFRESH_THRESHOLD=120` 后允许后台刷新，但旧 CV 数据最多只复用到 `CACHE_TTL=180`。这样既避免每次 scrape 都拉公网 RTSP，也避免缓存长期不更新。
* 如果程序运行几小时后变卡，优先把 `CV_PROCESS_MAX_TASKS` 调小到 `10-20`，让 CV 子进程更频繁回收。
* 如果 `/metrics` 中 `onvif_exporter_cv_queue_size` 长期接近 `CV_QUEUE_MAXSIZE`，说明视频分析跟不上抓取速度。低内存机器优先增大 `REFRESH_THRESHOLD`、增大 `CACHE_TTL` 或降低 Prometheus `scrape_interval` 频率；只有内存和 CPU 还有余量时，才增加 `CV_MAX_CONCURRENCY`。
* 如果摄像头数量很多，建议让 `CACHE_TTL` 大于 `REFRESH_THRESHOLD` 至少 60 秒，减少缓存过期窗口。

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

## 🎛️ 摄像机控制 API

`/control` 用于通过 ONVIF PTZ 服务控制摄像机。该接口会实时连接目标摄像机并发送控制指令，不使用 CV 缓存。

> 建议只在可信网络或开启 HTTP Basic Auth 后暴露该接口。开启方式见 `EXPORTER_AUTH_USERNAME` 和 `EXPORTER_AUTH_PASSWORD`。

### 接口

```http
GET /control
```

### 通用参数

| 参数 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `target` | 是 | 无 | 摄像机 IP 地址或主机名。 |
| `user` | 否 | `admin` | 摄像机 ONVIF 用户名。 |
| `password` | 是 | 无 | 摄像机 ONVIF 密码。 |
| `port` | 否 | `80` | 摄像机 ONVIF 服务端口。 |
| `mode` | 是 | 无 | 控制模式。可选值：`ptz`、`move`、`preset`。 |

### 模式参数

| mode | 额外参数 | 说明 |
| --- | --- | --- |
| `ptz` | `pan`, `tilt`, `zoom` | 绝对移动到指定 PTZ 坐标。 |
| `move` | `pan`, `tilt`, `zoom` | 相对当前位置偏移指定 PTZ 坐标。 |
| `preset` | `preset` | 调用摄像机内置预置位 token。 |

### 返回值

成功时返回 JSON：

```json
{
  "status": "success",
  "target": "192.168.1.3",
  "message": "AbsoluteMove 到 [Pan:0.5, Tilt:0.1, Zoom:0.7] 成功"
}
```

参数缺失时返回 `400`，ONVIF 控制失败时返回 `500`。

### 示例：绝对移动

```bash
curl "http://127.0.0.1:9121/control?target=192.168.1.3&user=admin&password=secret&port=80&mode=ptz&pan=0.5&tilt=0.1&zoom=0.7"
```

### 示例：相对移动

```bash
curl "http://127.0.0.1:9121/control?target=192.168.1.3&user=admin&password=secret&port=80&mode=move&pan=0.05&tilt=0&zoom=0"
```

### 示例：调用预置位

```bash
curl "http://127.0.0.1:9121/control?target=192.168.1.3&user=admin&password=secret&port=80&mode=preset&preset=1"
```

### 示例：开启 Exporter 鉴权后调用

如果部署时设置了 `EXPORTER_AUTH_USERNAME` 和 `EXPORTER_AUTH_PASSWORD`，需要给 Exporter 自身接口加 Basic Auth：

```bash
curl -u "exporter_user:exporter_password" "http://127.0.0.1:9121/control?target=192.168.1.3&user=admin&password=secret&mode=preset&preset=1"
```

### 注意事项

* `user` 和 `password` 是摄像机的 ONVIF 账号，不是 Exporter 的 HTTP Basic Auth 账号。
* `ptz` 是绝对坐标移动，`move` 是相对偏移；不同厂商的 PTZ 坐标范围可能不同，通常在 `-1.0` 到 `1.0` 或 `0.0` 到 `1.0` 之间。
* `preset` 需要使用摄像机已有的预置位 token，例如 `1`、`2`、`Preset001`，具体取决于设备厂商。
* 当前实现没有单独的 `Stop` 接口；使用 `move` 时建议发送较小偏移量，避免设备持续动作或越界。
* 如果摄像机返回了错误的 ONVIF XAddr，程序会自动把控制服务地址修正为 `target:port`。

---

## 📊 核心指标字典 (Metrics Dictionary)

`/probe` 会按摄像头 target 动态返回设备与音视频质量指标；`/metrics` 返回 Exporter 自身运行状态。

| Metric Name | Type | 来源 | Labels | 描述 (Description) |
| --- | --- | --- | --- | --- |
| `probe_success` | Gauge | `/probe` | 无 | 探测是否整体成功。`1` 表示 ONVIF 网络通信和鉴权成功，`0` 表示失败。 |
| `onvif_device_info` | Gauge | `/probe` | `manufacturer`, `model`, `firmware`, `mac`, `encoding` | 设备静态信息，值固定为 `1`，具体信息通过 labels 表示。 |
| `onvif_system_time_drift_seconds` | Gauge | `/probe` | 无 | 摄像头 UTC 系统时间与 Exporter 服务器时间的漂移秒数。正数表示摄像头时间落后于服务器。 |
| `onvif_video_stream_profile_info` | Gauge | `/probe` | `token`, `name` | 实际用于 RTSP/CV 分析的 ONVIF 媒体 Profile。程序默认选择分辨率最小的 Profile。 |
| `onvif_video_resolution_width` | Gauge | `/probe` | 无 | 实际用于 RTSP/CV 分析的视频编码分辨率宽度。 |
| `onvif_video_resolution_height` | Gauge | `/probe` | 无 | 实际用于 RTSP/CV 分析的视频编码分辨率高度。 |
| `onvif_video_framerate_limit` | Gauge | `/probe` | 无 | 实际用于 RTSP/CV 分析的视频编码帧率上限。 |
| `onvif_imaging_autofocus_enabled` | Gauge | `/probe` | 无 | 自动对焦状态。`1` 表示 `AUTO`，`0` 表示 `MANUAL`，`-1` 表示未获取到或设备不支持。 |
| `onvif_ptz_supported` | Gauge | `/probe` | 无 | 是否成功获取 PTZ 坐标。`1` 表示支持或成功返回，`0` 表示不支持或获取失败。 |
| `onvif_ptz_pan` | Gauge | `/probe` | 无 | 云台 Pan 当前坐标。仅在成功获取 PTZ 数据时设置。 |
| `onvif_ptz_tilt` | Gauge | `/probe` | 无 | 云台 Tilt 当前坐标。仅在成功获取 PTZ 数据时设置。 |
| `onvif_ptz_zoom` | Gauge | `/probe` | 无 | 云台 Zoom 当前坐标。仅在成功获取 PTZ 数据时设置。 |
| `onvif_ptz_expected_pan` | Gauge | `/probe` | 无 | 期望 Pan 坐标。仅当请求参数 `expected_pan` 存在时返回。 |
| `onvif_ptz_expected_tilt` | Gauge | `/probe` | 无 | 期望 Tilt 坐标。仅当请求参数 `expected_tilt` 存在时返回。 |
| `onvif_ptz_expected_zoom` | Gauge | `/probe` | 无 | 期望 Zoom 坐标。仅当请求参数 `expected_zoom` 存在时返回。 |
| `onvif_video_stream_exists` | Gauge | `/probe` | 无 | 是否成功从 RTSP 流中读取首帧。`1` 表示成功，`0` 表示失败、缓存过期或尚未完成首次 CV 分析。 |
| `onvif_video_is_black_screen` | Gauge | `/probe` | 无 | 黑屏判定。`1` 表示平均亮度低于 `BLACK_THRESHOLD`，`0` 表示未判定为黑屏。 |
| `onvif_audio_mean_volume_db` | Gauge | `/probe` | 无 | FFmpeg `volumedetect` 计算得到的平均音量 dB。默认静音/未知值为 `-99.0`。 |
| `onvif_video_cv_brightness` | Gauge | `/probe` | 无 | 首帧灰度平均亮度，范围约 `0-255`。 |
| `onvif_video_cv_contrast` | Gauge | `/probe` | 无 | 首帧灰度标准差，用于表示画面对比度。 |
| `onvif_video_cv_saturation` | Gauge | `/probe` | 无 | HSV 饱和度通道平均值，用于观察画面色彩饱和程度。 |
| `onvif_video_cv_red_blue_ratio` | Gauge | `/probe` | 无 | 红蓝通道均值比值。明显偏离 `1.0` 时可用于发现偏色、IR-Cut 滤光片异常等问题。 |
| `onvif_video_cv_sharpness` | Gauge | `/probe` | 无 | 拉普拉斯方差锐度值。数值大幅下降通常提示跑焦、起雾、脏污或遮挡。 |
| `onvif_exporter_cv_max_concurrency` | Gauge | `/metrics` | 无 | 当前 CV 视频分析最大并发数。 |
| `onvif_exporter_onvif_max_concurrency` | Gauge | `/metrics` | 无 | 当前 ONVIF 网络请求线程最大并发数。 |
| `onvif_exporter_cv_queue_capacity` | Gauge | `/metrics` | 无 | 当前 CV 后台刷新队列容量。 |
| `onvif_exporter_cv_cache_entries` | Gauge | `/metrics` | 无 | Exporter 当前 CV 缓存 target 数量。 |
| `onvif_exporter_cv_queue_size` | Gauge | `/metrics` | 无 | Exporter 当前 CV 后台刷新队列长度。长期接近 `CV_QUEUE_MAXSIZE` 表示后台分析处理不过来。 |
| `onvif_exporter_cv_probing_targets` | Gauge | `/metrics` | 无 | Exporter 当前正在排队或分析的 CV target 数量。 |

---

## 🚨 告警规则最佳实践 (Alerting Rules)

下面是一组可直接放入 Prometheus rule file 的示例。阈值需要按现场基线调整，尤其是清晰度、音量、亮度和红蓝比。

```yaml
groups:
  - name: onvif-exporter.rules
    rules:
      - alert: OnvifCameraProbeFailed
        expr: probe_success == 0
        for: 3m
        labels:
          severity: critical
        annotations:
          summary: "摄像机 ONVIF 探测失败"
          description: "{{ $labels.instance }} ONVIF 通信或鉴权失败超过 3 分钟。"

      - alert: OnvifCameraProbeMissing
        expr: up{job=~"onvif_cameras.*"} == 0
        for: 3m
        labels:
          severity: critical
        annotations:
          summary: "摄像机 scrape 失败"
          description: "{{ $labels.instance }} 连续 3 分钟 scrape 失败，可能是 exporter、网络或 Prometheus 配置异常。"

      - alert: OnvifVideoStreamFailed
        expr: probe_success == 1 and onvif_video_stream_exists == 0
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "摄像机 RTSP 视频流读取失败"
          description: "{{ $labels.instance }} ONVIF 正常，但 RTSP 首帧读取失败或 CV 缓存已过期。"

      - alert: OnvifVideoBlackScreen
        expr: onvif_video_is_black_screen == 1
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "摄像机画面疑似黑屏或遮挡"
          description: "{{ $labels.instance }} 平均亮度低于 BLACK_THRESHOLD，持续 5 分钟。"

      - alert: OnvifVideoTooDark
        expr: onvif_video_cv_brightness > 0 and onvif_video_cv_brightness < 20
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "摄像机画面过暗"
          description: "{{ $labels.instance }} 亮度长期低于 20，请检查补光、遮挡或曝光配置。"

      - alert: OnvifVideoLowContrast
        expr: onvif_video_cv_contrast > 0 and onvif_video_cv_contrast < 8
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "摄像机画面对比度过低"
          description: "{{ $labels.instance }} 对比度长期偏低，可能存在起雾、脏污、遮挡或严重曝光问题。"

      - alert: OnvifVideoLowSharpness
        expr: onvif_video_cv_sharpness > 0 and onvif_video_cv_sharpness < avg_over_time(onvif_video_cv_sharpness[1h]) * 0.3
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "摄像机画面清晰度显著下降"
          description: "{{ $labels.instance }} 当前锐度低于过去 1 小时均值的 30%，可能跑焦、起雾或镜头脏污。"

      - alert: OnvifVideoColorCast
        expr: onvif_video_cv_red_blue_ratio > 1.8 or onvif_video_cv_red_blue_ratio < 0.5
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "摄像机画面疑似偏色"
          description: "{{ $labels.instance }} 红蓝通道比明显异常，可能是 IR-Cut 滤光片或白平衡问题。"

      - alert: OnvifAudioSilent
        expr: onvif_audio_mean_volume_db <= -90
        for: 15m
        labels:
          severity: info
        annotations:
          summary: "摄像机音频疑似静音或无音频"
          description: "{{ $labels.instance }} 平均音量长期接近静音。若该点位不需要音频，可忽略此告警。"

      - alert: OnvifCameraTimeDrift
        expr: abs(onvif_system_time_drift_seconds) > 300
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "摄像机系统时间漂移过大"
          description: "{{ $labels.instance }} 与服务器时间偏差超过 300 秒，可能影响录像取证和事件回溯。"

      - alert: OnvifAutofocusDisabled
        expr: onvif_imaging_autofocus_enabled == 0
        for: 10m
        labels:
          severity: info
        annotations:
          summary: "摄像机自动对焦被关闭"
          description: "{{ $labels.instance }} 当前处于 MANUAL 对焦模式。若该设备应自动对焦，请检查配置。"

      - alert: OnvifPtzUnsupportedOrFailed
        expr: onvif_ptz_supported == 0
        for: 10m
        labels:
          severity: info
        annotations:
          summary: "摄像机 PTZ 坐标不可用"
          description: "{{ $labels.instance }} 未能获取 PTZ 坐标。定焦设备可忽略，球机需要检查 ONVIF PTZ 服务。"

      - alert: OnvifPtzPanDeviation
        expr: abs(onvif_ptz_pan - onvif_ptz_expected_pan) > 0.001
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "摄像机 Pan 坐标偏离期望值"
          description: "{{ $labels.instance }} Pan 当前值与期望值偏差超过 0.001。"

      - alert: OnvifPtzTiltDeviation
        expr: abs(onvif_ptz_tilt - onvif_ptz_expected_tilt) > 0.001
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "摄像机 Tilt 坐标偏离期望值"
          description: "{{ $labels.instance }} Tilt 当前值与期望值偏差超过 0.001。"

      - alert: OnvifPtzZoomDeviation
        expr: abs(onvif_ptz_zoom - onvif_ptz_expected_zoom) > 0.001
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "摄像机 Zoom 坐标偏离期望值"
          description: "{{ $labels.instance }} Zoom 当前值与期望值偏差超过 0.001。"

      - alert: OnvifExporterCvQueueBacklog
        expr: onvif_exporter_cv_queue_size >= 0.8 * onvif_exporter_cv_queue_capacity
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "ONVIF Exporter CV 队列积压"
          description: "CV 后台队列持续高于 80%。低内存机器优先降低抓取频率或拉长 CV 刷新间隔。"

      - alert: OnvifExporterCvCacheTooLarge
        expr: onvif_exporter_cv_cache_entries >= 0.9 * 256
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "ONVIF Exporter CV 缓存接近上限"
          description: "CV 缓存 target 数接近 CV_CACHE_MAX_ENTRIES。如果你修改了 CV_CACHE_MAX_ENTRIES，请同步调整该规则里的 256。"
```

如果使用的是 Prometheus Operator，可以把 `groups` 内容放进 `PrometheusRule` 的 `spec.groups` 下；如果是原生 Prometheus，把以上内容保存为 rule file 并在 `prometheus.yml` 的 `rule_files` 中引用。
