import asyncio
import random
from typing import Optional, Dict, List, Any, Callable
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
)

router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.add_extension("jinja2.ext.do")

RAPID_API_HOST = "ytstream-download-youtube-videos.p.rapidapi.com"


class StreamProvider(Enum):
    SIA = "sia"
    PIPED = "piped"
    RAPIDAPI = "rapidapi"
    ZERNIO = "zernio"
    INVIDIOUS = "invidious"
    SENNIN = "sennin"


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    timeout: float
    weight: int
    description: str
    handler: Optional[Callable] = None


@dataclass
class StreamUrl:
    url: str
    resolution: str
    format: str
    audio_url: str = ""


@dataclass
class StreamResult:
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


def _normalize_stream_urls(
    formats: List[Dict[str, Any]],
    format_type: str = "mp4/mixed",
    audio_url: str = "",
) -> List[StreamUrl]:
    urls = []

    if not isinstance(formats, list):
        return urls

    for item in formats:
        if not isinstance(item, dict):
            continue

        url = item.get("url")
        if not url or not isinstance(url, str):
            continue

        resolution = item.get(
            "quality",
            item.get(
                "qualityLabel",
                item.get(
                    "quality_label",
                    item.get("resolution", "Auto"),
                ),
            ),
        )

        urls.append(
            StreamUrl(
                url=url,
                resolution=str(resolution),
                format=format_type,
                audio_url=audio_url,
            )
        )

    return urls


async def _fetch_with_timeout(
    url: str,
    timeout: float,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, str]] = None,
    use_no_redirect: bool = False,
) -> Optional[Dict[str, Any]]:
    try:
        session = no_redirect_client if use_no_redirect else client_session

        resp = await asyncio.wait_for(
            session.get(
                url,
                headers=headers,
                params=params,
                timeout=timeout,
            ),
            timeout=timeout + 0.5,
        )

        if resp.status_code == 200:
            return resp.json()

        return None

    except asyncio.CancelledError:
        raise
    except (asyncio.TimeoutError, asyncio.TimeoutError):
        return None
    except Exception:
        return None


async def _validate_stream_url(
    url: str,
    timeout: float = 1.0,
) -> bool:
    if not url or not isinstance(url, str):
        return False

    if url.startswith("blob:"):
        return True

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 10; K) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/131.0.0.0 Mobile Safari/537.36"
            ),
            "Accept": "*/*",
            "Range": "bytes=0-1023",
        }

        resp = await asyncio.wait_for(
            no_redirect_client.get(
                url,
                headers=headers,
                timeout=timeout,
            ),
            timeout=timeout + 0.25,
        )

        status = resp.status_code

        if status in (200, 206):
            return True

        if status in (301, 302, 303, 307, 308):
            location = (
                resp.headers.get("location")
                or resp.headers.get("Location")
            )

            if not location:
                return False

            return await _validate_stream_url(
                location,
                timeout=timeout,
            )

        return False

    except asyncio.CancelledError:
        raise
    except Exception:
        return False


async def _validate_stream_candidates(
    stream_urls: List[StreamUrl],
    video_urls: List[str],
    timeout: float = 1.0,
) -> tuple[List[StreamUrl], List[str]]:
    if not stream_urls and not video_urls:
        return [], []

    unique_urls = []
    seen = set()

    for stream in stream_urls:
        if stream.url and stream.url not in seen:
            seen.add(stream.url)
            unique_urls.append(stream.url)

    for url in video_urls:
        if url and url not in seen:
            seen.add(url)
            unique_urls.append(url)

    if not unique_urls:
        return [], []

    validation_tasks = {
        url: asyncio.create_task(
            _validate_stream_url(
                url,
                timeout=timeout,
            )
        )
        for url in unique_urls
    }

    try:
        results = await asyncio.gather(
            *validation_tasks.values(),
            return_exceptions=True,
        )
    except asyncio.CancelledError:
        for task in validation_tasks.values():
            if not task.done():
                task.cancel()
        raise

    valid_urls = {
        url
        for url, result in zip(
            validation_tasks.keys(),
            results,
        )
        if result is True
    }

    if not valid_urls:
        return [], []

    valid_stream_urls = [
        stream
        for stream in stream_urls
        if stream.url in valid_urls
    ]

    valid_video_urls = [
        url
        for url in video_urls
        if url in valid_urls
    ]

    if not valid_video_urls:
        valid_video_urls = [
            stream.url
            for stream in valid_stream_urls
            if stream.url
        ]

    return valid_stream_urls, valid_video_urls


async def _prepare_result(
    result: Optional[StreamResult],
    validate: bool = True,
) -> Optional[StreamResult]:
    if not result:
        return None

    if not result.stream_urls and not result.video_urls:
        return None

    if not validate:
        return result

    valid_stream_urls, valid_video_urls = (
        await _validate_stream_candidates(
            result.stream_urls,
            result.video_urls,
            timeout=1.0,
        )
    )

    if not valid_stream_urls and not valid_video_urls:
        return None

    result.stream_urls = valid_stream_urls
    result.video_urls = valid_video_urls

    if not result.video_urls and result.stream_urls:
        result.video_urls = [
            stream.url
            for stream in result.stream_urls
            if stream.url
        ]

    return result


async def fetch_sia_stream(v: str) -> StreamResult:
    try:
        url = f"https://siatube.com/api/stream/{v}"

        data = await _fetch_with_timeout(
            url,
            timeout=2.5,
        )

        if not data:
            raise Exception("Sia API response empty")

        stream_urls = []
        video_urls = []

        muxed = (
            data.get("muxed")
            or data.get("formats")
            or []
        )

        if isinstance(muxed, list):
            normalized = _normalize_stream_urls(
                muxed,
                "mp4/mixed",
            )
            stream_urls.extend(normalized)
            video_urls.extend(
                [
                    stream.url
                    for stream in normalized
                    if stream.url
                ]
            )

        hls_url = (
            data.get("hls")
            or data.get("m3u8")
            or data.get("manifestUrl")
        )

        if hls_url and hls_url not in video_urls:
            video_urls.append(hls_url)

            stream_urls.append(
                StreamUrl(
                    url=hls_url,
                    resolution="HLS/Live",
                    format="application/x-mpegURL",
                )
            )

        audio_only = data.get("audioOnly", []) or []

        audio_url = ""

        if (
            isinstance(audio_only, list)
            and audio_only
            and isinstance(audio_only[0], dict)
        ):
            audio_url = audio_only[0].get("url", "")

        video_only = data.get("videoOnly", []) or []

        if isinstance(video_only, list):
            stream_urls.extend(
                _normalize_stream_urls(
                    video_only,
                    "webm/videoOnly",
                    audio_url,
                )
            )

        if not video_urls and stream_urls:
            video_urls = [
                stream.url
                for stream in stream_urls
                if stream.url
                and stream.format != "webm/videoOnly"
            ]

        if not stream_urls:
            raise Exception("No streams found")

        return StreamResult(
            stream_urls=stream_urls,
            video_urls=video_urls,
            stream_api_used="sia",
        )

    except asyncio.CancelledError:
        raise
    except Exception as e:
        raise Exception(f"Sia failed: {str(e)}")


async def fetch_piped_stream(v: str) -> StreamResult:
    instances = list(PIPED_INSTANCES)
    random.shuffle(instances)

    last_error = None

    async def fetch_instance(instance: str):
        try:
            url = f"{instance.rstrip('/')}/streams/{v}"

            data = await _fetch_with_timeout(
                url,
                timeout=2.5,
            )

            if not data:
                return None

            stream_urls = []
            video_urls = []

            audio_url = ""

            for item in data.get("audioStreams", []) or []:
                if not isinstance(item, dict):
                    continue

                if item.get("mimeType", "").startswith("audio"):
                    audio_url = item.get("url", "")
                    break

            for item in data.get("videoStreams", []) or []:
                if not isinstance(item, dict):
                    continue

                url_str = item.get("url")

                if not url_str:
                    continue

                quality = item.get(
                    "quality",
                    item.get(
                        "qualityLabel",
                        "Auto",
                    ),
                )

                if item.get("videoOnly", False):
                    stream_urls.append(
                        StreamUrl(
                            url=url_str,
                            resolution=str(quality),
                            format="webm/videoOnly",
                            audio_url=audio_url,
                        )
                    )
                else:
                    stream_urls.append(
                        StreamUrl(
                            url=url_str,
                            resolution=str(quality),
                            format="mp4/mixed",
                        )
                    )

                    video_urls.append(url_str)

            if not video_urls:
                video_urls = [
                    stream.url
                    for stream in stream_urls
                    if stream.url
                    and stream.format != "webm/videoOnly"
                ]

            if not stream_urls:
                return None

            return StreamResult(
                stream_urls=stream_urls,
                video_urls=video_urls,
                title=data.get("title"),
                author=data.get("uploader"),
                author_id=data.get(
                    "uploaderUrl",
                    "",
                ).replace(
                    "/channel/",
                    "",
                ),
                description_html=data.get(
                    "description",
                    "",
                ).replace(
                    "\n",
                    "<br>",
                ),
                view_count=data.get(
                    "views",
                    0,
                ),
                like_count=data.get(
                    "likes",
                    0,
                ),
                stream_api_used="piped",
            )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            return e

    if not instances:
        raise Exception("No Piped instances")

    tasks = [
        asyncio.create_task(
            fetch_instance(instance)
        )
        for instance in instances
    ]

    try:
        pending = set(tasks)

        while pending:
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in done:
                try:
                    result = task.result()

                    if isinstance(result, StreamResult):
                        prepared = await _prepare_result(
                            result,
                            validate=True,
                        )

                        if prepared:
                            for other in pending:
                                other.cancel()

                            if pending:
                                await asyncio.gather(
                                    *pending,
                                    return_exceptions=True,
                                )

                            return prepared

                    elif isinstance(result, Exception):
                        last_error = result

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    last_error = e

    finally:
        for task in tasks:
            if not task.done():
                task.cancel()

        if tasks:
            await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )

    raise Exception(
        f"Piped failed: "
        f"{str(last_error) if last_error else 'All instances failed'}"
    )


async def fetch_rapidapi_stream(v: str) -> StreamResult:
    keys = _get_rapid_api_keys()

    if not keys:
        raise Exception("No RapidAPI keys")

    keys = list(keys)
    random.shuffle(keys)

    last_error = None

    for key in keys:
        try:
            url = f"https://{RAPID_API_HOST}/dl?id={v}"

            headers = {
                "X-RapidAPI-Key": key,
                "X-RapidAPI-Host": RAPID_API_HOST,
            }

            data = await _fetch_with_timeout(
                url,
                headers=headers,
                timeout=2.5,
            )

            if not data:
                continue

            formats = data.get(
                "formats",
                [],
            )

            stream_urls = _normalize_stream_urls(
                formats,
                "mp4/mixed",
            )

            video_urls = [
                stream.url
                for stream in stream_urls
                if stream.url
            ]

            if not stream_urls:
                continue

            return StreamResult(
                stream_urls=stream_urls,
                video_urls=video_urls,
                title=data.get("title"),
                stream_api_used="rapidapi",
            )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            last_error = e
            continue

    raise Exception(
        f"RapidAPI failed: "
        f"{str(last_error) if last_error else 'All keys failed'}"
    )


async def fetch_zernio_stream(v: str) -> StreamResult:
    try:
        target_url = (
            f"https://www.youtube.com/watch?v={v}"
        )

        url = (
            "https://getlate.dev/api/tools/"
            "youtube-live-downloader"
            f"?url={target_url}&formatId=2"
        )

        resp = await asyncio.wait_for(
            no_redirect_client.get(
                url,
                timeout=3.0,
            ),
            timeout=3.5,
        )

        if resp.status_code in (
            301,
            302,
            303,
            307,
            308,
        ):
            location = (
                resp.headers.get("location")
                or resp.headers.get("Location")
            )

            if location:
                return StreamResult(
                    stream_urls=[
                        StreamUrl(
                            url=location,
                            resolution="360p",
                            format="mp4/mixed",
                        )
                    ],
                    video_urls=[location],
                    stream_api_used="zernio",
                )

        if resp.status_code == 200:
            try:
                data = resp.json()

                candidate_urls = []

                if isinstance(data, dict):
                    for key in (
                        "url",
                        "downloadUrl",
                        "download_url",
                        "videoUrl",
                        "video_url",
                    ):
                        value = data.get(key)

                        if (
                            isinstance(value, str)
                            and value
                        ):
                            candidate_urls.append(value)

                    formats = data.get(
                        "formats",
                        [],
                    )

                    if isinstance(formats, list):
                        for item in formats:
                            if not isinstance(item, dict):
                                continue

                            value = item.get("url")

                            if (
                                isinstance(value, str)
                                and value
                            ):
                                candidate_urls.append(
                                    value
                                )

                if candidate_urls:
                    stream_urls = [
                        StreamUrl(
                            url=url,
                            resolution="Auto",
                            format="mp4/mixed",
                        )
                        for url in dict.fromkeys(
                            candidate_urls
                        )
                    ]

                    return StreamResult(
                        stream_urls=stream_urls,
                        video_urls=[
                            stream.url
                            for stream in stream_urls
                        ],
                        stream_api_used="zernio",
                    )

            except Exception:
                pass

        raise Exception("No valid stream received")

    except asyncio.CancelledError:
        raise
    except Exception as e:
        raise Exception(
            f"Zernio failed: {str(e)}"
        )


async def fetch_sennin_stream(v: str) -> StreamResult:
    try:
        url = (
            f"{SENNIN_API_BASE}/api/stream/{v}"
        )

        data = await _fetch_with_timeout(
            url,
            timeout=3.5,
        )

        if not data:
            raise Exception(
                "Sennin API response empty"
            )

        formats = data.get(
            "formats",
            [],
        )

        stream_urls = _normalize_stream_urls(
            formats,
            "mp4/mixed",
        )

        video_urls = [
            stream.url
            for stream in stream_urls
            if stream.url
        ]

        if not stream_urls:
            raise Exception(
                "No streams found"
            )

        return StreamResult(
            stream_urls=stream_urls,
            video_urls=video_urls,
            stream_api_used="sennin",
        )

    except asyncio.CancelledError:
        raise
    except Exception as e:
        raise Exception(
            f"Sennin failed: {str(e)}"
        )


async def fetch_sia_comments(
    v: str,
) -> Dict[str, Any]:
    try:
        url = (
            f"https://siatube.com/api/comments"
            f"?videoId={v}"
        )

        data = await _fetch_with_timeout(
            url,
            timeout=3.5,
        )

        if (
            data
            and isinstance(data, dict)
            and "comments" in data
        ):
            return data

        raise Exception(
            "Invalid response format"
        )

    except asyncio.CancelledError:
        raise
    except Exception as e:
        raise Exception(
            f"Sia comments failed: {str(e)}"
        )


async def fetch_sennin_comments(
    v: str,
    sort: str = "top",
) -> Dict[str, Any]:
    try:
        url = (
            f"{SENNIN_API_BASE}/api/comments"
        )

        params = {
            "videoId": v,
            "sort": sort,
        }

        data = await _fetch_with_timeout(
            url,
            params=params,
            timeout=4.0,
        )

        if (
            data
            and data.get("success") is True
        ):
            return data

        raise Exception(
            "Invalid response"
        )

    except asyncio.CancelledError:
        raise
    except Exception as e:
        raise Exception(
            f"Sennin comments failed: {str(e)}"
        )


async def _fetch_invidious_stream_result(
    v: str,
    force_instance: Optional[str] = None,
) -> Optional[StreamResult]:
    from app.video import (
        extract_invidious_streams,
        fetch_video_info_invidious_robust,
    )

    try:
        v_data = await fetch_video_info_invidious_robust(
            v,
            force_instance=force_instance,
        )

        res_dict = extract_invidious_streams(
            v_data
        )

        stream_urls = [
            StreamUrl(
                url=s["url"],
                resolution=s.get(
                    "resolution",
                    "Auto",
                ),
                format=s.get(
                    "format",
                    "mp4/mixed",
                ),
                audio_url=s.get(
                    "audioUrl",
                    "",
                ),
            )
            for s in res_dict.get(
                "streamUrls",
                [],
            )
            if isinstance(s, dict)
            and s.get("url")
        ]

        video_urls = [
            url
            for url in res_dict.get(
                "videoUrls",
                [],
            )
            if isinstance(url, str)
            and url
        ]

        if not video_urls and stream_urls:
            video_urls = [
                stream.url
                for stream in stream_urls
                if stream.url
                and stream.format != "webm/videoOnly"
            ]

        if not stream_urls and not video_urls:
            return None

        return StreamResult(
            stream_urls=stream_urls,
            video_urls=video_urls,
            stream_api_used="invidious",
            title=res_dict.get("title"),
            author=res_dict.get("author"),
            author_id=res_dict.get(
                "authorId"
            ),
            description_html=res_dict.get(
                "descriptionHtml"
            ),
            view_count=res_dict.get(
                "viewCount",
                0,
            ),
            like_count=res_dict.get(
                "likeCount",
                0,
            ),
        )

    except asyncio.CancelledError:
        raise
    except Exception:
        return None


async def _fetch_stream_with_fallback(
    v: str,
    api: Optional[str] = None,
    force_instance: Optional[str] = None,
) -> Optional[StreamResult]:
    provider_map = {
        "sia": fetch_sia_stream,
        "piped": fetch_piped_stream,
        "rapidapi": fetch_rapidapi_stream,
        "zernio": fetch_zernio_stream,
        "sennin": fetch_sennin_stream,
    }

    if api and api in provider_map:
        try:
            result = await provider_map[api](v)

            if result:
                result = await _prepare_result(
                    result,
                    validate=True,
                )

                if result:
                    return result

        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    result = await _fetch_invidious_stream_result(
        v,
        force_instance=force_instance,
    )

    if result:
        result = await _prepare_result(
            result,
            validate=True,
        )

        if result:
            return result

    return None


async def _race_stream_provider(
    name: str,
    fetch_fn: Callable,
    v: str,
) -> Optional[StreamResult]:
    try:
        result = await fetch_fn(v)

        if not result:
            return None

        return await _prepare_result(
            result,
            validate=True,
        )

    except asyncio.CancelledError:
        raise
    except Exception:
        return None


async def fetch_fastest_stream_urls(
    v: str,
    api: Optional[str] = None,
    force_instance: Optional[str] = None,
    timeout: float = 2.5,
) -> Optional[Dict[str, Any]]:
    cache_key = (
        f"fastest_stream:"
        f"{v}:"
        f"{api or ''}:"
        f"{force_instance or ''}"
    )

    async def _do_fetch() -> Optional[Dict[str, Any]]:
        if api:
            try:
                result = await _fetch_stream_with_fallback(
                    v,
                    api=api,
                    force_instance=force_instance,
                )

                if result:
                    return result.to_dict()

            except asyncio.CancelledError:
                raise
            except Exception:
                pass

        from app.video import (
            extract_invidious_streams,
            fetch_video_info_invidious_robust,
        )

        invidious_task = asyncio.create_task(
            _fetch_invidious_stream_result(
                v,
                force_instance=force_instance,
            )
        )

        rapidapi_task = asyncio.create_task(
            _race_stream_provider(
                "rapidapi",
                fetch_rapidapi_stream,
                v,
            )
        )

        sia_task = asyncio.create_task(
            _race_stream_provider(
                "sia",
                fetch_sia_stream,
                v,
            )
        )

        priority_tasks = {
            "invidious": invidious_task,
            "rapidapi": rapidapi_task,
            "sia": sia_task,
        }

        pending = set(
            priority_tasks.values()
        )

        deadline = (
            asyncio.get_running_loop().time()
            + timeout
            + 1.5
        )

        try:
            while pending:
                remaining = (
                    deadline
                    - asyncio.get_running_loop().time()
                )

                if remaining <= 0:
                    break

                done, pending = await asyncio.wait(
                    pending,
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if not done:
                    break

                for task in done:
                    try:
                        result = task.result()

                        if result:
                            prepared = result

                            if (
                                isinstance(
                                    prepared,
                                    StreamResult,
                                )
                                and prepared.video_urls
                            ):
                                for other in pending:
                                    other.cancel()

                                if pending:
                                    await asyncio.gather(
                                        *pending,
                                        return_exceptions=True,
                                    )

                                return prepared.to_dict()

                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        continue

        finally:
            for task in pending:
                task.cancel()

            if pending:
                await asyncio.gather(
                    *pending,
                    return_exceptions=True,
                )

        zernio_task = asyncio.create_task(
            _race_stream_provider(
                "zernio",
                fetch_zernio_stream,
                v,
            )
        )

        try:
            done, pending = await asyncio.wait(
                {zernio_task},
                timeout=3.5,
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in done:
                try:
                    result = task.result()

                    if result:
                        return result.to_dict()

                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass

            for task in pending:
                task.cancel()

            if pending:
                await asyncio.gather(
                    *pending,
                    return_exceptions=True,
                )

        except asyncio.CancelledError:
            zernio_task.cancel()

            await asyncio.gather(
                zernio_task,
                return_exceptions=True,
            )

            raise

        invidious_retry_task = asyncio.create_task(
            _fetch_invidious_stream_result(
                v,
                force_instance=force_instance,
            )
        )

        try:
            result = await asyncio.wait_for(
                invidious_retry_task,
                timeout=timeout + 1.0,
            )

            if result:
                result = await _prepare_result(
                    result,
                    validate=True,
                )

                if result:
                    return result.to_dict()

        except asyncio.CancelledError:
            raise
        except Exception:
            invidious_retry_task.cancel()

            await asyncio.gather(
                invidious_retry_task,
                return_exceptions=True,
            )

        providers = [
            (
                "sia",
                fetch_sia_stream,
            ),
            (
                "piped",
                fetch_piped_stream,
            ),
            (
                "rapidapi",
                fetch_rapidapi_stream,
            ),
            (
                "zernio",
                fetch_zernio_stream,
            ),
            (
                "sennin",
                fetch_sennin_stream,
            ),
        ]

        tasks = {
            name: asyncio.create_task(
                _race_stream_provider(
                    name,
                    fetch_fn,
                    v,
                )
            )
            for name, fetch_fn in providers
        }

        pending = set(
            tasks.values()
        )

        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending,
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if not done:
                    break

                for task in done:
                    try:
                        result = task.result()

                        if result:
                            for other in pending:
                                other.cancel()

                            if pending:
                                await asyncio.gather(
                                    *pending,
                                    return_exceptions=True,
                                )

                            return result.to_dict()

                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        continue

        finally:
            for task in pending:
                task.cancel()

            if pending:
                await asyncio.gather(
                    *pending,
                    return_exceptions=True,
                )

        return None

    return await fetch_with_inflight(
        cache_key,
        _do_fetch,
        ttl=120.0,
    )


async def fetch_comments(
    v: str,
    force_instance: Optional[str] = None,
    api: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    cache_key = (
        f"comments:"
        f"{v}:"
        f"{force_instance or ''}:"
        f"{api or ''}"
    )

    async def _do_fetch() -> Optional[Dict[str, Any]]:
        if api == "sia":
            try:
                return await fetch_sia_comments(v)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

        elif api == "sennin":
            try:
                return await fetch_sennin_comments(v)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

        elif api == "invidious":
            try:
                return await fetch_invidious(
                    f"/comments/{v}",
                    force_instance=force_instance,
                    list_type="video",
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

        tasks = {
            "invidious": asyncio.create_task(
                fetch_invidious(
                    f"/comments/{v}",
                    force_instance=force_instance,
                    list_type="video",
                )
            ),
            "sia": asyncio.create_task(
                fetch_sia_comments(v)
            ),
            "sennin": asyncio.create_task(
                fetch_sennin_comments(v)
            ),
        }

        pending = set(tasks.values())

        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending,
                    timeout=3.0,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if not done:
                    break

                for task in done:
                    try:
                        result = task.result()

                        if result:
                            for other in pending:
                                other.cancel()

                            if pending:
                                await asyncio.gather(
                                    *pending,
                                    return_exceptions=True,
                                )

                            return result

                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        continue

        finally:
            for task in pending:
                task.cancel()

            if pending:
                await asyncio.gather(
                    *pending,
                    return_exceptions=True,
                )

        return None

    return await fetch_with_inflight(
        cache_key,
        _do_fetch,
        ttl=180.0,
            )
