import asyncio
import random
import time
from typing import Optional, Dict, List, Any
from fastapi import APIRouter
from fastapi.templating import Jinja2Templates
from dataclasses import dataclass
from enum import Enum

from app.search import (
    fetch_invidious,
    fetch_with_inflight,
    client_session,
    no_redirect_client,
    get_invidious_instances_from_url,
    INVIDIOUS_VIDEO_LIST_URL,
    PIPED_INSTANCES,
    SENNIN_API_BASE,
    _get_rapid_api_keys,
    RAPID_API_HOST,
)

router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.add_extension("jinja2.ext.do")

RAPID_API_HOST = "ytstream-download-youtube-videos.p.rapidapi.com"

# ========== 型定義 ==========
class StreamProvider(Enum):
    """ストリームプロバイダーの列挙型"""
    SIA = "sia"
    PIPED = "piped"
    RAPIDAPI = "rapidapi"
    ZERNIO = "zernio"
    INVIDIOUS = "invidious"
    SENNIN = "sennin"


@dataclass
class ProviderConfig:
    """プロバイダー設定"""
    name: str
    base_url: str
    timeout: float
    weight: int
    description: str
    handler: Optional[callable] = None


@dataclass
class StreamUrl:
    """ストリームURL"""
    url: str
    resolution: str
    format: str
    audio_url: str = ""


@dataclass
class StreamResult:
    """ストリーム取得結果"""
    stream_urls: List[StreamUrl]
    video_urls: List[str]
    stream_api_used: str
    title: Optional[str] = None
    author: Optional[str] = None
    author_id: Optional[str] = None
    description_html: Optional[str] = None
    view_count: int = 0
    like_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """辞書形式に変換"""
        return {
            "streamUrls": [
                {
                    "url": s.url,
                    "resolution": s.resolution,
                    "format": s.format,
                    "audioUrl": s.audio_url,
                }
                for s in self.stream_urls
            ],
            "videoUrls": self.video_urls,
            "stream_api_used": self.stream_api_used,
            "title": self.title,
            "author": self.author,
            "authorId": self.author_id,
            "descriptionHtml": self.description_html,
            "viewCount": self.view_count,
            "likeCount": self.like_count,
        }


# ========== API設定（簡潔化） ==========
STREAM_API_CONFIG = {
    StreamProvider.SIA: ProviderConfig(
        name="sia",
        base_url="https://siatube.com/api/stream",
        timeout=2.5,
        weight=100,
        description="Sia Tube API",
    ),
    StreamProvider.PIPED: ProviderConfig(
        name="piped",
        base_url="piped_instances",
        timeout=2.5,
        weight=90,
        description="Piped インスタンス",
    ),
    StreamProvider.RAPIDAPI: ProviderConfig(
        name="rapidapi",
        base_url=f"https://{RAPID_API_HOST}/dl",
        timeout=2.5,
        weight=80,
        description="RapidAPI YouTubeStreamer",
    ),
    StreamProvider.ZERNIO: ProviderConfig(
        name="zernio",
        base_url="https://getlate.dev/api/tools/youtube-live-downloader",
        timeout=3.0,
        weight=70,
        description="Zernio ダウンローダ",
    ),
    StreamProvider.INVIDIOUS: ProviderConfig(
        name="invidious",
        base_url="invidious_instances",
        timeout=4.0,
        weight=60,
        description="Invidious API",
    ),
    StreamProvider.SENNIN: ProviderConfig(
        name="sennin",
        base_url="https://discerning-adventure-production-ebfc.up.railway.app/api/stream",
        timeout=3.5,
        weight=50,
        description="Sennin API",
    ),
}

INFO_API_CONFIG = {
    StreamProvider.INVIDIOUS: ProviderConfig(
        name="invidious",
        base_url="invidious_instances",
        timeout=4.0,
        weight=100,
        description="Invidious API",
    ),
    StreamProvider.SIA: ProviderConfig(
        name="sia",
        base_url="https://siatube.com/api/video",
        timeout=3.0,
        weight=90,
        description="Sia Tube API",
    ),
    StreamProvider.SENNIN: ProviderConfig(
        name="sennin",
        base_url="https://discerning-adventure-production-ebfc.up.railway.app/api/video",
        timeout=4.0,
        weight=80,
        description="Sennin API",
    ),
}

# ========== ユーティリティ関数 ==========

def _normalize_stream_urls(
    formats: List[Dict[str, Any]],
    format_type: str = "mp4/mixed",
    audio_url: str = ""
) -> List[StreamUrl]:
    """フォーマットリストをStreamUrlのリストに正規化"""
    urls = []
    for item in formats:
        url = item.get("url")
        if not url:
            continue
        
        resolution = item.get(
            "quality",
            item.get("qualityLabel", item.get("quality_label", "Auto"))
        )
        
        urls.append(StreamUrl(
            url=url,
            resolution=str(resolution),
            format=format_type,
            audio_url=audio_url,
        ))
    return urls


async def _fetch_with_timeout(
    url: str,
    timeout: float,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, str]] = None,
    use_no_redirect: bool = False,
) -> Optional[Dict[str, Any]]:
    """タイムアウト付きでリクエストを実行"""
    try:
        session = no_redirect_client if use_no_redirect else client_session
        resp = await asyncio.wait_for(
            session.get(url, headers=headers, params=params, timeout=timeout),
            timeout=timeout + 0.5,  # 余裕を持たせる
        )
        if resp.status_code == 200:
            return resp.json()
        return None
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return None
    except Exception:
        return None


# ========== 個別プロバイダー実装 ==========

async def fetch_sia_stream(v: str) -> StreamResult:
    """Sia Tube - ストリーム取得"""
    try:
        url = f"https://siatube.com/api/stream/{v}"
        data = await _fetch_with_timeout(url, timeout=2.5)
        
        if not data:
            raise Exception("Sia API response empty")

        stream_urls = []
        video_urls = []

        # マージされた形式（muxed/formats）
        muxed = data.get("muxed", []) or data.get("formats", []) or []
        if isinstance(muxed, list):
            stream_urls.extend(_normalize_stream_urls(muxed, "mp4/mixed"))
            video_urls.extend([s.url for s in stream_urls if s.url])

        # HLS形式
        hls_url = data.get("hls") or data.get("m3u8") or data.get("manifestUrl")
        if hls_url and hls_url not in video_urls:
            video_urls.append(hls_url)
            stream_urls.append(StreamUrl(
                url=hls_url,
                resolution="HLS/Live",
                format="application/x-mpegURL",
            ))

        # 音声のみ
        audio_only = data.get("audioOnly", []) or []
        audio_url = (
            audio_only[0].get("url")
            if isinstance(audio_only, list) and len(audio_only) > 0
            else ""
        )

        # 映像のみ
        video_only = data.get("videoOnly", []) or []
        if isinstance(video_only, list):
            stream_urls.extend(_normalize_stream_urls(
                video_only,
                "webm/videoOnly",
                audio_url
            ))

        if not video_urls and stream_urls:
            video_urls = [s.url for s in stream_urls if s.url]

        if not stream_urls:
            raise Exception("No streams found")

        return StreamResult(
            stream_urls=stream_urls,
            video_urls=video_urls,
            stream_api_used="sia",
        )

    except Exception as e:
        raise Exception(f"Sia failed: {str(e)}")


async def fetch_piped_stream(v: str) -> StreamResult:
    """Piped - ストリーム取得（複数インスタンス対応）"""
    instances = list(PIPED_INSTANCES)
    random.shuffle(instances)
    
    last_error = None
    for instance in instances:
        try:
            url = f"{instance.rstrip('/')}/streams/{v}"
            data = await _fetch_with_timeout(url, timeout=2.5)
            
            if not data:
                continue

            stream_urls = []
            video_urls = []

            # 音声ストリーム
            audio_url = ""
            for item in data.get("audioStreams", []):
                if item.get("mimeType", "").startswith("audio"):
                    audio_url = item.get("url", "")
                    break

            # 映像ストリーム
            for item in data.get("videoStreams", []):
                url_str = item.get("url")
                if not url_str:
                    continue
                
                quality = item.get("quality", "")
                if item.get("videoOnly", False):
                    stream_urls.append(StreamUrl(
                        url=url_str,
                        resolution=quality,
                        format="webm/videoOnly",
                        audio_url=audio_url,
                    ))
                else:
                    stream_urls.append(StreamUrl(
                        url=url_str,
                        resolution=quality,
                        format="mp4/mixed",
                    ))
                    video_urls.append(url_str)

            if not video_urls:
                video_urls = [s.url for s in stream_urls if s.url]

            if not stream_urls:
                continue

            return StreamResult(
                stream_urls=stream_urls,
                video_urls=video_urls,
                title=data.get("title"),
                author=data.get("uploader"),
                author_id=data.get("uploaderUrl", "").replace("/channel/", ""),
                description_html=data.get("description", "").replace("\n", "<br>"),
                view_count=data.get("views", 0),
                like_count=data.get("likes", 0),
                stream_api_used="piped",
            )

        except Exception as e:
            last_error = e
            continue

    raise Exception(f"Piped failed: {str(last_error) if last_error else 'All instances failed'}")


async def fetch_rapidapi_stream(v: str) -> StreamResult:
    """RapidAPI - ストリーム取得"""
    keys = _get_rapid_api_keys()
    random.shuffle(keys)

    last_error = None
    for key in keys:
        try:
            url = f"https://{RAPID_API_HOST}/dl?id={v}"
            headers = {
                "X-RapidAPI-Key": key,
                "X-RapidAPI-Host": RAPID_API_HOST
            }
            data = await _fetch_with_timeout(url, headers=headers, timeout=2.5)

            if not data:
                continue

            formats = data.get("formats", [])
            stream_urls = _normalize_stream_urls(formats, "mp4/mixed")
            video_urls = [s.url for s in stream_urls if s.url]

            if not stream_urls:
                continue

            return StreamResult(
                stream_urls=stream_urls,
                video_urls=video_urls,
                title=data.get("title"),
                stream_api_used="rapidapi",
            )

        except Exception as e:
            last_error = e
            continue

    raise Exception(f"RapidAPI failed: {str(last_error) if last_error else 'All keys failed'}")


async def fetch_zernio_stream(v: str) -> StreamResult:
    """Zernio - ストリーム取得（リダイレクト対応、formatId=2指定）"""
    try:
        target_url = f"https://www.youtube.com/watch?v={v}"
        url = f"https://getlate.dev/api/tools/youtube-live-downloader?url={target_url}&formatId=2"
        
        # リダイレクトを追跡
        resp = await asyncio.wait_for(
            no_redirect_client.get(url, timeout=3.0),
            timeout=3.5
        )
        
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location") or resp.headers.get("Location")
            if location:
                return StreamResult(
                    stream_urls=[StreamUrl(
                        url=location,
                        resolution="360p",
                        format="mp4/mixed",
                    )],
                    video_urls=[location],
                    stream_api_used="zernio",
                )

        raise Exception("No redirect received")

    except Exception as e:
        raise Exception(f"Zernio failed: {str(e)}")


async def fetch_sennin_stream(v: str) -> StreamResult:
    """Sennin - ストリーム取得（フォールバック用）"""
    try:
        url = f"{SENNIN_API_BASE}/api/stream/{v}"
        data = await _fetch_with_timeout(url, timeout=3.5)
        
        if not data:
            raise Exception("Sennin API response empty")

        formats = data.get("formats", [])
        stream_urls = _normalize_stream_urls(formats, "mp4/mixed")
        video_urls = [s.url for s in stream_urls if s.url]

        if not stream_urls:
            raise Exception("No streams found")

        return StreamResult(
            stream_urls=stream_urls,
            video_urls=video_urls,
            stream_api_used="sennin",
        )

    except Exception as e:
        raise Exception(f"Sennin failed: {str(e)}")


# ========== コメント取得 ==========

async def fetch_sia_comments(v: str) -> Dict[str, Any]:
    """Sia Tube - コメント取得"""
    try:
        url = f"https://siatube.com/api/comments?videoId={v}"
        data = await _fetch_with_timeout(url, timeout=3.5)
        
        if data and isinstance(data, dict) and "comments" in data:
            return data

        raise Exception("Invalid response format")

    except Exception as e:
        raise Exception(f"Sia comments failed: {str(e)}")


async def fetch_sennin_comments(v: str, sort: str = "top") -> Dict[str, Any]:
    """Sennin - コメント取得"""
    try:
        url = f"{SENNIN_API_BASE}/api/comments"
        params = {"videoId": v, "sort": sort}
        data = await _fetch_with_timeout(url, params=params, timeout=4.0)

        if data and data.get("success") is True:
            return data

        raise Exception("Invalid response")

    except Exception as e:
        raise Exception(f"Sennin comments failed: {str(e)}")


# ========== 最適化: 並列取得 ==========

async def _fetch_stream_with_fallback(
    v: str,
    api: Optional[str] = None,
    force_instance: Optional[str] = None,
) -> Optional[StreamResult]:
    """
    単一プロバイダーからストリーム取得（フォールバック付き）
    """
    from app.video import extract_invidious_streams, fetch_video_info_invidious_robust

    provider_map = {
        "sia": fetch_sia_stream,
        "piped": fetch_piped_stream,
        "rapidapi": fetch_rapidapi_stream,
        "zernio": fetch_zernio_stream,
        "sennin": fetch_sennin_stream,
    }

    # 指定プロバイダーで取得
    if api and api in provider_map:
        try:
            return await provider_map[api](v)
        except Exception:
            pass  # フォールバックに進む

    # Invidious フォールバック
    try:
        v_data = await fetch_video_info_invidious_robust(
            v, force_instance=force_instance
        )
        res_dict = extract_invidious_streams(v_data)
        res_dict["stream_api_used"] = "invidious_fallback"
        # 辞書をStreamResultに変換
        return StreamResult(
            stream_urls=[
                StreamUrl(
                    url=s["url"],
                    resolution=s.get("resolution", "Auto"),
                    format=s.get("format", "mp4/mixed"),
                    audio_url=s.get("audioUrl", ""),
                )
                for s in res_dict.get("streamUrls", [])
            ],
            video_urls=res_dict.get("videoUrls", []),
            stream_api_used=res_dict.get("stream_api_used", "invidious_fallback"),
            title=res_dict.get("title"),
            author=res_dict.get("author"),
            author_id=res_dict.get("authorId"),
            description_html=res_dict.get("descriptionHtml"),
            view_count=res_dict.get("viewCount", 0),
            like_count=res_dict.get("likeCount", 0),
        )
    except Exception:
        pass

    return None


async def fetch_fastest_stream_urls(
    v: str,
    api: Optional[str] = None,
    force_instance: Optional[str] = None,
    timeout: float = 2.5,
) -> Optional[Dict[str, Any]]:
    """
    最速のストリームURL取得（最適化版）

    戦略:
    1. 指定APIがあれば優先
    2. Invidiousを並列実行
    3. 複数プロバイダーをタイムアウト競争
    """
    from app.video import extract_invidious_streams, fetch_video_info_invidious_robust

    cache_key = f"fastest_stream:{v}:{api or ''}:{force_instance or ''}"

    async def _do_fetch() -> Optional[Dict[str, Any]]:
        # 1. 指定APIで直接取得
        if api:
            try:
                result = await _fetch_stream_with_fallback(
                    v, api=api, force_instance=force_instance
                )
                if result:
                    return result.to_dict()
            except Exception:
                pass

        # Zernio 最優先試行
        try:
            zernio_result = await fetch_zernio_stream(v)
            if zernio_result and zernio_result.video_urls:
                return zernio_result.to_dict()
        except Exception:
            pass

        # 2. Invidious 優先（安定性重視）
        try:
            v_data = await asyncio.wait_for(
                fetch_video_info_invidious_robust(v, force_instance=force_instance),
                timeout=timeout + 1.0,
            )
            res_dict = extract_invidious_streams(v_data)
            if res_dict.get("videoUrls"):
                res_dict["stream_api_used"] = "invidious"
                return res_dict
        except Exception:
            pass

        # 3. 複数プロバイダーを並列実行（最速競争）
        providers = [
            ("sia", fetch_sia_stream),
            ("piped", fetch_piped_stream),
            ("rapidapi", fetch_rapidapi_stream),
            ("zernio", fetch_zernio_stream),
        ]

        tasks = {
            name: asyncio.create_task(fetch_fn(v))
            for name, fetch_fn in providers
        }

        done, pending = await asyncio.wait(
            tasks.values(),
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )

        # 最初に完了したものを返す
        for name, task in tasks.items():
            if task in done:
                try:
                    result = task.result()
                    if result and result.video_urls:
                        # 残りのタスクをキャンセル
                        for t in pending:
                            t.cancel()
                        return result.to_dict()
                except Exception:
                    continue

        # すべてのタスクをキャンセル
        for task in pending:
            task.cancel()

        return None

    return await fetch_with_inflight(cache_key, _do_fetch, ttl=120.0)


async def fetch_comments(
    v: str,
    force_instance: Optional[str] = None,
    api: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    コメント取得（最適化版）

    戦略:
    1. 指定APIで取得
    2. 複数プロバイダーを並列実行（最速競争）
    """
    cache_key = f"comments:{v}:{force_instance or ''}:{api or ''}"

    async def _do_fetch() -> Optional[Dict[str, Any]]:
        # 指定APIで優先取得
        if api == "sia":
            try:
                return await fetch_sia_comments(v)
            except Exception:
                pass
        elif api == "sennin":
            try:
                return await fetch_sennin_comments(v)
            except Exception:
                pass
        elif api == "invidious":
            try:
                return await fetch_invidious(
                    f"/comments/{v}", force_instance=force_instance, list_type="video"
                )
            except Exception:
                pass

        # 複数プロバイダーを並列実行
        tasks = {
            "invidious": asyncio.create_task(
                fetch_invidious(
                    f"/comments/{v}",
                    force_instance=force_instance,
                    list_type="video",
                )
            ),
            "sia": asyncio.create_task(fetch_sia_comments(v)),
            "sennin": asyncio.create_task(fetch_sennin_comments(v)),
        }

        done, pending = await asyncio.wait(
            tasks.values(),
            timeout=3.0,
            return_when=asyncio.FIRST_COMPLETED,
        )

        # 最初に完了したものを返す
        for name, task in tasks.items():
            if task in done:
                try:
                    result = task.result()
                    if result:
                        for t in pending:
                            t.cancel()
                        return result
                except Exception:
                    continue

        # すべてのタスクをキャンセル
        for task in pending:
            task.cancel()

        return None

    return await fetch_with_inflight(cache_key, _do_fetch, ttl=180.0)
