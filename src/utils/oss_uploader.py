"""OSS 图片上传工具 — PNG bytes/本地路径 → 公网 URL。

用法:
    from src.utils.oss_uploader import upload_bytes, upload_image, ensure_public_url

    url = upload_bytes(png_bytes, "charts/tech_chart/TSLA_20260601.png")
    # → "https://happyagent.oss-cn-beijing.aliyuncs.com/charts/tech_chart/TSLA_20260601.png"

    url = ensure_public_url("/path/to/chart.png")
    # 自动上传并返回公网 URL；已是 URL 则原样返回
"""

from __future__ import annotations

import io
import os
import tempfile
import threading
import time

import alibabacloud_oss_v2 as oss
from loguru import logger
from PIL import Image

from src.config import settings

_COMPRESS_MAX_WIDTH = 1200
_COMPRESS_JPEG_QUALITY = 90

def _compress_to_jpeg(data: bytes | str, max_width: int = _COMPRESS_MAX_WIDTH, quality: int = _COMPRESS_JPEG_QUALITY) -> bytes:
    """将图片压缩为 JPEG，保持宽高比，限制最大宽度。"""
    img = Image.open(io.BytesIO(data)) if isinstance(data, bytes) else Image.open(data)

    # RGBA 转 RGB（JPEG 不支持透明通道）
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGB")

    # 等比缩放
    if img.width > max_width:
        ratio = max_width / img.width
        new_height = int(img.height * ratio)
        img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


_client: oss.Client | None = None
_client_lock = threading.Lock()


def _get_client() -> oss.Client:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                credentials_provider = oss.credentials.StaticCredentialsProvider(
                    access_key_id=settings.oss_access_key_id,
                    access_key_secret=settings.oss_access_key_secret,
                )
                cfg = oss.config.load_default()
                cfg.credentials_provider = credentials_provider
                cfg.region = settings.oss_region
                cfg.endpoint = settings.oss_endpoint
                _client = oss.Client(cfg)
    return _client


def upload_bytes(png_bytes: bytes, remote_key: str, compress: bool = True) -> str:
    """将内存中的图片上传到 OSS，返回公网 URL。

    compress=True（默认）：压缩为 JPEG 800px 宽，显著减少 token 消耗。
    """
    logger.debug("OSS 上传开始: key={}, size={}B, compress={}", remote_key, len(png_bytes), compress)
    if compress:
        png_bytes = _compress_to_jpeg(png_bytes)
        # 修正扩展名
        if remote_key.endswith(".png"):
            remote_key = remote_key[:-4] + ".jpg"

    fd, tmp_path = tempfile.mkstemp(suffix=".jpg" if compress else ".png", prefix="chart_")
    try:
        os.write(fd, png_bytes)
        os.close(fd)
        return upload_image(tmp_path, remote_key, compress=False)
    finally:
        if os.path.isfile(tmp_path):
            os.unlink(tmp_path)


def upload_image(local_path: str, remote_key: str, compress: bool = True) -> str:
    """上传本地图片到 OSS，返回公网访问 URL。

    compress=True（默认）：压缩为 JPEG 800px 宽，显著减少 token 消耗。
    """
    if not os.path.isfile(local_path):
        raise FileNotFoundError(f"图片文件不存在: {local_path}")

    file_size = os.path.getsize(local_path)
    logger.debug("OSS 上传开始: key={}, size={}B, compress={}", remote_key, file_size, compress)

    if compress:
        with open(local_path, "rb") as f:
            compressed = _compress_to_jpeg(f.read())
        if remote_key.endswith(".png"):
            remote_key = remote_key[:-4] + ".jpg"
        fd, tmp_path = tempfile.mkstemp(suffix=".jpg", prefix="chart_")
        os.write(fd, compressed)
        os.close(fd)
        client = _get_client()
        try:
            result = client.put_object_from_file(
                oss.PutObjectRequest(bucket=settings.oss_bucket, key=remote_key),
                tmp_path,
            )
            if result.status_code != 200:
                raise RuntimeError(f"OSS 上传失败 (HTTP {result.status_code})")
        finally:
            if os.path.isfile(tmp_path):
                os.unlink(tmp_path)
    else:
        client = _get_client()
        result = client.put_object_from_file(
            oss.PutObjectRequest(bucket=settings.oss_bucket, key=remote_key),
            local_path,
        )
        if result.status_code != 200:
            raise RuntimeError(f"OSS 上传失败 (HTTP {result.status_code})")

    url = f"{settings.oss_base_url}/{remote_key}"
    logger.debug("OSS 上传成功: {}", url)
    return url


def ensure_public_url(path_or_url: str, remote_key: str | None = None, compress: bool = True) -> str:
    """将本地路径转为公网 URL；已是 HTTP(S) URL 则原样返回。"""
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        return path_or_url
    if remote_key is None:
        base = os.path.basename(path_or_url)
        name, ext = os.path.splitext(base)
        ext = ".jpg" if compress else ext
        remote_key = f"charts/{name}_{int(time.time() * 1000)}{ext}"
    return upload_image(path_or_url, remote_key, compress=compress)
