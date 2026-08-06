import asyncio
import base64
import json
import time
import httpx
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.add_extension("jinja2.ext.do")

FALLBACK_INVIDIOUS_INSTANCES = [
    "https://invidious.ritoge.com",
    "https://yt.omada.cafe",
    "https://invidious.darkness.services",
    "https://invidious.f5.si",
    "https://invidious.ducks.party",
    "https://y.com.sb",
    "https://super8.absturztau.be",
    "https://inv.zoomerville.com",
    "https://invidious.nerdvpn.de",
    "https://inv.thepixora.com",
]

PIPED_INSTANCES = [
    "https://pipedapi.wireway.ch",
    "https://api.piped.private.coffee",
    "https://pipedapi.winscloud.net",
]

SENNIN_API_BASE = "https://discerning-adventure-production-ebfc.up.railway.app"

_ENCRYPTED_KEYS = [
    "ZTYxNTE4MzAzNG1zaDJkZmRhMzFhNDdhNmYxMnAxZmE2Y2Nqc241OWExYTVlMDY0MTU=",
    "NjllMjk5OWE3OW1zaGNiNjU3MTg0YmE2NzMxY3AxNmY2ODRqc24zMjA1NGEwNzBiYTU=",
]

INVIDIOUS_VIDEO_LIST_URL = "https://raw.githubusercontent.com/ikirikittsuhao-ctrl/Invidious-check/refs/heads/main/lists/video.json"
INVIDIOUS_SEARCH_LIST_URL = "https://raw.githubusercontent.com/ikirikittsuhao-ctrl/Invidious-check/refs/heads/main/lists/search.json"

limits = httpx.Limits(max_connections=500, max_keepalive_connections=200)
client_session = httpx.AsyncClient(
    timeout=4.5, limits=limits, follow_redirects=True
)
no_redirect_client = httpx.AsyncClient(
    timeout=3.5, limits=limits, follow_redirects=False
)

_CACHE = {}
_INFLIGHT = {}
_CACHE_LOCK = asyncio.Lock()


def _get_rapid_api_keys():
  return [
      base64.b64decode(k.encode("utf-8")).decode("utf-8")
      for k in _ENCRYPTED_KEYS
  ]


def get_cache(key: str):
  now = time.time()
  hit = _CACHE.get(key)
  if hit and hit["exp"] > now:
    return hit["val"]
  return None


def set_cache(key: str, val: any, ttl: float = 180.0):
  now = time.time()
  if len(_CACHE) >= 1000:
    oldest = min(_CACHE, key=lambda k: _CACHE[k]["exp"])
    _CACHE.pop(oldest, None)
  _CACHE[key] = {"val": val, "exp": now + ttl}


async def fetch_with_inflight(key: str, fetch_func, ttl: float = 180.0):
  cached = get_cache(key)
  if cached is not None:
    return cached

  loop = asyncio.get_event_loop()
  async with _CACHE_LOCK:
    cached = get_cache(key)
    if cached is not None:
      return cached
    if key in _INFLIGHT:
      fut = _INFLIGHT[key]
      return await asyncio.shield(fut)

    fut = loop.create_future()
    _INFLIGHT[key] = fut

  try:
    res = await fetch_func()
    if res is not None:
      set_cache(key, res, ttl=ttl)
    if not fut.done():
      fut.set_result(res)
    return res
  except Exception as e:
    if not fut.done():
      fut.set_exception(e)
      try:
        fut.exception()
      except Exception:
        pass
    raise e
  finally:
    async with _CACHE_LOCK:
      _INFLIGHT.pop(key, None)


async def get_invidious_instances_from_url(list_url: str) -> list:
  cache_key = f"inv_instances_list:{list_url}"
  cached = get_cache(cache_key)
  if cached:
    return cached

  try:
    resp = await client_session.get(list_url, timeout=3.0)
    if resp.status_code == 200:
      data = resp.json()
      instances = []
      if isinstance(data, list):
        for item in data:
          if isinstance(item, dict) and "instance" in item:
            inst = item["instance"].strip()
            if inst:
              instances.append(inst)
          elif isinstance(item, str):
            instances.append(item.strip())
      if instances:
        set_cache(cache_key, instances, ttl=600.0)
        return instances
  except Exception:
    pass

  return FALLBACK_INVIDIOUS_INSTANCES


async def get_fastest_invidious_instance(
    list_url: str = INVIDIOUS_VIDEO_LIST_URL,
) -> str:
  cache_key = f"fastest_inv_instance:{list_url}"
  cached = get_cache(cache_key)
  if cached:
    return cached

  base_instances = await get_invidious_instances_from_url(list_url)
  target_instances = base_instances[:10]

  async def ping_instance(instance):
    start = time.time()
    try:
      url = f"{instance.rstrip('/')}/api/v1/stats"
      resp = await client_session.get(url, timeout=2.0)
      if resp.status_code == 200:
        elapsed = time.time() - start
        return instance, elapsed
    except Exception:
      pass
    return instance, float("inf")

  tasks = [ping_instance(inst) for inst in target_instances]
  results = await asyncio.gather(*tasks)

  valid_results = [r for r in results if r[1] < float("inf")]
  if valid_results:
    fastest_instance = min(valid_results, key=lambda x: x[1])[0]
    set_cache(cache_key, fastest_instance, ttl=300.0)
    return fastest_instance

  return (
      base_instances[0] if base_instances else FALLBACK_INVIDIOUS_INSTANCES[0]
  )


def _is_valid_invidious_response(res):
  if not res:
    return False
  if isinstance(res, dict):
    if "error" in res or "message" in res:
      return False
    return True
  if isinstance(res, list):
    return True
  return False


async def fetch_invidious(
    endpoint: str,
    params: dict = None,
    force_instance: str = None,
    list_type: str = "video",
):
  param_str = json.dumps(params, sort_keys=True) if params else ""
  cache_key = f"inv:{endpoint}:{param_str}:{force_instance or ''}:{list_type}"

  async def _do_fetch():
    list_url = (
        INVIDIOUS_SEARCH_LIST_URL
        if list_type == "search"
        else INVIDIOUS_VIDEO_LIST_URL
    )
    base_instances = await get_invidious_instances_from_url(list_url)

    if force_instance:
      instances = [force_instance] + [
          i for i in base_instances if i != force_instance
      ]
      last_error = None
      for instance in instances:
        try:
          url = f"{instance.rstrip('/')}/api/v1{endpoint}"
          response = await client_session.get(url, params=params, timeout=4.0)
          response.raise_for_status()
          res_data = response.json()
          if _is_valid_invidious_response(res_data):
            return res_data
          else:
            raise Exception("Invalid Invidious response format")
        except Exception as e:
          last_error = e
          continue
      raise last_error if last_error else Exception(
          "All Invidious instances failed"
      )
    else:
      fastest = await get_fastest_invidious_instance(list_url)
      instances = [fastest] + [i for i in base_instances if i != fastest]
      target_instances = instances[:8]

      async def task(instance):
        url = f"{instance.rstrip('/')}/api/v1{endpoint}"
        resp = await client_session.get(url, params=params, timeout=3.5)
        resp.raise_for_status()
        res_data = resp.json()
        if _is_valid_invidious_response(res_data):
          return res_data
        raise Exception("Invalid response from Invidious instance")

      tasks = [asyncio.create_task(task(inst)) for inst in target_instances]

      for completed in asyncio.as_completed(tasks):
        try:
          res = await completed
          if _is_valid_invidious_response(res):
            for t in tasks:
              if not t.done():
                t.cancel()
                try:
                  await t
                except asyncio.CancelledError:
                  pass
            return res
        except Exception:
          continue

      remaining = [i for i in instances if i not in target_instances]
      last_err = None
      for inst in remaining:
        try:
          url = f"{inst.rstrip('/')}/api/v1{endpoint}"
          response = await client_session.get(
              url, params=params, timeout=3.5
          )
          response.raise_for_status()
          res_data = response.json()
          if _is_valid_invidious_response(res_data):
            return res_data
        except Exception as e:
          last_err = e
          continue
      raise (
          last_err
          if last_err
          else Exception("All Invidious instances failed")
      )

  return await fetch_with_inflight(cache_key, _do_fetch, ttl=180.0)


@router.get("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    q: str = Query(...),
    page: int = 1,
    type: str = Query("video"),
    force_instance: str = Query(None),
):
  try:
    search_type = type if type != "short" else "video"
    query_q = q if type != "short" else f"{q} shorts"
    params = {"q": query_q, "page": page, "type": search_type}

    data = await fetch_invidious(
        "/search",
        params,
        force_instance=force_instance,
        list_type="search",
    )

    results_raw = data if isinstance(data, list) else []

    if type == "short":
      results = [
          {
              "type": item.get("type"),
              "videoId": item.get("videoId"),
              "title": item.get("title"),
              "lengthSeconds": item.get("lengthSeconds"),
              "author": item.get("author"),
              "authorThumbnails": item.get("authorThumbnails"),
              "videoThumbnails": item.get("videoThumbnails"),
              "viewCountText": item.get("viewCountText"),
              "viewCount": item.get("viewCount"),
              "publishedText": item.get("publishedText"),
          }
          for item in results_raw
          if item.get("type") == "video" and item.get("videoId")
      ]
    elif type == "channel":
      results = [
          {
              "type": item.get("type"),
              "authorId": item.get("authorId"),
              "author": item.get("author"),
              "authorThumbnails": item.get("authorThumbnails"),
              "subCountText": item.get("subCountText"),
              "videoCount": item.get("videoCount"),
          }
          for item in results_raw
          if item.get("type") == "channel"
      ]
    elif type == "playlist":
      results = [
          {
              "type": item.get("type"),
              "playlistId": item.get("playlistId"),
              "title": item.get("title"),
              "author": item.get("author"),
              "authorThumbnails": item.get("authorThumbnails"),
              "videoThumbnails": item.get("videoThumbnails"),
              "videoCount": item.get("videoCount"),
          }
          for item in results_raw
          if item.get("type") == "playlist"
      ]
    else:
      results = [
          {
              "type": item.get("type"),
              "videoId": item.get("videoId"),
              "playlistId": item.get("playlistId"),
              "authorId": item.get("authorId"),
              "title": item.get("title"),
              "lengthSeconds": item.get("lengthSeconds"),
              "author": item.get("author"),
              "authorThumbnails": item.get("authorThumbnails"),
              "videoThumbnails": item.get("videoThumbnails"),
              "viewCountText": item.get("viewCountText"),
              "viewCount": item.get("viewCount"),
              "publishedText": item.get("publishedText"),
              "subCountText": item.get("subCountText"),
              "videoCount": item.get("videoCount"),
          }
          for item in results_raw
      ]

    response = templates.TemplateResponse(
        "search.html",
        {
            "request": request,
            "query": q,
            "results": results,
            "type": type,
            "page": page,
        },
    )

    try:
      search_history_json = request.cookies.get("search_history", "[]")
      search_history = json.loads(search_history_json)
      if q in search_history:
        search_history.remove(q)
      search_history.append(q)
      if len(search_history) > 5:
        search_history = search_history[-5:]
      response.set_cookie(
          key="search_history",
          value=json.dumps(search_history),
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
        INVIDIOUS_SEARCH_LIST_URL
    )
    return templates.TemplateResponse(
        "apiallerror.html",
        {"request": request, "instances": fallback_instances},
    )


@router.get("/playlist", response_class=HTMLResponse)
async def playlist(
    request: Request,
    list: str = Query(...),
    force_instance: str = Query(None),
):
  try:
    data = await fetch_invidious(
        f"/playlists/{list}", force_instance=force_instance, list_type="video"
    )
    return templates.TemplateResponse(
        "playlist.html",
        {
            "request": request,
            "title": data.get("title"),
            "playlistId": list,
            "author": data.get("author"),
            "authorId": data.get("authorId"),
            "videos": data.get("videos", []),
            "description": data.get("descriptionHtml", ""),
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
