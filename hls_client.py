from urllib.parse import urljoin

import m3u8


def parse_master_playlist(content: str, base_url: str) -> list[dict]:
    """Parse master playlist and return variant info list."""
    obj = m3u8.loads(content)
    variants = []
    for playlist in obj.playlists:
        uri = playlist.uri
        if not uri.startswith("http"):
            uri = urljoin(base_url, uri)
        variants.append({
            "uri": uri,
            "bandwidth": playlist.stream_info.bandwidth or 0,
        })
    return variants


def parse_media_playlist(content: str, base_url: str) -> tuple[list[str], float]:
    """Parse media playlist and return (segment URLs, target_duration)."""
    obj = m3u8.loads(content)
    segments = []
    for seg in obj.segments:
        uri = seg.uri
        if not uri.startswith("http"):
            uri = urljoin(base_url, uri)
        segments.append(uri)
    target_duration = obj.target_duration or 6.0
    return segments, target_duration


def select_variant(variants: list[dict]) -> dict | None:
    """Select the highest bandwidth variant."""
    if not variants:
        return None
    return max(variants, key=lambda v: v["bandwidth"])
