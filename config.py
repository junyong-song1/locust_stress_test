import os

# Base URL (without playlist.m3u8)
BASE_URL = os.getenv(
    "BASE_URL",
    "https://d1lel4oh55a2dj.cloudfront.net/out/v1/stress-test-ch/stress-test-02/vc12",
)

# Legacy — still used for master playlist fetch
BASE_PLAYLIST_URL = f"{BASE_URL}/playlist.m3u8"

# Time-machine delay range (seconds)
TIME_DELAY_MIN = int(os.getenv("TIME_DELAY_MIN", "1"))
TIME_DELAY_MAX = int(os.getenv("TIME_DELAY_MAX", "10800"))

# User weights
LIVE_USER_WEIGHT = int(os.getenv("LIVE_USER_WEIGHT", "5"))
TIME_MACHINE_USER_WEIGHT = int(os.getenv("TIME_MACHINE_USER_WEIGHT", "5"))

# Segment fetching
FETCH_SEGMENTS = os.getenv("FETCH_SEGMENTS", "false").lower() in ("true", "1", "yes")
MAX_SEGMENTS_PER_CYCLE = int(os.getenv("MAX_SEGMENTS_PER_CYCLE", "3"))

# Playlist refresh interval (seconds)
PLAYLIST_REFRESH_INTERVAL = float(os.getenv("PLAYLIST_REFRESH_INTERVAL", "0"))

# Cache reporter interval (seconds)
CACHE_REPORT_INTERVAL = float(os.getenv("CACHE_REPORT_INTERVAL", "1.0"))

# ---------------------------------------------------------------------------
# 14 Renditions (codec × resolution) with weighted random selection
# ---------------------------------------------------------------------------
RENDITIONS = [
    {"name": "playlist_1",  "codec": "AVC",  "res": "1920x1080", "bw": "7.8Mbps",  "weight": 5,  "label": "AVC 1080p 7.8M"},
    {"name": "playlist_2",  "codec": "AVC",  "res": "1600x900",  "bw": "5.3Mbps",  "weight": 5,  "label": "AVC 900p 5.3M"},
    {"name": "playlist_3",  "codec": "AVC",  "res": "1280x720",  "bw": "3.6Mbps",  "weight": 15, "label": "AVC 720p 3.6M"},
    {"name": "playlist_4",  "codec": "AVC",  "res": "960x540",   "bw": "2.6Mbps",  "weight": 10, "label": "AVC 540p 2.6M"},
    {"name": "playlist_5",  "codec": "AVC",  "res": "640x360",   "bw": "1.9Mbps",  "weight": 10, "label": "AVC 360p 1.9M"},
    {"name": "playlist_6",  "codec": "AVC",  "res": "480x270",   "bw": "1.6Mbps",  "weight": 5,  "label": "AVC 270p 1.6M"},
    {"name": "playlist_7",  "codec": "AVC",  "res": "480x270",   "bw": "1.1Mbps",  "weight": 5,  "label": "AVC 270p 1.1M"},
    {"name": "playlist_8",  "codec": "HEVC", "res": "1920x1080", "bw": "6.1Mbps",  "weight": 5,  "label": "HEVC 1080p 6.1M"},
    {"name": "playlist_9",  "codec": "HEVC", "res": "1600x900",  "bw": "4.4Mbps",  "weight": 5,  "label": "HEVC 900p 4.4M"},
    {"name": "playlist_10", "codec": "HEVC", "res": "1280x720",  "bw": "2.8Mbps",  "weight": 10, "label": "HEVC 720p 2.8M"},
    {"name": "playlist_11", "codec": "HEVC", "res": "960x540",   "bw": "1.9Mbps",  "weight": 5,  "label": "HEVC 540p 1.9M"},
    {"name": "playlist_12", "codec": "HEVC", "res": "640x360",   "bw": "1.1Mbps",  "weight": 5,  "label": "HEVC 360p 1.1M"},
    {"name": "playlist_13", "codec": "AVC",  "res": "1920x1080", "bw": "13.7Mbps", "weight": 5,  "label": "AVC 1080p 13.7M"},
    {"name": "playlist_14", "codec": "HEVC", "res": "1920x1080", "bw": "10.0Mbps", "weight": 5,  "label": "HEVC 1080p 10.0M"},
]

# Precompute weights list for random.choices
RENDITION_WEIGHTS = [r["weight"] for r in RENDITIONS]

# name → label mapping for display
RENDITION_LABEL_MAP = {r["name"]: r["label"] for r in RENDITIONS}

# ---------------------------------------------------------------------------
# Phase presets (Live:TM ratio)
# ---------------------------------------------------------------------------
PHASE_PRESETS = {
    "P1": {"live": 0, "tm": 10, "desc": "TM 단독 — 캐싱키 수렴/CF 히트 검증"},
    "P2": {"live": 8, "tm": 2,  "desc": "혼합 부하 — Live 우선 + TM 캐시 유지"},
    "P3": {"live": 10, "tm": 0, "desc": "Live 단독 — 기준선 측정"},
}

# ---------------------------------------------------------------------------
# Load Shape Stages (CCU ramp-up plan)
# Each stage: (duration_sec, target_users, spawn_rate)
#   duration_sec: how long this stage lasts
#   target_users: target CCU at the end of this stage
#   spawn_rate:   users spawned per second during ramp
# ---------------------------------------------------------------------------
LOAD_STAGES = [
    # Stage 1: Warm-up  0 → 100K
    {"duration": 300,  "users": 100000,  "spawn_rate": 500,  "label": "S1 Warm-up"},
    # Stage 2: Stabilize 100K → 300K
    {"duration": 300,  "users": 300000,  "spawn_rate": 1000, "label": "S2 Stabilize"},
    # Stage 3: Peak     300K → 500K
    {"duration": 300,  "users": 500000,  "spawn_rate": 1000, "label": "S3 Peak"},
    # Stage 4: Sustain  500K hold
    {"duration": 600,  "users": 500000,  "spawn_rate": 0,    "label": "S4 Sustain"},
]

# Enable/disable shape class (set False to use manual Web UI control)
USE_LOAD_SHAPE = os.getenv("USE_LOAD_SHAPE", "false").lower() in ("true", "1", "yes")

# ---------------------------------------------------------------------------
# PASS/FAIL criteria
# ---------------------------------------------------------------------------
PASS_CRITERIA = {
    "cache_hit_rate":   {"pass": 95.0, "fail": 80.0, "unit": "%",  "label": "Cache Hit Rate"},
    "hit_avg_latency":  {"pass": 100,  "fail": 500,  "unit": "ms", "label": "HIT avg 응답속도"},
    "miss_avg_latency": {"pass": 500,  "fail": 2000, "unit": "ms", "label": "MISS avg 응답속도"},
    "error_rate":       {"pass": 0.1,  "fail": 1.0,  "unit": "%",  "label": "에러율"},
}
