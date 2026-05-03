import asyncio
import time
import cv2
import ffmpeg
import os
import re
import gc
import random
import urllib.parse
import datetime
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Response
from prometheus_client import CollectorRegistry, Gauge, generate_latest
from onvif import ONVIFCamera

app = FastAPI(title="ONVIF Exporter (Pro Edition)")

# --- 配置区 ---
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "3"))  # 后台工作进程数
CACHE_TTL = int(os.getenv("CACHE_TTL", "90"))  # 缓存绝对过期时间
REFRESH_THRESHOLD = int(os.getenv("REFRESH_THRESHOLD", "50"))  # 静默刷新阈值
BLACK_THRESHOLD = int(os.getenv("BLACK_THRESHOLD", "15"))  # 黑屏亮度阈值

# --- 全局状态 (任务队列架构) ---
cache = {}
probe_queue = asyncio.Queue()
probing_targets = set()

# 进程池与线程池 (生命周期内统一管理)
process_pool = None
thread_pool = None


def sync_detect_stream(stream_uri: str):
    """
    阻塞型：视频流质量检测。
    注意：此函数将运行在独立的子进程中，OS会在此函数结束后强制回收所有内存！
    """
    stream_exists = False
    is_black = False
    audio_volume_db = -91.0
    brightness = 0.0
    contrast = 0.0
    saturation = 0.0
    rb_ratio = 1.0
    sharpness = 0.0

    try:
        # 强制 TCP 传输，单线程解码，极短超时限制，防止假死
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|threads;1|timeout;3000"
        cap = cv2.VideoCapture(stream_uri)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        if cap.isOpened():
            ret, frame = cap.read()
            if ret:
                stream_exists = True

                # 图像运算
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

                # 1. 清晰度 (对焦判定)
                sharpness = cv2.Laplacian(gray, cv2.CV_64F).var()

                # 2. 亮度与黑屏
                brightness = cv2.mean(gray)[0]
                if brightness < BLACK_THRESHOLD:
                    is_black = True

                # 3. 对比度
                _, std_dev = cv2.meanStdDev(gray)
                contrast = std_dev[0][0]

                # 4. 饱和度
                hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
                saturation = cv2.mean(hsv[:, :, 1])[0]

                # 5. 红蓝通道比
                mean_bgr = cv2.mean(frame)
                mean_b = mean_bgr[0]
                mean_r = mean_bgr[2]
                rb_ratio = mean_r / (mean_b + 1e-5)

        cap.release()

        # 音频检测
        if stream_exists:
            try:
                out, err = (
                    ffmpeg
                    .input(stream_uri, t=1)
                    .filter('volumedetect')
                    .output('null', f='null')
                    .global_args('-timelimit', '4')  # 防止 ffmpeg 假死
                    .run(capture_stderr=True, capture_stdout=True, quiet=True)
                )
                err_str = err.decode('utf-8')
                match = re.search(r'mean_volume:\s+([-\d.]+)\s+dB', err_str)
                if match:
                    audio_volume_db = float(match.group(1))
            except Exception:
                pass

    except Exception as e:
        print(f"流检测进程异常: {e}")
    finally:
        if 'cap' in locals():
            cap.release()
        if 'frame' in locals():
            del frame

    return stream_exists, is_black, audio_volume_db, brightness, contrast, saturation, rb_ratio, sharpness


def sync_onvif_probe(target: str, user: str, password: str, port: int = 80):
    """阻塞型：ONVIF 协议交互，获取设备全量数据 (含0.0.0.0补丁)"""
    try:
        cam = ONVIFCamera(target, port, user, password)
        fixed_netloc = f"{target}:{port}" if port != 80 else target

        # 核心补丁：修正所有错乱的 WSDL IP
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

        # 二次补丁：修正 RTSP 地址
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

        # 获取对焦状态
        focus_mode_val = -1.0
        try:
            video_source_token = profiles[0].VideoSourceConfiguration.SourceToken
            imaging_service = cam.create_imaging_service()
            imaging_settings = imaging_service.GetImagingSettings({'VideoSourceToken': video_source_token})
            if hasattr(imaging_settings, 'Focus') and imaging_settings.Focus:
                mode = imaging_settings.Focus.AutoFocusMode
                if mode == 'AUTO':
                    focus_mode_val = 1.0
                elif mode == 'MANUAL':
                    focus_mode_val = 0.0
        except Exception:
            pass

        # 获取 PTZ 状态
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

        return dev_info, stream_uri, ptz_data, mac_address, time_drift, video_metrics, focus_mode_val
    except Exception as e:
        raise Exception(f"ONVIF交互失败: {e}")


async def perform_probe(target: str, user: str, password: str, port: int) -> bytes:
    """编排一次完整的探测并生成 Prometheus 格式数据"""
    registry = CollectorRegistry()
    metric_success = Gauge('probe_success', '探测是否整体成功 (1成功, 0失败)', registry=registry)
    metric_stream_exists = Gauge('onvif_video_stream_exists', '视频流是否成功读取', registry=registry)
    metric_is_black = Gauge('onvif_video_is_black_screen', '视频是否黑屏', registry=registry)
    metric_audio_vol = Gauge('onvif_audio_mean_volume_db', '音频平均音量(dB)', registry=registry)
    metric_device_info = Gauge('onvif_device_info', 'ONVIF设备信息',
                               ['manufacturer', 'model', 'firmware', 'mac', 'encoding'], registry=registry)
    metric_ptz_supported = Gauge('onvif_ptz_supported', '设备支持PTZ', registry=registry)
    metric_ptz_pan = Gauge('onvif_ptz_pan', '云台Pan', registry=registry)
    metric_ptz_tilt = Gauge('onvif_ptz_tilt', '云台Tilt', registry=registry)
    metric_ptz_zoom = Gauge('onvif_ptz_zoom', '变焦Zoom', registry=registry)
    metric_time_drift = Gauge('onvif_system_time_drift_seconds', '系统时间漂移(秒)', registry=registry)
    metric_video_width = Gauge('onvif_video_resolution_width', '分辨率宽', registry=registry)
    metric_video_height = Gauge('onvif_video_resolution_height', '分辨率高', registry=registry)
    metric_video_fps = Gauge('onvif_video_framerate_limit', '帧率限制', registry=registry)
    metric_cv_brightness = Gauge('onvif_video_cv_brightness', '图像平均亮度', registry=registry)
    metric_cv_contrast = Gauge('onvif_video_cv_contrast', '图像对比度', registry=registry)
    metric_cv_saturation = Gauge('onvif_video_cv_saturation', '图像平均饱和度', registry=registry)
    metric_cv_rb_ratio = Gauge('onvif_video_cv_red_blue_ratio', '红蓝通道比', registry=registry)
    metric_focus_mode = Gauge('onvif_imaging_autofocus_enabled', '自动对焦开启(1=AUTO, 0=MANUAL, -1=不支持)',
                              registry=registry)
    metric_cv_sharpness = Gauge('onvif_video_cv_sharpness', '画面清晰度(对焦锐度)', registry=registry)

    metric_success.set(0)
    loop = asyncio.get_running_loop()

    try:
        # ONVIF 交互(轻量级，放在线程池即可)
        dev_info, stream_uri, ptz_data, mac_address, time_drift, video_metrics, focus_mode_val = await loop.run_in_executor(
            thread_pool, sync_onvif_probe, target, user, password, port
        )

        metric_device_info.labels(manufacturer=dev_info.Manufacturer, model=dev_info.Model,
                                  firmware=dev_info.FirmwareVersion, mac=mac_address,
                                  encoding=video_metrics['encoding']).set(1.0)
        metric_time_drift.set(time_drift)
        metric_video_width.set(video_metrics['width'])
        metric_video_height.set(video_metrics['height'])
        metric_video_fps.set(video_metrics['fps'])
        metric_focus_mode.set(focus_mode_val)

        if ptz_data:
            metric_ptz_supported.set(1)
            metric_ptz_pan.set(ptz_data['pan'])
            metric_ptz_tilt.set(ptz_data['tilt'])
            metric_ptz_zoom.set(ptz_data['zoom'])
        else:
            metric_ptz_supported.set(0)

        auth_uri = stream_uri.replace("rtsp://", f"rtsp://{user}:{password}@")

        # ====================================================================
        # 💥 核心修改：将最吃内存的 CV/FFmpeg 任务丢进独立的【进程池】中执行 💥
        # 即使底层 C++ 内存泄漏，进程结束后也会被 OS 直接强制物理回收！
        # ====================================================================
        stream_exists, is_black, audio_volume_db, brightness, contrast, saturation, rb_ratio, sharpness = await loop.run_in_executor(
            process_pool, sync_detect_stream, auth_uri
        )

        metric_cv_sharpness.set(sharpness)
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
        print(f"[{target}] 探测失败: {e}")

    return generate_latest(registry)


async def probe_worker():
    """后台工作协程，调度排队的任务"""
    while True:
        target, user, password, port = await probe_queue.get()
        cache_key = f"{target}_{port}"
        try:
            # 错峰缓冲
            await asyncio.sleep(random.uniform(0.5, 2.0))
            metrics_data = await perform_probe(target, user, password, port)
            cache[cache_key] = {"data": metrics_data, "time": time.time()}
        except Exception as e:
            print(f"Worker异常: {e}")
        finally:
            if cache_key in probing_targets:
                probing_targets.remove(cache_key)
            probe_queue.task_done()
            gc.collect()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 生命周期管理器：初始化进程池和线程池"""
    global process_pool, thread_pool

    # 强制修改 multiprocessing 的启动方式为 spawn (解决 Linux 下 fork 导致 OpenCV 卡死的问题)
    try:
        multiprocessing.set_start_method('spawn')
    except RuntimeError:
        pass

    # 初始化进程池负责高消耗的 CV 任务，线程池负责轻量级 ONVIF 任务
    process_pool = ProcessPoolExecutor(max_workers=MAX_CONCURRENCY)
    thread_pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENCY * 2)

    workers = []
    for _ in range(MAX_CONCURRENCY):
        workers.append(asyncio.create_task(probe_worker()))

    yield

    # 关闭服务时释放资源
    for w in workers:
        w.cancel()
    process_pool.shutdown(wait=False)
    thread_pool.shutdown(wait=False)


app = FastAPI(title="ONVIF Exporter", lifespan=lifespan)


def get_fallback_metrics():
    registry = CollectorRegistry()
    Gauge('probe_success', '探测是否整体成功 (1成功, 0失败)', registry=registry).set(0)
    return generate_latest(registry)


@app.get("/probe")
async def probe(
        target: str = Query(..., description="摄像机IP地址"),
        user: str = Query("admin", description="ONVIF用户名"),
        password: str = Query(..., description="ONVIF密码"),
        port: int = Query(80, description="ONVIF端口")
):
    """异步任务队列 + Stale-while-revalidate 端点"""
    now = time.time()
    cache_key = f"{target}_{port}"
    cache_age = now - cache[cache_key]['time'] if cache_key in cache else float('inf')

    # 1. 缓存有效直接返回
    if cache_age < REFRESH_THRESHOLD:
        return Response(cache[cache_key]['data'], media_type="text/plain")

    # 2. 将任务丢入后台队列 (防止重复排队)
    if cache_key not in probing_targets:
        probing_targets.add(cache_key)
        probe_queue.put_nowait((target, user, password, port))

    # 3. 决定返回的数据
    if cache_age < CACHE_TTL:
        return Response(cache[cache_key]['data'], media_type="text/plain")
    else:
        return Response(get_fallback_metrics(), media_type="text/plain")


@app.get("/metrics")
async def metrics():
    return Response("# ONVIF Exporter Core is running.\n", media_type="text/plain")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9121)