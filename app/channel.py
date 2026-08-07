import asyncio
import httpx
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.search import (
    fetch_invidious,
    fetch_with_inflight,
    client_session,
    get_invidious_instances_from_url,
    INVIDIOUS_SEARCH_LIST_URL,
    INVIDIOUS_VIDEO_LIST_URL,
)

router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.add_extension("jinja2.ext.do")


def format_sub_count(sub_count_val) -> str:
    """登録者数のデータを文字列表示用に整形するヘルパー関数"""
    if sub_count_val is None or sub_count_val == "":
        return "非公開"
    
    # 既に "10万人" や "1.2M" などの文字列形式で返ってきた場合
    if isinstance(sub_count_val, str):
        val_str = sub_count_val.strip()
        if val_str:
            return val_str
        return "非公開"

    # 数値（int / float）で返ってきた場合のフォーマット
    if isinstance(sub_count_val, (int, float)):
        if sub_count_val <= 0:
            return "非公開"
        if sub_count_val >= 10_000_000:
            return f"{sub_count_val / 10_000_000:.1f}千万人".replace(".0", "")
        elif sub_count_val >= 10_000:
            return f"{sub_count_val / 10_000:.1f}万人".replace(".0", "")
        return f"{sub_count_val:,}人"

    return "非公開"


async def fetch_sia_channel(ucid: str):
    try:
        url = f"https://siatube.com/api/channel/{ucid}"
        resp = await client_session.get(url, timeout=3.0)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict) and (
                "author" in data
                or "title" in data
                or "videos" in data
                or "name" in data
            ):
                return data
    except Exception:
        pass
    raise Exception("Sia channel failed")


async def fetch_invidious_channel(ucid: str, force_instance: str = None, list_type: str = "search"):
    return await fetch_invidious(
        f"/channels/{ucid}",
        force_instance=force_instance,
        list_type=list_type,
    )


async def fetch_invidious_channel_videos(ucid: str, sort_by: str, force_instance: str = None, list_type: str = "search"):
    return await fetch_invidious(
        f"/channels/{ucid}/videos",
        {"sort_by": sort_by},
        force_instance=force_instance,
        list_type=list_type,
    )


async def fetch_invidious_channel_shorts(ucid: str, force_instance: str = None, list_type: str = "search"):
    return await fetch_invidious(
        f"/channels/{ucid}/shorts",
        force_instance=force_instance,
        list_type=list_type,
    )


async def fetch_invidious_channel_playlists(ucid: str, force_instance: str = None, list_type: str = "search"):
    return await fetch_invidious(
        f"/channels/{ucid}/playlists",
        force_instance=force_instance,
        list_type=list_type,
    )


async def fetch_invidious_channel_community(ucid: str, force_instance: str = None, list_type: str = "search"):
    return await fetch_invidious(
        f"/channels/{ucid}/community",
        force_instance=force_instance,
        list_type=list_type,
    )


@router.get("/channel/{ucid}", response_class=HTMLResponse)
async def channel(
    request: Request,
    ucid: str,
    sort_by: str = "newest",
    tab: str = "videos",
    force_instance: str = Query(None),
    api: str = Query(None),
):
    try:
        cache_key = (
            f"channel_data_all:{ucid}:{sort_by}:{force_instance or ''}:{api or ''}"
        )

        async def _do_fetch_channel():
            sia_attempt = None
            if api == "sia" or not api:
                try:
                    sia_res = await fetch_sia_channel(ucid)
                    if sia_res and isinstance(sia_res, dict):
                        sia_attempt = {
                            "channel": sia_res,
                            "videos": sia_res.get("videos", []),
                            "shorts": sia_res.get("shorts", []),
                            "playlists": sia_res.get("playlists", []),
                            "community": sia_res.get("community", []),
                        }
                        if api == "sia":
                            return sia_attempt
                except Exception:
                    pass

            tasks = [
                fetch_invidious_channel(ucid, force_instance=force_instance),
                fetch_invidious_channel_videos(ucid, sort_by, force_instance=force_instance),
                fetch_invidious_channel_shorts(ucid, force_instance=force_instance),
                fetch_invidious_channel_playlists(ucid, force_instance=force_instance),
                fetch_invidious_channel_community(ucid, force_instance=force_instance),
            ]

            results = await asyncio.gather(*tasks, return_exceptions=True)
            invidious_result = {
                "channel": (
                    results[0] if not isinstance(results[0], Exception) else {}
                ),
                "videos": results[1] if not isinstance(results[1], Exception) else {},
                "shorts": results[2] if not isinstance(results[2], Exception) else {},
                "playlists": (
                    results[3] if not isinstance(results[3], Exception) else {}
                ),
                "community": (
                    results[4] if not isinstance(results[4], Exception) else {}
                ),
            }

            if sia_attempt and not invidious_result.get("channel"):
                return sia_attempt

            return invidious_result

        fetched_res = await fetch_with_inflight(
            cache_key, _do_fetch_channel, ttl=180.0
        )

        channel_data = fetched_res.get("channel", {})
        videos_data = fetched_res.get("videos", {})
        shorts_data = fetched_res.get("shorts", {})
        playlists_data = fetched_res.get("playlists", {})
        community_data = fetched_res.get("community", {})

        if isinstance(videos_data, list):
            final_videos = videos_data
        elif isinstance(videos_data, dict):
            final_videos = videos_data.get("videos", [])
        else:
            final_videos = []

        if isinstance(shorts_data, list):
            final_shorts = shorts_data
        elif isinstance(shorts_data, dict):
            final_shorts = shorts_data.get("videos", [])
        else:
            final_shorts = []

        playlists = []
        raw_playlists = (
            playlists_data.get("playlists", [])
            if isinstance(playlists_data, dict)
            else (playlists_data if isinstance(playlists_data, list) else [])
        )
        for pl in raw_playlists:
            if not isinstance(pl, dict):
                continue
            thumb = pl.get("playlistThumbnail", "") or pl.get("thumbnail", "")
            if thumb and not thumb.startswith("http"):
                thumb = f"https://img.youtube.com/vi/{thumb}/mqdefault.jpg"
            playlists.append({
                "id": pl.get("playlistId", "") or pl.get("id", ""),
                "title": pl.get("title", ""),
                "video_count": pl.get("videoCount", 0),
                "thumbnail": thumb,
            })

        # チャンネル名の取得
        author_name = (
            channel_data.get("author")
            or channel_data.get("name")
            or channel_data.get("title")
            or ""
        )

        # アバター・アイコン画像の取得
        author_icon = ""
        if channel_data.get("authorThumbnails"):
            author_icon = channel_data.get("authorThumbnails")[-1]["url"]
        elif channel_data.get("authorIcon"):
            author_icon = channel_data.get("authorIcon")
        elif channel_data.get("avatar"):
            author_icon = channel_data.get("avatar")
        elif channel_data.get("authorAvatar"):
            author_icon = channel_data.get("authorAvatar")

        # 登録者数（subCount / subscriberCount 等）のマルチフィールド対応とフォーマット化
        raw_sub_count = (
            channel_data.get("subCountText")
            or channel_data.get("subCount")
            or channel_data.get("subscribers")
            or channel_data.get("subscriberCount")
            or channel_data.get("subscribersCount")
        )
        sub_count = format_sub_count(raw_sub_count)

        # コミュニティ投稿の取得
        comments_list = (
            community_data.get("comments", [])
            if isinstance(community_data, dict)
            else (community_data if isinstance(community_data, list) else [])
        )
        community = []
        for post in comments_list:
            if not isinstance(post, dict):
                continue
            community.append({
                "id": post.get("commentId", "") or post.get("id", ""),
                "content": (
                    post.get("contentHtml")
                    or post.get("text")
                    or post.get("content")
                    or ""
                ).replace("\n", "<br>"),
                "published_text": post.get("publishedText")
                or post.get("publishedTime")
                or "",
                "likes": post.get("likeCount")
                or (
                    post.get("likes", {}).get("count")
                    if isinstance(post.get("likes"), dict)
                    else 0
                ),
                "author": author_name,
                "author_icon": author_icon,
            })

        return templates.TemplateResponse(
            "channel.html",
            {
                "request": request,
                "ucid": ucid,
                "author": author_name,
                "author_icon": author_icon,
                "sub_count": sub_count,
                "description": channel_data.get("descriptionHtml")
                or channel_data.get("description", ""),
                "videos": final_videos,
                "shorts": final_shorts,
                "playlists": playlists,
                "community": community,
                "sort_by": sort_by,
                "tab": tab,
            },
        )
    except httpx.TimeoutException:
        return templates.TemplateResponse("apitimeout.html", {"request": request})
    except Exception:
        fallback_instances = await get_invidious_instances_from_url(
            INVIDIOUS_SEARCH_LIST_URL
        )
        return templates.TemplateResponse(
            "apiallerror.html",
            {"request": request, "instances": fallback_instances},
        )
