import asyncio
from datetime import datetime
import json
import httpx
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

router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.add_extension("jinja2.ext.do")


async def fetch_sennin_video_info(v: str):
  try:
    url = f"https://discerning-adventure-production-ebfc.up.railway.app/api/video/{v}"
    resp = await client_session.get(url, timeout=4.0)
    if resp.status_code == 200:
      data = resp.json()
      if data and not data.get("unavailable"):
        norm_data = normalize_sennin_video_info(data)
        norm_data["api_used"] = "sennin"
        return norm_data
  except Exception:
    pass
  raise Exception("Sennin video info failed")


async def fetch_sia_video(v: str):
  try:
    url = f"https://siatube.com/api/video/{v}"
    resp = await client_session.get(url, timeout=3.0)
    if resp.status_code == 200:
      data = resp.json()

      author_info = (
          data.get("author", {}) if isinstance(data.get("author"), dict) else {}
      )
      author_name = author_info.get("name") or data.get("uploader") or ""
      author_id = author_info.get("id", "")
      author_icon = author_info.get("thumbnail", "")
      sub_count = author_info.get("subscribers", "非公開")

      if not author_name:
        raise Exception("Sia author_name is empty")

      desc_obj = data.get("description", {})
      if isinstance(desc_obj, dict):
        desc_text = desc_obj.get("text", "")
      else:
        desc_text = str(desc_obj or "")
      desc_html = desc_text.replace("\n", "<br>")

      rel_data = data.get("Related-videos", {}) or data.get(
          "relatedVideos", {}
      )
      raw_rel = (
          rel_data.get("relatedVideos", [])
          if isinstance(rel_data, dict)
          else (rel_data if isinstance(rel_data, list) else [])
      )

      recommended = []
      for item in raw_rel:
        if not isinstance(item, dict):
          continue
        thumbs = item.get("thumbnails", [])
        thumb_url = (
            thumbs[0].get("url", "")
            if isinstance(thumbs, list) and thumbs
            else ""
        )

        recommended.append({
            "video_id": item.get("videoId") or item.get("id"),
            "title": item.get("title"),
            "author": item.get("channelName") or item.get("author"),
            "view_count_text": item.get("viewCountText"),
            "thumbnail": thumb_url,
        })

      return {
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
  except Exception:
    pass
  raise Exception("Sia video info failed")


def normalize_sennin_video_info(sennin_data: dict) -> dict:
  if not sennin_data or not isinstance(sennin_data, dict):
    return {}

  author_info = (
      sennin_data.get("author", {})
      if isinstance(sennin_data.get("author"), dict)
      else {}
  )
  author_name = author_info.get("name") or ""
  author_id = author_info.get("id") or ""
  author_icon = author_info.get("thumbnail") or ""
  sub_count = author_info.get("subscribers") or "非公開"

  desc_obj = sennin_data.get("description", {})
  if isinstance(desc_obj, dict):
    desc_html = desc_obj.get("formatted") or (
        desc_obj.get("text", "").replace("\n", "<br>")
    )
    desc_text = desc_obj.get("text", "")
  else:
    desc_text = str(desc_obj or "")
    desc_html = desc_text.replace("\n", "<br>")

  rel_data = sennin_data.get("Related-videos", {})
  raw_rel = (
      rel_data.get("relatedVideos", []) if isinstance(rel_data, dict) else []
  )

  recommended = []
  for item in raw_rel:
    if not isinstance(item, dict):
      continue

    thumb_url = item.get("thumbnail") or ""
    if (
        not thumb_url
        and isinstance(item.get("thumbnails"), list)
        and len(item["thumbnails"]) > 0
    ):
      thumb_url = item["thumbnails"][0].get("url", "")

    recommended.append({
        "video_id": item.get("videoId") or item.get("id"),
        "title": item.get("title"),
        "author": item.get("channelName") or item.get("author"),
        "view_count_text": item.get("viewCountText"),
        "thumbnail": thumb_url,
    })

  return {
      "title": sennin_data.get("title", ""),
      "author": author_name,
      "authorId": author_id,
      "authorIcon": author_icon,
      "subCountText": sub_count,
      "viewCount": sennin_data.get("views")
      or sennin_data.get("extended_stats", {}).get("views_original", 0),
      "likeCount": sennin_data.get("likes", 0),
      "description": desc_text,
      "descriptionHtml": desc_html,
      "recommendedVideos": recommended,
      "thumbnail": sennin_data.get("thumbnail", ""),
  }


async def fetch_video_info_invidious_robust(v: str, force_instance: str = None) -> dict:
  res = await fetch_invidious(
      f"/videos/{v}", force_instance=force_instance, list_type="video"
  )
  if isinstance(res, dict) and not res.get("error") and (res.get("title") or res.get("videoId")):
    res["api_used"] = "invidious"
    return res
  
  base_instances = await get_invidious_instances_from_url(INVIDIOUS_VIDEO_LIST_URL)
  for instance in base_instances:
    try:
      url = f"{instance.rstrip('/')}/api/v1/videos/{v}"
      resp = await client_session.get(url, timeout=4.0)
      if resp.status_code == 200:
        data = resp.json()
        if isinstance(data, dict) and not data.get("error") and (data.get("title") or data.get("videoId")):
          data["api_used"] = "invidious"
          return data
    except Exception:
      continue

  raise Exception("Robust Invidious video info fetch failed")


async def fetch_video_info(v: str, force_instance: str = None, api: str = None):
  cache_key = f"video_info:{v}:{force_instance or ''}:{api or ''}"

  async def _do_fetch():
    if api == "invidious":
      return await fetch_video_info_invidious_robust(v, force_instance=force_instance)
    elif api == "sia":
      try:
        return await fetch_sia_video(v)
      except Exception:
        return await fetch_video_info_invidious_robust(v, force_instance=force_instance)
    elif api == "sennin":
      try:
        return await fetch_sennin_video_info(v)
      except Exception:
        return await fetch_video_info_invidious_robust(v, force_instance=force_instance)

    try:
      return await fetch_video_info_invidious_robust(v, force_instance=force_instance)
    except Exception:
      pass
    try:
      return await fetch_sia_video(v)
    except Exception:
      pass
    try:
      return await fetch_sennin_video_info(v)
    except Exception:
      pass

    raise Exception("All video info endpoints failed")

  return await fetch_with_inflight(cache_key, _do_fetch, ttl=180.0)


def extract_invidious_streams(v_data: dict):
  if not v_data:
    return {"streamUrls": [], "videoUrls": []}

  adaptive = v_data.get("adaptiveFormats", [])
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

  format_streams = v_data.get("formatStreams", [])
  stream_urls = [
      {
          "url": fmt.get("url"),
          "resolution": fmt.get("qualityLabel"),
          "format": "mp4/mixed",
          "audioUrl": "",
      }
      for fmt in format_streams
  ]

  stream_urls.extend({
      "url": fmt.get("url"),
      "resolution": fmt.get("qualityLabel"),
      "format": "webm/videoOnly",
      "audioUrl": audio_url,
  } for fmt in adaptive if "video" in fmt.get("type", "") and "webm" in fmt.get("container", ""))

  video_urls = [fmt.get("url") for fmt in format_streams] or [
      fmt.get("url") for fmt in adaptive if "video" in fmt.get("type", "")
  ]

  return {"streamUrls": stream_urls, "videoUrls": video_urls}


def process_comments(comment_data):
  if isinstance(comment_data, Exception) or not comment_data:
    return []

  if (
      isinstance(comment_data, dict)
      and comment_data.get("success") is True
      and isinstance(comment_data.get("comments"), list)
  ):
    return normalize_sennin_comments(comment_data)

  comments = (
      comment_data.get("comments", [])
      if isinstance(comment_data, dict)
      else (comment_data if isinstance(comment_data, list) else [])
  )
  processed = []

  for c in comments:
    if not isinstance(c, dict):
      continue
    item = dict(c)

    author_obj = item.get("author")
    if isinstance(author_obj, dict):
      item["author"] = author_obj.get("name", "")
      item["authorIcon"] = (
          author_obj.get("avatar")
          or author_obj.get("authorIcon")
          or item.get("avatar", "")
      )
      item["authorThumbnail"] = item["authorIcon"]
      item["authorId"] = author_obj.get("channelId", "")
    else:
      author_thumbs = item.get("authorThumbnails", [])
      if author_thumbs and isinstance(author_thumbs, list):
        item["authorIcon"] = author_thumbs[-1].get("url", "")
        item["authorThumbnail"] = item["authorIcon"]
      else:
        item["authorIcon"] = item.get("authorIcon") or item.get("avatar", "")
        item["authorThumbnail"] = item["authorIcon"]

    if "avatar" in item and item["avatar"]:
      item["authorIcon"] = item["avatar"]
      item["authorThumbnail"] = item["avatar"]
    elif isinstance(author_obj, dict) and author_obj.get("avatar"):
      item["authorIcon"] = author_obj.get("avatar")
      item["avatar"] = author_obj.get("avatar")
      item["authorThumbnail"] = author_obj.get("avatar")
    elif "authorIcon" in item and item["authorIcon"]:
      item["avatar"] = item["authorIcon"]
      item["authorThumbnail"] = item["authorIcon"]

    if "authorIcon" in item and item["authorIcon"]:
      item["authorIconUrl"] = item["authorIcon"]
      item["avatar"] = item["authorIcon"]
      item["authorThumbnail"] = item["authorIcon"]

    if not item.get("authorThumbnails") or not isinstance(
        item.get("authorThumbnails"), list
    ):
      icon_url = (
          item.get("authorIcon")
          or item.get("authorThumbnail")
          or item.get("avatar")
          or ""
      )
      if icon_url:
        item["authorThumbnails"] = [{"url": icon_url}]
      else:
        item["authorThumbnails"] = []

    if "text" in item and "contentHtml" not in item and "content" not in item:
      text_str = item.get("text", "")
      item["content"] = text_str
      item["contentHtml"] = text_str.replace("\n", "<br>")
    elif "contentHtml" in item and "content" not in item:
      item["content"] = item.get("contentHtml", "")
    elif "content" in item and "contentHtml" not in item:
      item["contentHtml"] = item.get("content", "").replace("\n", "<br>")

    if not item.get("contentHtml"):
      text_str = item.get("text") or item.get("content") or ""
      item["contentHtml"] = text_str.replace("\n", "<br>")

    if "publishedTime" in item and "publishedText" not in item:
      item["publishedText"] = item.get("publishedTime", "")
    elif "publishedText" not in item:
      item["publishedText"] = item.get("published", "") or item.get(
          "publishedTime", ""
      )

    likes_obj = item.get("likes")
    if isinstance(likes_obj, dict):
      item["likeCount"] = likes_obj.get("count", 0)

    processed.append(item)

  return processed


def normalize_sennin_comments(sennin_data: dict) -> list:
  if not sennin_data or not isinstance(sennin_data, dict):
    return []

  comments = sennin_data.get("comments", [])
  if not isinstance(comments, list):
    return []

  processed = []
  for c in comments:
    if not isinstance(c, dict):
      continue

    author_info = (
        c.get("author", {}) if isinstance(c.get("author"), dict) else {}
    )
    likes_info = c.get("likes", {}) if isinstance(c.get("likes"), dict) else {}
    replies_info = (
        c.get("replies", {}) if isinstance(c.get("replies"), dict) else {}
    )

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
            likes_info.get("count")
            if isinstance(likes_info, dict)
            else c.get("likes", 0)
        ),
        "replyCount": (
            replies_info.get("count")
            if isinstance(replies_info, dict)
            else c.get("replies", 0)
        ),
        "isCreator": author_info.get("creator", False),
        "isVerified": author_info.get("verified", False),
    })

  return processed


@router.get("/shorts/{v}", response_class=HTMLResponse)
async def shorts_player(
    request: Request,
    v: str,
    force_instance: str = Query(None),
    api: str = Query(None),
):
  try:
    from app.stream import fetch_fastest_stream_urls, fetch_comments
    
    video_info_task = fetch_video_info(
        v, force_instance=force_instance, api=api
    )
    stream_task = fetch_fastest_stream_urls(
        v, api=api, force_instance=force_instance
    )
    comment_task = fetch_comments(v, force_instance=force_instance, api=api)

    video_data, stream_data, comment_data = await asyncio.gather(
        video_info_task, stream_task, comment_task, return_exceptions=True
    )

    if isinstance(video_data, Exception) and (
        not stream_data or isinstance(stream_data, Exception)
    ):
      raise video_data if isinstance(
          video_data, Exception
      ) else Exception("Failed to load video")

    v_data = video_data if isinstance(video_data, dict) else {}
    s_data = stream_data if isinstance(stream_data, dict) else {}

    if (
        s_data
        and s_data.get("videoUrls")
    ):
      video_urls = s_data.get("videoUrls", [])
    else:
      invidious_streams = extract_invidious_streams(v_data)
      video_urls = invidious_streams.get("videoUrls", [])

    v_title = v_data.get("title", "")
    v_author = v_data.get("author", "")
    v_views = v_data.get("viewCount", 0)
    v_likes = v_data.get("likeCount", 0)
    v_desc = v_data.get("descriptionHtml") or v_data.get(
        "description", ""
    ).replace("\n", "<br>")

    formatted_comments = process_comments(comment_data)

    info_api_used = v_data.get("api_used") or ("invidious" if v_data else "unknown")
    stream_api_used = s_data.get("stream_api_used") or ("invidious" if video_urls else "unknown")

    return templates.TemplateResponse(
        "short.html",
        {
            "request": request,
            "videoid": v,
            "video_title": v_title,
            "videourls": video_urls,
            "author": v_author,
            "view_count": v_views,
            "like_count": v_likes,
            "description": v_desc,
            "comments": formatted_comments,
            "info_api_used": info_api_used,
            "stream_api_used": stream_api_used,
            "api_used": info_api_used,
        },
    )
  except httpx.TimeoutException:
    return templates.TemplateResponse("apitimeout.html", {"request": request})
  except Exception:
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
    force_instance: str = Query(None),
    api: str = Query(None),
):
  try:
    from app.stream import fetch_fastest_stream_urls, fetch_comments
    
    info_task = fetch_video_info(v, force_instance=force_instance, api=api)
    stream_task = fetch_fastest_stream_urls(
        v, api=api, force_instance=force_instance
    )
    comment_task = fetch_comments(v, force_instance=force_instance, api=api)

    video_data, stream_res, comment_data = await asyncio.gather(
        info_task, stream_task, comment_task, return_exceptions=True
    )

    if isinstance(video_data, Exception) and isinstance(stream_res, Exception):
      raise video_data

    v_data = video_data if isinstance(video_data, dict) else {}
    s_data = stream_res if isinstance(stream_res, dict) else {}

    stream_urls = s_data.get("streamUrls", [])
    video_urls = s_data.get("videoUrls", [])

    if not stream_urls and v_data:
      invidious_streams = extract_invidious_streams(v_data)
      stream_urls = invidious_streams.get("streamUrls", [])
      video_urls = invidious_streams.get("videoUrls", [])
      if not s_data.get("stream_api_used"):
        s_data["stream_api_used"] = "invidious"

    recommended = []
    raw_recs = v_data.get("recommendedVideos", [])
    for rec in raw_recs:
      if not isinstance(rec, dict):
        continue
      recommended.append({
          "video_id": rec.get("video_id") or rec.get("videoId"),
          "title": rec.get("title"),
          "author": rec.get("author"),
          "view_count_text": rec.get("view_count_text")
          or rec.get("viewCountText"),
          "thumbnail": rec.get("thumbnail", ""),
      })

    author_icon = v_data.get("authorIcon")
    if not author_icon:
      author_thumbs = v_data.get("authorThumbnails", [])
      author_icon = author_thumbs[-1]["url"] if author_thumbs else ""

    youtube_url = f"https://www.youtube.com/watch?v={v}"
    v_title = v_data.get("title") or s_data.get("title") or ""
    v_author = v_data.get("author") or s_data.get("author") or ""
    v_sub_count = v_data.get("subCountText") or "非公開"
    v_desc = (
        v_data.get("descriptionHtml")
        or s_data.get("descriptionHtml")
        or v_data.get("description", "").replace("\n", "<br>")
    )

    formatted_comments = process_comments(comment_data)

    info_api_used = v_data.get("api_used") or ("invidious" if v_data else "unknown")
    stream_api_used = s_data.get("stream_api_used") or ("invidious" if stream_urls else "unknown")

    response = templates.TemplateResponse(
        "watch.html",
        {
            "request": request,
            "videoid": v,
            "video_title": v_title,
            "videourls": video_urls,
            "streamUrls": stream_urls,
            "author": v_author,
            "author_id": v_data.get("authorId") or s_data.get("authorId") or "",
            "author_icon": author_icon,
            "subscribers_count": v_sub_count,
            "view_count": v_data.get("viewCount", s_data.get("viewCount", 0)),
            "like_count": v_data.get("likeCount", s_data.get("likeCount", 0)),
            "description": v_desc,
            "recommended_videos": recommended,
            "comments": formatted_comments,
            "youtube_url": youtube_url,
            "info_api_used": info_api_used,
            "stream_api_used": stream_api_used,
            "api_used": info_api_used,
        },
    )

    try:
      history_json = request.cookies.get("history", "[]")
      history = json.loads(history_json)
      history = [item for item in history if item.get("videoId") != v]
      history.append({
          "videoId": v,
          "title": v_title,
          "author": v_author,
          "added_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
      })
      if len(history) > 50:
        history = history[-50:]
      response.set_cookie(
          key="history",
          value=json.dumps(history),
          max_age=2592000,
          httponly=True,
      )
    except:
      pass

    return response

  except httpx.TimeoutException:
    return templates.TemplateResponse("apitimeout.html", {"request": request})
  except Exception:
    fallback_instances = await get_invidious_instances_from_url(
        INVIDIOUS_VIDEO_LIST_URL
    )
    return templates.TemplateResponse(
        "apiallerror.html",
        {"request": request, "instances": fallback_instances},
    )
