import asyncio
import random
from fastapi import APIRouter
from fastapi.templating import Jinja2Templates

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

# ========== API設定定義 ==========
STREAM_API_CONFIG = {
    "sia": {
        "base_url": "https://siatube.com/api/stream",
        "timeout": 2.5,
        "weight": 100,
        "description": "Sia Tube API"
    },
    "piped": {
        "base_url": "piped_instances",  # 複数インスタンス
        "timeout": 2.5,
        "weight": 90,
        "description": "Piped インスタンス"
    },
    "rapidapi": {
        "base_url": f"https://{RAPID_API_HOST}/dl",
        "timeout": 2.5,
        "weight": 80,
        "description": "RapidAPI YouTubeStreamer"
    },
    "zernio": {
        "base_url": "https://getlate.dev/api/tools/youtube-live-downloader",
        "timeout": 3.0,
        "weight": 70,
        "description": "Zernio ダウンローダ"
    },
    "invidious": {
        "base_url": "invidious_instances",
        "timeout": 4.0,
        "weight": 60,
        "description": "Invidious API"
    },
    "sennin": {
        "base_url": "https://discerning-adventure-production-ebfc.up.railway.app/api/stream",
        "timeout": 3.5,
        "weight": 50,
        "description": "Sennin API"
    }
}

INFO_API_CONFIG = {
    "invidious": {
        "base_url": "invidious_instances",
        "timeout": 4.0,
        "weight": 100,
        "description": "Invidious API"
    },
    "sia": {
        "base_url": "https://siatube.com/api/video",
        "timeout": 3.0,
        "weight": 90,
        "description": "Sia Tube API"
    },
    "sennin": {
        "base_url": "https://discerning-adventure-production-ebfc.up.railway.app/api/video",
        "timeout": 4.0,
        "weight": 80,
        "description": "Sennin API"
    }
}


async def fetch_sennin_comments(v: str, sort: str = "top"):
    """Sennin APIからコメント取得（最新実装）"""
    try:
        url = f"{SENNIN_API_BASE}/api/comments"
        params = {"videoId": v, "sort": sort}
        resp = await client_session.get(url, params=params, timeout=4.0)
        if resp.status_code == 200:
            data = resp.json()
            if data and data.get("success") is True:
                return data
    except asyncio.TimeoutError:
        raise Exception("Sennin comments timeout")
    except Exception:
        pass
    raise Exception("Sennin comments failed")


async def fetch_sia_stream(v: str):
    """Sia TubeのストリームURL取得"""
    try:
        url = f"https://siatube.com/api/stream/{v}"
        resp = await client_session.get(url, timeout=2.5)
        if resp.status_code == 200:
            data = resp.json()
            stream_urls = []
            video_urls = []

            # マージされた形式を処理
            muxed = data.get("muxed", []) or data.get("formats", []) or []
            if isinstance(muxed, list):
                for item in muxed:
                    u = item.get("url")
                    if u:
                        video_urls.append(u)
                        stream_urls.append({
                            "url": u,
                            "resolution": item.get("quality", item.get("qualityLabel", "Auto")),
                            "format": "mp4/mixed",
                            "audioUrl": "",
                        })

            # HLS形式
            hls_url = data.get("hls") or data.get("m3u8") or data.get("manifestUrl")
            if hls_url:
                if hls_url not in video_urls:
                    video_urls.append(hls_url)
                stream_urls.append({
                    "url": hls_url,
                    "resolution": "HLS/Live",
                    "format": "application/x-mpegURL",
                    "audioUrl": "",
                })

            # 音声のみ
            audio_only = data.get("audioOnly", []) or []
            audio_url = (
                audio_only[0].get("url")
                if isinstance(audio_only, list) and len(audio_only) > 0
                else None
            )

            # 映像のみ
            video_only = data.get("videoOnly", []) or []
            if isinstance(video_only, list):
                for item in video_only:
                    u = item.get("url")
                    if u:
                        stream_urls.append({
                            "url": u,
                            "resolution": item.get("quality", item.get("qualityLabel", "1080p")),
                            "format": "webm/videoOnly",
                            "audioUrl": audio_url or "",
                        })

            if not video_urls and stream_urls:
                video_urls = [s["url"] for s in stream_urls if s.get("url")]

            if video_urls:
                return {
                    "streamUrls": stream_urls,
                    "videoUrls": video_urls,
                    "stream_api_used": "sia",
                }
    except asyncio.TimeoutError:
        raise Exception("Sia stream timeout")
    except Exception:
        pass
    raise Exception("Sia failed")


async def fetch_piped_stream(v: str):
    """PipedのストリームURL取得（複数インスタンス対応）"""
    instances = list(PIPED_INSTANCES)
    random.shuffle(instances)
    for instance in instances:
        try:
            url = f"{instance.rstrip('/')}/streams/{v}"
            resp = await client_session.get(url, timeout=2.5)
            if resp.status_code == 200:
                data = resp.json()
                stream_urls = []
                video_urls = []
                audio_url = None

                # 音声ストリーム
                for item in data.get("audioStreams", []):
                    if item.get("mimeType", "").startswith("audio"):
                        audio_url = item.get("url")
                        break

                # 映像ストリーム
                for item in data.get("videoStreams", []):
                    url_str = item.get("url")
                    quality = item.get("quality", "")
                    if item.get("videoOnly", False):
                        stream_urls.append({
                            "url": url_str,
                            "resolution": quality,
                            "format": "webm/videoOnly",
                            "audioUrl": audio_url,
                        })
                    else:
                        stream_urls.append({
                            "url": url_str,
                            "resolution": quality,
                            "format": "mp4/mixed",
                            "audioUrl": "",
                        })
                        video_urls.append(url_str)

                if not video_urls:
                    video_urls = [s["url"] for s in stream_urls if s.get("url")]

                return {
                    "streamUrls": stream_urls,
                    "videoUrls": video_urls,
                    "title": data.get("title"),
                    "author": data.get("uploader"),
                    "authorId": data.get("uploaderUrl", "").replace("/channel/", ""),
                    "descriptionHtml": data.get("description", "").replace("\n", "<br>"),
                    "viewCount": data.get("views", 0),
                    "likeCount": data.get("likes", 0),
                    "stream_api_used": "piped",
                }
        except asyncio.TimeoutError:
            continue
        except Exception:
            continue
    raise Exception("Piped failed")


async def fetch_zernio_stream(v: str):
    """ZernioのストリームURL取得（ライブ・ダウンローダ対応）"""
    try:
        target_url = f"https://www.youtube.com/watch?v={v}"
        url = f"https://getlate.dev/api/tools/youtube-live-downloader?url={target_url}"
        resp = await no_redirect_client.get(url, timeout=3.0)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location") or resp.headers.get("Location")
            if location:
                return {
                    "streamUrls": [{
                        "url": location,
                        "resolution": "Live/Auto",
                        "format": "mp4/mixed",
                        "audioUrl": "",
                    }],
                    "videoUrls": [location],
                    "stream_api_used": "zernio",
                }
    except asyncio.TimeoutError:
        raise Exception("Zernio timeout")
    except Exception:
        pass
    raise Exception("Zernio failed")


async def fetch_rapidapi_stream(v: str):
    """RapidAPI YouTubeStreamerからストリームURL取得"""
    keys = _get_rapid_api_keys()
    random.shuffle(keys)

    for key in keys:
        try:
            url = f"https://{RAPID_API_HOST}/dl?id={v}"
            headers = {"X-RapidAPI-Key": key, "X-RapidAPI-Host": RAPID_API_HOST}
            resp = await client_session.get(url, headers=headers, timeout=2.5)
            if resp.status_code == 200:
                data = resp.json()
                formats = data.get("formats", [])
                stream_urls = []
                video_urls = []
                for f in formats:
                    u = f.get("url")
                    if u:
                        video_urls.append(u)
                        stream_urls.append({
                            "url": u,
                            "resolution": f.get("qualityLabel", "720p"),
                            "format": "mp4/mixed",
                            "audioUrl": "",
                        })
                if video_urls:
                    return {
                        "streamUrls": stream_urls,
                        "videoUrls": video_urls,
                        "title": data.get("title"),
                        "stream_api_used": "rapidapi",
                    }
        except asyncio.TimeoutError:
            continue
        except Exception:
            continue
    raise Exception("RapidAPI failed")


async def fetch_fastest_stream_urls(
    v: str, api: str = None, force_instance: str = None
):
    """最速のストリームURL取得（API選択最適化版）"""
    from app.video import extract_invidious_streams, fetch_video_info_invidious_robust
    
    cache_key = f"fastest_stream:{v}:{api or ''}:{force_instance or ''}"

    async def _do_fetch():
        # 指定APIで取得
        if api == "sia":
            try:
                return await fetch_sia_stream(v)
            except Exception:
                v_data = await fetch_video_info_invidious_robust(v, force_instance=force_instance)
                res = extract_invidious_streams(v_data)
                res["stream_api_used"] = "invidious_fallback"
                return res
        elif api == "piped":
            try:
                return await fetch_piped_stream(v)
            except Exception:
                v_data = await fetch_video_info_invidious_robust(v, force_instance=force_instance)
                res = extract_invidious_streams(v_data)
                res["stream_api_used"] = "invidious_fallback"
                return res
        elif api == "rapidapi":
            try:
                return await fetch_rapidapi_stream(v)
            except Exception:
                v_data = await fetch_video_info_invidious_robust(v, force_instance=force_instance)
                res = extract_invidious_streams(v_data)
                res["stream_api_used"] = "invidious_fallback"
                return res
        elif api == "zernio":
            try:
                return await fetch_zernio_stream(v)
            except Exception:
                v_data = await fetch_video_info_invidious_robust(v, force_instance=force_instance)
                res = extract_invidious_streams(v_data)
                res["stream_api_used"] = "invidious_fallback"
                return res
        elif api == "invidious":
            v_data = await fetch_video_info_invidious_robust(v, force_instance=force_instance)
            res = extract_invidious_streams(v_data)
            res["stream_api_used"] = "invidious"
            return res
        elif api == "sennin":
            try:
                # Sennin APIがある場合はそれを使用
                return await fetch_sia_stream(v)  # Sennin用の実装に置き換え可能
            except Exception:
                v_data = await fetch_video_info_invidious_robust(v, force_instance=force_instance)
                res = extract_invidious_streams(v_data)
                res["stream_api_used"] = "invidious_fallback"
                return res

        # デフォルト: 並列取得で最速のものを返す
        try:
            v_data = await fetch_video_info_invidious_robust(v, force_instance=force_instance)
            inv_streams = extract_invidious_streams(v_data)
            if inv_streams and inv_streams.get("videoUrls"):
                inv_streams["stream_api_used"] = "invidious"
                return inv_streams
        except Exception:
            pass

        # フォールバック: 複数APIを並列実行で最速を取得
        tasks = [
            ("sia", asyncio.create_task(fetch_sia_stream(v))),
            ("piped", asyncio.create_task(fetch_piped_stream(v))),
            ("rapidapi", asyncio.create_task(fetch_rapidapi_stream(v))),
            ("zernio", asyncio.create_task(fetch_zernio_stream(v))),
        ]

        completed_first = None
        for api_name, task in tasks:
            try:
                result = await asyncio.wait_for(task, timeout=2.5)
                if result and result.get("videoUrls"):
                    if completed_first is None:
                        completed_first = result
                    break
            except (asyncio.TimeoutError, Exception):
                continue

        # 残りのタスクをキャンセル
        for _, task in tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if completed_first:
            return completed_first

        return None

    return await fetch_with_inflight(cache_key, _do_fetch, ttl=120.0)


async def fetch_sia_comments(v: str):
    """Sia TubeのコメントURL取得"""
    try:
        url = f"https://siatube.com/api/comments?videoId={v}"
        resp = await client_session.get(url, timeout=3.5)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict) and "comments" in data:
                return data
    except asyncio.TimeoutError:
        raise Exception("Sia comments timeout")
    except Exception:
        pass
    raise Exception("Sia comments failed")


async def fetch_comments(v: str, force_instance: str = None, api: str = None):
    """コメント取得（API選択最適化版）"""
    cache_key = f"comments:{v}:{force_instance or ''}:{api or ''}"

    async def _do_fetch():
        if api == "sia":
            try:
                return await fetch_sia_comments(v)
            except Exception:
                return await fetch_invidious(
                    f"/comments/{v}", force_instance=force_instance, list_type="video"
                )
        elif api == "invidious":
            return await fetch_invidious(
                f"/comments/{v}", force_instance=force_instance, list_type="video"
            )
        elif api == "sennin":
            try:
                return await fetch_sennin_comments(v)
            except Exception:
                return await fetch_invidious(
                    f"/comments/{v}", force_instance=force_instance, list_type="video"
                )

        # デフォルト: Invidious → Sia → Sennin の順で試行
        try:
            return await fetch_invidious(
                f"/comments/{v}", force_instance=force_instance, list_type="video"
            )
        except Exception:
            pass

        try:
            return await fetch_sia_comments(v)
        except Exception:
            pass

        try:
            return await fetch_sennin_comments(v)
        except Exception:
            pass

        return None

    return await fetch_with_inflight(cache_key, _do_fetch, ttl=180.0)
