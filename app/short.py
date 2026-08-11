from fastapi import APIRouter, Query
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote, urlparse, parse_qs, unquote
import time

router = APIRouter(prefix="/api/short", tags=["short"])

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
REQUEST_TIMEOUT = 20


def normalize_hostname(hostname):
    if not hostname:
        return ""

    hostname = hostname.lower().strip()

    if hostname.startswith("www."):
        hostname = hostname[4:]

    return hostname


def is_youtube_hostname(hostname):
    hostname = normalize_hostname(hostname)

    return hostname in [
        "youtube.com",
        "m.youtube.com"
    ]


def is_duckduckgo_hostname(hostname):
    hostname = normalize_hostname(hostname)

    return hostname in [
        "duckduckgo.com",
        "html.duckduckgo.com"
    ]


def clean_url(url):
    if not url:
        return ""

    url = url.strip()

    for _ in range(3):
        decoded_url = unquote(url)

        if decoded_url == url:
            break

        url = decoded_url

    return url


def extract_youtube_url(url):
    if not url:
        return None

    try:
        current_url = clean_url(url)

        for _ in range(5):
            parsed = urlparse(current_url)

            if is_youtube_hostname(parsed.hostname):
                return current_url

            if not is_duckduckgo_hostname(parsed.hostname):
                return None

            query = parse_qs(parsed.query)

            if "uddg" not in query:
                return None

            next_url = None

            for encoded_url in query["uddg"]:
                decoded_url = clean_url(encoded_url)

                decoded_parsed = urlparse(decoded_url)

                if is_youtube_hostname(decoded_parsed.hostname):
                    return decoded_url

                next_url = decoded_url

            if not next_url:
                return None

            current_url = next_url

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

        path = parsed.path or ""

        if path.startswith("/shorts/"):
            video_id = path.split("/shorts/", 1)[1]

            video_id = video_id.split("/", 1)[0]
            video_id = video_id.split("?", 1)[0]
            video_id = video_id.strip()

            if video_id:
                return video_id

        if path == "/watch":
            params = parse_qs(parsed.query)

            if "v" in params:
                video_id = params["v"][0]

                if video_id:
                    return video_id

        if path.startswith("/embed/"):
            video_id = path.split("/embed/", 1)[1]

            video_id = video_id.split("/", 1)[0]
            video_id = video_id.split("?", 1)[0]
            video_id = video_id.strip()

            if video_id:
                return video_id

        return None

    except Exception:
        return None


def is_explicit_shorts_url(url):
    if not url:
        return False

    try:
        parsed = urlparse(url)

        if not is_youtube_hostname(parsed.hostname):
            return False

        return parsed.path.startswith("/shorts/")

    except Exception:
        return False


def build_shorts_url(video_id):
    return f"https://www.youtube.com/shorts/{video_id}"


def build_thumbnail_url(video_id):
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


def get_result_title(result):
    selectors = [
        ".result__title a",
        ".result__a",
        "a.result__a",
        "h2 a",
        "h3 a"
    ]

    for selector in selectors:
        element = result.select_one(selector)

        if element:
            title = element.get_text(" ", strip=True)

            if title:
                return title

    return ""


def get_result_url(result):
    selectors = [
        ".result__title a",
        ".result__a",
        "a.result__a",
        "h2 a",
        "h3 a"
    ]

    for selector in selectors:
        element = result.select_one(selector)

        if element:
            url = element.get("href", "")

            if url:
                return url

    return ""


def get_result_snippet(result):
    selectors = [
        ".result__snippet",
        ".result__body",
        ".result__description"
    ]

    for selector in selectors:
        element = result.select_one(selector)

        if element:
            snippet = element.get_text(" ", strip=True)

            if snippet:
                return snippet

    return ""


def extract_result_elements(soup):
    selectors = [
        ".result",
        ".results .result",
        "article.result"
    ]

    elements = []

    for selector in selectors:
        found = soup.select(selector)

        if found:
            elements.extend(found)

    unique_elements = []
    seen_elements = set()

    for element in elements:
        element_id = id(element)

        if element_id in seen_elements:
            continue

        seen_elements.add(element_id)
        unique_elements.append(element)

    return unique_elements


def request_duckduckgo(url, method="GET", data=None):
    last_response = None
    last_error = None

    for attempt in range(MAX_RETRIES):
        try:
            if method.upper() == "POST":
                response = requests.post(
                    url,
                    headers=HEADERS,
                    data=data,
                    timeout=REQUEST_TIMEOUT,
                    allow_redirects=True
                )
            else:
                response = requests.get(
                    url,
                    headers=HEADERS,
                    timeout=REQUEST_TIMEOUT,
                    allow_redirects=True
                )

            last_response = response

            if response.status_code in [200, 202]:
                if response.text:
                    return response

            last_error = f"HTTP {response.status_code}"

        except requests.RequestException as e:
            last_error = str(e)

        if attempt < MAX_RETRIES - 1:
            time.sleep(1.5 * (attempt + 1))

    if last_response is not None:
        if last_response.text:
            return last_response

    raise RuntimeError(
        "DuckDuckGoへのアクセスに失敗しました: "
        f"{last_error}"
    )


def search_duckduckgo(query, max_results=20):
    search_query = f"site:youtube.com/shorts {query}"

    encoded_query = quote(search_query)

    url = (
        "https://html.duckduckgo.com/html/"
        f"?q={encoded_query}"
    )

    response = request_duckduckgo(url)

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    result_elements = extract_result_elements(soup)

    if not result_elements:
        time.sleep(1)

        post_url = "https://html.duckduckgo.com/html/"

        response = request_duckduckgo(
            post_url,
            method="POST",
            data={"q": search_query}
        )

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        result_elements = extract_result_elements(soup)

    results = []
    used_video_ids = set()

    for result in result_elements:
        title = get_result_title(result)
        raw_url = get_result_url(result)
        snippet = get_result_snippet(result)

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

    except requests.exceptions.Timeout:
        return {
            "success": False,
            "query": query,
            "type": "youtube_shorts",
            "count": 0,
            "results": []
        }

    except requests.exceptions.RequestException:
        return {
            "success": False,
            "query": query,
            "type": "youtube_shorts",
            "count": 0,
            "results": []
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

    search_query = f"site:youtube.com/shorts {query}"

    encoded_query = quote(search_query)

    url = (
        "https://html.duckduckgo.com/html/"
        f"?q={encoded_query}"
    )

    try:
        response = request_duckduckgo(url)

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        result_elements = extract_result_elements(soup)

        if not result_elements:
            time.sleep(1)

            post_url = "https://html.duckduckgo.com/html/"

            response = request_duckduckgo(
                post_url,
                method="POST",
                data={"q": search_query}
            )

            soup = BeautifulSoup(
                response.text,
                "html.parser"
            )

            result_elements = extract_result_elements(soup)

        results = []

        for result in result_elements:
            title = get_result_title(result)
            raw_url = get_result_url(result)
            snippet = get_result_snippet(result)

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
