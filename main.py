import asyncio
import time
import cv2
import ffmpeg
import os
import re
import gc
import random
from fastapi import FastAPI, Query, Response
from contextlib import asynccontextmanager
from prometheus_client import CollectorRegistry, Gauge, generate_latest
from onvif import ONVIFCamera
import urllib.parse
import datetime

# --- 配置区 ---
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "3"))  # 后台工作线程数
CACHE_TTL = int(os.getenv("CACHE_TTL", "90"))  # 缓存绝对过期时间（超期返回全0）
REFRESH_THRESHOLD = int(os.getenv("REFRESH_THRESHOLD", "50"))  # 静默刷新阈值（超期触发后台任务）
BLACK_THRESHOLD = int(os.getenv("BLACK_THRESHOLD", "15"))

# --- 全局状态 (任务队列架构) ---
cache = {}
probe_queue = asyncio.Queue()  # 任务队列
probing_targets = set()  # 正在探测的任务集合（防止同一个IP重复排队）


def sync_detect_stream(stream_uri: str):
    # ==========================================
    # (保持你原有的逻辑不变)
    # ==========================================
    stream_exists = False
    is_black = False
    audio_volume_db = -91.0
    brightness = 0.0
    contrast = 0.0
    saturation = 0.0
    rb_ratio = 1.0

    try:
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|threads;1|timeout;3000"
        cap = cv2.VideoCapture(stream_uri)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        if cap.isOpened():
            ret, frame = cap.read()
            if ret:
                stream_exists = True
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                brightness = cv2.mean(gray)[0]
                if brightness < BLACK_THRESHOLD:
                    is_black = True
                _, std_dev = cv2.meanStdDev(gray)
                contrast = std_dev[0][0]
                hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
                saturation = cv2.mean(hsv[:, :, 1])[0]
                mean_bgr = cv2.mean(frame)
                mean_b = mean_bgr[0]
                mean_r = mean_bgr[2]
                rb_ratio = mean_r / (mean_b + 1e-5)
        cap.release()

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
            except Exception:
                pass
    except Exception as e:
        print(f"流检测异常: {e}")
    finally:
        if 'cap' in locals():
            cap.release()
        if 'frame' in locals():
            del frame

    return stream_exists, is_black, audio_volume_db, brightness, contrast, saturation, rb_ratio


def sync_onvif_probe(target: str, user: str, password: str, port: int = 80):
    # ==========================================
    # (保持你原有的逻辑不变，包含 0.0.0.0 补丁)
    # ==========================================
    try:
        cam = ONVIFCamera(target, port, user, password)
        fixed_netloc = f"{target}:{port}" if port != 80 else target

        for namespace, wrong_url in cam.xaddrs.items():
            parsed = urllib.parse.urlparse(wrong_url)
            cam.xaddrs[namespace] = parsed._replace(netloc=fixed_netloc).geturl()

        if hasattr(cam.devicemgmt, 'url'):
            parsed = urllib.parse.urlparse(cam.devicemgmt.url)
            cam.devicemgmt.url = parsed._replace(netloc=fixed_netloc).geturl()

        dev_info = cam.devicemgmt.GetDeviceInformation()

        mac_address = "unknown"
        try:
            net_interfaces = cam.devicemgmt.GetNetworkInterfaces()
            if net_interfaces:
                mac_address = net_interfaces[0].Info.HwAddress
        except Exception:
            pass

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
                time_drift = time.time() - cam_dt.timestamp()
        except Exception:
            pass

        media_service = cam.create_media_service()
        profiles = media_service.GetProfiles()
        if not profiles:
            raise Exception("未找到媒体配置文件 (Media Profiles)")

        token = profiles[0].token
        req = media_service.create_type('GetStreamUri')
        req.ProfileToken = token
        req.StreamSetup = {'Stream': 'RTP-Unicast', 'Transport': {'Protocol': 'RTSP'}}

        stream_uri = media_service.GetStreamUri(req).Uri
        parsed_rtsp = urllib.parse.urlparse(stream_uri)
        if parsed_rtsp.hostname in ['0.0.0.0', '127.0.0.1', 'localhost']:
            rtsp_port = parsed_rtsp.port if parsed_rtsp.port else 554
            fixed_rtsp_netloc = f"{target}:{rtsp_port}"
            stream_uri = parsed_rtsp._replace(netloc=fixed_rtsp_netloc).geturl()

        video_conf = profiles[0].VideoEncoderConfiguration
        video_metrics = {
            "width": video_conf.Resolution.Width,
            "height": video_conf.Resolution.Height,
            "fps": video_conf.RateControl.FrameRateLimit if hasattr(video_conf, 'RateControl') else 0,
            "encoding": video_conf.Encoding
        }

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
    # ==========================================
    # (保持你原有的组装 Metrics 逻辑不变)
    # ==========================================
    registry = CollectorRegistry()
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
    metric_time_drift = Gauge('onvif_system_time_drift_seconds', '摄像头时间与监控服务器时间的差值(秒)',
                              registry=registry)
    metric_video_width = Gauge('onvif_video_resolution_width', '主码流分辨率宽度', registry=registry)
    metric_video_height = Gauge('onvif_video_resolution_height', '主码流分辨率高度', registry=registry)
    metric_video_fps = Gauge('onvif_video_framerate_limit', '主码流帧率限制', registry=registry)
    metric_cv_brightness = Gauge('onvif_video_cv_brightness', '图像平均亮度 (0-255)', registry=registry)
    metric_cv_contrast = Gauge('onvif_video_cv_contrast', '图像对比度/清晰度 (标准差)', registry=registry)
    metric_cv_saturation = Gauge('onvif_video_cv_saturation', '图像平均饱和度 (0-255)', registry=registry)
    metric_cv_rb_ratio = Gauge('onvif_video_cv_red_blue_ratio', '图像红蓝通道比值 (检测IR-Cut变色)', registry=registry)

    metric_success.set(0)

    try:
        dev_info, stream_uri, ptz_data, mac_address, time_drift, video_metrics = await asyncio.to_thread(
            sync_onvif_probe, target, user, password, port
        )

        metric_device_info.labels(
            manufacturer=dev_info.Manufacturer, model=dev_info.Model, firmware=dev_info.FirmwareVersion,
            mac=mac_address, encoding=video_metrics['encoding']
        ).set(1.0)

        metric_time_drift.set(time_drift)
        metric_video_width.set(video_metrics['width'])
        metric_video_height.set(video_metrics['height'])
        metric_video_fps.set(video_metrics['fps'])

        if ptz_data:
            metric_ptz_supported.set(1)
            metric_ptz_pan.set(ptz_data['pan'])
            metric_ptz_tilt.set(ptz_data['tilt'])
            metric_ptz_zoom.set(ptz_data['zoom'])
        else:
            metric_ptz_supported.set(0)

        auth_uri = stream_uri.replace("rtsp://", f"rtsp://{user}:{password}@")
        stream_exists, is_black, audio_volume_db, brightness, contrast, saturation, rb_ratio = await asyncio.to_thread(
            sync_detect_stream, auth_uri)

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


# ==========================================
# 🚀 核心架构升级：后台工作池 (Worker Pool)
# ==========================================
async def probe_worker():
    """后台工作线程，不断从队列中取出探测任务执行"""
    while True:
        target, user, password, port = await probe_queue.get()
        cache_key = f"{target}_{port}"

        try:
            # 错峰缓冲，避免多个 worker 同时启动导致 CPU/内存 瞬间毛刺
            await asyncio.sleep(random.uniform(0.5, 2.0))

            # 执行真实耗时的探测任务
            metrics_data = await perform_probe(target, user, password, port)

            # 更新全局缓存
            cache[cache_key] = {
                "data": metrics_data,
                "time": time.time()
            }
        except Exception as e:
            print(f"后台工作线程执行异常: {e}")
        finally:
            # 探测完毕，将其移出正在探测的集合，允许下次重复排队
            if cache_key in probing_targets:
                probing_targets.remove(cache_key)
            probe_queue.task_done()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 生命周期管理器：启动时创建后台工作池"""
    workers = []
    for _ in range(MAX_CONCURRENCY):
        worker = asyncio.create_task(probe_worker())
        workers.append(worker)
    yield
    # 程序关闭时清理
    for w in workers:
        w.cancel()


app = FastAPI(title="ONVIF Exporter", lifespan=lifespan)


# ==========================================


def get_fallback_metrics():
    """生成一个只有 probe_success=0 的兜底返回数据"""
    registry = CollectorRegistry()
    metric_success = Gauge('probe_success', '探测是否整体成功 (1成功, 0失败)', registry=registry)
    metric_success.set(0)
    return generate_latest(registry)


@app.get("/probe")
async def probe(
        target: str = Query(..., description="摄像机IP地址"),
        user: str = Query("admin", description="ONVIF用户名"),
        password: str = Query(..., description="ONVIF密码"),
        port: int = Query(80, description="ONVIF端口")
):
    """
    非阻塞抓取端点 (Stale-while-revalidate 模式)
    """
    now = time.time()
    cache_key = f"{target}_{port}"

    # 获取缓存年龄
    cache_age = now - cache[cache_key]['time'] if cache_key in cache else float('inf')

    # 逻辑 1：如果缓存极新（< 50秒），直接返回，不需要触发后台刷新
    if cache_age < REFRESH_THRESHOLD:
        return Response(cache[cache_key]['data'], media_type="text/plain")

    # 逻辑 2：无论是因为没有缓存，还是缓存过期，都需要触发一次后台探测任务
    if cache_key not in probing_targets:
        probing_targets.add(cache_key)
        # 将任务压入后台队列排队，立刻往下走，绝不阻塞当前 HTTP 请求
        probe_queue.put_nowait((target, user, password, port))

    # 逻辑 3：决定给 Prometheus 吐什么数据
    if cache_age < CACHE_TTL:
        # 缓存虽旧(50~90秒)，但还能凑合看，返回旧缓存，维持监控曲线平滑
        return Response(cache[cache_key]['data'], media_type="text/plain")
    else:
        # 缓存彻底过期（>90秒）或者纯新人没缓存，返回全0失败数据
        fallback_data = get_fallback_metrics()
        return Response(fallback_data, media_type="text/plain")


@app.get("/metrics")
async def metrics():
    return Response("# ONVIF Exporter is running.\n", media_type="text/plain")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9121)