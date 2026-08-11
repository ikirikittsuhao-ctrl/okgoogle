import asyncio
import json
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, StreamingResponse

from core import INNERTUBE_BASE, get_client

router = APIRouter()

MIRROR_NODES = [
    "https://xeroxyt-nt-apiv1-0ydt.onrender.com",
    "https://xeroxyt-nt-apiv1-5vsz.onrender.com",
    "https://xeroxyt-nt-apiv1-m28t.onrender.com",
]


def convert_timestamp_to_seconds(text: str) -> int:
    if not text:
        return 0
    try:
        parts = [int(p) for p in text.strip().split(":")]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
    except (ValueError, TypeError):
        pass
    return 0


def extract_view_number(raw_text: str) -> int:
    if not raw_text:
        return 0
    text = str(raw_text).lower().replace(",", "").strip()
    try:
        if "k" in text:
            num = float(re.sub(r"[^\d.]", "", text.split("k")[0]))
            return int(num * 1_000)
        if "m" in text:
            num = float(re.sub(r"[^\d.]", "", text.split("m")[0]))
            return int(num * 1_000_000)
        if "b" in text:
            num = float(re.sub(r"[^\d.]", "", text.split("b")[0]))
            return int(num * 1_000_000_000)
        
        if "万" in text:
            num = float(re.sub(r"[^\d.]", "", text.split("万")[0]))
            return int(num * 10_000)
        if "億" in text:
            num = float(re.sub(r"[^\d.]", "", text.split("億")[0]))
            return int(num * 100_000_000)

        digits = "".join(c for c in text if c.isdigit())
        return int(digits) if digits else 0
    except (ValueError, TypeError):
        return 0


def format_short_payload(
    video_id: str,
    title: str,
    author: str = "",
    author_id: str = "",
    length_seconds: int = 30,
    view_count: int = 0,
    view_count_text: str = "",
    thumbnail_url: Optional[str] = None,
    published_text: str = "",
) -> Dict[str, Any]:
    if not thumbnail_url:
        thumbnail_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

    return {
        "videoId": video_id,
        "title": title,
        "lengthSeconds": length_seconds,
        "isShort": True,
        "author": author,
        "authorId": author_id,
        "authorThumbnails": [],
        "viewCount": view_count,
        "viewCountText": view_count_text or (f"{view_count:,} views" if view_count else ""),
        "videoThumbnails": [
            {"url": thumbnail_url, "quality": "high"}
        ],
        "publishedText": published_text,
    }


def process_innertube_response(data: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    shorts = []
    cont_key = data.get("_contKey")
    results = data.get("results") or data.get("items") or []

    for item in results:
        if not isinstance(item, dict):
            continue

        item_type = item.get("type", "")
        is_reel = (item_type == "Reel")

        dur_secs = 0
        dur = item.get("duration", {})
        if isinstance(dur, dict):
            dur_secs = dur.get("seconds", 0) or 0
        elif isinstance(dur, (int, float)):
            dur_secs = int(dur)

        is_short_video = (item_type == "Video" and 0 < dur_secs <= 90)

        if not (is_reel or is_short_video):
            continue

        video_id = item.get("id") or item.get("videoId") or ""
        if not video_id:
            continue

        title_raw = item.get("title", "")
        if isinstance(title_raw, dict):
            runs = title_raw.get("runs", [{}])
            title = title_raw.get("text", "") or (runs[0].get("text", "") if runs else "")
        else:
            title = str(title_raw)

        author_raw = item.get("author", {})
        author, author_id = "", ""
        if isinstance(author_raw, dict):
            author = author_raw.get("name", "") or str(author_raw.get("text", ""))
            ep = author_raw.get("endpoint", {}) or {}
            author_id = author_raw.get("id", "") or ep.get("payload", {}).get("browseId", "")
        elif author_raw:
            author = str(author_raw)

        vc_raw = item.get("view_count") or item.get("short_view_count") or {}
        vc_text = vc_raw.get("text", "0") if isinstance(vc_raw, dict) else str(vc_raw)
        view_count = extract_view_number(vc_text)

        thumbs = item.get("thumbnails", []) or []
        thumb_url = thumbs[0].get("url") if thumbs and isinstance(thumbs[0], dict) else None

        shorts.append(
            format_short_payload(
                video_id=video_id,
                title=title,
                author=author,
                author_id=author_id,
                length_seconds=dur_secs if is_short_video else 30,
                view_count=view_count,
                view_count_text=vc_text,
                thumbnail_url=thumb_url,
            )
        )

    return shorts, cont_key


@router.get("/v1/shorts/query")
async def fetch_shorts_by_query(q: str = Query(...)):
    try:
        client = await get_client()
        search_query = q if "#shorts" in q.lower() else f"{q} #shorts"

        resp = await client.get(
            f"{INNERTUBE_BASE}/search",
            params={"q": search_query, "type": "all"},
            timeout=httpx.Timeout(12.0),
        )
        resp.raise_for_status()
        shorts, cont_key = process_innertube_response(resp.json())
        return JSONResponse({"items": shorts, "contKey": cont_key})
    except Exception as e:
        return JSONResponse({"error": str(e), "items": []}, status_code=502)


@router.get("/v1/shorts/query/next")
async def fetch_shorts_next_page(contKey: str = Query(...)):
    try:
        client = await get_client()
        resp = await client.get(
            f"{INNERTUBE_BASE}/search/continue",
            params={"key": contKey},
            timeout=httpx.Timeout(12.0),
        )
        resp.raise_for_status()
        shorts, cont_key = process_innertube_response(resp.json())
        return JSONResponse({"items": shorts, "contKey": cont_key})
    except Exception as e:
        return JSONResponse({"error": str(e), "items": []}, status_code=502)


def check_short_eligibility(item: Dict[str, Any]) -> bool:
    """Shortsの対象かどうかを判定する（より寛容な判定）"""
    if not isinstance(item, dict):
        return False

    # ShortsLockupView は確実にShorts
    if item.get("type") == "ShortsLockupView":
        return True

    # on_tap_endpoint で videoId があれば Shorts の可能性が高い
    on_tap = item.get("on_tap_endpoint") or {}
    if isinstance(on_tap, dict):
        payload = on_tap.get("payload") or {}
        if isinstance(payload, dict) and payload.get("videoId"):
            return True

    # reelWatchEndpoint は Shorts
    ep = item.get("endpoint") or {}
    if isinstance(ep, dict) and ep.get("name") == "reelWatchEndpoint":
        return True

    # thumbnail_overlays で SHORTS タグがあれば Shorts
    for ov in (item.get("thumbnail_overlays") or []):
        if isinstance(ov, dict) and ov.get("style") == "SHORTS":
            return True

    # title に #shorts があれば Shorts
    title_field = item.get("title") or {}
    title_text = (title_field.get("text", "") if isinstance(title_field, dict) else str(title_field)).lower()
    if "#shorts" in title_text:
        return True

    # duration が 90秒以下なら Shorts の可能性
    dur = item.get("duration")
    if dur:
        dur_text = dur.get("text") or dur.get("simpleText", "") if isinstance(dur, dict) else str(dur)
        secs = convert_timestamp_to_seconds(dur_text)
        if 0 < secs <= 90:
            return True

    # video_id が存在して、他の条件がなくても一応対象（オリジナルのままにしつつ寛容に）
    video_id = item.get("id") or item.get("videoId") or item.get("video_id")
    if video_id:
        # ただしタイプが明らかに異なる場合は除外
        item_type = item.get("type", "").lower()
        if "channel" not in item_type and "playlist" not in item_type:
            return True

    return False


def transform_mirror_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """ミラーノードのレスポンスアイテムを標準フォーマットに変換"""
    if not isinstance(item, dict):
        return None

    item_type = item.get("type", "")

    # === ShortsLockupView の処理 ===
    if item_type == "ShortsLockupView":
        on_tap = item.get("on_tap_endpoint") or {}
        on_tap_payload = (on_tap.get("payload") or {}) if isinstance(on_tap, dict) else {}
        shorts_video_id = on_tap_payload.get("videoId") if isinstance(on_tap_payload, dict) else None

        if not shorts_video_id:
            return None

        overlay = item.get("overlay_metadata") or {}
        title = ""
        if isinstance(overlay, dict):
            primary = overlay.get("primary_text") or {}
            title = primary.get("text", "") if isinstance(primary, dict) else ""

        if not title:
            acc = item.get("accessibility_text") or ""
            title = acc.split(",")[0] if acc else shorts_video_id

        sec_text = overlay.get("secondary_text") or {} if isinstance(overlay, dict) else {}
        raw_views = sec_text.get("text", "") if isinstance(sec_text, dict) else ""
        view_count = extract_view_number(raw_views)

        # サムネイル取得
        thumb_url = None
        thumbnail = item.get("thumbnail") or {}
        if isinstance(thumbnail, dict):
            sources = thumbnail.get("sources") or []
            if sources and isinstance(sources, list):
                thumb_url = sources[0].get("url") if isinstance(sources[0], dict) else None

        if not thumb_url:
            thumb_url = f"https://i.ytimg.com/vi/{shorts_video_id}/mqdefault.jpg"

        return format_short_payload(
            video_id=shorts_video_id,
            title=title,
            length_seconds=60,
            view_count=view_count,
            view_count_text=raw_views,
            thumbnail_url=thumb_url,
        )

    # === on_tap_endpoint で videoId がある場合 ===
    on_tap = item.get("on_tap_endpoint") or {}
    on_tap_payload = (on_tap.get("payload") or {}) if isinstance(on_tap, dict) else {}
    shorts_video_id = on_tap_payload.get("videoId") if isinstance(on_tap_payload, dict) else None

    if shorts_video_id:
        overlay = item.get("overlay_metadata") or {}
        title = ""
        if isinstance(overlay, dict):
            primary = overlay.get("primary_text") or {}
            title = primary.get("text", "") if isinstance(primary, dict) else ""

        if not title:
            acc = item.get("accessibility_text") or ""
            title = acc.split(",")[0] if acc else shorts_video_id

        sec_text = overlay.get("secondary_text") or {} if isinstance(overlay, dict) else {}
        raw_views = sec_text.get("text", "") if isinstance(sec_text, dict) else ""
        view_count = extract_view_number(raw_views)

        # サムネイル取得
        thumb_url = None
        thumbnail = item.get("thumbnail") or {}
        if isinstance(thumbnail, dict):
            sources = thumbnail.get("sources") or []
            if sources and isinstance(sources, list):
                thumb_url = sources[0].get("url") if isinstance(sources[0], dict) else None

        if not thumb_url:
            thumb_url = f"https://i.ytimg.com/vi/{shorts_video_id}/mqdefault.jpg"

        return format_short_payload(
            video_id=shorts_video_id,
            title=title,
            length_seconds=60,
            view_count=view_count,
            view_count_text=raw_views,
            thumbnail_url=thumb_url,
        )

    # === 通常の動画アイテムの処理 ===
    video_id = item.get("id") or item.get("videoId") or item.get("video_id")
    if not video_id:
        return None

    title_field = item.get("title") or {}
    title = title_field.get("text") or title_field.get("simpleText") or str(title_field) if title_field else ""
    if not title:
        title = video_id

    author_field = item.get("author") or item.get("channel") or {}
    author_name, author_id = "", ""
    if isinstance(author_field, dict):
        author_name = author_field.get("name", "")
        author_id = author_field.get("id", "")

    vc_field = item.get("view_count") or item.get("short_view_count") or {}
    vc_text = vc_field.get("text", "") if isinstance(vc_field, dict) else str(vc_field) if vc_field else ""
    view_count = extract_view_number(vc_text)

    pub_field = item.get("published") or {}
    pub_text = pub_field.get("text", "") if isinstance(pub_field, dict) else str(pub_field) if pub_field else ""

    # サムネイル取得
    thumb_url = None
    thumbnails = item.get("thumbnails") or []
    if thumbnails and isinstance(thumbnails, list):
        first_thumb = thumbnails[0] if isinstance(thumbnails[0], dict) else {}
        thumb_url = first_thumb.get("url")

    if not thumb_url:
        thumb_url = f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"

    # 動画の長さを取得
    dur = item.get("duration") or {}
    dur_secs = 30
    if isinstance(dur, dict):
        dur_text = dur.get("text") or dur.get("simpleText", "")
        dur_secs = convert_timestamp_to_seconds(dur_text) or 30
    elif isinstance(dur, (int, float)):
        dur_secs = int(dur)
    elif dur:
        dur_secs = convert_timestamp_to_seconds(str(dur)) or 30

    return format_short_payload(
        video_id=video_id,
        title=title,
        author=author_name,
        author_id=author_id,
        length_seconds=dur_secs,
        view_count=view_count,
        view_count_text=vc_text,
        thumbnail_url=thumb_url,
        published_text=pub_text,
    )


@router.get("/v1/shorts/stream")
async def stream_shorts_feed(q: str = Query(...)):
    """Shortsストリーミング検索エンドポイント - 複数のミラーノードから並行取得"""
    query_variants = [q, f"{q} ショート", f"{q} #shorts"]

    async def execute_node_request(client: httpx.AsyncClient, base: str, search_q: str, page: int) -> List[Dict[str, Any]]:
        """単一のミラーノードへのリクエスト実行"""
        try:
            resp = await client.get(
                f"{base}/api/search",
                params={"q": search_q, "page": page},
                timeout=httpx.Timeout(15.0),
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                return []

            candidates = list(data.get("shorts") or [])
            
            # videos から Shorts 対象のものをフィルタリング
            for v in (data.get("videos") or []):
                if check_short_eligibility(v):
                    candidates.append(v)
            
            return candidates
        except Exception:
            return []

    async def generate_event_stream():
        """イベントストリーム生成"""
        seen: set[str] = set()
        
        async with httpx.AsyncClient() as client:
            # 全ミラーノード・クエリバリアント・ページを並行実行
            coros = [
                execute_node_request(client, base, search_q, page)
                for base in MIRROR_NODES
                for search_q in query_variants
                for page in range(1, 4)
            ]
            tasks = [asyncio.ensure_future(c) for c in coros]

            for fut in asyncio.as_completed(tasks):
                batch = await fut
                new_items = []
                
                for raw in batch:
                    normalized = transform_mirror_item(raw)
                    if normalized and normalized.get("videoId") not in seen:
                        seen.add(normalized["videoId"])
                        new_items.append(normalized)

                # バッチごとにイベント送信
                if new_items:
                    yield f"data: {json.dumps({'items': new_items}, ensure_ascii=False)}\n\n"

        # 完了イベント送信
        yield 'data: {"done":true}\n\n'

    return StreamingResponse(
        generate_event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
