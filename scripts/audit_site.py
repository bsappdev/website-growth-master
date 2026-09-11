#!/usr/bin/env python3
"""Safely crawl a bounded, same-origin web site and emit a JSON SEO snapshot.

The crawler deliberately uses only :mod:`urllib` and :class:`html.parser.HTMLParser`.
It never executes page JavaScript, submits forms, follows non-HTTP URLs, or follows a
redirect outside the origin supplied by the caller.  Pages are fetched at most once
and ``--max-pages`` and ``--max-redirects`` bound the work performed.

Exit codes
----------
0
    The crawl completed without fetch failures or verified broken internal links.
1
    The crawl completed, but found a fetch failure or a verified broken internal
    link.  The JSON report is still written and is the source of detail.
2
    Invalid command-line input (for example, a non-HTTP start URL).
3
    An unexpected local/runtime failure, such as an unreadable output path.

The module also exposes :func:`run_audit` and :class:`SiteAuditor` so callers can
inject an ``urllib``-compatible opener in deterministic tests.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


DEFAULT_MAX_PAGES = 100
DEFAULT_MAX_REDIRECTS = 8
DEFAULT_MAX_BODY_BYTES = 2_000_000
USER_AGENT = "website-growth-engine-audit/1.0 (+safe-bounded-crawl)"


def _collapse_text(value: str) -> str:
    """Return display-safe, whitespace-normalized text extracted from HTML."""

    return " ".join(value.split())


def _origin(url: str) -> Optional[Tuple[str, str, int]]:
    """Return a strict origin tuple or ``None`` for unsupported/malformed URLs."""

    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80
    return parsed.scheme.lower(), parsed.hostname.lower(), port


def normalize_http_url(value: str, base_url: Optional[str] = None) -> Optional[str]:
    """Resolve and normalize an HTTP(S) URL without a fragment.

    Userinfo URLs are rejected.  This keeps audit requests from accidentally
    carrying credentials found in untrusted HTML.
    """

    if not isinstance(value, str):
        return None
    try:
        absolute = urljoin(base_url, value) if base_url else value
        parsed = urlsplit(absolute)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        port = parsed.port  # forces validation of an invalid port
    except (TypeError, ValueError):
        return None

    host = parsed.hostname.lower()
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    netloc = host if port in (None, default_port) else "%s:%s" % (host, port)
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, ""))


class _NoRedirect(HTTPRedirectHandler):
    """Cause urllib to surface 3xx responses so redirect hops can be reported."""

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class PageParser(HTMLParser):
    """Minimal passive HTML extractor; it does not evaluate or interpret scripts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: List[str] = []
        self.h1_parts: List[str] = []
        self.links: List[str] = []
        self.images: List[Dict[str, str]] = []
        self.meta_description: Optional[str] = None
        self.robots: List[str] = []
        self.canonicals: List[str] = []
        self._capture: Optional[str] = None
        self._capture_depth = 0
        self._ignored_depth = 0

    @staticmethod
    def _attrs(values: Sequence[Tuple[str, Optional[str]]]) -> Dict[str, str]:
        return {str(key).lower(): value or "" for key, value in values}

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        attributes = self._attrs(attrs)
        if tag in {"script", "style", "noscript", "template"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag in {"title", "h1"}:
            if self._capture is None:
                self._capture = tag
                self._capture_depth = 1
            elif self._capture == tag:
                self._capture_depth += 1
        elif tag == "a" and attributes.get("href"):
            self.links.append(attributes["href"])
        elif tag == "img":
            self.images.append({"src": attributes.get("src", ""), "alt": attributes.get("alt", "")})
        elif tag == "meta":
            name = attributes.get("name", "").strip().lower()
            content = attributes.get("content", "")
            if name == "description" and self.meta_description is None:
                self.meta_description = _collapse_text(content)
            elif name == "robots" and content:
                self.robots.append(_collapse_text(content))
        elif tag == "link":
            rel_values = {item.lower() for item in attributes.get("rel", "").split()}
            href = attributes.get("href", "")
            if "canonical" in rel_values and href:
                self.canonicals.append(href)

    def handle_startendtag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        # HTMLParser does not call handle_starttag for XHTML-style tags.
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "template"} and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if self._ignored_depth or self._capture != tag:
            return
        self._capture_depth -= 1
        if self._capture_depth <= 0:
            self._capture = None
            self._capture_depth = 0

    def handle_data(self, data: str) -> None:
        if self._ignored_depth or self._capture is None:
            return
        if self._capture == "title":
            self.title_parts.append(data)
        elif self._capture == "h1":
            self.h1_parts.append(data)

    def extracted(self) -> Dict[str, Any]:
        """Return normalized, JSON-safe values collected from a document."""

        return {
            "title": _collapse_text(" ".join(self.title_parts)) or None,
            "meta_description": self.meta_description or None,
            "h1": _collapse_text(" ".join(self.h1_parts)) or None,
            "robots": self.robots,
            "canonical_hrefs": self.canonicals,
            "links": self.links,
            "images": self.images,
        }


@dataclass
class FetchResult:
    """The passive result of one bounded HTTP request sequence."""

    requested_url: str
    final_url: str
    status: Optional[int]
    redirects: List[Dict[str, Any]]
    headers: Mapping[str, str]
    body: bytes
    error: Optional[str] = None
    body_truncated: bool = False


class SiteAuditor:
    """Bounded crawler whose ``opener`` can be replaced with a test double."""

    def __init__(
        self,
        start_url: str,
        *,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        timeout: float = 10.0,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        opener: Optional[Any] = None,
    ) -> None:
        normalized = normalize_http_url(start_url)
        if normalized is None:
            raise ValueError("start_url must be an absolute http:// or https:// URL")
        if max_pages < 1:
            raise ValueError("max_pages must be at least 1")
        if max_redirects < 0:
            raise ValueError("max_redirects must be zero or greater")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if max_body_bytes < 1:
            raise ValueError("max_body_bytes must be at least 1")

        self.start_url = normalized
        self.origin = _origin(normalized)
        assert self.origin is not None
        self.max_pages = max_pages
        self.max_redirects = max_redirects
        self.timeout = timeout
        self.max_body_bytes = max_body_bytes
        self.opener = opener or build_opener(_NoRedirect())

    def _same_origin(self, url: str) -> bool:
        return _origin(url) == self.origin

    @staticmethod
    def _headers(response: Any) -> Mapping[str, str]:
        headers = getattr(response, "headers", None)
        if headers is None:
            return {}
        try:
            return {str(key).lower(): str(value) for key, value in headers.items()}
        except AttributeError:
            return {}

    @staticmethod
    def _status(response: Any) -> Optional[int]:
        value = getattr(response, "status", None)
        if value is None:
            value = getattr(response, "code", None)
        if value is None and hasattr(response, "getcode"):
            value = response.getcode()
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _open_once(self, url: str) -> Tuple[Optional[Any], Optional[HTTPError], Optional[str]]:
        request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"})
        try:
            return self.opener.open(request, timeout=self.timeout), None, None
        except HTTPError as exc:
            return None, exc, None
        except (URLError, OSError, ValueError) as exc:
            return None, None, "%s: %s" % (exc.__class__.__name__, str(exc))

    def _fetch(self, requested_url: str) -> FetchResult:
        current = requested_url
        redirects: List[Dict[str, Any]] = []
        for _ in range(self.max_redirects + 1):
            response, http_error, request_error = self._open_once(current)
            if request_error:
                return FetchResult(requested_url, current, None, redirects, {}, b"", request_error)
            source = response if response is not None else http_error
            assert source is not None
            status = self._status(source)
            headers = self._headers(source)
            is_redirect = status is not None and 300 <= status < 400
            if is_redirect:
                raw_location = headers.get("location", "")
                destination = normalize_http_url(raw_location, current) if raw_location else None
                hop: Dict[str, Any] = {"url": current, "status": status, "location": destination}
                redirects.append(hop)
                if destination is None:
                    return FetchResult(requested_url, current, status, redirects, headers, b"", "invalid_or_missing_redirect_location")
                if not self._same_origin(destination):
                    return FetchResult(requested_url, current, status, redirects, headers, b"", "cross_origin_redirect_blocked")
                if len(redirects) > self.max_redirects:
                    return FetchResult(requested_url, current, status, redirects, headers, b"", "redirect_limit_exceeded")
                current = destination
                continue

            if http_error is not None:
                return FetchResult(requested_url, current, status, redirects, headers, b"", "http_error")
            assert response is not None
            try:
                body = response.read(self.max_body_bytes + 1)
            except (OSError, ValueError) as exc:
                return FetchResult(requested_url, current, status, redirects, headers, b"", "%s: %s" % (exc.__class__.__name__, str(exc)))
            truncated = len(body) > self.max_body_bytes
            if truncated:
                body = body[: self.max_body_bytes]
            return FetchResult(requested_url, current, status, redirects, headers, body, None, truncated)
        # The loop can only fall through if future edits change redirect handling.
        return FetchResult(requested_url, current, None, redirects, {}, b"", "redirect_limit_exceeded")

    @staticmethod
    def _decode_html(body: bytes, headers: Mapping[str, str]) -> str:
        content_type = headers.get("content-type", "")
        match = re.search(r"charset\s*=\s*['\"]?([^;'\"\s]+)", content_type, re.IGNORECASE)
        encoding = match.group(1) if match else "utf-8"
        try:
            return body.decode(encoding, errors="replace")
        except LookupError:
            return body.decode("utf-8", errors="replace")

    @staticmethod
    def _is_html(headers: Mapping[str, str]) -> bool:
        return "html" in headers.get("content-type", "").lower()

    def _page_record(self, fetched: FetchResult) -> Tuple[Dict[str, Any], List[str], List[Dict[str, str]]]:
        record: Dict[str, Any] = {
            "requested_url": fetched.requested_url,
            "final_url": fetched.final_url,
            "status": fetched.status,
            "redirects": fetched.redirects,
            "content_type": fetched.headers.get("content-type"),
            "title": None,
            "meta_description": None,
            "h1": None,
            "canonical": None,
            "robots": [],
            "error": fetched.error,
            "body_truncated": fetched.body_truncated,
        }
        if fetched.error or not fetched.body or not self._is_html(fetched.headers):
            return record, [], []

        parser = PageParser()
        try:
            parser.feed(self._decode_html(fetched.body, fetched.headers))
            parser.close()
        except (ValueError, OverflowError) as exc:
            record["error"] = "html_parse_error: %s" % exc
            return record, [], []
        extracted = parser.extracted()
        canonicals = [normalize_http_url(value, fetched.final_url) for value in extracted["canonical_hrefs"]]
        record.update(
            {
                "title": extracted["title"],
                "meta_description": extracted["meta_description"],
                "h1": extracted["h1"],
                "canonical": next((value for value in canonicals if value), None),
                "robots": extracted["robots"],
            }
        )
        internal_links: List[str] = []
        for raw_link in extracted["links"]:
            target = normalize_http_url(raw_link, fetched.final_url)
            if target and self._same_origin(target):
                internal_links.append(target)
        images: List[Dict[str, str]] = []
        for image in extracted["images"]:
            alt = _collapse_text(image.get("alt", ""))
            if alt:
                images.append({"src": image.get("src", ""), "alt": alt})
        return record, list(dict.fromkeys(internal_links)), images

    @staticmethod
    def _duplicate_groups(pages: Iterable[Mapping[str, Any]], field: str) -> List[Dict[str, Any]]:
        buckets: Dict[str, List[str]] = defaultdict(list)
        for page in pages:
            value = page.get(field)
            if isinstance(value, str) and value.strip():
                buckets[_collapse_text(value).casefold()].append(str(page["requested_url"]))
        return [
            {field: key, "urls": sorted(urls)}
            for key, urls in sorted(buckets.items())
            if len(urls) > 1
        ]

    @staticmethod
    def _issue_candidate(
        issue_id: str,
        *,
        priority: str,
        observation: str,
        affected_url: str,
        evidence_pointer: Mapping[str, Any],
        scope: Mapping[str, Any],
        acceptance_test: str,
    ) -> Dict[str, Any]:
        """Return only an evidence-backed candidate, never a causal assertion."""

        return {
            "issue_id": issue_id,
            "priority": priority,
            "observation": observation,
            "evidence_pointer": dict(evidence_pointer),
            "scope": dict(scope),
            "affected_url": affected_url,
            "root_cause_hypothesis": None,
            "confidence": "HIGH",
            "proposed_next_step": "Inspect the server, route, and deployment evidence before making a change.",
            "risk": "Low for investigation; no remediation is proposed by this crawler.",
            "rollback": "Not applicable: this report made no site changes.",
            "acceptance_test": acceptance_test,
            "owner": None,
            "status": None,
        }

    def _issue_candidates(self, pages: Mapping[str, Mapping[str, Any]], broken: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        """Create candidates solely for deterministic, verified availability failures."""

        candidates: List[Dict[str, Any]] = []
        start = pages.get(self.start_url)
        if start and (start.get("error") is not None or start.get("status") is None or int(start["status"]) >= 400):
            candidates.append(
                self._issue_candidate(
                    "AUDIT-001",
                    priority="HIGH",
                    observation="The requested start URL could not be fetched successfully.",
                    affected_url=self.start_url,
                    evidence_pointer={"report_section": "pages", "requested_url": self.start_url, "status": start.get("status"), "error": start.get("error")},
                    scope={"start_url": True},
                    acceptance_test="A subsequent bounded crawl returns a successful HTTP status for the start URL.",
                )
            )
        next_number = len(candidates) + 1
        for item in broken:
            target = str(item["url"])
            if target == self.start_url:
                continue
            status = item.get("status")
            description = "A verified internal link resolves to an unsuccessful response."
            candidates.append(
                self._issue_candidate(
                    "AUDIT-%03d" % next_number,
                    priority="MEDIUM",
                    observation=description,
                    affected_url=target,
                    evidence_pointer={"report_section": "broken_internal_links", "url": target, "status": status, "error": item.get("error")},
                    scope={"linked_from": list(item.get("linked_from", []))},
                    acceptance_test="A subsequent bounded crawl fetches the linked URL successfully and reports no broken internal link for it.",
                )
            )
            next_number += 1
        return candidates

    def crawl(self) -> Dict[str, Any]:
        """Crawl reachable same-origin HTML pages and return a deterministic report."""

        queue: Deque[str] = deque([self.start_url])
        queued: Set[str] = {self.start_url}
        pages: List[Dict[str, Any]] = []
        page_by_url: Dict[str, Dict[str, Any]] = {}
        sources: Dict[str, Set[str]] = defaultdict(set)
        image_alts: List[Dict[str, str]] = []

        while queue and len(pages) < self.max_pages:
            url = queue.popleft()
            fetched = self._fetch(url)
            record, internal_links, images = self._page_record(fetched)
            pages.append(record)
            page_by_url[url] = record
            for image in images:
                image_alts.append({"page": url, **image})
            for target in internal_links:
                sources[target].add(url)
                if target not in queued and target not in page_by_url:
                    queue.append(target)
                    queued.add(target)

        # Links left in the queue were known but deliberately not fetched due to the
        # caller's bound.  They are distinguishable from verified broken links.
        unverified = sorted(url for url in sources if url not in page_by_url)
        broken: List[Dict[str, Any]] = []
        for target in sorted(sources):
            page = page_by_url.get(target)
            if page is None:
                continue
            status = page.get("status")
            failed = page.get("error") is not None or status is None or int(status) >= 400
            if failed:
                broken.append(
                    {
                        "url": target,
                        "linked_from": sorted(sources[target]),
                        "status": status,
                        "error": page.get("error"),
                    }
                )

        redirect_chains = []
        for page in pages:
            redirects = page.get("redirects") or []
            if redirects:
                chain = [page["requested_url"]] + [hop.get("location") for hop in redirects if hop.get("location")]
                redirect_chains.append(
                    {
                        "requested_url": page["requested_url"],
                        "chain": chain,
                        "hops": len(redirects),
                        "final_status": page.get("status"),
                        "error": page.get("error"),
                    }
                )

        failed_pages = sum(
            1
            for page in pages
            if page.get("error") is not None or page.get("status") is None or int(page["status"]) >= 400
        )
        issue_candidates = self._issue_candidates(page_by_url, broken)
        report = {
            "schema_version": "1.0",
            "start_url": self.start_url,
            "origin": {"scheme": self.origin[0], "host": self.origin[1], "port": self.origin[2]},
            "limits": {
                "max_pages": self.max_pages,
                "max_redirects": self.max_redirects,
                "max_body_bytes": self.max_body_bytes,
            },
            "pages": pages,
            "broken_internal_links": broken,
            "unverified_internal_links": [
                {"url": target, "linked_from": sorted(sources[target]), "reason": "max_pages_reached"}
                for target in unverified
            ],
            "redirect_chains": redirect_chains,
            "duplicate_titles": self._duplicate_groups(pages, "title"),
            "duplicate_meta_descriptions": self._duplicate_groups(pages, "meta_description"),
            "nonempty_image_alt_observations": image_alts,
            "audit_issue_candidates": issue_candidates,
            "summary": {
                "pages_fetched": len(pages),
                "failed_pages": failed_pages,
                "broken_internal_links": len(broken),
                "unverified_internal_links": len(unverified),
                "redirect_chains": len(redirect_chains),
                "duplicate_title_groups": len(self._duplicate_groups(pages, "title")),
                "duplicate_meta_description_groups": len(self._duplicate_groups(pages, "meta_description")),
                "nonempty_image_alt_observations": len(image_alts),
                "audit_issue_candidates": len(issue_candidates),
            },
        }
        return report


def run_audit(start_url: str, **kwargs: Any) -> Dict[str, Any]:
    """Convenience wrapper returning the report from :class:`SiteAuditor`."""

    return SiteAuditor(start_url, **kwargs).crawl()


def _write_json(report: Mapping[str, Any], output: Optional[str]) -> None:
    encoded = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if output:
        Path(output).write_text(encoded, encoding="utf-8")
    else:
        sys.stdout.write(encoded)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bounded same-origin SEO crawler. It passively parses HTML and never executes scripts.",
        epilog="Exit 0: no verified failures; 1: site failures; 2: invalid input; 3: local/runtime failure.",
    )
    parser.add_argument("start_url", help="Absolute http(s) URL at the origin to crawl")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES, help="Maximum same-origin URLs to fetch (default: %(default)s)")
    parser.add_argument("--max-redirects", type=int, default=DEFAULT_MAX_REDIRECTS, help="Maximum redirect hops per URL (default: %(default)s)")
    parser.add_argument("--timeout", type=float, default=10.0, help="Timeout in seconds per request (default: %(default)s)")
    parser.add_argument("--max-body-bytes", type=int, default=DEFAULT_MAX_BODY_BYTES, help="Maximum bytes parsed per response (default: %(default)s)")
    parser.add_argument("--output", help="Write JSON report to this path instead of stdout")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        auditor = SiteAuditor(
            args.start_url,
            max_pages=args.max_pages,
            max_redirects=args.max_redirects,
            timeout=args.timeout,
            max_body_bytes=args.max_body_bytes,
        )
    except (ValueError, argparse.ArgumentError) as exc:
        parser.error(str(exc))
        return 2  # parser.error raises SystemExit; retained for embedders.
    try:
        report = auditor.crawl()
        _write_json(report, args.output)
    except (OSError, TypeError, ValueError) as exc:
        sys.stderr.write("audit_site.py: %s\n" % exc)
        return 3
    return 1 if report["summary"]["failed_pages"] or report["summary"]["broken_internal_links"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
