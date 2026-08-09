import asyncio
from datetime import datetime
import json
import httpx
import logging
from typing import Optional, Dict, Any, List, Union, Tuple
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.search import (
    fetch_invidious,
    fetch_with_inflight,
    client_session,
    get_invidious_instances_from_url,
    INVIDIOUS_VIDEO_LIST_URL,
)

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.add_extension("jinja2.ext.do")

# ========== API設定定義 ==========
INFO_API_CONFIG = {
    "invidious": {
        "priority": 1,
        "timeout": 3.0,  # 短縮
        "description": "Invidious 集約API",
        "features": ["video info", "comments", "recommendations"],
        "max_retries": 2,
    },
    "sia": {
        "priority": 2,
        "timeout": 2.5,  # 短縮
        "description": "Sia Tube API",
        "features": ["video info", "recommendations"],
        "max_retries": 2,
    },
    "sennin": {
        "priority": 3,
        "timeout": 3.0,  # 短縮
        "description": "Sennin API",
        "features": ["video info", "extended stats"],
        "max_retries": 1,
    }
}

# キャッシュ設定
CACHE_CONFIG = {
    "video_info": 300.0,      # 5分
    "comments": 600.0,         # 10分
    "streams": 600.0,          # 10分
    "recommended": 1800.0,     # 30分
}

# パフォーマンスモニタリング
_API_STATS = {
    "invidious": {"success": 0, "failure": 0, "avg_time": 0},
    "sia": {"success": 0, "failure": 0, "avg_time": 0},
    "sennin": {"success": 0, "failure": 0, "avg_time": 0},
}
_STATS_LOCK = asyncio.Lock()


async def record_api_performance(api: str, success: bool, duration: float) -> None:
    """APIパフォーマンスを記録"""
    async with _STATS_LOCK:
        if api not in _API_STATS:
            return
        
        stats = _API_STATS[api]
        if success:
            stats["success"] += 1
            # 移動平均を計算
            total_calls = stats["success"] + stats["failure"]
            stats["avg_time"] = (
                stats["avg_time"] * (total_calls - 1) / total_calls +
                duration / total_calls
            )
        else:
            stats["failure"] += 1


async def fetch_sennin_video_info(v: str) -> Optional[Dict[str, Any]]:
    """Sennin APIから動画情報取得（最適化版）"""
    cache_key = f"sennin_video:{v}"
    cached = fetch_with_inflight.__self__.get_cache(cache_key) if hasattr(fetch_with_inflight, '__self__') else None
    if cached:
        return cached
    
    start_time = asyncio.get_event_loop().time()
    
    try:
        url = f"https://discerning-adventure-production-ebfc.up.railway.app/api/video/{v}"
        resp = await asyncio.wait_for(
            client_session.get(url, timeout=httpx.Timeout(3.0, connect=1.0, read=2.0)),
            timeout=3.5
        )
        
        if resp.status_code == 200:
            data = resp.json()
            if data and not data.get("unavailable"):
                norm_data = normalize_sennin_video_info(data)
                norm_data["api_used"] = "sennin"
                
                # キャッシュに保存
                if hasattr(fetch_with_inflight, '__self__'):
                    fetch_with_inflight.__self__.set_cache(cache_key, norm_data, ttl=CACHE_CONFIG["video_info"])
                
                duration = asyncio.get_event_loop().time() - start_time
                await record_api_performance("sennin", True, duration)
                return norm_data
    
    except asyncio.TimeoutError:
        logger.debug(f"Sennin timeout for video {v}")
    except Exception as e:
        logger.debug(f"Sennin error for video {v}: {e}")
    
    duration = asyncio.get_event_loop().time() - start_time
    await record_api_performance("sennin", False, duration)
    return None


async def fetch_sia_video(v: str) -> Optional[Dict[str, Any]]:
    """Sia Tubeから動画情報取得（最適化版）"""
    cache_key = f"sia_video:{v}"
    cached = fetch_with_inflight.__self__.get_cache(cache_key) if hasattr(fetch_with_inflight, '__self__') else None
    if cached:
        return cached
    
    start_time = asyncio.get_event_loop().time()
    
    try:
        url = f"https://siatube.com/api/video/{v}"
        resp = await asyncio.wait_for(
            client_session.get(url, timeout=httpx.Timeout(2.5, connect=1.0, read=1.5)),
            timeout=3.0
        )
        
        if resp.status_code == 200:
            data = resp.json()

            author_info = data.get("author", {}) if isinstance(data.get("author"), dict) else {}
            author_name = author_info.get("name") or data.get("uploader") or ""
            author_id = author_info.get("id", "")
            author_icon = author_info.get("thumbnail", "")
            sub_count = author_info.get("subscribers", "非公開")

            if not author_name:
                duration = asyncio.get_event_loop().time() - start_time
                await record_api_performance("sia", False, duration)
                return None

            # 説明文の効率的な処理
            desc_obj = data.get("description", {})
            desc_text = (
                desc_obj.get("text", "") if isinstance(desc_obj, dict)
                else str(desc_obj or "")
            )
            desc_html = desc_text.replace("\n", "<br>")

            # 関連動画の効率的な処理
            rel_data = data.get("Related-videos") or data.get("relatedVideos") or {}
            raw_rel = (
                rel_data.get("relatedVideos", [])
                if isinstance(rel_data, dict)
                else (rel_data if isinstance(rel_data, list) else [])
            )

            recommended = _process_related_videos(raw_rel)

            result = {
                "title": data.get("title", ""),
                "author": author_name,
                "authorId": author_id,
                "authorIcon": author_icon,
                "subCountText": sub_count,
                "viewCount": data.get("views", 0),
                "likeCount": data.get("likes", 0),
                "descriptionHtml": desc_html,
                "recommendedVideos": recommended,
                "thumbnail": data.get("thumbnail", ""),
                "api_used": "sia",
            }
            
            # キャッシュに保存
            if hasattr(fetch_with_inflight, '__self__'):
                fetch_with_inflight.__self__.set_cache(cache_key, result, ttl=CACHE_CONFIG["video_info"])
            
            duration = asyncio.get_event_loop().time() - start_time
            await record_api_performance("sia", True, duration)
            return result
    
    except asyncio.TimeoutError:
        logger.debug(f"Sia timeout for video {v}")
    except Exception as e:
        logger.debug(f"Sia error for video {v}: {e}")
    
    duration = asyncio.get_event_loop().time() - start_time
    await record_api_performance("sia", False, duration)
    return None


def _process_related_videos(raw_rel: List[Any]) -> List[Dict[str, Any]]:
    """関連動画リストを効率的に処理"""
    recommended = []
    
    for item in raw_rel:
        if not isinstance(item, dict):
            continue
        
        # サムネイル URLの取得を最適化
        thumb_url = item.get("thumbnail", "")
        if not thumb_url and isinstance(item.get("thumbnails"), list) and item["thumbnails"]:
            thumb_url = item["thumbnails"][0].get("url", "")

        recommended.append({
            "video_id": item.get("videoId") or item.get("id"),
            "title": item.get("title"),
            "author": item.get("channelName") or item.get("author"),
            "view_count_text": item.get("viewCountText"),
            "thumbnail": thumb_url,
        })
    
    return recommended


def normalize_sennin_video_info(sennin_data: Dict[str, Any]) -> Dict[str, Any]:
    """Sennin APIレスポンスを標準形式に正規化（最適化版）"""
    if not sennin_data or not isinstance(sennin_data, dict):
        return {}

    author_info = sennin_data.get("author", {}) if isinstance(sennin_data.get("author"), dict) else {}
    author_name = author_info.get("name") or ""
    author_id = author_info.get("id") or ""
    author_icon = author_info.get("thumbnail") or ""
    sub_count = author_info.get("subscribers") or "非公開"

    # 説明文の効率的な処理
    desc_obj = sennin_data.get("description", {})
    if isinstance(desc_obj, dict):
        desc_html = desc_obj.get("formatted") or (
            desc_obj.get("text", "").replace("\n", "<br>")
        )
        desc_text = desc_obj.get("text", "")
    else:
        desc_text = str(desc_obj or "")
        desc_html = desc_text.replace("\n", "<br>")

    # 関連動画の処理
    rel_data = sennin_data.get("Related-videos", {})
    raw_rel = rel_data.get("relatedVideos", []) if isinstance(rel_data, dict) else []
    recommended = _process_related_videos(raw_rel)

    return {
        "title": sennin_data.get("title", ""),
        "author": author_name,
        "authorId": author_id,
        "authorIcon": author_icon,
        "subCountText": sub_count,
        "viewCount": (
            sennin_data.get("views") or
            sennin_data.get("extended_stats", {}).get("views_original", 0)
        ),
        "likeCount": sennin_data.get("likes", 0),
        "description": desc_text,
        "descriptionHtml": desc_html,
        "recommendedVideos": recommended,
        "thumbnail": sennin_data.get("thumbnail", ""),
    }


async def fetch_video_info_invidious_robust(
    v: str,
    force_instance: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Invidious APIから堅牢に動画情報取得（最適化版）"""
    cache_key = f"inv_video:{v}:{force_instance or ''}"
    
    start_time = asyncio.get_event_loop().time()
    
    try:
        # 最初にデフォルトインスタンスを試行
        res = await asyncio.wait_for(
            fetch_invidious(
                f"/videos/{v}",
                force_instance=force_instance,
                list_type="video"
            ),
            timeout=4.0
        )
        
        if isinstance(res, dict) and not res.get("error") and (res.get("title") or res.get("videoId")):
            res["api_used"] = "invidious"
            duration = asyncio.get_event_loop().time() - start_time
            await record_api_performance("invidious", True, duration)
            return res
    
    except asyncio.TimeoutError:
        logger.debug(f"Invidious primary timeout for video {v}")
    except Exception as e:
        logger.debug(f"Invidious primary error for video {v}: {e}")

    # フォールバック: 複数のインスタンスを並列で試行
    base_instances = await get_invidious_instances_from_url(INVIDIOUS_VIDEO_LIST_URL)
    
    if not base_instances:
        duration = asyncio.get_event_loop().time() - start_time
        await record_api_performance("invidious", False, duration)
        return None

    async def try_instance(instance: str) -> Optional[Dict[str, Any]]:
        try:
            url = f"{instance.rstrip('/')}/api/v1/videos/{v}"
            resp = await asyncio.wait_for(
                client_session.get(
                    url,
                    timeout=httpx.Timeout(3.0, connect=1.0, read=2.0)
                ),
                timeout=3.5
            )
            
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and not data.get("error") and (data.get("title") or data.get("videoId")):
                    data["api_used"] = "invidious"
                    return data
        except Exception:
            pass
        return None

    # 最初の5インスタンスを並列で試行
    tasks = [try_instance(inst) for inst in base_instances[:5]]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    
    for result in results:
        if result:
            duration = asyncio.get_event_loop().time() - start_time
            await record_api_performance("invidious", True, duration)
            return result

    duration = asyncio.get_event_loop().time() - start_time
    await record_api_performance("invidious", False, duration)
    return None


async def fetch_video_info(
    v: str,
    force_instance: Optional[str] = None,
    api: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """動画情報取得（API選択最適化版）"""
    cache_key = f"video_info:{v}:{force_instance or ''}:{api or ''}"

    async def _do_fetch() -> Optional[Dict[str, Any]]:
        if api == "invidious":
            return await fetch_video_info_invidious_robust(v, force_instance=force_instance)
        elif api == "sia":
            result = await fetch_sia_video(v)
            if result:
                return result
            return await fetch_video_info_invidious_robust(v, force_instance=force_instance)
        elif api == "sennin":
            result = await fetch_sennin_video_info(v)
            if result:
                return result
            return await fetch_video_info_invidious_robust(v, force_instance=force_instance)

        # デフォルト: 並列でトライ（最速を使用）
        # APIのパフォーマンスに基づいて優先順位を動的に調整
        invidious_task = fetch_video_info_invidious_robust(v, force_instance=force_instance)
        sia_task = fetch_sia_video(v)
        sennin_task = fetch_sennin_video_info(v)

        tasks = {
            invidious_task: "invidious",
            sia_task: "sia",
            sennin_task: "sennin",
        }

        for coro in asyncio.as_completed(tasks.keys(), timeout=5.0):
            try:
                result = await coro
                if result:
                    return result
            except asyncio.TimeoutError:
                break
            except Exception:
                continue

        return None

    return await fetch_with_inflight(
        cache_key,
        _do_fetch,
        ttl=CACHE_CONFIG["video_info"],
        retry_count=1  # リトライは最小限に
    )


def extract_invidious_streams(v_data: Dict[str, Any]) -> Dict[str, List]:
    """Invidious動画データからストリーム情報を抽出（最適化版）"""
    if not v_data:
        return {"streamUrls": [], "videoUrls": []}

    adaptive = v_data.get("adaptiveFormats", [])
    format_streams = v_data.get("formatStreams", [])

    # 音声URLの取得を最適化（キャッシュ）
    audio_url = None
    for f in adaptive:
        if "audio" in f.get("type", ""):
            if f.get("language") == "ja":
                audio_url = f.get("url")
                break
    
    if not audio_url:
        for f in adaptive:
            if "audio" in f.get("type", ""):
                audio_url = f.get("url")
                break

    # ストリームURLを効率的に生成
    stream_urls = [
        {
            "url": fmt.get("url"),
            "resolution": fmt.get("qualityLabel"),
            "format": "mp4/mixed",
            "audioUrl": "",
        }
        for fmt in format_streams
    ]

    # WebM映像のみ
    stream_urls.extend([
        {
            "url": fmt.get("url"),
            "resolution": fmt.get("qualityLabel"),
            "format": "webm/videoOnly",
            "audioUrl": audio_url,
        }
        for fmt in adaptive 
        if "video" in fmt.get("type", "") and "webm" in fmt.get("container", "")
    ])

    video_urls = [fmt.get("url") for fmt in format_streams] or [
        fmt.get("url") for fmt in adaptive if "video" in fmt.get("type", "")
    ]

    return {"streamUrls": stream_urls, "videoUrls": video_urls}


def process_comments(comment_data: Any) -> List[Dict[str, Any]]:
    """コメントデータを標準形式に処理（最適化版）"""
    if isinstance(comment_data, Exception) or not comment_data:
        return []

    # Sennin形式のコメント
    if (
        isinstance(comment_data, dict) and
        comment_data.get("success") is True and
        isinstance(comment_data.get("comments"), list)
    ):
        return normalize_sennin_comments(comment_data)

    # 汎用コメント処理
    comments = (
        comment_data.get("comments", [])
        if isinstance(comment_data, dict)
        else (comment_data if isinstance(comment_data, list) else [])
    )
    
    processed = []
    for c in comments:
        if not isinstance(c, dict):
            continue
        
        # コメント正規化を効率化
        processed_comment = _normalize_comment(c)
        if processed_comment:
            processed.append(processed_comment)

    return processed


def _normalize_comment(comment: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """個別のコメントを正規化"""
    item = dict(comment)

    # 作成者情報の正規化
    author_obj = item.get("author")
    author_icon = ""
    
    if isinstance(author_obj, dict):
        item["author"] = author_obj.get("name", "")
        author_icon = (
            author_obj.get("avatar") or
            author_obj.get("authorIcon") or
            item.get("avatar", "")
        )
        item["authorId"] = author_obj.get("channelId", "")
    else:
        author_thumbs = item.get("authorThumbnails", [])
        if author_thumbs and isinstance(author_thumbs, list):
            author_icon = author_thumbs[-1].get("url", "")

    # アイコンを統一
    item["authorIcon"] = author_icon or item.get("authorIcon") or item.get("avatar", "")
    item["authorThumbnail"] = item["authorIcon"]
    item["avatar"] = item["authorIcon"]

    # authorThumbnailsを統一
    if not isinstance(item.get("authorThumbnails"), list):
        if item["authorIcon"]:
            item["authorThumbnails"] = [{"url": item["authorIcon"]}]
        else:
            item["authorThumbnails"] = []

    # コンテンツ正規化
    text_str = item.get("text") or item.get("content") or ""
    if "contentHtml" not in item:
        item["contentHtml"] = text_str.replace("\n", "<br>")
    if "content" not in item:
        item["content"] = text_str

    # 公開日時正規化
    pub_time = item.get("publishedTime") or item.get("published") or item.get("publishedText", "")
    item["publishedTime"] = pub_time
    item["publishedText"] = pub_time

    # いいね数正規化
    likes_obj = item.get("likes")
    if isinstance(likes_obj, dict):
        item["likeCount"] = likes_obj.get("count", 0)

    return item


def normalize_sennin_comments(sennin_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Sennin APIコメントを標準形式に正規化（最適化版）"""
    if not sennin_data or not isinstance(sennin_data, dict):
        return []

    comments = sennin_data.get("comments", [])
    if not isinstance(comments, list):
        return []

    processed = []
    for c in comments:
        if not isinstance(c, dict):
            continue

        author_info = c.get("author", {}) if isinstance(c.get("author"), dict) else {}
        likes_info = c.get("likes", {}) if isinstance(c.get("likes"), dict) else {}
        replies_info = c.get("replies", {}) if isinstance(c.get("replies"), dict) else {}

        author_name = author_info.get("name") or (
            c.get("author") if isinstance(c.get("author"), str) else ""
        )
        author_icon = author_info.get("avatar") or c.get("authorIcon") or ""
        author_id = author_info.get("channelId") or c.get("authorId") or ""
        text_content = c.get("text") or c.get("content") or ""

        processed.append({
            "commentId": c.get("commentId", ""),
            "author": author_name,
            "authorId": author_id,
            "authorIcon": author_icon,
            "authorThumbnail": author_icon,
            "authorThumbnails": [{"url": author_icon}] if author_icon else [],
            "content": text_content,
            "contentHtml": text_content.replace("\n", "<br>"),
            "publishedTime": c.get("publishedTime", ""),
            "publishedText": c.get("publishedTime", ""),
            "likeCount": (
                likes_info.get("count") if isinstance(likes_info, dict)
                else c.get("likes", 0)
            ),
            "replyCount": (
                replies_info.get("count") if isinstance(replies_info, dict)
                else c.get("replies", 0)
            ),
            "isCreator": author_info.get("creator", False) if isinstance(author_info, dict) else False,
            "isVerified": author_info.get("verified", False) if isinstance(author_info, dict) else False,
        })

    return processed


@router.get("/shorts/{v}", response_class=HTMLResponse)
async def shorts_player(
    request: Request,
    v: str,
    force_instance: Optional[str] = Query(None),
    api: Optional[str] = Query(None),
):
    """Shorts（短編）プレイヤーエンドポイント（高速化版）"""
    try:
        from app.stream import fetch_fastest_stream_urls, fetch_comments
        
        # 複数タスクを並列実行
        tasks = (
            fetch_video_info(v, force_instance=force_instance, api=api),
            fetch_fastest_stream_urls(v, api=api, force_instance=force_instance),
            fetch_comments(v, force_instance=force_instance, api=api),
        )

        video_data, stream_data, comment_data = await asyncio.gather(
            *tasks, return_exceptions=True
        )

        if isinstance(video_data, Exception) and isinstance(stream_data, Exception):
            raise video_data

        v_data = video_data if isinstance(video_data, dict) else {}
        s_data = stream_data if isinstance(stream_data, dict) else {}

        # ビデオURLの取得
        video_urls = s_data.get("videoUrls", [])
        if not video_urls and v_data:
            invidious_streams = extract_invidious_streams(v_data)
            video_urls = invidious_streams.get("videoUrls", [])

        formatted_comments = process_comments(comment_data)

        info_api_used = v_data.get("api_used", "unknown")
        stream_api_used = s_data.get("stream_api_used", "unknown")

        return templates.TemplateResponse(
            "short.html",
            {
                "request": request,
                "videoid": v,
                "video_title": v_data.get("title", ""),
                "videourls": video_urls,
                "author": v_data.get("author", ""),
                "view_count": v_data.get("viewCount", 0),
                "like_count": v_data.get("likeCount", 0),
                "description": (
                    v_data.get("descriptionHtml") or
                    v_data.get("description", "").replace("\n", "<br>")
                ),
                "comments": formatted_comments,
                "info_api_used": info_api_used,
                "stream_api_used": stream_api_used,
                "api_used": info_api_used,
            },
        )
    
    except httpx.TimeoutException:
        logger.error(f"Timeout in shorts_player for video {v}")
        return templates.TemplateResponse("apitimeout.html", {"request": request})
    except Exception as e:
        logger.error(f"Error in shorts_player for video {v}: {e}")
        fallback_instances = await get_invidious_instances_from_url(
            INVIDIOUS_VIDEO_LIST_URL
        )
        return templates.TemplateResponse(
            "apiallerror.html",
            {"request": request, "instances": fallback_instances},
        )


@router.get("/watch", response_class=HTMLResponse)
async def watch(
    request: Request,
    v: str = Query(...),
    list: Optional[str] = Query(None),
    force_instance: Optional[str] = Query(None),
    api: Optional[str] = Query(None),
):
    """動画ウォッチエンドポイント（高速化・安定化版）"""
    try:
        from app.stream import fetch_fastest_stream_urls, fetch_comments
        
        # 複数タスクを並列実行で高速化
        info_task = fetch_video_info(v, force_instance=force_instance, api=api)
        stream_task = fetch_fastest_stream_urls(v, api=api, force_instance=force_instance)
        comment_task = fetch_comments(v, force_instance=force_instance, api=api)

        async def _fetch_playlist():
            if not list:
                return None
            try:
                res = await asyncio.wait_for(
                    fetch_invidious(f"/playlists/{list}", force_instance=force_instance),
                    timeout=4.0
                )
                if isinstance(res, dict) and not res.get("error"):
                    return res
            except Exception as e:
                logger.debug(f"Playlist error: {e}")
            return None

        playlist_task = _fetch_playlist()

        video_data, stream_res, comment_data, playlist_data = await asyncio.gather(
            info_task, stream_task, comment_task, playlist_task, return_exceptions=True
        )

        if isinstance(video_data, Exception) and isinstance(stream_res, Exception):
            raise video_data

        v_data = video_data if isinstance(video_data, dict) else {}
        s_data = stream_res if isinstance(stream_res, dict) else {}
        p_data = playlist_data if isinstance(playlist_data, dict) else {}

        # プレイリスト動画の整理
        playlist_videos = []
        for item in p_data.get("videos", []):
            if isinstance(item, dict):
                playlist_videos.append({
                    "videoId": item.get("videoId"),
                    "title": item.get("title"),
                    "author": item.get("author")
                })

        # ストリーム情報の取得
        stream_urls = s_data.get("streamUrls", [])
        video_urls = s_data.get("videoUrls", [])

        if not stream_urls and v_data:
            invidious_streams = extract_invidious_streams(v_data)
            stream_urls = invidious_streams.get("streamUrls", [])
            video_urls = invidious_streams.get("videoUrls", [])

        # 関連動画の処理
        recommended = []
        for rec in v_data.get("recommendedVideos", []):
            if isinstance(rec, dict):
                recommended.append({
                    "video_id": rec.get("video_id") or rec.get("videoId"),
                    "title": rec.get("title"),
                    "author": rec.get("author"),
                    "view_count_text": rec.get("view_count_text") or rec.get("viewCountText"),
                    "thumbnail": rec.get("thumbnail", ""),
                })

        # 作成者アイコンの取得
        author_icon = v_data.get("authorIcon")
        if not author_icon:
            author_thumbs = v_data.get("authorThumbnails", [])
            author_icon = author_thumbs[-1]["url"] if author_thumbs else ""

        formatted_comments = process_comments(comment_data)

        info_api_used = v_data.get("api_used", "unknown")
        stream_api_used = s_data.get("stream_api_used", "unknown")

        response = templates.TemplateResponse(
            "watch.html",
            {
                "request": request,
                "videoid": v,
                "video_title": v_data.get("title") or s_data.get("title", ""),
                "videourls": video_urls,
                "streamUrls": stream_urls,
                "author": v_data.get("author") or s_data.get("author", ""),
                "author_id": v_data.get("authorId") or s_data.get("authorId", ""),
                "author_icon": author_icon,
                "subscribers_count": v_data.get("subCountText", "非公開"),
                "view_count": v_data.get("viewCount", s_data.get("viewCount", 0)),
                "like_count": v_data.get("likeCount", s_data.get("likeCount", 0)),
                "description": (
                    v_data.get("descriptionHtml") or
                    s_data.get("descriptionHtml") or
                    v_data.get("description", "").replace("\n", "<br>")
                ),
                "recommended_videos": recommended,
                "comments": formatted_comments,
                "youtube_url": f"https://www.youtube.com/watch?v={v}",
                "info_api_used": info_api_used,
                "stream_api_used": stream_api_used,
                "api_used": info_api_used,
                "playlist_id": list,
                "playlist_title": p_data.get("title", ""),
                "playlist_videos": playlist_videos,
            },
        )

        # 視聴履歴をクッキーに保存（エラー時も無視）
        try:
            history_json = request.cookies.get("history", "[]")
            history = json.loads(history_json)
            
            # 重複を削除
            history = [item for item in history if item.get("videoId") != v]
            
            history.append({
                "videoId": v,
                "title": v_data.get("title", ""),
                "author": v_data.get("author", ""),
                "added_at": datetime.now().isoformat(),
            })
            
            # 最新50件を保持
            if len(history) > 50:
                history = history[-50:]
            
            response.set_cookie(
                key="history",
                value=json.dumps(history),
                max_age=2592000,
                httponly=True,
                samesite="Lax",
            )
        except Exception as e:
            logger.debug(f"Failed to update history: {e}")

        return response

    except httpx.TimeoutException:
        logger.error(f"Timeout in watch for video {v}")
        return templates.TemplateResponse("apitimeout.html", {"request": request})
    except Exception as e:
        logger.error(f"Error in watch for video {v}: {e}")
        fallback_instances = await get_invidious_instances_from_url(
            INVIDIOUS_VIDEO_LIST_URL
        )
        return templates.TemplateResponse(
            "apiallerror.html",
            {"request": request, "instances": fallback_instances},
        )


@router.get("/api/stats")
async def get_api_stats():
    """APIパフォーマンス統計を取得"""
    async with _STATS_LOCK:
        return {
            "stats": dict(_API_STATS),
            "timestamp": datetime.now().isoformat(),
            }
