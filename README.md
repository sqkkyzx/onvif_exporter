# ONVIF Exporter

## 📖 项目描述 (Description)

**ONVIF Exporter** 是一个专为安防监控系统和企业级网络设施设计的 Prometheus Exporter。

它采用类似 `blackbox-exporter` 的动态探测（Probe）模式，不仅能够通过 ONVIF 协议获取摄像头的硬件状态（固件版本、云台 PTZ 坐标、系统时间漂移），还能利用 **OpenCV** 和 **FFmpeg** 实时拉取并分析 RTSP 音视频流的画面质量。

通过将计算机视觉技术与传统 IT 监控结合，此 Exporter 可以精准捕捉安防网络中的“哑设备故障”（如：画面黑屏、镜头被遮挡、起雾、收音麦克风损坏、IR-Cut 滤光片卡死导致偏色等），极大提升了弱电与安防网络的运维效率。

---

## ✨ 核心特性 (Features)

- **🚀 动态探测设计**: 通过 `/probe` 端点结合 URL 参数进行按需探测，无需在 Exporter 端维护庞大的设备配置表。
- **⏱️ 时间漂移检测**: 监控摄像头系统时间与服务器时间的偏差，精准告警 NTP 同步失效问题，保证录像取证的时间有效性。
- **🎛️ PTZ 云台追踪**: 实时获取支持云台设备的 Pan, Tilt, Zoom 坐标，防范摄像头被恶意移动造成监控盲区。
- **👁️ CV 画面质量分析**:
  - **连通性**: RTSP 视频流与音频流拉取状态。
  - **黑屏检测**: 基于灰度计算的图像亮度异常告警。
  - **清晰度/对比度检测**: 基于像素标准差检测镜头起雾或模糊。
  - **IR-Cut 偏色检测**: 计算红蓝通道比值，发现硬件级“红眼”偏色故障。
- **⚡ 高并发与算力保护**: 底层基于 FastAPI + AsyncIO 构建，内置线程池与全局并发锁（Semaphore），防止因同时分析大量视频流导致监控服务器 CPU 过载。

---

## 🛠️ 安装与部署 (Installation & Deployment)

### 推荐：使用 Docker 部署

如果你使用的是 Proxmox VE、Kubernetes 或标准 Linux 环境，强烈推荐使用 Docker 部署，这样可以免去配置 `ffmpeg` 和 `opencv` 底层依赖的烦恼。

1. **构建镜像** (确保目录下有 `Dockerfile`, `pyproject.toml`, `uv.lock` 和 `main.py`):
   ```bash
   docker build -t onvif-exporter:latest .
   ```

2. **运行容器** (对外暴露 **9121** 端口):
   ```bash
   docker run -d \
     --name onvif-exporter \
     -p 9121:9121 \
     --restart unless-stopped \
     onvif-exporter:latest
   ```

### 本地物理机运行 (使用 uv/pip)

系统级依赖：请确保宿主机已安装 `ffmpeg` (例如：`apt-get install ffmpeg`)。
```bash
# 使用 uv 同步安装依赖
uv sync

# 启动服务
uvicorn main:app --host 0.0.0.0 --port 9121
```

---

## ⚙️ Prometheus 配置指南

在你的 `prometheus.yml` 中添加以下抓取任务（Scrape Job）。
请利用 `relabel_configs` 将 Prometheus 配置的摄像头 IP 动态映射给 Exporter 的 `/probe` 接口。
```yaml
scrape_configs:
  - job_name: 'onvif_cameras'
    metrics_path: /probe
    # 强烈建议将抓取间隔设置在 60s 或以上，给予音视频解码足够的缓冲时间
    scrape_interval: 60s
    scrape_timeout: 45s
    static_configs:
      - targets:
        - 192.168.1.10
        - 192.168.1.11
        # 在这里继续添加你需要监控的摄像机 IP
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      # 指向你部署的 ONVIF Exporter 地址和 9121 端口
      - target_label: __address__
        replacement: 127.0.0.1:9121
    # 传递 ONVIF 专属账号与密码
    params:
      user: ['admin']          # 请确保该账号具有 ONVIF 访问权限
      password: ['Your_Password']
      port: ['2000']
```

---

## 📊 暴露的指标字典 (Metrics Dictionary)

以下是 Exporter 成功探测后生成的指标列表：

| Metric Name | Type | Description (描述) |
| :--- | :--- | :--- |
| `probe_success` | Gauge | 探测是否整体成功 (1=成功, 0=失败) |
| `onvif_device_info` | Gauge | 静态设备指纹，包含标签：`manufacturer`, `model`, `firmware`, `mac`, `encoding`。值始终为 1.0。 |
| `onvif_video_stream_exists` | Gauge | 是否成功从 RTSP 流中解码出首帧图像 (1=存在, 0=拉流失败) |
| `onvif_system_time_drift_seconds`| Gauge | ⚠️ 摄像头系统时间与 Exporter 服务器时间的偏差（秒）。超过 300 秒应告警 NTP 失效。|
| `onvif_video_resolution_width` | Gauge | 视频主码流当前宽度 (如: 1920, 3840) |
| `onvif_video_resolution_height`| Gauge | 视频主码流当前高度 (如: 1080, 2160) |
| `onvif_video_framerate_limit` | Gauge | 视频主码流最大帧率配置 (如: 25, 30) |
| `onvif_video_is_black_screen` | Gauge | 画面黑屏判定 (1=黑屏/被遮挡, 0=正常) |
| `onvif_video_cv_brightness` | Gauge | 图像平均灰度亮度 (0~255区间，数值极高可能过曝) |
| `onvif_video_cv_contrast` | Gauge | 图像对比度/像素标准差。数值过低提示镜头可能起雾或严重失焦。 |
| `onvif_video_cv_saturation` | Gauge | 图像平均色彩饱和度 (0~255区间) |
| `onvif_video_cv_red_blue_ratio`| Gauge | 红蓝色彩通道比值。正常应接近 1.0，如大幅偏离说明 IR-Cut 滤光片卡死，发生物理级偏色。 |
| `onvif_audio_mean_volume_db` | Gauge | 音频流 1 秒采样周期的均方根音量 (单位: dB)。 |
| `onvif_ptz_supported` | Gauge | 目标设备是否支持云台 PTZ 控制 (1=支持, 0=不支持) |
| `onvif_ptz_pan` | Gauge | 云台当前水平坐标 (Pan) |
| `onvif_ptz_tilt` | Gauge | 云台当前垂直坐标 (Tilt) |
| `onvif_ptz_zoom` | Gauge | 云台当前变焦倍率 (Zoom) |

---

### 最佳实践 (Best Practices for Alerting)

基于以上指标，你可以很轻松地在 Grafana 或 Prometheus 中配置如下告警规则：

* **摄像头离线告警**: `probe_success == 0`
* **录像取证风险（时间不准）**: `abs(onvif_system_time_drift_seconds) > 300`
* **摄像头遭物理遮挡告警**: `onvif_video_is_black_screen == 1`
* **红外滤光片(IR-Cut)机械故障告警**: `onvif_video_cv_red_blue_ratio > 1.5`
* **云台人为恶意篡改告警**: `delta(onvif_ptz_pan[5m]) != 0` (在非系统调度期间)