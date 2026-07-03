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
import subprocess
import sys
import urllib.parse
import datetime
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Response, Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from prometheus_client import CollectorRegistry, Gauge, generate_latest
from onvif import ONVIFCamera



# ==========================================
# 1. 动态加载项目元数据 (从 pyproject.toml)
# ==========================================
def get_project_metadata():
    metadata_paths = [Path("pyproject.toml")]
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        metadata_paths.append(Path(bundle_dir) / "pyproject.toml")

    for metadata_path in metadata_paths:
        if not metadata_path.exists():
            continue

        try:
            with metadata_path.open("rb") as f:
                data = tomllib.load(f)
            project = data.get("project", {})
            return (
                project.get("name", "onvif-exporter"),
                project.get("version", "unknown"),
                project.get("description", "ONVIF/CV Exporter for Prometheus")
            )
        except Exception as e:
            logging.getLogger("uvicorn.error").debug(f"Failed to load project metadata from {metadata_path}: {e}")

    return "onvif-exporter", "dev", "ONVIF Exporter (Metadata Missing)"

APP_NAME, APP_VERSION, APP_DESC = get_project_metadata()

# ==========================================
# 2. 核心配置与环境变量
# ==========================================

# CV 多 worker 调度：总 CV 任务槽位 = CV_WORKER_COUNT * CV_WORKER_TASK_LIMIT。
CV_WORKER_COUNT = max(1, int(os.getenv("CV_WORKER_COUNT", "1")))
CV_WORKER_TASK_LIMIT = max(1, int(os.getenv("CV_WORKER_TASK_LIMIT", "1")))
CV_WORKER_SLOT_COUNT = CV_WORKER_COUNT * CV_WORKER_TASK_LIMIT
# ONVIF 协议交互是轻量网络请求，独立控制线程数，避免和重内存 CV 并发绑死。
ONVIF_MAX_CONCURRENCY = int(os.getenv("ONVIF_MAX_CONCURRENCY", "4"))
# 视频流可读状态缓存寿命。它只表示最近是否成功读到视频帧，不等待完整分析结果。
STREAM_CACHE_TTL = int(os.getenv("STREAM_CACHE_TTL", "180"))
# 分析结果缓存寿命。黑屏、亮度、清晰度、音量等慢结果独立于视频流可读状态。
ANALYSIS_CACHE_TTL = int(os.getenv("ANALYSIS_CACHE_TTL", "180"))
# 触发后台更新任务的“时间临界点”（单位：秒）。
REFRESH_THRESHOLD = int(os.getenv("REFRESH_THRESHOLD", "120"))
# 判定画面为“黑屏”的像素平均亮度界限（范围 0 - 255）。
BLACK_THRESHOLD = int(os.getenv("BLACK_THRESHOLD", "15"))
# 是否开启严格的 PTZ 全零异常检测 (默认 False，可通过 ENV 设为 True)
STRICT_ZERO_PTZ_CHECK = os.getenv("STRICT_ZERO_PTZ_CHECK", "False").lower() in ("true", "1", "yes")
# CV 子进程执行多少个任务后重建。OpenCV/FFmpeg 在长生命周期进程里常见 native 内存缓慢增长。
CV_PROCESS_MAX_TASKS = int(os.getenv("CV_PROCESS_MAX_TASKS", "25"))
# CV 后台队列上限。Prometheus 高频 scrape 或大量 target 异常时，避免无限排队。
CV_QUEUE_MAXSIZE = int(os.getenv("CV_QUEUE_MAXSIZE", str(CV_WORKER_SLOT_COUNT * 2)))
# CV 缓存最大目标数。防止 target 参数变化或动态发现导致字典无限增长。
CV_CACHE_MAX_ENTRIES = int(os.getenv("CV_CACHE_MAX_ENTRIES", "256"))
# 缓存清理间隔。清理超过各自 TTL 的旧 target 数据。
CV_CACHE_CLEAN_INTERVAL = int(os.getenv("CV_CACHE_CLEAN_INTERVAL", "120"))
# 是否输出正常 HTTP 请求访问日志。默认关闭，避免 Prometheus 高频抓取刷屏。
ACCESS_LOG_ENABLED = os.getenv("EXPORTER_ACCESS_LOG", "False").lower() in ("true", "1", "yes")
# 是否输出完整 FFmpeg stderr。默认只输出最后几行关键错误，避免日志过长。
FFMPEG_VERBOSE_ERROR = os.getenv("EXPORTER_FFMPEG_VERBOSE_ERROR", "False").lower() in ("true", "1", "yes")
# FFmpeg 音频采样秒数。volumedetect 需要实际解码音频样本，过短可能拿不到稳定值。
FFMPEG_AUDIO_SAMPLE_SECONDS = float(os.getenv("FFMPEG_AUDIO_SAMPLE_SECONDS", "2"))
# Python 侧强制超时，避免 RTSP/FFmpeg 在异常设备上无限卡住。
FFMPEG_AUDIO_TIMEOUT_SECONDS = float(os.getenv("FFMPEG_AUDIO_TIMEOUT_SECONDS", "8"))
# 音频未检测成功或缓存未生成时的哨兵值。
AUDIO_UNKNOWN_VOLUME_DB = -99.0
# OpenCV 侧 RTSP 缓冲帧数。部分 FFmpeg 后端可能不完全遵守该参数，但保留为可调项。
CAP_PROP_BUFFERSIZE = max(0, int(os.getenv("CAP_PROP_BUFFERSIZE", "1")))
# CV 单次检测最多尝试读取多少帧。RTSP/HEVC 偶发丢参考帧时，单帧读取容易误判为无流。
CV_READ_ATTEMPTS = max(1, int(os.getenv("CV_READ_ATTEMPTS", "3")))
# 多次读帧之间的等待时间，给 RTSP/解码器一点时间等到可解码帧。
CV_READ_RETRY_DELAY_SECONDS = max(0.0, float(os.getenv("CV_READ_RETRY_DELAY_SECONDS", "0.3")))
# 选流时优先使用 H.264。HEVC/H.265 更容易触发 OpenCV/FFmpeg 解码抖动和更高 CPU 占用。
PREFER_H264_STREAM = os.getenv("PREFER_H264_STREAM", "True").lower() in ("true", "1", "yes")

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
cv_stream_cache = {}
cv_analysis_cache = {}
cv_queue = None
cv_probing_targets = set()
process_pool = None
thread_pool = None
last_cv_cache_clean = 0.0

logger = logging.getLogger("uvicorn.error")

# Beta 诊断日志：正式版发布前直接改为 False 或删除相关调用。
BETA_DIAGNOSTICS_ENABLED = True
BETA_DIAGNOSTIC_PURPLE = "\033[95m"
BETA_DIAGNOSTIC_RESET = "\033[0m"


def beta_diag_log(message: str) -> None:
    """输出 beta 诊断日志；整行用紫色，便于从普通日志里快速筛出。"""
    if BETA_DIAGNOSTICS_ENABLED:
        logger.info("%s[BETA-DIAG] %s%s", BETA_DIAGNOSTIC_PURPLE, message, BETA_DIAGNOSTIC_RESET)


def format_diag_seconds(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.3f}s"


def format_diag_keys(keys: list[str]) -> str:
    if not keys:
        return "-"

    preview = ", ".join(keys[:5])
    if len(keys) > 5:
        return f"{preview}, +{len(keys) - 5} more"
    return preview


def format_rtsp_endpoint(stream_uri: str) -> str:
    parsed = urllib.parse.urlparse(stream_uri)
    if not parsed.hostname:
        return "unknown"

    port = parsed.port
    if port is None and parsed.scheme == "rtsp":
        port = 554

    return f"{parsed.hostname}:{port}" if port is not None else parsed.hostname


# ==========================================
# 生命周期与 Banner
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global process_pool, thread_pool, cv_queue
    try:
        multiprocessing.set_start_method('spawn')
    except RuntimeError:
        pass

    cv_queue = asyncio.Queue(maxsize=CV_QUEUE_MAXSIZE)
    process_pool = ProcessPoolExecutor(
        max_workers=CV_WORKER_SLOT_COUNT,
        max_tasks_per_child=CV_PROCESS_MAX_TASKS
    )
    thread_pool = ThreadPoolExecutor(max_workers=ONVIF_MAX_CONCURRENCY)
    workers = [
        asyncio.create_task(cv_worker(worker_id, slot_id))
        for worker_id in range(CV_WORKER_COUNT)
        for slot_id in range(CV_WORKER_TASK_LIMIT)
    ]

    # 打印标准化 Banner
    auth_status = "🔐 已开启" if (AUTH_USERNAME and AUTH_PASSWORD) else "🔓 已禁用 (设置 ENV 开启)"
    banner = f"""
    ========================================================
    🚀 {APP_NAME} v{APP_VERSION} 初始化完成
    ========================================================
    * 描述: {APP_DESC}
    * CV 调度 worker: {CV_WORKER_COUNT} workers x {CV_WORKER_TASK_LIMIT} tasks/worker = {CV_WORKER_SLOT_COUNT} process slots
    * CV 子进程任务上限: {CV_PROCESS_MAX_TASKS} tasks/child
    * CV 单次读帧尝试: {CV_READ_ATTEMPTS} 次 / 间隔 {CV_READ_RETRY_DELAY_SECONDS}s / buffer={CAP_PROP_BUFFERSIZE}
    * ONVIF 选流策略: {"优先 H.264" if PREFER_H264_STREAM else "仅按最低分辨率"}
    * ONVIF 线程并发数: {ONVIF_MAX_CONCURRENCY} (Thread Pool)
    * 缓存机制: stream={STREAM_CACHE_TTL}s / analysis={ANALYSIS_CACHE_TTL}s / {REFRESH_THRESHOLD}s 静默刷新 / 最多 {CV_CACHE_MAX_ENTRIES} targets
    * CV 队列上限: {CV_QUEUE_MAXSIZE}
    * HTTP 访问日志: {"已开启" if ACCESS_LOG_ENABLED else "已关闭"}
    * HTTP 鉴权: {auth_status}
    ========================================================
    """
    logger.info(banner)
    beta_diag_log(f"diagnostics_enabled=true version={APP_VERSION} mode=hardcoded_beta")

    yield

    for w in workers: w.cancel()
    await asyncio.gather(*workers, return_exceptions=True)
    process_pool.shutdown(wait=False, cancel_futures=True)
    thread_pool.shutdown(wait=False, cancel_futures=True)

# ==========================================
# FastAPI
# ==========================================
app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    description=APP_DESC,
    lifespan=lifespan,
    docs_url="/docs" if not (AUTH_USERNAME and AUTH_PASSWORD) else None, # 开启鉴权时最好隐藏自动文档
    redoc_url=None
)


def build_authenticated_rtsp_uri(stream_uri: str, user: str, password: str):
    """Safely inject RTSP credentials without duplicating an existing userinfo."""
    parsed = urllib.parse.urlparse(stream_uri)
    if parsed.scheme != "rtsp" or not parsed.hostname:
        return stream_uri

    hostname = parsed.hostname
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"

    userinfo = f"{urllib.parse.quote(user, safe='')}:{urllib.parse.quote(password, safe='')}"
    netloc = f"{userinfo}@{hostname}"
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"

    return parsed._replace(netloc=netloc).geturl()


def format_ffmpeg_error(exc: Exception):
    stderr = getattr(exc, "stderr", None)
    if stderr:
        stderr_text = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else str(stderr)
        lines = [line.strip() for line in stderr_text.splitlines() if line.strip()]
        if FFMPEG_VERBOSE_ERROR:
            return stderr_text.strip()
        return " | ".join(lines[-8:]) if lines else str(exc)
    return str(exc)


def detect_audio_volume_db(stream_uri: str) -> float | None:
    """
    Run FFmpeg volumedetect with a Python-side timeout.

    Some FFmpeg builds do not support `-timelimit` on Windows, and some reject
    `rw_timeout` for RTSP inputs. Keeping the FFmpeg arguments minimal and using
    subprocess timeout is more portable while still preventing stuck probes.
    """
    audio_stream = ffmpeg.input(stream_uri, rtsp_transport='tcp').audio
    command = (
        audio_stream
        .filter('volumedetect')
        .output('pipe:', format='null', t=FFMPEG_AUDIO_SAMPLE_SECONDS)
        .global_args('-nostdin')
        .compile()
    )

    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=FFMPEG_AUDIO_TIMEOUT_SECONDS
    )
    err_str = completed.stderr or ""
    match = re.search(r'mean_volume:\s+([-\d.]+)\s+dB', err_str)
    if match:
        return float(match.group(1))

    if completed.returncode != 0:
        logger.error("FFmpeg 音频检测失败: %s", format_ffmpeg_error(completed))
    else:
        logger.warning("FFmpeg 音频检测未解析到 mean_volume: %s", format_ffmpeg_error(completed))
    return None


def build_default_analysis_data():
    return {
        "is_black": False,
        "audio_volume_db": AUDIO_UNKNOWN_VOLUME_DB,
        "brightness": 0.0,
        "contrast": 0.0,
        "saturation": 0.0,
        "rb_ratio": 1.0,
        "sharpness": 0.0
    }


def sync_detect_stream_frame(stream_uri: str):
    """
    阻塞型：读取视频首帧并完成轻量图像分析。
    不在这里做音频检测，避免慢分析拖住 stream_exists 的缓存更新。
    """
    result = {
        "stream_exists": False,
        "analysis": build_default_analysis_data()
    }

    try:
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|threads;1|timeout;3000"
        cap = cv2.VideoCapture(stream_uri)
        if CAP_PROP_BUFFERSIZE > 0:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, CAP_PROP_BUFFERSIZE)

        if cap.isOpened():
            frame = None
            for attempt in range(1, CV_READ_ATTEMPTS + 1):
                ret, candidate_frame = cap.read()
                if ret and candidate_frame is not None and candidate_frame.size > 0:
                    frame = candidate_frame
                    break

                if candidate_frame is not None:
                    del candidate_frame
                if attempt < CV_READ_ATTEMPTS and CV_READ_RETRY_DELAY_SECONDS > 0:
                    time.sleep(CV_READ_RETRY_DELAY_SECONDS)

            if frame is not None:
                result["stream_exists"] = True
                analysis = result["analysis"]

                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                analysis["sharpness"] = cv2.Laplacian(gray, cv2.CV_64F).var()

                brightness = cv2.mean(gray)[0]
                analysis["brightness"] = brightness
                if brightness < BLACK_THRESHOLD:
                    analysis["is_black"] = True

                _, std_dev = cv2.meanStdDev(gray)
                analysis["contrast"] = std_dev[0][0]

                hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
                analysis["saturation"] = cv2.mean(hsv[:, :, 1])[0]

                mean_bgr = cv2.mean(frame)
                mean_b = mean_bgr[0]
                analysis["rb_ratio"] = mean_bgr[2] / (mean_b + 1e-5)

                del frame, gray, hsv

        cap.release()

    except Exception as e:
        print(f"视频帧检测进程异常: {e}")
    finally:
        if 'cap' in locals(): cap.release()
        if 'frame' in locals(): del frame

    return result


def sync_detect_audio_volume(stream_uri: str):
    """阻塞型：音频音量分析。独立运行，不能拖住视频流状态缓存。"""
    try:
        return detect_audio_volume_db(stream_uri)
    except ffmpeg.Error as e:
        logger.error("FFmpeg 音频检测失败: %s", format_ffmpeg_error(e))
    except subprocess.TimeoutExpired:
        logger.error("FFmpeg 音频检测超时: timeout=%ss", FFMPEG_AUDIO_TIMEOUT_SECONDS)
    except Exception as e:
        logger.error("FFmpeg 音频检测异常: %s", e)
    return None


def cleanup_cv_cache(now: float):
    """限制 CV 缓存增长，避免动态 target 或异常请求导致常驻内存持续上涨。"""
    global last_cv_cache_clean
    if now - last_cv_cache_clean < CV_CACHE_CLEAN_INTERVAL:
        return

    last_cv_cache_clean = now

    cache_specs = (
        ("stream", cv_stream_cache, STREAM_CACHE_TTL),
        ("analysis", cv_analysis_cache, ANALYSIS_CACHE_TTL),
    )
    for cache_name, cache, ttl in cache_specs:
        expired_keys = [
            key for key, entry in cache.items()
            if now - entry.get("time", 0) > ttl
        ]
        for key in expired_keys:
            cache.pop(key, None)
        if expired_keys:
            beta_diag_log(
                f"cache_cleanup cache={cache_name} reason=expired ttl={ttl}s "
                f"removed={len(expired_keys)} keys={format_diag_keys(expired_keys)} remaining={len(cache)}"
            )

        overflow = len(cache) - CV_CACHE_MAX_ENTRIES
        if overflow > 0:
            oldest_keys = sorted(cache, key=lambda key: cache[key].get("time", 0))[:overflow]
            for key in oldest_keys:
                cache.pop(key, None)
            beta_diag_log(
                f"cache_cleanup cache={cache_name} reason=max_entries max_entries={CV_CACHE_MAX_ENTRIES} "
                f"removed={len(oldest_keys)} keys={format_diag_keys(oldest_keys)} remaining={len(cache)}"
            )


def get_profile_resolution(profile) -> tuple[int, int] | None:
    video_conf = getattr(profile, "VideoEncoderConfiguration", None)
    if video_conf is None:
        return None

    resolution = getattr(video_conf, "Resolution", None)
    if resolution is None:
        return None

    width = getattr(resolution, "Width", None)
    height = getattr(resolution, "Height", None)
    if width is None or height is None:
        return None

    try:
        width = int(width)
        height = int(height)
    except (TypeError, ValueError):
        return None

    if width <= 0 or height <= 0:
        return None

    return width, height


def get_profile_encoding(profile) -> str:
    video_conf = getattr(profile, "VideoEncoderConfiguration", None)
    if video_conf is None:
        return ""

    encoding = getattr(video_conf, "Encoding", "")
    return str(encoding or "").upper().replace(".", "").replace("-", "")


def get_profile_codec_priority(profile) -> int:
    if not PREFER_H264_STREAM:
        return 0

    encoding = get_profile_encoding(profile)
    if encoding in ("H264", "AVC"):
        return 0
    if encoding and encoding not in ("H265", "HEVC"):
        return 1
    if not encoding:
        return 2
    return 3


def describe_profile_for_diag(index: int, profile) -> str:
    resolution = get_profile_resolution(profile)
    width, height = resolution or (0, 0)
    encoding = get_profile_encoding(profile) or "UNKNOWN"
    return (
        f"{index}:token={getattr(profile, 'token', '')},"
        f"name={str(getattr(profile, 'Name', '') or '')},"
        f"encoding={encoding},resolution={width}x{height},"
        f"codec_priority={get_profile_codec_priority(profile)}"
    )


def select_lowest_resolution_profile(profiles):
    profiles_with_video = [
        (
            get_profile_codec_priority(profile),
            resolution[0] * resolution[1] if resolution is not None else sys.maxsize,
            index,
            profile
        )
        for index, profile in enumerate(profiles)
        if getattr(profile, "VideoEncoderConfiguration", None) is not None
        for resolution in [get_profile_resolution(profile)]
    ]
    if profiles_with_video:
        return min(profiles_with_video, key=lambda item: (item[0], item[1], item[2]))[3]

    return profiles[0]


def select_ptz_profile(profiles):
    for profile in profiles:
        if getattr(profile, "PTZConfiguration", None) is not None:
            return profile

    return profiles[0]


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

        stream_profile = select_lowest_resolution_profile(profiles)
        ptz_profile = select_ptz_profile(profiles)

        stream_token = stream_profile.token
        ptz_token = ptz_profile.token
        req = media_service.create_type('GetStreamUri')
        req.ProfileToken = stream_token
        req.StreamSetup = {'Stream': 'RTP-Unicast', 'Transport': {'Protocol': 'RTSP'}}

        stream_uri = media_service.GetStreamUri(req).Uri
        parsed_rtsp = urllib.parse.urlparse(stream_uri)
        if parsed_rtsp.hostname in ['0.0.0.0', '127.0.0.1', 'localhost']:
            rtsp_port = parsed_rtsp.port if parsed_rtsp.port else 554
            fixed_rtsp_netloc = f"{target}:{rtsp_port}"
            stream_uri = parsed_rtsp._replace(netloc=fixed_rtsp_netloc).geturl()

        video_conf = stream_profile.VideoEncoderConfiguration
        rate_control = getattr(video_conf, "RateControl", None)
        stream_width, stream_height = get_profile_resolution(stream_profile) or (0, 0)
        video_metrics = {
            "width": stream_width,
            "height": stream_height,
            "fps": getattr(rate_control, "FrameRateLimit", 0) if rate_control is not None else 0,
            "encoding": getattr(video_conf, "Encoding", "unknown"),
            "profile_token": str(stream_token),
            "profile_name": str(getattr(stream_profile, "Name", "") or "")
        }
        beta_diag_log(
            f"onvif_profiles target={target} port={port} "
            f"selected_token={video_metrics['profile_token']} selected_name={video_metrics['profile_name']} "
            f"selected_encoding={video_metrics['encoding']} selected_resolution={stream_width}x{stream_height} "
            f"rtsp_endpoint={format_rtsp_endpoint(stream_uri)} "
            f"profiles=[{'; '.join(describe_profile_for_diag(index, profile) for index, profile in enumerate(profiles))}]"
        )

        focus_mode_val = -1.0
        try:
            video_source_token = stream_profile.VideoSourceConfiguration.SourceToken
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
            ptz_status = ptz_service.GetStatus({'ProfileToken': ptz_token})

            pan_val = ptz_status.Position.PanTilt.x
            tilt_val = ptz_status.Position.PanTilt.y
            zoom_val = ptz_status.Position.Zoom.x

            if STRICT_ZERO_PTZ_CHECK and pan_val == 0.0 and tilt_val == 0.0 and zoom_val == 0.0:
                logger.warning(f"[{target}] PTZ 返回值全为 0.0，触发 STRICT_ZERO_PTZ_CHECK，判定为 PTZ 异常/不支持。")
                ptz_data = None
            else:
                ptz_data = {
                    "pan": pan_val,
                    "tilt": tilt_val,
                    "zoom": zoom_val
                }
        except Exception as e:
            logger.error(f"PTZ 状态获取失败: {e}")
            pass

        return dev_info, stream_uri, ptz_data, mac_address, time_drift, video_metrics, focus_mode_val
    except Exception as e:
        raise Exception(f"ONVIF交互失败: {e}")


def sync_onvif_control(target: str, user: str, password: str, port: int,
                       mode: str, pan: float, tilt: float, zoom: float, preset: str):
    """阻塞型：发送 ONVIF PTZ 控制指令"""
    try:
        cam = ONVIFCamera(target, port, user, password)
        fixed_netloc = f"{target}:{port}" if port != 80 else target

        # 同样需要修正 WSDL 地址补丁，否则控制指令会发到 0.0.0.0 去
        for namespace, wrong_url in cam.xaddrs.items():
            parsed = urllib.parse.urlparse(wrong_url)
            cam.xaddrs[namespace] = parsed._replace(netloc=fixed_netloc).geturl()

        media_service = cam.create_media_service()
        profiles = media_service.GetProfiles()
        if not profiles:
            raise Exception("未找到媒体配置文件，无法获取 ProfileToken")

        token = select_ptz_profile(profiles).token
        ptz_service = cam.create_ptz_service()

        if mode == "ptz":
            # 绝对移动
            req = ptz_service.create_type('AbsoluteMove')
            req.ProfileToken = token
            req.Position = {'PanTilt': {'x': pan, 'y': tilt}, 'Zoom': {'x': zoom}}
            ptz_service.AbsoluteMove(req)
            return f"AbsoluteMove 到 [Pan:{pan}, Tilt:{tilt}, Zoom:{zoom}] 成功"

        elif mode == "move":
            # 相对移动
            req = ptz_service.create_type('RelativeMove')
            req.ProfileToken = token
            req.Translation = {'PanTilt': {'x': pan, 'y': tilt}, 'Zoom': {'x': zoom}}
            ptz_service.RelativeMove(req)
            return f"RelativeMove 偏移 [Pan:{pan}, Tilt:{tilt}, Zoom:{zoom}] 成功"

        elif mode == "preset":
            # 调用预置位
            req = ptz_service.create_type('GotoPreset')
            req.ProfileToken = token
            req.PresetToken = preset
            ptz_service.GotoPreset(req)
            return f"GotoPreset [预置位:{preset}] 成功"

        else:
            raise ValueError(f"未知的 mode: {mode}")

    except Exception as e:
        logger.error(f"[{target}] PTZ 控制失败: {e}")
        raise Exception(f"控制指令执行失败: {str(e)}")

async def cv_worker(worker_id: int, slot_id: int):
    """后台工作协程：专门负责消耗资源的 CV 拉流解析"""
    loop = asyncio.get_running_loop()
    while True:
        cache_key, auth_uri = await cv_queue.get()
        task_started = time.perf_counter()
        stagger_sleep = 0.0
        frame_elapsed = None
        audio_elapsed = None
        audio_status = "skipped"
        stream_exists = False
        try:
            # 错峰缓冲，避免高并发爆破内存
            stagger_sleep = random.uniform(0.5, 2.0)
            await asyncio.sleep(stagger_sleep)
            frame_started = time.perf_counter()
            frame_data = await loop.run_in_executor(process_pool, sync_detect_stream_frame, auth_uri)
            frame_elapsed = time.perf_counter() - frame_started
            now = time.time()
            stream_exists = frame_data["stream_exists"]

            # STEP 1: 首帧读到后立即更新视频流状态，避免慢分析拖累 stream_exists。
            cv_stream_cache[cache_key] = {
                "time": now,
                "data": {
                    "stream_exists": stream_exists
                }
            }

            if stream_exists:
                analysis_data = frame_data["analysis"]
                cv_analysis_cache[cache_key] = {
                    "time": now,
                    "data": analysis_data
                }

                # STEP 2: 音频是慢分析，单独补齐到分析缓存；失败时保留图像分析结果。
                audio_started = time.perf_counter()
                audio_volume_db = await loop.run_in_executor(process_pool, sync_detect_audio_volume, auth_uri)
                audio_elapsed = time.perf_counter() - audio_started
                if audio_volume_db is not None:
                    audio_status = "updated"
                    analysis_data = dict(analysis_data)
                    analysis_data["audio_volume_db"] = audio_volume_db
                    cv_analysis_cache[cache_key] = {
                        "time": time.time(),
                        "data": analysis_data
                    }
                else:
                    audio_status = "failed"
            else:
                audio_status = "skipped_no_stream"
        except Exception as e:
            print(f"CV Worker 异常: worker={worker_id} slot={slot_id} target={cache_key} error={e}")
        finally:
            cv_probing_targets.discard(cache_key)
            beta_diag_log(
                f"cv_worker target={cache_key} worker={worker_id} slot={slot_id} "
                f"stream_exists={1 if stream_exists else 0} total={format_diag_seconds(time.perf_counter() - task_started)} "
                f"stagger={format_diag_seconds(stagger_sleep)} frame={format_diag_seconds(frame_elapsed)} "
                f"audio={format_diag_seconds(audio_elapsed)} audio_status={audio_status} "
                f"queue_size={cv_queue.qsize()} probing_targets={len(cv_probing_targets)} "
                f"stream_cache_entries={len(cv_stream_cache)} analysis_cache_entries={len(cv_analysis_cache)}"
            )
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
    metric_stream_profile_info = Gauge('onvif_video_stream_profile_info', '实际用于RTSP/CV分析的ONVIF媒体Profile',
                                       ['token', 'name'], registry=registry)
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
    metric_stream_cache_valid = Gauge('onvif_video_stream_cache_valid', '视频流状态缓存是否有效', registry=registry)
    metric_stream_cache_age = Gauge('onvif_video_stream_cache_age_seconds', '视频流状态缓存年龄(秒)，无缓存为 -1', registry=registry)
    metric_analysis_cache_valid = Gauge('onvif_video_analysis_cache_valid', '视频分析结果缓存是否有效', registry=registry)
    metric_analysis_cache_age = Gauge('onvif_video_analysis_cache_age_seconds', '视频分析结果缓存年龄(秒)，无缓存为 -1', registry=registry)

    metric_success.set(0)
    loop = asyncio.get_running_loop()
    cache_key = f"{target}_{port}"
    probe_started = time.perf_counter()
    onvif_elapsed = None
    enqueue_state = "not_checked"

    try:
        # ==========================================
        # 1. 实时快轨：每次都强制刷新获取 ONVIF 状态
        # ==========================================
        onvif_started = time.perf_counter()
        try:
            dev_info, stream_uri, ptz_data, mac_address, time_drift, video_metrics, focus_mode_val = await loop.run_in_executor(
                thread_pool, sync_onvif_probe, target, user, password, port
            )
        finally:
            onvif_elapsed = time.perf_counter() - onvif_started

        # 只要走到这里没报错，说明网络通畅且鉴权成功
        metric_success.set(1)

        # 填入新鲜的 ONVIF 数据
        metric_device_info.labels(manufacturer=dev_info.Manufacturer, model=dev_info.Model,
                                  firmware=dev_info.FirmwareVersion, mac=mac_address,
                                  encoding=video_metrics['encoding']).set(1.0)
        metric_stream_profile_info.labels(token=video_metrics['profile_token'],
                                          name=video_metrics['profile_name']).set(1.0)
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
        cleanup_cv_cache(now)
        stream_cache_entry = cv_stream_cache.get(cache_key)
        analysis_cache_entry = cv_analysis_cache.get(cache_key)
        stream_cache_age = now - stream_cache_entry['time'] if stream_cache_entry else float('inf')
        analysis_cache_age = now - analysis_cache_entry['time'] if analysis_cache_entry else float('inf')
        stream_cache_valid = stream_cache_entry is not None and stream_cache_age < STREAM_CACHE_TTL
        analysis_cache_valid = analysis_cache_entry is not None and analysis_cache_age < ANALYSIS_CACHE_TTL

        metric_stream_cache_valid.set(1 if stream_cache_valid else 0)
        metric_stream_cache_age.set(stream_cache_age if stream_cache_entry else -1)
        metric_analysis_cache_valid.set(1 if analysis_cache_valid else 0)
        metric_analysis_cache_age.set(analysis_cache_age if analysis_cache_entry else -1)

        # 流状态缓存达到刷新阈值且尚未排队时，后台刷新；分析结果由 worker 独立补齐。
        if stream_cache_age > REFRESH_THRESHOLD:
            if cache_key in cv_probing_targets:
                enqueue_state = "already_probing"
            else:
                cv_probing_targets.add(cache_key)
                auth_uri = build_authenticated_rtsp_uri(stream_uri, user, password)
                try:
                    cv_queue.put_nowait((cache_key, auth_uri))
                    enqueue_state = "queued"
                except asyncio.QueueFull:
                    enqueue_state = "queue_full"
                    cv_probing_targets.discard(cache_key)
                    logger.warning(
                        "CV 队列已满，跳过本次后台刷新: target=%s queue_size=%s",
                        cache_key,
                        cv_queue.qsize()
                    )
        else:
            enqueue_state = "fresh_stream_cache"

        # 3. 组装 CV 数据：流状态和分析结果分开返回。
        stream_exists_value = 0
        if stream_cache_valid:
            stream_data = stream_cache_entry['data']
            stream_exists_value = 1 if stream_data['stream_exists'] else 0
            metric_stream_exists.set(stream_exists_value)
        else:
            metric_stream_exists.set(0)

        if analysis_cache_entry:
            analysis_data = analysis_cache_entry['data']
        else:
            analysis_data = build_default_analysis_data()

        metric_is_black.set(1 if analysis_data['is_black'] else 0)
        metric_audio_vol.set(analysis_data['audio_volume_db'])
        metric_cv_brightness.set(analysis_data['brightness'])
        metric_cv_contrast.set(analysis_data['contrast'])
        metric_cv_saturation.set(analysis_data['saturation'])
        metric_cv_rb_ratio.set(analysis_data['rb_ratio'])
        metric_cv_sharpness.set(analysis_data['sharpness'])
        beta_diag_log(
            f"probe target={target} port={port} success=1 total={format_diag_seconds(time.perf_counter() - probe_started)} "
            f"onvif={format_diag_seconds(onvif_elapsed)} enqueue={enqueue_state} "
            f"stream_exists={stream_exists_value} stream_cache_valid={1 if stream_cache_valid else 0} "
            f"stream_cache_age={format_diag_seconds(stream_cache_age if stream_cache_entry else None)} "
            f"analysis_cache_valid={1 if analysis_cache_valid else 0} "
            f"analysis_cache_age={format_diag_seconds(analysis_cache_age if analysis_cache_entry else None)} "
            f"queue_size={cv_queue.qsize()} probing_targets={len(cv_probing_targets)} "
            f"rtsp_endpoint={format_rtsp_endpoint(stream_uri)} "
            f"profile_token={video_metrics['profile_token']} profile_name={video_metrics['profile_name']} "
            f"encoding={video_metrics['encoding']} resolution={video_metrics['width']}x{video_metrics['height']}"
        )

    except Exception as e:
        print(f"[{target}] ONVIF 探测失败: {e}")
        beta_diag_log(
            f"probe target={target} port={port} success=0 total={format_diag_seconds(time.perf_counter() - probe_started)} "
            f"onvif={format_diag_seconds(onvif_elapsed)} enqueue={enqueue_state} error={e}"
        )
        # 如果 ONVIF 都挂了，直接走 fallback (仅有 probe_success=0)
        pass

    return Response(generate_latest(registry), media_type="text/plain")


@app.get("/control", dependencies=[Depends(verify_auth)])
async def control_ptz(
        target: str = Query(..., description="摄像机IP地址"),
        user: str = Query("admin", description="ONVIF用户名"),
        password: str = Query(..., description="ONVIF密码"),
        port: int = Query(80, description="ONVIF端口"),
        mode: str = Query(..., description="控制模式: ptz, move, preset"),
        pan: float | None = Query(None, description="Pan 坐标/偏移量"),
        tilt: float | None = Query(None, description="Tilt 坐标/偏移量"),
        zoom: float | None = Query(None, description="Zoom 焦距/偏移量"),
        preset: str | None = Query(None, description="预置位Token (如 '1')")
):
    """
    摄像机 PTZ 控制端点
    - mode=ptz: 移动到绝对坐标，需提供 pan, tilt, zoom
    - mode=move: 相对当前坐标偏移，需提供 pan, tilt, zoom
    - mode=preset: 转到预置位，需提供 preset
    """
    # 1. 严格参数校验
    if mode in ("ptz", "move"):
        if pan is None or tilt is None or zoom is None:
            raise HTTPException(status_code=400, detail=f"在 '{mode}' 模式下，pan, tilt, zoom 为必填项。")
    elif mode == "preset":
        if preset is None:
            raise HTTPException(status_code=400, detail="在 'preset' 模式下，preset 为必填项。")
    else:
        raise HTTPException(status_code=400, detail="mode 必须是 ptz, move 或 preset。")

    # 2. 扔到线程池执行控制指令 (由于是轻量级网络请求，复用线程池即可)
    loop = asyncio.get_running_loop()
    try:
        # noinspection PyTypeChecker
        result_msg = await loop.run_in_executor(
            thread_pool,
            sync_onvif_control,
            target, user, password, port, mode, pan, tilt, zoom, preset
        )
        return {"status": "success", "target": target, "message": result_msg}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/metrics", dependencies=[Depends(verify_auth)])
async def metrics():
    """Exporter 自身状态监控"""
    queue_size = cv_queue.qsize() if cv_queue else 0
    body = (
        f"# {APP_NAME} v{APP_VERSION} Core is running.\n"
        f"onvif_exporter_cv_worker_count {CV_WORKER_COUNT}\n"
        f"onvif_exporter_cv_worker_task_limit {CV_WORKER_TASK_LIMIT}\n"
        f"onvif_exporter_cv_worker_slots {CV_WORKER_SLOT_COUNT}\n"
        f"onvif_exporter_onvif_max_concurrency {ONVIF_MAX_CONCURRENCY}\n"
        f"onvif_exporter_cv_queue_capacity {CV_QUEUE_MAXSIZE}\n"
        f"onvif_exporter_cap_prop_buffersize {CAP_PROP_BUFFERSIZE}\n"
        f"onvif_exporter_cv_read_attempts {CV_READ_ATTEMPTS}\n"
        f"onvif_exporter_cv_read_retry_delay_seconds {CV_READ_RETRY_DELAY_SECONDS}\n"
        f"onvif_exporter_prefer_h264_stream {1 if PREFER_H264_STREAM else 0}\n"
        f"onvif_exporter_stream_cache_ttl_seconds {STREAM_CACHE_TTL}\n"
        f"onvif_exporter_analysis_cache_ttl_seconds {ANALYSIS_CACHE_TTL}\n"
        f"onvif_exporter_cv_cache_entries {len(cv_analysis_cache)}\n"
        f"onvif_exporter_cv_stream_cache_entries {len(cv_stream_cache)}\n"
        f"onvif_exporter_cv_analysis_cache_entries {len(cv_analysis_cache)}\n"
        f"onvif_exporter_cv_queue_size {queue_size}\n"
        f"onvif_exporter_cv_probing_targets {len(cv_probing_targets)}\n"
    )
    return Response(body, media_type="text/plain")


def main():
    import uvicorn

    if "--version" in sys.argv:
        print(f"{APP_NAME} {APP_VERSION}")
        return

    multiprocessing.freeze_support()

    log_config = uvicorn.config.LOGGING_CONFIG
    log_config["formatters"]["access"]["fmt"] = "%(asctime)s - %(levelname)s - [%(client_addr)s] - \"%(request_line)s\" %(status_code)s"
    log_config["formatters"]["default"]["fmt"] = "%(asctime)s - %(levelname)s - %(message)s"

    log_config["formatters"]["access"]["datefmt"] = "%Y-%m-%d %H:%M:%S"
    log_config["formatters"]["default"]["datefmt"] = "%Y-%m-%d %H:%M:%S"

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=9121,
        log_config=log_config,
        log_level="info",
        access_log=ACCESS_LOG_ENABLED,
        server_header=False
    )


if __name__ == "__main__":
    main()
