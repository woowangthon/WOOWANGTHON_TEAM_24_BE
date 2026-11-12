"""Read-only HTTP checks. Does not upload documents or invoke AI."""

import argparse
import json
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import ProxyHandler, build_opener


@dataclass
class Response:
    status: int
    media_type: str
    body: bytes


def fetch(url: str) -> Response:
    # Do not route localhost checks through an inherited HTTP proxy.
    opener = build_opener(ProxyHandler({}))
    try:
        response = opener.open(url, timeout=10)
    except HTTPError as error:
        response = error
    with response:
        return Response(response.status, response.headers.get_content_type(), response.read())


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("src"):
            self.paths.append((attrs["src"], {"text/javascript", "application/javascript"}))
        if tag == "link" and attrs.get("rel") == "stylesheet" and attrs.get("href"):
            self.paths.append((attrs["href"], {"text/css"}))


def run_checks(base_url: str) -> None:
    base = urlsplit(base_url)
    require(
        base.scheme in {"http", "https"} and bool(base.hostname)
        and base.path in {"", "/"} and not base.query and not base.fragment
        and base.username is None and base.password is None,
        "Use an HTTP(S) origin without credentials or a path.",
    )
    base_url = base_url.rstrip("/")

    def get(path):
        return fetch(base_url + path)

    response = get("/api/health")
    require(response.status == 200 and response.media_type == "application/json", "Health response failed.")
    require(json.loads(response.body) == {"status": "ok"}, "Unexpected health JSON.")
    response = get("/docs")
    require(response.status == 200 and response.media_type == "text/html", "Swagger response failed.")
    for path in ("/api", "/api/", "/api/not-existing"):
        response = get(path)
        require(response.status == 404 and response.media_type == "application/json", f"API 404 failed: {path}")
        require(json.loads(response.body) == {"detail": "없는 API 주소예요."}, f"Wrong API detail: {path}")
    response = get("/api/documents/not-an-integer")
    require(response.status == 422 and response.media_type == "application/json", "Existing API validation was shadowed.")
    require(isinstance(json.loads(response.body).get("detail"), list), "Unexpected validation JSON.")

    index = get("/")
    require(index.status == 200 and index.media_type == "text/html", "Frontend index is missing.")
    for path in ("/dashboard", "/review/8"):
        response = get(path)
        require(response.status == 200 and response.media_type == "text/html" and response.body == index.body, f"SPA navigation failed: {path}")
    assets = Assets()
    assets.feed(index.body.decode("utf-8"))
    require(bool(assets.paths), "No JS/CSS references found in the frontend index.")
    for path, media_types in assets.paths:
        url = urljoin(base_url + "/", path)
        require(urlsplit(url).netloc == base.netloc and urlsplit(url).scheme == base.scheme, "Frontend asset uses another origin.")
        response = fetch(url)
        require(response.status == 200 and response.media_type in media_types and bool(response.body) and response.body != index.body, "A frontend asset is missing or received HTML.")
    response = get("/openapi.json")
    require(response.status == 200 and response.media_type == "application/json", "OpenAPI response failed.")
    paths = json.loads(response.body)["paths"]
    require("/api/health" in paths and "/" not in paths and not any("full_path" in path for path in paths), "OpenAPI includes the SPA fallback or lacks health.")
    print("PASS: health, docs, API 404/422, index, SPA navigation, JS/CSS, OpenAPI")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    args = parser.parse_args()
    try:
        run_checks(args.base_url)
    except (ValueError, KeyError, OSError, URLError) as error:
        # Never print response bodies or environment variables.
        parser.exit(1, f"HTTP verification failed: {error}\n")


if __name__ == "__main__":
    main()
