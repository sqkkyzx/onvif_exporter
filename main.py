import asyncio
import time
import cv2
import ffmpeg
import os
import re
import gc

import random
from fastapi import FastAPI, Query, Response
from prometheus_client import CollectorRegistry, Gauge, generate_latest
from onvif import ONVIFCamera
import urllib.parse
import datetime

app = FastAPI(title="ONVIF Exporter")

# --- 配置区 ---
# 最大并发检测数（避免算力过载）
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "3"))
# 缓存时间（秒），略小于 Prometheus 的 60s 抓取间隔
CACHE_TTL = int(os.getenv("CACHE_TTL", "50"))
# 黑屏检测阈值（0-255，平均像素亮度低于此判定为黑屏）
BLACK_THRESHOLD = int(os.getenv("BLACK_THRESHOLD", "15"))

# --- 全局状态 ---
semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
cache = {}


def sync_detect_stream(stream_uri: str):
    """
    阻塞型：使用 OpenCV 和 FFmpeg 检测视频流质量和音频
    """
    # 默认值
    stream_exists = False
    is_black = False
    audio_volume_db = -91.0

    # 新增的 CV 指标默认值
    brightness = 0.0
    contrast = 0.0
    saturation = 0.0
    rb_ratio = 1.0  # 红蓝偏色比，默认 1.0

    try:
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|threads;1|timeout;3000"
        cap = cv2.VideoCapture(stream_uri)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        if cap.isOpened():
            ret, frame = cap.read()
            if ret:
                stream_exists = True

                # 1. 计算亮度 (基于灰度图的平均值)
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                brightness = cv2.mean(gray)[0]
                if brightness < BLACK_THRESHOLD:
                    is_black = True

                # 2. 计算对比度 (基于灰度图的标准差)
                # 标准差越小，画面色彩分布越集中，即对比度越低（如起雾、全黑、过曝）
                _, std_dev = cv2.meanStdDev(gray)
                contrast = std_dev[0][0]

                # 3. 计算饱和度 (转换到 HSV 空间，提取 S 通道均值)
                hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
                saturation = cv2.mean(hsv[:, :, 1])[0]

                # 4. 计算红蓝通道比值 (伪色温/偏色检测)
                # OpenCV 读取的矩阵顺序是 BGR (蓝, 绿, 红)
                mean_bgr = cv2.mean(frame)
                mean_b = mean_bgr[0]
                mean_r = mean_bgr[2]

                # 防止除零错误，加一个极小值
                rb_ratio = mean_r / (mean_b + 1e-5)

        cap.release()

        # 音频检测逻辑保持不变...
        if stream_exists:
            try:
                out, err = (
                    ffmpeg
                    .input(stream_uri, t=1)
                    .filter('volumedetect')
                    .output('null', f='null')
                    .run(capture_stderr=True, capture_stdout=True, quiet=True)
                )
                err_str = err.decode('utf-8')
                match = re.search(r'mean_volume:\s+([-\d.]+)\s+dB', err_str)
                if match:
                    audio_volume_db = float(match.group(1))
            except Exception as e:
                pass  # 忽略音频错误日志

    except Exception as e:
        print(f"流检测异常: {e}")

    finally:
        if 'cap' in locals():
            cap.release()
        # 显式删除局部变量引用，辅助垃圾回收
        if 'frame' in locals():
            del frame

    # 返回这 7 个指标
    return stream_exists, is_black, audio_volume_db, brightness, contrast, saturation, rb_ratio


def sync_onvif_probe(target: str, user: str, password: str, port: int = 80):
    """阻塞型：连接 ONVIF 获取设备信息、媒体 URI 及各种附加状态 (含 0.0.0.0 地址纠错补丁)"""
    try:
        # 1. 建立基础连接 (此时库会收到带有 0.0.0.0 的瞎扯 XML)
        cam = ONVIFCamera(target, port, user, password)

        # ==========================================
        # 核心补丁：暴力修正所有服务的注册地址
        # ==========================================
        fixed_netloc = f"{target}:{port}" if port != 80 else target

        # 遍历 xaddrs 字典，把所有错乱的 IP (如 0.0.0.0, 127.0.0.1) 替换为我们确知的 Target
        for namespace, wrong_url in cam.xaddrs.items():
            parsed = urllib.parse.urlparse(wrong_url)
            # 替换域名/IP和端口部分
            cam.xaddrs[namespace] = parsed._replace(netloc=fixed_netloc).geturl()

        # 同时修复主设备管理的 URL
        if hasattr(cam.devicemgmt, 'url'):
            parsed = urllib.parse.urlparse(cam.devicemgmt.url)
            cam.devicemgmt.url = parsed._replace(netloc=fixed_netloc).geturl()
        # ==========================================

        # 获取基础设备信息
        dev_info = cam.devicemgmt.GetDeviceInformation()

        # 获取 MAC 地址
        mac_address = "unknown"
        try:
            net_interfaces = cam.devicemgmt.GetNetworkInterfaces()
            if net_interfaces:
                mac_address = net_interfaces[0].Info.HwAddress
        except Exception:
            pass

        # 获取系统时间并计算时间漂移
        time_drift = 0.0
        try:
            sys_time = cam.devicemgmt.GetSystemDateAndTime()
            if sys_time and sys_time.UTCDateTime:
                utc = sys_time.UTCDateTime
                cam_dt = datetime.datetime(
                    utc.Date.Year, utc.Date.Month, utc.Date.Day,
                    utc.Time.Hour, utc.Time.Minute, utc.Time.Second,
                    tzinfo=datetime.timezone.utc
                )
                import time
                time_drift = time.time() - cam_dt.timestamp()
        except Exception:
            pass

        # 2. 创建媒体服务 (此时它会使用我们刚刚修正后的正确地址)
        media_service = cam.create_media_service()

        profiles = media_service.GetProfiles()
        if not profiles:
            raise Exception("未找到媒体配置文件 (Media Profiles)")

        token = profiles[0].token
        req = media_service.create_type('GetStreamUri')
        req.ProfileToken = token
        req.StreamSetup = {'Stream': 'RTP-Unicast', 'Transport': {'Protocol': 'RTSP'}}

        # 获取原始 RTSP 流地址
        stream_uri = media_service.GetStreamUri(req).Uri

        # 二次补丁：防止 RTSP 返回的地址也是 0.0.0.0
        parsed_rtsp = urllib.parse.urlparse(stream_uri)
        if parsed_rtsp.hostname in ['0.0.0.0', '127.0.0.1', 'localhost']:
            # 保留它原有的 RTSP 端口 (通常是 554)，只替换 IP
            rtsp_port = parsed_rtsp.port if parsed_rtsp.port else 554
            fixed_rtsp_netloc = f"{target}:{rtsp_port}"
            stream_uri = parsed_rtsp._replace(netloc=fixed_rtsp_netloc).geturl()

        # 提取视频编码参数
        video_conf = profiles[0].VideoEncoderConfiguration
        video_metrics = {
            "width": video_conf.Resolution.Width,
            "height": video_conf.Resolution.Height,
            "fps": video_conf.RateControl.FrameRateLimit if hasattr(video_conf, 'RateControl') else 0,
            "encoding": video_conf.Encoding
        }

        # 3. 获取 PTZ 状态
        ptz_data = None
        try:
            ptz_service = cam.create_ptz_service()
            status = ptz_service.GetStatus({'ProfileToken': token})
            ptz_data = {
                "pan": status.Position.PanTilt.x,
                "tilt": status.Position.PanTilt.y,
                "zoom": status.Position.Zoom.x
            }
        except Exception:
            pass

        return dev_info, stream_uri, ptz_data, mac_address, time_drift, video_metrics
    except Exception as e:
        raise Exception(f"ONVIF 交互失败: {e}")


async def perform_probe(target: str, user: str, password: str, port: int) -> bytes:
    registry = CollectorRegistry()

    # 原有 Metrics...
    metric_success = Gauge('probe_success', '探测是否整体成功 (1成功, 0失败)', registry=registry)
    metric_stream_exists = Gauge('onvif_video_stream_exists', '视频流是否成功读取', registry=registry)
    metric_is_black = Gauge('onvif_video_is_black_screen', '视频是否为黑屏 (1黑屏, 0正常)', registry=registry)
    metric_audio_vol = Gauge('onvif_audio_mean_volume_db', '音频平均音量 (dB)', registry=registry)
    metric_device_info = Gauge('onvif_device_info', 'ONVIF 设备信息',
                               ['manufacturer', 'model', 'firmware', 'mac', 'encoding'], registry=registry)
    metric_ptz_supported = Gauge('onvif_ptz_supported', '设备支持PTZ', registry=registry)
    metric_ptz_pan = Gauge('onvif_ptz_pan', '云台Pan', registry=registry)
    metric_ptz_tilt = Gauge('onvif_ptz_tilt', '云台Tilt', registry=registry)
    metric_ptz_zoom = Gauge('onvif_ptz_zoom', '变焦Zoom', registry=registry)
    metric_time_drift = Gauge('onvif_system_time_drift_seconds', '摄像头时间与监控服务器时间的差值(秒)', registry=registry)
    metric_video_width = Gauge('onvif_video_resolution_width', '主码流分辨率宽度', registry=registry)
    metric_video_height = Gauge('onvif_video_resolution_height', '主码流分辨率高度', registry=registry)
    metric_video_fps = Gauge('onvif_video_framerate_limit', '主码流帧率限制', registry=registry)

    metric_cv_brightness = Gauge('onvif_video_cv_brightness', '图像平均亮度 (0-255)', registry=registry)
    metric_cv_contrast = Gauge('onvif_video_cv_contrast', '图像对比度/清晰度 (标准差)', registry=registry)
    metric_cv_saturation = Gauge('onvif_video_cv_saturation', '图像平均饱和度 (0-255)', registry=registry)
    metric_cv_rb_ratio = Gauge('onvif_video_cv_red_blue_ratio', '图像红蓝通道比值 (检测IR-Cut变色)', registry=registry)

    metric_success.set(0)

    try:
        # 解包新增的返回值
        dev_info, stream_uri, ptz_data, mac_address, time_drift, video_metrics = await asyncio.to_thread(
            sync_onvif_probe, target, user, password, port
        )

        # 填充设备信息 (加入了 mac 和视频编码格式)
        metric_device_info.labels(
            manufacturer=dev_info.Manufacturer,
            model=dev_info.Model,
            firmware=dev_info.FirmwareVersion,
            mac=mac_address,
            encoding=video_metrics['encoding']
        ).set(1.0)

        # 填充新增指标
        metric_time_drift.set(time_drift)
        metric_video_width.set(video_metrics['width'])
        metric_video_height.set(video_metrics['height'])
        metric_video_fps.set(video_metrics['fps'])

        # 填充 PTZ 数据...
        if ptz_data:
            metric_ptz_supported.set(1)
            metric_ptz_pan.set(ptz_data['pan'])
            metric_ptz_tilt.set(ptz_data['tilt'])
            metric_ptz_zoom.set(ptz_data['zoom'])
        else:
            metric_ptz_supported.set(0)

        # 视频音频算力消耗检测...
        auth_uri = stream_uri.replace("rtsp://", f"rtsp://{user}:{password}@")
        stream_exists, is_black, audio_volume_db, brightness, contrast, saturation, rb_ratio = await asyncio.to_thread(sync_detect_stream, auth_uri)

        metric_cv_brightness.set(brightness)
        metric_cv_contrast.set(contrast)
        metric_cv_saturation.set(saturation)
        metric_cv_rb_ratio.set(rb_ratio)

        metric_stream_exists.set(1 if stream_exists else 0)
        metric_is_black.set(1 if is_black else 0)
        metric_audio_vol.set(audio_volume_db)

        if stream_exists:
            metric_success.set(1)

    except Exception as e:
        print(f"探测目标 {target} 失败: {e}")
    finally:
        gc.collect()
    return generate_latest(registry)


@app.get("/probe")
async def probe(
        target: str = Query(..., description="摄像机IP地址"),
        user: str = Query("admin", description="ONVIF用户名"),
        password: str = Query(..., description="ONVIF密码"),
        port: int = Query(80, description="ONVIF端口")
):
    """
    Prometheus Blackbox 抓取端点
    """
    now = time.time()
    cache_key = f"{target}_{port}"

    # 1. 第一层缓存检查（无锁快速返回）
    if cache_key in cache and (now - cache[cache_key]['time']) < CACHE_TTL:
        return Response(cache[cache_key]['data'], media_type="text/plain")

    # 2. 排队获取并发锁
    async with semaphore:
        await asyncio.sleep(random.uniform(0.5, 2.0))
        # 3. 第二层缓存检查（防止等待锁期间，前面的请求已经刷新了缓存）
        now = time.time()
        if cache_key in cache and (now - cache[cache_key]['time']) < CACHE_TTL:
            return Response(cache[cache_key]['data'], media_type="text/plain")

        # 4. 执行实际检测
        metrics_data = await perform_probe(target, user, password, port)

        # 5. 更新缓存
        cache[cache_key] = {
            "data": metrics_data,
            "time": time.time()
        }

        return Response(metrics_data, media_type="text/plain")


@app.get("/metrics")
async def metrics():
    """Exporter 自身的 Metrics（基础状态探测）"""
    return Response("# ONVIF Exporter is running.\n", media_type="text/plain")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9121)