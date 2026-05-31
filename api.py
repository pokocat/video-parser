#!/usr/bin/env python3
"""
视频解析下载 API 服务 (FastAPI 版本)
支持抖音、哔哩哔哩、小红书、快手、好看视频等平台
"""

import base64
import hashlib
import os
import re
import subprocess
import tempfile
import time
from typing import Optional
from contextlib import asynccontextmanager

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException, ChunkedEncodingError, ConnectionError
from urllib3.util.retry import Retry
from fastapi import FastAPI, Request, Header
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from openai import OpenAI
from pydantic import BaseModel

from configs.logging_config import logger
from configs.general_constants import (
    DOMAIN_TO_NAME, MINI_PROGRAM_LEGAL_DOMAIN,
    SAVE_VIDEO_PATH, DOMAIN, check_essential_dirs
)
from utils.web_fetcher import WebFetcher, UrlParser
from utils.vigenere_cipher import VigenereCipher
from src.downloader_factory import DownloaderFactory


# ==================== Pydantic 模型 ====================

class ParseRequest(BaseModel):
    """解析请求模型"""
    text: str


class DownloadRequest(BaseModel):
    """下载请求模型"""
    video_url: str
    video_id: str


class FrontendMediaRequest(BaseModel):
    """React 前端媒体操作请求模型"""
    video_url: str
    video_id: str
    platform: Optional[str] = ""
    title: Optional[str] = ""
    audio_url: Optional[str] = None
    original_url: Optional[str] = None


class APIResponse(BaseModel):
    """统一响应模型"""
    retcode: int
    retdesc: str
    data: Optional[dict] = None
    ranking: Optional[dict] = None
    succ: bool


# ==================== 工具函数 ====================

def make_response(retcode: int, retdesc: str, data: dict = None,
                  ranking: dict = None, succ: bool = False) -> dict:
    """生成统一的响应格式"""
    return {
        'retcode': retcode,
        'retdesc': retdesc,
        'data': data,
        'ranking': ranking,
        'succ': succ
    }


def validate_timestamp(request_timestamp: int) -> bool:
    """验证时间戳是否在合理的时间窗口内"""
    current_timestamp = int(time.time() * 1000)
    time_window = 5 * 60 * 1000  # 5分钟的时间窗口
    return abs(current_timestamp - request_timestamp) <= time_window


def validate_request_headers(
    x_timestamp: str,
    x_gclt_text: str,
    x_egct_text: str,
    *args
) -> Optional[dict]:
    """
    验证请求头和参数

    Returns:
        None 如果验证通过，否则返回错误响应字典
    """
    # 检查是否有缺失的参数
    missing_params = [param for param in args if not param]
    if missing_params:
        logger.error(f'Missing parameters in request')
        return make_response(400, 'Missing parameters in request', None, None, False)

    if not x_timestamp:
        logger.error('Missing timestamp in request')
        return make_response(400, 'Missing timestamp in request', None, None, False)

    try:
        if not validate_timestamp(int(x_timestamp)):
            logger.error('Invalid timestamp')
            return make_response(400, 'Invalid timestamp', None, None, False)
    except ValueError:
        logger.error('Invalid timestamp format')
        return make_response(400, 'Invalid timestamp format', None, None, False)

    if not VigenereCipher(x_timestamp).verify_decryption(x_egct_text, x_gclt_text):
        logger.error('Decryption verification failed')
        return make_response(400, 'Decryption verification failed', None, None, False)

    return None


# ==================== 前端媒体工具函数 ====================

QWEN_API_BASE_URL = os.getenv('QWEN_API_BASE_URL', 'https://api-inference.modelscope.cn/v1')
QWEN_API_KEY = os.getenv('QWEN_API_KEY', '')
QWEN_MODEL_ID = os.getenv('QWEN_MODEL_ID', 'Qwen/Qwen3-VL-8B-Instruct')
MAX_FRAMES = int(os.getenv('MAX_FRAMES', '6'))


def media_slug(*parts: Optional[str]) -> str:
    """生成 URL 安全的媒体文件名片段"""
    source = next((part for part in parts if part), "video")
    slug = re.sub(r'[^A-Za-z0-9_-]+', '-', source).strip('-')[:48]
    if slug:
        return slug
    return hashlib.sha1(source.encode('utf-8')).hexdigest()[:16]


def media_static_url(filename: str) -> str:
    """生成可被浏览器访问的静态视频 URL"""
    path = f"/static/videos/{filename}"
    if DOMAIN:
        return f"{DOMAIN.rstrip('/')}{path}"
    return path


def referer_for_media(request_body: FrontendMediaRequest) -> Optional[str]:
    """根据来源平台补充下载 Referer"""
    original_url = request_body.original_url or ""
    platform = request_body.platform or ""
    if 'douyin.com' in original_url or platform == '抖音':
        return 'https://www.douyin.com/'
    if 'bilibili.com' in original_url or platform == '哔哩哔哩':
        return original_url or 'https://www.bilibili.com/'
    if 'xiaohongshu.com' in original_url or platform == '小红书':
        return 'https://www.xiaohongshu.com/'
    if 'kuaishou.com' in original_url or platform == '快手':
        return 'https://www.kuaishou.com/'
    if 'haokan.baidu.com' in original_url or platform == '好看视频':
        return 'https://haokan.baidu.com/'
    return None


def download_binary(url: str, output_path: str, referer: Optional[str] = None) -> None:
    """流式下载远程媒体文件"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
    }
    if referer:
        headers['Referer'] = referer

    response = requests.get(url, headers=headers, stream=True, timeout=180)
    response.raise_for_status()
    with open(output_path, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)


def merge_video_audio(video_path: str, audio_path: str, output_path: str) -> None:
    """使用 ffmpeg 合并 B 站音视频轨道"""
    result = subprocess.run(
        [
            'ffmpeg', '-y',
            '-i', video_path,
            '-i', audio_path,
            '-c:v', 'copy',
            '-c:a', 'aac',
            '-strict', 'experimental',
            output_path
        ],
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 合并失败: {result.stderr}")


def ensure_frontend_media(request_body: FrontendMediaRequest, prefix: str) -> tuple[str, str]:
    """
    为 React 前端准备可访问的 mp4 文件。

    Returns:
        (filename, file_path)
    """
    check_essential_dirs()
    safe_id = media_slug(request_body.video_id, request_body.title, request_body.video_url)
    filename = f"{prefix}-{safe_id}.mp4"
    output_path = os.path.join(SAVE_VIDEO_PATH, filename)
    if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        return filename, output_path

    referer = referer_for_media(request_body)
    try:
        if request_body.platform == '哔哩哔哩' and request_body.audio_url:
            temp_dir = tempfile.mkdtemp(prefix='frontend_media_')
            video_temp = os.path.join(temp_dir, 'video.m4s')
            audio_temp = os.path.join(temp_dir, 'audio.m4s')
            try:
                download_binary(request_body.video_url, video_temp, referer)
                download_binary(request_body.audio_url, audio_temp, referer)
                merge_video_audio(video_temp, audio_temp, output_path)
            finally:
                for temp_path in [video_temp, audio_temp]:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                try:
                    os.rmdir(temp_dir)
                except OSError:
                    pass
        else:
            download_binary(request_body.video_url, output_path, referer)
    except Exception:
        if os.path.exists(output_path):
            os.remove(output_path)
        raise

    return filename, output_path


def get_video_duration(video_path: str) -> float:
    """获取视频时长（秒）"""
    result = subprocess.run(
        [
            'ffprobe', '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            video_path
        ],
        capture_output=True,
        text=True
    )
    try:
        return float(result.stdout.strip())
    except (TypeError, ValueError):
        return 0


def extract_video_frames(video_path: str, num_frames: int = MAX_FRAMES) -> list[str]:
    """等间隔抽取视频关键帧"""
    duration = get_video_duration(video_path)
    if duration <= 0:
        duration = 60
    interval = duration / (num_frames + 1)
    temp_dir = tempfile.mkdtemp(prefix='video_frames_')
    frames = []
    for index in range(num_frames):
        output_path = os.path.join(temp_dir, f'frame_{index:03d}.jpg')
        result = subprocess.run(
            [
                'ffmpeg', '-y',
                '-ss', str(interval * (index + 1)),
                '-i', video_path,
                '-vframes', '1',
                '-q:v', '2',
                output_path
            ],
            capture_output=True,
            text=True
        )
        if result.returncode == 0 and os.path.exists(output_path):
            frames.append(output_path)
    return frames


def image_to_base64(image_path: str) -> str:
    """将图片转换为 base64 编码"""
    with open(image_path, 'rb') as f:
        return base64.b64encode(f.read()).decode('utf-8')


def cleanup_frames(frames: list[str]) -> None:
    """清理 AI 抽帧临时文件"""
    if not frames:
        return
    temp_dir = os.path.dirname(frames[0])
    for frame_path in frames:
        try:
            os.remove(frame_path)
        except OSError:
            pass
    try:
        os.rmdir(temp_dir)
    except OSError:
        pass


# ==================== 应用生命周期 ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时执行
    logger.info("Starting Video Parser API Service (FastAPI)...")
    check_essential_dirs()
    yield
    # 关闭时执行
    logger.info("Shutting down Video Parser API Service...")


# ==================== 创建 FastAPI 应用 ====================

app = FastAPI(
    title="视频解析下载 API",
    description="支持抖音、哔哩哔哩、小红书、快手、好看视频等平台的视频解析下载服务",
    version="2.0.0",
    lifespan=lifespan
)

# CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态文件服务
static_dir = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


# ==================== API 路由 ====================

@app.get("/")
async def root():
    """根路由 - 健康检查"""
    return {"status": "ok", "message": "Video Parser API is running (FastAPI)"}


@app.get("/health")
async def health_check():
    """健康检查接口"""
    return {"status": "healthy", "timestamp": int(time.time() * 1000)}


@app.post("/api/parse")
async def parse_video(
    body: ParseRequest,
    x_timestamp: str = Header(default="", alias="X-Timestamp"),
    x_gclt_text: str = Header(default="", alias="X-GCLT-Text"),
    x_egct_text: str = Header(default="", alias="X-EGCT-Text"),
    wx_open_id: str = Header(default="Guest", alias="WX-OPEN-ID"),
):
    """
    解析视频链接

    从分享链接中提取视频信息，包括标题、封面、视频直链等
    """
    try:
        text = body.text

        # 验证请求
        validation_error = validate_request_headers(x_timestamp, x_gclt_text, x_egct_text, text)
        if validation_error:
            return JSONResponse(status_code=400, content=validation_error)

        # 提取URL
        extracted_url = UrlParser.get_url(text)
        logger.info(f'extracted_url: {extracted_url}')

        if not extracted_url:
            logger.error('No valid URL found in text')
            return JSONResponse(
                status_code=400,
                content=make_response(400, '未找到有效的视频链接', None, None, False)
            )

        # 获取重定向URL
        redirect_url = WebFetcher.fetch_redirect_url(extracted_url)
        logger.info(f'redirect_url: {redirect_url}')

        # 如果重定向失败，直接使用原始URL
        if not redirect_url:
            redirect_url = extracted_url
            logger.info(f'Using original URL: {redirect_url}')

        platform = DOMAIN_TO_NAME.get(UrlParser.get_domain(redirect_url))
        video_id = UrlParser.get_video_id(redirect_url)
        real_url = UrlParser.extract_video_address(redirect_url)
        logger.info(f'platform: {platform}, video_id: {video_id}, real_url: {real_url}')

        if not platform:
            logger.error(f'This link is not supported for extraction: {real_url}')
            return JSONResponse(
                status_code=400,
                content=make_response(400, '该链接尚未支持提取', None, None, False)
            )

        title = cover_url = video_url = None
        downloader = None

        # 小红书平台特殊处理（重试机制）
        if platform == '小红书':
            max_attempts = 5
            attempts = 0
            while attempts < max_attempts:
                downloader = DownloaderFactory.create_downloader(platform, real_url)
                title = downloader.get_title_content()
                video_url = downloader.get_real_video_url()
                cover_url = downloader.get_cover_photo_url()
                if video_url:
                    break
                attempts += 1
                logger.debug(f"Attempt {attempts} failed. Retrying...")
            if not video_url:
                logger.error("Failed to retrieve video URL after 5 attempts.")
        else:
            downloader = DownloaderFactory.create_downloader(platform, real_url)
            title = downloader.get_title_content()
            video_url = downloader.get_real_video_url()
            cover_url = downloader.get_cover_photo_url()

        # 转换为 HTTPS
        updated_video_url = UrlParser.convert_to_https(video_url)
        updated_cover_url = UrlParser.convert_to_https(cover_url)

        data_dict = {
            'video_id': video_id,
            'platform': platform,
            'title': title,
            'video_url': updated_video_url,
            'cover_url': updated_cover_url
        }

        # B站视频需要额外返回音频URL
        if platform == '哔哩哔哩' and downloader and hasattr(downloader, 'get_audio_url'):
            audio_url = downloader.get_audio_url()
            if audio_url:
                data_dict['audio_url'] = UrlParser.convert_to_https(audio_url)

        logger.debug(f'{wx_open_id} {platform} Parse Success')
        return JSONResponse(
            status_code=200,
            content=make_response(200, '成功', data_dict, None, True)
        )

    except Exception as e:
        logger.error(f"Parse error: {e}")
        import traceback
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content=make_response(500, '功能太火爆啦，请稍后再试', None, None, False)
        )


@app.post("/api/download")
async def download_video(
    body: DownloadRequest,
    x_timestamp: str = Header(default="", alias="X-Timestamp"),
    x_gclt_text: str = Header(default="", alias="X-GCLT-Text"),
    x_egct_text: str = Header(default="", alias="X-EGCT-Text"),
    wx_open_id: str = Header(default="Guest", alias="WX-OPEN-ID"),
):
    """
    获取视频下载链接

    对于某些平台，需要服务器端下载并返回可访问的链接
    """
    try:
        request_video_url = body.video_url
        request_video_id = body.video_id

        # 验证请求
        validation_error = validate_request_headers(x_timestamp, x_gclt_text, x_egct_text, request_video_url)
        if validation_error:
            return JSONResponse(status_code=400, content=validation_error)

        # 判断视频链接的域名是否为小程序的合法域名
        domain = UrlParser.get_domain(request_video_url)
        logger.debug(f'Checking domain: {domain}')

        if domain in MINI_PROGRAM_LEGAL_DOMAIN:
            # 如果是，直接返回视频链接
            logger.debug(f'{wx_open_id} 直接返回视频链接')
            return JSONResponse(
                status_code=200,
                content=make_response(200, '成功', {'download_url': request_video_url}, None, True)
            )
        else:
            # 如果不是，服务器端下载视频并保存
            video_filename = f'{request_video_id}.mp4'
            video_path = os.path.join(SAVE_VIDEO_PATH, video_filename)

            # 如果 DOMAIN 未配置，使用本地测试地址
            current_domain = DOMAIN or "http://localhost:5001"

            if not os.path.exists(video_path):
                logger.info(f"Starting server-side download for {request_video_url}")
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
                    'Referer': 'https://www.pearvideo.com/'
                }

                # 如果是梨视频，必须带 Referer
                if "pearvideo.com" in request_video_url:
                    headers['Referer'] = 'https://www.pearvideo.com/'

                # 创建带重试机制的session
                session = requests.Session()
                retries = Retry(
                    total=5,
                    backoff_factor=1,
                    status_forcelist=[500, 502, 503, 504],
                    allowed_methods=["GET"]
                )
                session.mount('http://', HTTPAdapter(max_retries=retries))
                session.mount('https://', HTTPAdapter(max_retries=retries))

                try:
                    response = session.get(request_video_url, headers=headers, stream=True, timeout=120)
                    response.raise_for_status()
                except (RequestException, ConnectionError) as e:
                    logger.error(f'{wx_open_id} Failed to connect: {e}')
                    return JSONResponse(
                        status_code=200,
                        content=make_response(200, '服务器下载失败，返回原始链接', {'download_url': request_video_url}, None, True)
                    )

                try:
                    # 保存视频到服务器
                    with open(video_path, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                except (ChunkedEncodingError, IOError) as e:
                    logger.error(f'{wx_open_id} Failed to save video: {e}')
                    # 删除可能不完整的文件
                    if os.path.exists(video_path):
                        os.remove(video_path)
                    return JSONResponse(
                        status_code=200,
                        content=make_response(200, '服务器下载失败，返回原始链接', {'download_url': request_video_url}, None, True)
                    )

            # 返回视频的 URL
            download_url = f'{current_domain}/static/videos/{video_filename}'
            logger.debug(f'{wx_open_id} 返回视频地址: {download_url}')
            return JSONResponse(
                status_code=200,
                content=make_response(200, '成功', {'download_url': download_url}, None, True)
            )

    except Exception as e:
        logger.error(f"Download error: {e}")
        import traceback
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content=make_response(500, '功能太火爆啦，请稍后再试', None, None, False)
        )


@app.post("/api/frontend/cache")
async def frontend_cache_video(
    body: FrontendMediaRequest,
    x_timestamp: str = Header(default="", alias="X-Timestamp"),
    x_gclt_text: str = Header(default="", alias="X-GCLT-Text"),
    x_egct_text: str = Header(default="", alias="X-EGCT-Text"),
):
    """为 React 前端准备浏览器可播放的缓存视频"""
    validation_error = validate_request_headers(
        x_timestamp, x_gclt_text, x_egct_text, body.video_url, body.video_id
    )
    if validation_error:
        return JSONResponse(status_code=400, content=validation_error)

    try:
        filename, output_path = ensure_frontend_media(body, "frontend-cache")
        size_mb = os.path.getsize(output_path) / 1024 / 1024
        return JSONResponse(
            status_code=200,
            content=make_response(
                200,
                '成功',
                {
                    'play_url': media_static_url(filename),
                    'file_name': filename,
                    'size_mb': round(size_mb, 1),
                    'cached': True
                },
                None,
                True
            )
        )
    except FileNotFoundError:
        return JSONResponse(
            status_code=500,
            content=make_response(500, '未安装 ffmpeg，请先安装 ffmpeg', None, None, False)
        )
    except Exception as e:
        logger.error(f"Frontend cache error: {e}")
        return JSONResponse(
            status_code=500,
            content=make_response(500, f'缓存失败: {str(e)}', None, None, False)
        )


@app.post("/api/frontend/download")
async def frontend_download_video(
    body: FrontendMediaRequest,
    x_timestamp: str = Header(default="", alias="X-Timestamp"),
    x_gclt_text: str = Header(default="", alias="X-GCLT-Text"),
    x_egct_text: str = Header(default="", alias="X-EGCT-Text"),
):
    """为 React 前端准备浏览器可下载的视频文件"""
    validation_error = validate_request_headers(
        x_timestamp, x_gclt_text, x_egct_text, body.video_url, body.video_id
    )
    if validation_error:
        return JSONResponse(status_code=400, content=validation_error)

    try:
        filename, output_path = ensure_frontend_media(body, "frontend-download")
        size_mb = os.path.getsize(output_path) / 1024 / 1024
        return JSONResponse(
            status_code=200,
            content=make_response(
                200,
                '成功',
                {
                    'download_url': media_static_url(filename),
                    'file_name': filename,
                    'size_mb': round(size_mb, 1)
                },
                None,
                True
            )
        )
    except FileNotFoundError:
        return JSONResponse(
            status_code=500,
            content=make_response(500, '未安装 ffmpeg，请先安装 ffmpeg', None, None, False)
        )
    except Exception as e:
        logger.error(f"Frontend download error: {e}")
        return JSONResponse(
            status_code=500,
            content=make_response(500, f'下载准备失败: {str(e)}', None, None, False)
        )


@app.post("/api/frontend/analyze")
async def frontend_analyze_video(
    body: FrontendMediaRequest,
    x_timestamp: str = Header(default="", alias="X-Timestamp"),
    x_gclt_text: str = Header(default="", alias="X-GCLT-Text"),
    x_egct_text: str = Header(default="", alias="X-EGCT-Text"),
):
    """为 React 前端执行视频抽帧与 AI 内容提取"""
    validation_error = validate_request_headers(
        x_timestamp, x_gclt_text, x_egct_text, body.video_url, body.video_id
    )
    if validation_error:
        return JSONResponse(status_code=400, content=validation_error)

    if not QWEN_API_KEY:
        return JSONResponse(
            status_code=400,
            content=make_response(400, '未配置 QWEN_API_KEY，无法进行 AI 内容提取', None, None, False)
        )

    frames = []
    try:
        _, video_path = ensure_frontend_media(body, "frontend-cache")
        frames = extract_video_frames(video_path, MAX_FRAMES)
        if not frames:
            return JSONResponse(
                status_code=500,
                content=make_response(500, '无法提取视频帧，请确保已安装 ffmpeg', None, None, False)
            )

        prompt = f"""这是从一个视频中提取的 {len(frames)} 帧关键画面。
请根据这些画面，详细描述这个视频的内容，包括：
1. 视频中出现的人物或物体
2. 发生的事件或动作
3. 场景环境
4. 视频的主题或表达的意思
5. 视频的整体叙事或故事线

请用中文回答。"""

        content = [{'type': 'text', 'text': prompt}]
        for frame_path in frames:
            content.append({
                'type': 'image_url',
                'image_url': {
                    'url': f'data:image/jpeg;base64,{image_to_base64(frame_path)}'
                }
            })

        qwen_client = OpenAI(
            base_url=QWEN_API_BASE_URL,
            api_key=QWEN_API_KEY,
        )
        response = qwen_client.chat.completions.create(
            model=QWEN_MODEL_ID,
            messages=[{
                'role': 'user',
                'content': content,
            }],
            stream=False
        )

        return JSONResponse(
            status_code=200,
            content=make_response(
                200,
                '成功',
                {
                    'content': response.choices[0].message.content,
                    'frames': len(frames),
                    'model': QWEN_MODEL_ID
                },
                None,
                True
            )
        )
    except FileNotFoundError:
        return JSONResponse(
            status_code=500,
            content=make_response(500, '未安装 ffmpeg，请先安装 ffmpeg', None, None, False)
        )
    except Exception as e:
        logger.error(f"Frontend analyze error: {e}")
        return JSONResponse(
            status_code=500,
            content=make_response(500, f'AI 内容提取失败: {str(e)}', None, None, False)
        )
    finally:
        cleanup_frames(frames)


# ==================== 主程序入口 ====================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api:app",
        host="0.0.0.0",
        port=5001,
        reload=True,
        log_level="info"
    )
