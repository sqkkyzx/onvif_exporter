import asyncio
import logging
import time
import tomllib

import cv2
import ffmpeg
import os
import re
import gc
import random
import secrets
import urllib.parse
import datetime
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Response, Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from prometheus_client import CollectorRegistry, Gauge, generate_latest
from onvif import ONVIFCamera



# ==========================================
# 1. 动态加载项目元数据 (从 pyproject.toml)
# ==========================================
def get_project_metadata():
    try:
        with open("pyproject.toml", "rb") as f:
            data = tomllib.load(f)
            project = data.get("project", {})
            return (
                project.get("name", "onvif-exporter"),
                project.get("version", "unknown"),
                project.get("description", "ONVIF/CV Exporter for Prometheus")
            )
    except Exception as e:
        logger.debug(f"Failed to load project metadata: {e}")
        # 兼容找不到文件的情况
        return "onvif-exporter", "dev", "ONVIF Exporter (Metadata Missing)"

APP_NAME, APP_VERSION, APP_DESC = get_project_metadata()

# ==========================================
# 2. 核心配置与环境变量
# ==========================================

# 同时执行拉取和分析视频流（OpenCV/FFmpeg）的最大任务数
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "3"))
# 分析视频流（OpenCV/FFmpeg）缓存数据能存活的“最大寿命”（单位：秒）。
CACHE_TTL = int(os.getenv("CACHE_TTL", "90"))
# 触发后台更新任务的“时间临界点”（单位：秒）。
REFRESH_THRESHOLD = int(os.getenv("REFRESH_THRESHOLD", "60"))
# 判定画面为“黑屏”的像素平均亮度界限（范围 0 - 255）。
BLACK_THRESHOLD = int(os.getenv("BLACK_THRESHOLD", "15"))

# --- 鉴权环境变量 ---
AUTH_USERNAME = os.getenv("EXPORTER_AUTH_USERNAME")
AUTH_PASSWORD = os.getenv("EXPORTER_AUTH_PASSWORD")


# ==========================================
# 3. HTTP 基础鉴权依赖 (安全比较)
# ==========================================
security = HTTPBasic(auto_error=False)


def verify_auth(credentials: HTTPBasicCredentials | None = Depends(security)):
    """
    HTTP Basic 鉴权验证器。
    如果环境变量未配置，则直接放行 (默认无鉴权)。
    如果已配置，则使用 timing-attack 安全的 compare_digest 进行对比。
    """
    if not AUTH_USERNAME or not AUTH_PASSWORD:
        return True  # 未开启鉴权，放行

    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )

    correct_username = secrets.compare_digest(credentials.username, AUTH_USERNAME)
    correct_password = secrets.compare_digest(credentials.password, AUTH_PASSWORD)

    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True


# ==========================================
# 全局状态池定义
# ==========================================
cv_cache = {}
cv_queue = asyncio.Queue()
cv_probing_targets = set()
process_pool = None
thread_pool = None

logger = logging.getLogger("uvicorn.error")


# ==========================================
# 生命周期与 Banner
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global process_pool, thread_pool
    try:
        multiprocessing.set_start_method('spawn')
    except RuntimeError:
        pass

    process_pool = ProcessPoolExecutor(max_workers=MAX_CONCURRENCY)
    thread_pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENCY * 3)
    workers = [asyncio.create_task(cv_worker()) for _ in range(MAX_CONCURRENCY)]

    # 打印标准化 Banner
    auth_status = "🔐 已开启" if (AUTH_USERNAME and AUTH_PASSWORD) else "🔓 已禁用 (设置 ENV 开启)"
    banner = f"""
    ========================================================
    🚀 {APP_NAME} v{APP_VERSION} 初始化完成
    ========================================================
    * 描述: {APP_DESC}
    * 进程池并发数: {MAX_CONCURRENCY} (Process Pool)
    * 线程池并发数: {MAX_CONCURRENCY * 3} (Thread Pool)
    * 缓存机制: {CACHE_TTL}s 过期 / {REFRESH_THRESHOLD}s 静默刷新
    * HTTP 鉴权: {auth_status}
    ========================================================
    """
    logger.info(banner)

    yield

    for w in workers: w.cancel()
    process_pool.shutdown(wait=False)
    thread_pool.shutdown(wait=False)

# ==========================================
# FastAPI
# ==========================================
app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    description=APP_DESC,
    lifespan=lifespan,
    docs_url="/docs" if not AUTH_USERNAME else None, # 开启鉴权时最好隐藏自动文档
    redoc_url=None
)


def sync_detect_stream(stream_uri: str):
    """
    阻塞型：视频流质量检测 (重度 I/O 与 CPU)。
    运行在独立子进程，OS物理回收内存。
    修改为返回 dict，方便灵活调用。
    """
    result = {
        "stream_exists": False,
        "is_black": False,
        "audio_volume_db": -91.0,
        "brightness": 0.0,
        "contrast": 0.0,
        "saturation": 0.0,
        "rb_ratio": 1.0,
        "sharpness": 0.0
    }

    try:
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|threads;1|timeout;3000"
        cap = cv2.VideoCapture(stream_uri)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        if cap.isOpened():
            ret, frame = cap.read()
            if ret:
                result["stream_exists"] = True

                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                result["sharpness"] = cv2.Laplacian(gray, cv2.CV_64F).var()

                brightness = cv2.mean(gray)[0]
                result["brightness"] = brightness
                if brightness < BLACK_THRESHOLD:
                    result["is_black"] = True

                _, std_dev = cv2.meanStdDev(gray)
                result["contrast"] = std_dev[0][0]

                hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
                result["saturation"] = cv2.mean(hsv[:, :, 1])[0]

                mean_bgr = cv2.mean(frame)
                mean_b = mean_bgr[0]
                result["rb_ratio"] = mean_bgr[2] / (mean_b + 1e-5)

        cap.release()

        if result["stream_exists"]:
            try:
                out, err = (
                    ffmpeg
                    .input(stream_uri, t=1)
                    .filter('volumedetect')
                    .output('null', f='null')
                    .global_args('-timelimit', '4')
                    .run(capture_stderr=True, capture_stdout=True, quiet=True)
                )
                err_str = err.decode('utf-8')
                match = re.search(r'mean_volume:\s+([-\d.]+)\s+dB', err_str)
                if match:
                    result["audio_volume_db"] = float(match.group(1))
            except Exception as e:
                logger.error(f"流检测进程异常: {e}")
                pass

    except Exception as e:
        print(f"流检测进程异常: {e}")
    finally:
        if 'cap' in locals(): cap.release()
        if 'frame' in locals(): del frame

    return result


def sync_onvif_probe(target: str, user: str, password: str, port: int = 80):
    """阻塞型：ONVIF 协议交互 (轻量级网络请求，实时执行)"""
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
        except Exception as e:
            logger.error(f"获取设备MAC地址异常: {e}")
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
        except Exception as e:
            logger.error(f"获取设备时间异常: {e}")
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
        except Exception as e:
            logger.error(f"获取图像设置失败: {e}")
            pass

        ptz_data = None
        try:
            ptz_service = cam.create_ptz_service()
            ptz_status = ptz_service.GetStatus({'ProfileToken': token})
            ptz_data = {
                "pan": ptz_status.Position.PanTilt.x,
                "tilt": ptz_status.Position.PanTilt.y,
                "zoom": ptz_status.Position.Zoom.x
            }
        except Exception as e:
            logger.error(f"PTZ 状态获取失败: {e}")
            pass

        return dev_info, stream_uri, ptz_data, mac_address, time_drift, video_metrics, focus_mode_val
    except Exception as e:
        raise Exception(f"ONVIF交互失败: {e}")


async def cv_worker():
    """后台工作协程：专门负责消耗资源的 CV 拉流解析"""
    loop = asyncio.get_running_loop()
    while True:
        cache_key, auth_uri = await cv_queue.get()
        try:
            # 错峰缓冲，避免高并发爆破内存
            await asyncio.sleep(random.uniform(0.5, 2.0))
            cv_data = await loop.run_in_executor(process_pool, sync_detect_stream, auth_uri)

            # 更新 CV 专属缓存
            cv_cache[cache_key] = {
                "time": time.time(),
                "data": cv_data
            }
        except Exception as e:
            print(f"CV Worker 异常: {e}")
        finally:
            if cache_key in cv_probing_targets:
                cv_probing_targets.remove(cache_key)
            cv_queue.task_done()
            gc.collect()


@app.get("/probe", dependencies=[Depends(verify_auth)])
async def probe(
        target: str = Query(..., description="摄像机IP地址"),
        user: str = Query("admin", description="ONVIF用户名"),
        password: str = Query(..., description="ONVIF密码"),
        port: int = Query(80, description="ONVIF端口"),
        # 🎯 新增：接收从 Prometheus Relabel 传过来的期望值参数
        expected_pan: float | None = Query(None, description="期望的 Pan 坐标"),
        expected_tilt: float | None = Query(None, description="期望的 Tilt 坐标"),
        expected_zoom: float | None = Query(None, description="期望的 Zoom 焦距")
):
    """双轨制抓取端点：实时返回 ONVIF，按需拉起 CV 异步缓存，包含动态阈值比对"""
    registry = CollectorRegistry()

    # 初始化所有指标
    metric_success = Gauge('probe_success', '设备是否在线可通信', registry=registry)
    metric_device_info = Gauge('onvif_device_info', 'ONVIF设备信息',
                               ['manufacturer', 'model', 'firmware', 'mac', 'encoding'], registry=registry)
    metric_time_drift = Gauge('onvif_system_time_drift_seconds', '系统时间漂移(秒)', registry=registry)
    metric_video_width = Gauge('onvif_video_resolution_width', '分辨率宽', registry=registry)
    metric_video_height = Gauge('onvif_video_resolution_height', '分辨率高', registry=registry)
    metric_video_fps = Gauge('onvif_video_framerate_limit', '帧率限制', registry=registry)
    metric_focus_mode = Gauge('onvif_imaging_autofocus_enabled', '自动对焦开启(1=AUTO, 0=MANUAL)', registry=registry)

    metric_ptz_supported = Gauge('onvif_ptz_supported', '设备支持PTZ', registry=registry)
    metric_ptz_pan = Gauge('onvif_ptz_pan', '云台Pan', registry=registry)
    metric_ptz_tilt = Gauge('onvif_ptz_tilt', '云台Tilt', registry=registry)
    metric_ptz_zoom = Gauge('onvif_ptz_zoom', '变焦Zoom', registry=registry)

    # 🎯 核心新增：注册期望值指标
    metric_exp_pan = Gauge('onvif_ptz_expected_pan', '期望的云台Pan', registry=registry)
    metric_exp_tilt = Gauge('onvif_ptz_expected_tilt', '期望的云台Tilt', registry=registry)
    metric_exp_zoom = Gauge('onvif_ptz_expected_zoom', '期望的变焦Zoom', registry=registry)

    # 只要 Prometheus 传了期望值，我们就原样将其作为独立 Metric 暴露出去
    if expected_pan is not None:
        metric_exp_pan.set(expected_pan)
    if expected_tilt is not None:
        metric_exp_tilt.set(expected_tilt)
    if expected_zoom is not None:
        metric_exp_zoom.set(expected_zoom)

    # CV 相关指标
    metric_stream_exists = Gauge('onvif_video_stream_exists', '视频流是否成功读取', registry=registry)
    metric_is_black = Gauge('onvif_video_is_black_screen', '视频是否黑屏', registry=registry)
    metric_audio_vol = Gauge('onvif_audio_mean_volume_db', '音频平均音量(dB)', registry=registry)
    metric_cv_brightness = Gauge('onvif_video_cv_brightness', '图像平均亮度', registry=registry)
    metric_cv_contrast = Gauge('onvif_video_cv_contrast', '图像对比度', registry=registry)
    metric_cv_saturation = Gauge('onvif_video_cv_saturation', '图像平均饱和度', registry=registry)
    metric_cv_rb_ratio = Gauge('onvif_video_cv_red_blue_ratio', '红蓝通道比', registry=registry)
    metric_cv_sharpness = Gauge('onvif_video_cv_sharpness', '画面清晰度(对焦锐度)', registry=registry)

    metric_success.set(0)
    loop = asyncio.get_running_loop()
    cache_key = f"{target}_{port}"

    try:
        # ==========================================
        # 1. 实时快轨：每次都强制刷新获取 ONVIF 状态
        # ==========================================
        dev_info, stream_uri, ptz_data, mac_address, time_drift, video_metrics, focus_mode_val = await loop.run_in_executor(
            thread_pool, sync_onvif_probe, target, user, password, port
        )

        # 只要走到这里没报错，说明网络通畅且鉴权成功
        metric_success.set(1)

        # 填入新鲜的 ONVIF 数据
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

        # ==========================================
        # 2. 异步慢轨：检查 CV 缓存并按需触发刷新
        # ==========================================
        now = time.time()
        cache_entry = cv_cache.get(cache_key)
        cache_age = now - cache_entry['time'] if cache_entry else float('inf')

        # 如果 CV 缓存过期且尚未排队，丢入后台队列执行拉流分析
        if cache_age > REFRESH_THRESHOLD and cache_key not in cv_probing_targets:
            cv_probing_targets.add(cache_key)
            auth_uri = stream_uri.replace("rtsp://", f"rtsp://{user}:{password}@")
            cv_queue.put_nowait((cache_key, auth_uri))

        # 3. 组装 CV 数据 (不论后台是否在更新，先返回缓存的数据)
        if cache_age < CACHE_TTL and cache_entry:
            cv_data = cache_entry['data']
            metric_stream_exists.set(1 if cv_data['stream_exists'] else 0)
            metric_is_black.set(1 if cv_data['is_black'] else 0)
            metric_audio_vol.set(cv_data['audio_volume_db'])
            metric_cv_brightness.set(cv_data['brightness'])
            metric_cv_contrast.set(cv_data['contrast'])
            metric_cv_saturation.set(cv_data['saturation'])
            metric_cv_rb_ratio.set(cv_data['rb_ratio'])
            metric_cv_sharpness.set(cv_data['sharpness'])
        else:
            # 缓存彻底过期或首次请求，设为默认安全值 (不打断监控，但标记为无流)
            metric_stream_exists.set(0)
            metric_is_black.set(0)
            metric_audio_vol.set(-91.0)
            metric_cv_brightness.set(0)
            metric_cv_contrast.set(0)
            metric_cv_saturation.set(0)
            metric_cv_rb_ratio.set(1.0)
            metric_cv_sharpness.set(0)

    except Exception as e:
        print(f"[{target}] ONVIF 探测失败: {e}")
        # 如果 ONVIF 都挂了，直接走 fallback (仅有 probe_success=0)
        pass

    return Response(generate_latest(registry), media_type="text/plain")


@app.get("/metrics", dependencies=[Depends(verify_auth)])
async def metrics():
    """Exporter 自身状态监控"""
    return Response(f"# {APP_NAME} v{APP_VERSION} Core is running.\n", media_type="text/plain")


if __name__ == "__main__":
    import uvicorn

    log_config = uvicorn.config.LOGGING_CONFIG
    log_config["formatters"]["access"]["fmt"] = "%(asctime)s - %(levelname)s - [%(client_addr)s] - \"%(request_line)s\" %(status_code)s"
    log_config["formatters"]["default"]["fmt"] = "%(asctime)s - %(levelname)s - %(message)s"

    log_config["formatters"]["access"]["datefmt"] = "%Y-%m-%d %H:%M:%S"
    log_config["formatters"]["default"]["datefmt"] = "%Y-%m-%d %H:%M:%S"

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=9121,
        log_config=log_config,
        log_level="info",
        server_header=False
    )