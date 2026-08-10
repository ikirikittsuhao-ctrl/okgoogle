import asyncio
import logging
from typing import Optional, Dict, List, Any
from datetime import datetime, timedelta
from dataclasses import dataclass

import httpx
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.stream import fetch_fastest_stream_urls, fetch_comments
from app.search import (
    fetch_invidious,
    fetch_with_inflight,
    client_session,
    get_invidious_instances_from_url,
    INVIDIOUS_SEARCH_LIST_URL,
    INVIDIOUS_VIDEO_LIST_URL,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.add_extension("jinja2.ext.do")


@dataclass
class CacheEntry:
    value: Any
    expires_at: datetime
    
    def is_expired(self) -> bool:
        return datetime.now() > self.expires_at


class SimpleMemoryCache:
    def __init__(self):
        self._cache: Dict[str, CacheEntry] = {}
    
    def get(self, key: str) -> Optional[Any]:
        entry = self._cache.get(key)
        if entry and not entry.is_expired():
            return entry.value
        if entry:
            del self._cache[key]
        return None
    
    def set(self, key: str, value: Any, ttl_seconds: int = 300) -> None:
        self._cache[key] = CacheEntry(
            value=value,
            expires_at=datetime.now() + timedelta(seconds=ttl_seconds)
        )
    
    def clear(self) -> None:
        self._cache.clear()
    
    def cleanup_expired(self) -> None:
        expired_keys = [
            k for k, v in self._cache.items() 
            if v.is_expired()
        ]
        for k in expired_keys:
            del self._cache[k]


_cache = SimpleMemoryCache()


class NumberFormatter:
    
    _format_cache = {}
    
    @staticmethod
    def format_subscriber_count(count_val: Any) -> str:
        cache_key = f"fmt_{str(count_val)[:50]}"
        if cache_key in NumberFormatter._format_cache:
            return NumberFormatter._format_cache[cache_key]
        
        result = NumberFormatter._do_format(count_val)
        NumberFormatter._format_cache[cache_key] = result
        
        if len(NumberFormatter._format_cache) > 1000:
            NumberFormatter._format_cache.clear()
        
        return result
    
    @staticmethod
    def _do_format(val: Any) -> str:
        if val is None or val == "":
            return "非公開"
        
        if isinstance(val, str):
            return val.strip() or "非公開"
        
        if isinstance(val, (int, float)):
            if val <= 0:
                return "非公開"
            if val >= 10_000_000:
                return f"{val / 10_000_000:.1f}千万人".replace(".0", "")
            elif val >= 10_000:
                return f"{val / 10_000:.1f}万人".replace(".0", "")
            return f"{val:,}人"
        
        return "非公開"


class APIFetcher:
    
    TIMEOUTS = {
        "sia": 2.0,
        "invidious": 3.0,
        "sennin": 4.0,
    }
    
    MAX_RETRIES = 2
    RETRY_BACKOFF = 0.5
    
    @staticmethod
    async def fetch_sia_video_info(v: str, retries: int = 0) -> Optional[Dict]:
        cache_key = f"sia_video_{v}"
        
        cached = _cache.get(cache_key)
        if cached:
            logger.debug(f"Cache hit: {cache_key}")
            return cached
        
        try:
            url = f"https://siatube.com/api/video/{v}"
            resp = await client_session.get(
                url,
                timeout=APIFetcher.TIMEOUTS["sia"]
            )
            
            if resp.status_code == 200:
                data = resp.json()
                if APIFetcher._is_valid_video_data(data):
                    _cache.set(cache_key, data, ttl_seconds=600)
                    return data
        
        except asyncio.TimeoutError:
            logger.warning(f"Sia timeout for {v}")
            if retries < APIFetcher.MAX_RETRIES:
                await asyncio.sleep(APIFetcher.RETRY_BACKOFF * (retries + 1))
                return await APIFetcher.fetch_sia_video_info(v, retries + 1)
        
        except Exception as e:
            logger.error(f"Sia fetch error for {v}: {e}")
        
        return None
    
    @staticmethod
    async def fetch_sennin_video_info(v: str, retries: int = 0) -> Optional[Dict]:
        cache_key = f"sennin_video_{v}"
        
        cached = _cache.get(cache_key)
        if cached:
            logger.debug(f"Cache hit: {cache_key}")
            return cached
        
        try:
            url = f"https://discerning-adventure-production-ebfc.up.railway.app/api/video/{v}"
            resp = await client_session.get(
                url,
                timeout=APIFetcher.TIMEOUTS["sennin"]
            )
            
            if resp.status_code == 200:
                data = resp.json()
                if APIFetcher._is_valid_video_data(data):
                    _cache.set(cache_key, data, ttl_seconds=600)
                    return data
        
        except asyncio.TimeoutError:
            logger.warning(f"Sennin timeout for {v}")
            if retries < APIFetcher.MAX_RETRIES:
                await asyncio.sleep(APIFetcher.RETRY_BACKOFF * (retries + 1))
                return await APIFetcher.fetch_sennin_video_info(v, retries + 1)
        
        except Exception as e:
            logger.error(f"Sennin fetch error for {v}: {e}")
        
        return None
    
    @staticmethod
    def _is_valid_video_data(data: Any) -> bool:
        return isinstance(data, dict) and any(
            key in data for key in ["title", "videoDetails", "streamingData", "videoId", "author"]
        )


class InvidiousVideoFetcher:
    
    @staticmethod
    async def fetch_video_data(
        v: str,
        force_instance: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        
        try:
            video_data = await asyncio.wait_for(
                fetch_invidious(f"/videos/{v}", force_instance=force_instance),
                timeout=APIFetcher.TIMEOUTS["invidious"] + 1.0
            )
            return video_data
        
        except asyncio.TimeoutError:
            logger.warning(f"Invidious timeout for {v}")
            return None
        
        except Exception as e:
            logger.error(f"Invidious fetch error for {v}: {e}")
            return None


class DataParser:
    
    @staticmethod
    def extract_video_title(video_data: Dict) -> str:
        for key in ("title", "videoDetails", "titleText"):
            if value := video_data.get(key):
                if isinstance(value, dict):
                    return value.get("text", value.get("title", ""))
                return str(value)
        return ""
    
    @staticmethod
    def extract_author(video_data: Dict) -> str:
        for key in ("author", "authorName", "channelName"):
            if value := video_data.get(key):
                return value
        return ""
    
    @staticmethod
    def extract_author_id(video_data: Dict) -> str:
        for key in ("authorId", "channelId", "ucid"):
            if value := video_data.get(key):
                return value
        return ""
    
    @staticmethod
    def extract_author_icon(video_data: Dict) -> str:
        if thumbnails := video_data.get("authorThumbnails"):
            if isinstance(thumbnails, list) and thumbnails:
                return thumbnails[-1].get("url", "")
        
        for key in ("authorIcon", "channelThumbnail", "authorAvatar"):
            if value := video_data.get(key):
                return value
        return ""
    
    @staticmethod
    def extract_description(video_data: Dict) -> str:
        for key in ("description", "descriptionHtml", "descriptionSnippet"):
            if value := video_data.get(key):
                return value
        return ""
    
    @staticmethod
    def extract_view_count(video_data: Dict) -> int:
        for key in ("viewCount", "views", "likeCount"):
            if value := video_data.get(key):
                if isinstance(value, int):
                    return value
                try:
                    return int(str(value).replace(",", ""))
                except:
                    pass
        return 0
    
    @staticmethod
    def extract_like_count(video_data: Dict) -> int:
        for key in ("likeCount", "likes", "rating"):
            if value := video_data.get(key):
                if isinstance(value, int):
                    return value
                try:
                    return int(str(value).replace(",", ""))
                except:
                    pass
        return 0
    
    @staticmethod
    def extract_video_streams(video_data: Dict) -> List[Dict]:
        streams = []
        
        if formats := video_data.get("formatStreams"):
            for fmt in formats:
                if url := fmt.get("url"):
                    streams.append({
                        "url": url,
                        "resolution": fmt.get("qualityLabel", fmt.get("quality", "Unknown")),
                        "format": fmt.get("type", "mp4"),
                        "audioUrl": "",
                    })
        
        if adaptive := video_data.get("adaptiveFormats"):
            for fmt in adaptive:
                if url := fmt.get("url"):
                    streams.append({
                        "url": url,
                        "resolution": fmt.get("qualityLabel", fmt.get("quality", "Unknown")),
                        "format": fmt.get("type", "webm"),
                        "audioUrl": "",
                    })
        
        return streams


async def fetch_video_info_best_api(
    v: str,
    api: Optional[str] = None,
    force_instance: Optional[str] = None,
) -> Dict[str, Any]:
    cache_key = f"video_best:{v}:{api or ''}:{force_instance or ''}"

    async def _do_fetch() -> Dict[str, Any]:
        result = {
            "title": "",
            "author": "",
            "author_id": "",
            "author_icon": "",
            "description": "",
            "view_count": 0,
            "like_count": 0,
            "streams": [],
            "info_api_used": "unknown",
        }

        if api == "sia":
            try:
                sia_data = await APIFetcher.fetch_sia_video_info(v)
                if sia_data:
                    result.update({
                        "title": DataParser.extract_video_title(sia_data),
                        "author": DataParser.extract_author(sia_data),
                        "author_id": DataParser.extract_author_id(sia_data),
                        "author_icon": DataParser.extract_author_icon(sia_data),
                        "description": DataParser.extract_description(sia_data),
                        "view_count": DataParser.extract_view_count(sia_data),
                        "like_count": DataParser.extract_like_count(sia_data),
                        "streams": DataParser.extract_video_streams(sia_data),
                        "info_api_used": "sia",
                    })
                    return result
            except Exception:
                pass
        
        elif api == "sennin":
            try:
                sennin_data = await APIFetcher.fetch_sennin_video_info(v)
                if sennin_data:
                    result.update({
                        "title": DataParser.extract_video_title(sennin_data),
                        "author": DataParser.extract_author(sennin_data),
                        "author_id": DataParser.extract_author_id(sennin_data),
                        "author_icon": DataParser.extract_author_icon(sennin_data),
                        "description": DataParser.extract_description(sennin_data),
                        "view_count": DataParser.extract_view_count(sennin_data),
                        "like_count": DataParser.extract_like_count(sennin_data),
                        "streams": DataParser.extract_video_streams(sennin_data),
                        "info_api_used": "sennin",
                    })
                    return result
            except Exception:
                pass
        
        elif api == "invidious":
            try:
                inv_data = await InvidiousVideoFetcher.fetch_video_data(v, force_instance=force_instance)
                if inv_data:
                    result.update({
                        "title": DataParser.extract_video_title(inv_data),
                        "author": DataParser.extract_author(inv_data),
                        "author_id": DataParser.extract_author_id(inv_data),
                        "author_icon": DataParser.extract_author_icon(inv_data),
                        "description": DataParser.extract_description(inv_data),
                        "view_count": DataParser.extract_view_count(inv_data),
                        "like_count": DataParser.extract_like_count(inv_data),
                        "streams": DataParser.extract_video_streams(inv_data),
                        "info_api_used": "invidious",
                    })
                    return result
            except Exception:
                pass

        tasks = {
            "invidious": asyncio.create_task(
                InvidiousVideoFetcher.fetch_video_data(v, force_instance=force_instance)
            ),
            "sia": asyncio.create_task(APIFetcher.fetch_sia_video_info(v)),
            "sennin": asyncio.create_task(APIFetcher.fetch_sennin_video_info(v)),
        }

        done, pending = await asyncio.wait(
            tasks.values(),
            timeout=4.0,
            return_when=asyncio.FIRST_COMPLETED,
        )

        for name, task in tasks.items():
            if task in done:
                try:
                    data = task.result()
                    if data:
                        result.update({
                            "title": DataParser.extract_video_title(data),
                            "author": DataParser.extract_author(data),
                            "author_id": DataParser.extract_author_id(data),
                            "author_icon": DataParser.extract_author_icon(data),
                            "description": DataParser.extract_description(data),
                            "view_count": DataParser.extract_view_count(data),
                            "like_count": DataParser.extract_like_count(data),
                            "streams": DataParser.extract_video_streams(data),
                            "info_api_used": name,
                        })
                        for t in pending:
                            t.cancel()
                        return result
                except Exception:
                    continue

        for task in pending:
            task.cancel()

        return result

    return await fetch_with_inflight(cache_key, _do_fetch, ttl=180.0)


@router.get("/watch", response_class=HTMLResponse)
async def watch(
    request: Request,
    v: str = Query(...),
    list: str = Query(None),
    info_api: str = Query(None),
    stream_api: str = Query(None),
    force_instance: str = Query(None),
):
    try:
        logger.info(f"Watch request: v={v}, info_api={info_api}, stream_api={stream_api}")

        video_info_task = asyncio.create_task(
            fetch_video_info_best_api(v, api=info_api, force_instance=force_instance)
        )

        stream_urls_task = asyncio.create_task(
            fetch_fastest_stream_urls(v, api=stream_api, force_instance=force_instance)
        )

        comments_task = asyncio.create_task(
            fetch_comments(v, force_instance=force_instance, api=info_api)
        )

        video_info, stream_urls_data, comments_data = await asyncio.gather(
            video_info_task,
            stream_urls_task,
            comments_task,
            return_exceptions=False
        )

        video_urls = []
        stream_urls = []
        stream_api_used = "unknown"

        if stream_urls_data:
            stream_urls = stream_urls_data.get("streamUrls", [])
            video_urls = stream_urls_data.get("videoUrls", [])
            stream_api_used = stream_urls_data.get("stream_api_used", "unknown")

        info_api_used = video_info.get("info_api_used", "unknown")

        context = {
            "request": request,
            "videoid": v,
            "video_title": video_info.get("title", ""),
            "author": video_info.get("author", ""),
            "author_id": video_info.get("author_id", ""),
            "author_icon": video_info.get("author_icon", ""),
            "subscribers_count": "非公開",
            "description": video_info.get("description", ""),
            "view_count": f"{video_info.get('view_count', 0):,}",
            "like_count": video_info.get("like_count", 0),
            "videourls": video_urls,
            "streamUrls": stream_urls,
            "youtube_url": f"https://www.youtube.com/watch?v={v}",
            "comments": comments_data.get("comments", []) if comments_data else [],
            "info_api_used": info_api_used,
            "stream_api_used": stream_api_used,
            "playlist_id": list or "",
            "playlist_title": "",
            "playlist_videos": [],
            "recommended_videos": [],
        }

        logger.info(
            f"Watch loaded: v={v}, info_api={info_api_used}, "
            f"stream_api={stream_api_used}, title={context['video_title']}"
        )

        return templates.TemplateResponse("watch_production.html", context)

    except asyncio.TimeoutError:
        logger.warning(f"Timeout fetching video: {v}")
        return templates.TemplateResponse("apitimeout.html", {"request": request})

    except Exception as e:
        logger.error(f"Watch fetch failed for {v}: {e}", exc_info=True)
        return templates.TemplateResponse(
            "apiallerror.html",
            {"request": request, "error": str(e)},
        )


@router.get("/api/watch/{v}")
async def api_watch(
    v: str,
    info_api: str = Query(None),
    stream_api: str = Query(None),
    force_instance: str = Query(None),
):
    try:
        logger.info(f"API Watch request: v={v}, info_api={info_api}, stream_api={stream_api}")

        video_info_task = asyncio.create_task(
            fetch_video_info_best_api(v, api=info_api, force_instance=force_instance)
        )

        stream_urls_task = asyncio.create_task(
            fetch_fastest_stream_urls(v, api=stream_api, force_instance=force_instance)
        )

        comments_task = asyncio.create_task(
            fetch_comments(v, force_instance=force_instance, api=info_api)
        )

        video_info, stream_urls_data, comments_data = await asyncio.gather(
            video_info_task,
            stream_urls_task,
            comments_task,
            return_exceptions=False
        )

        return {
            "videoid": v,
            "video_title": video_info.get("title", ""),
            "author": video_info.get("author", ""),
            "author_id": video_info.get("author_id", ""),
            "author_icon": video_info.get("author_icon", ""),
            "description": video_info.get("description", ""),
            "view_count": video_info.get("view_count", 0),
            "like_count": video_info.get("like_count", 0),
            "video_urls": stream_urls_data.get("videoUrls", []) if stream_urls_data else [],
            "stream_urls": stream_urls_data.get("streamUrls", []) if stream_urls_data else [],
            "comments": comments_data.get("comments", []) if comments_data else [],
            "info_api_used": video_info.get("info_api_used", "unknown"),
            "stream_api_used": stream_urls_data.get("stream_api_used", "unknown") if stream_urls_data else "unknown",
        }

    except Exception as e:
        logger.error(f"API Watch failed for {v}: {e}", exc_info=True)
        return {
            "error": str(e),
            "videoid": v,
            "info_api_used": "error",
            "stream_api_used": "error",
        }


@router.get("/api/stream/{v}")
async def api_stream(
    v: str,
    stream_api: str = Query(None),
    force_instance: str = Query(None),
):
    try:
        logger.info(f"API Stream request: v={v}, stream_api={stream_api}")

        stream_urls_data = await fetch_fastest_stream_urls(
            v, api=stream_api, force_instance=force_instance
        )

        if stream_urls_data:
            return {
                "videoid": v,
                "stream_urls": stream_urls_data.get("streamUrls", []),
                "video_urls": stream_urls_data.get("videoUrls", []),
                "stream_api_used": stream_urls_data.get("stream_api_used", "unknown"),
            }

        return {
            "videoid": v,
            "stream_urls": [],
            "video_urls": [],
            "stream_api_used": "error",
            "error": "Failed to fetch streams",
        }

    except Exception as e:
        logger.error(f"API Stream failed for {v}: {e}", exc_info=True)
        return {
            "videoid": v,
            "error": str(e),
            "stream_api_used": "error",
        }


@router.get("/api/info/{v}")
async def api_info(
    v: str,
    info_api: str = Query(None),
    force_instance: str = Query(None),
):
    try:
        logger.info(f"API Info request: v={v}, info_api={info_api}")

        video_info = await fetch_video_info_best_api(
            v, api=info_api, force_instance=force_instance
        )

        return {
            "videoid": v,
            "title": video_info.get("title", ""),
            "author": video_info.get("author", ""),
            "author_id": video_info.get("author_id", ""),
            "author_icon": video_info.get("author_icon", ""),
            "description": video_info.get("description", ""),
            "view_count": video_info.get("view_count", 0),
            "like_count": video_info.get("like_count", 0),
            "info_api_used": video_info.get("info_api_used", "unknown"),
        }

    except Exception as e:
        logger.error(f"API Info failed for {v}: {e}", exc_info=True)
        return {
            "videoid": v,
            "error": str(e),
            "info_api_used": "error",
        }


@router.get("/api/comments/{v}")
async def api_comments(
    v: str,
    info_api: str = Query(None),
    force_instance: str = Query(None),
):
    try:
        logger.info(f"API Comments request: v={v}, info_api={info_api}")

        comments_data = await fetch_comments(
            v, force_instance=force_instance, api=info_api
        )

        if comments_data:
            return {
                "videoid": v,
                "comments": comments_data.get("comments", []),
            }

        return {
            "videoid": v,
            "comments": [],
        }

    except Exception as e:
        logger.error(f"API Comments failed for {v}: {e}", exc_info=True)
        return {
            "videoid": v,
            "error": str(e),
            "comments": [],
        }


@router.get("/cache/clear")
async def clear_cache():
    _cache.clear()
    logger.info("Cache cleared")
    return {"status": "cleared"}


@router.get("/cache/stats")
async def cache_stats():
    return {
        "cache_size": len(_cache._cache),
        "expired_entries": sum(
            1 for entry in _cache._cache.values()
            if entry.is_expired()
        ),
        "timestamp": datetime.now().isoformat(),
    }
