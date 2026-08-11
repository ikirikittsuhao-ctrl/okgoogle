from fastapi import APIRouter, Query
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote, urlparse, parse_qs, unquote
import time

router = APIRouter(
    prefix="/api/short",
    tags=["short"]
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,image/avif,image/webp,"
        "*/*;q=0.8"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Referer": "https://duckduckgo.com/"
}

MAX_RETRIES = 3
REQUEST_TIMEOUT = 10

session = requests.Session()
session.headers.update(HEADERS)


def normalize_hostname(hostname):
    if not hostname:
        return ""

    hostname = hostname.lower().strip()

    if hostname.startswith("www."):
        hostname = hostname[4:]

    return hostname


def is_youtube_hostname(hostname):
    return normalize_hostname(hostname) in {
        "youtube.com",
        "m.youtube.com"
    }


def is_duckduckgo_hostname(hostname):
    return normalize_hostname(hostname) in {
        "duckduckgo.com",
        "html.duckduckgo.com"
    }


def clean_url(url):
    if not url:
        return ""

    url = url.strip()

    for _ in range(3):
        decoded = unquote(url)

        if decoded == url:
            break

        url = decoded

    return url


def extract_youtube_url(url):
    if not url:
        return None

    try:
        current = clean_url(url)

        for _ in range(5):
            parsed = urlparse(current)

            if is_youtube_hostname(parsed.hostname):
                return current

            if not is_duckduckgo_hostname(parsed.hostname):
                return None

            params = parse_qs(parsed.query)

            urls = params.get("uddg")

            if not urls:
                return None

            next_url = clean_url(urls[0])

            if not next_url:
                return None

            current = next_url

        return None

    except Exception:
        return None


def extract_video_id(url):
    if not url:
        return None

    try:
        parsed = urlparse(url)

        if not is_youtube_hostname(parsed.hostname):
            return None

        path = parsed.path.rstrip("/")

        if path.startswith("/shorts/"):
            video_id = path[len("/shorts/"):]

        elif path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [None])[0]

        elif path.startswith("/embed/"):
            video_id = path[len("/embed/"):]

        else:
            return None

        if not video_id:
            return None

        return video_id.split("/")[0].strip() or None

    except Exception:
        return None


def is_explicit_shorts_url(url):
    if not url:
        return False

    try:
        parsed = urlparse(url)

        return (
            is_youtube_hostname(parsed.hostname)
            and parsed.path.startswith("/shorts/")
        )

    except Exception:
        return False


def build_shorts_url(video_id):
    return f"https://www.youtube.com/shorts/{video_id}"


def build_thumbnail_url(video_id):
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


def get_result_data(result):
    title = ""
    url = ""
    snippet = ""

    for element in result.select(
        ".result__title a, .result__a, a.result__a, h2 a, h3 a"
    ):
        if not title:
            title = element.get_text(" ", strip=True)

        if not url:
            url = element.get("href", "")

        if title and url:
            break

    element = result.select_one(
        ".result__snippet, .result__body, .result__description"
    )

    if element:
        snippet = element.get_text(" ", strip=True)

    return title, url, snippet


def extract_result_elements(soup):
    return soup.select(
        ".result, .results .result, article.result"
    )


def request_duckduckgo(url, method="GET", data=None):
    last_error = None
    last_response = None

    for attempt in range(MAX_RETRIES):
        try:
            response = session.request(
                method,
                url,
                data=data,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True
            )

            last_response = response

            if response.status_code in (200, 202) and response.text:
                return response

            last_error = f"HTTP {response.status_code}"

        except requests.RequestException as e:
            last_error = str(e)

        if attempt < MAX_RETRIES - 1:
            time.sleep(0.7 * (attempt + 1))

    if last_response is not None and last_response.text:
        return last_response

    raise RuntimeError(
        f"DuckDuckGoへのアクセスに失敗しました: {last_error}"
    )


def get_duckduckgo_results(query):
    search_query = f"site:youtube.com/shorts {query}"

    url = (
        "https://html.duckduckgo.com/html/"
        f"?q={quote(search_query)}"
    )

    response = request_duckduckgo(url)

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    results = extract_result_elements(soup)

    if results:
        return search_query, results

    time.sleep(0.5)

    response = request_duckduckgo(
        "https://html.duckduckgo.com/html/",
        method="POST",
        data={"q": search_query}
    )

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    return search_query, extract_result_elements(soup)


def search_duckduckgo(query, max_results=20):
    _, result_elements = get_duckduckgo_results(query)

    results = []
    used_video_ids = set()

    for result in result_elements:
        title, raw_url, snippet = get_result_data(result)

        if not raw_url:
            continue

        youtube_url = extract_youtube_url(raw_url)

        if not youtube_url:
            continue

        if not is_explicit_shorts_url(youtube_url):
            continue

        video_id = extract_video_id(youtube_url)

        if not video_id:
            continue

        if video_id in used_video_ids:
            continue

        used_video_ids.add(video_id)

        results.append({
            "title": title,
            "url": build_shorts_url(video_id),
            "video_id": video_id,
            "thumbnail": build_thumbnail_url(video_id),
            "snippet": snippet,
            "type": "youtube_short",
            "source_url": youtube_url,
            "is_explicit_shorts": True
        })

        if len(results) >= max_results:
            break

    return results


@router.get("")
def short_index():
    return {
        "success": True,
        "name": "YouTube Shorts Search API",
        "version": "1.0.0",
        "status": "online",
        "endpoint": "/api/short/search?q=検索ワード",
        "type": "youtube_shorts"
    }


@router.get("/search")
def api_search(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=50)
):
    query = q.strip()

    if not query:
        return {
            "success": False,
            "query": q,
            "type": "youtube_shorts",
            "count": 0,
            "results": [],
            "error": "検索ワードが空です"
        }

    try:
        results = search_duckduckgo(
            query,
            limit
        )

        return {
            "success": True,
            "query": query,
            "type": "youtube_shorts",
            "count": len(results),
            "results": results
        }

    except Exception:
        return {
            "success": False,
            "query": query,
            "type": "youtube_shorts",
            "count": 0,
            "results": []
        }


@router.get("/debug")
def api_debug(
    q: str = Query(..., min_length=1)
):
    query = q.strip()

    if not query:
        return {
            "success": False,
            "query": q,
            "results": []
        }

    try:
        search_query, result_elements = get_duckduckgo_results(
            query
        )

        results = []

        for result in result_elements:
            title, raw_url, snippet = get_result_data(result)

            youtube_url = extract_youtube_url(raw_url)
            video_id = extract_video_id(youtube_url)

            results.append({
                "title": title,
                "raw_url": raw_url,
                "youtube_url": youtube_url,
                "video_id": video_id,
                "is_shorts": is_explicit_shorts_url(
                    youtube_url
                ),
                "snippet": snippet
            })

        return {
            "success": True,
            "query": query,
            "duckduckgo_query": search_query,
            "result_count": len(results),
            "results": results
        }

    except Exception:
        return {
            "success": False,
            "query": query,
            "results": []
        }
