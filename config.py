import os
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Base URL 구성 — Locust --host 또는 환경변수로 유연하게 변경 가능
#
# 사용법:
#   1) locust --host https://your-cf.cloudfront.net  (+ STREAM_PATH 환경변수)
#   2) BASE_URL=https://your-cf.cloudfront.net/out/v1/ch/vc12 locust
#   3) 아무것도 안 주면 기본값 사용
#
# BASE_URL = scheme + host + path (full URL, 하위 호환)
# STREAM_PATH = path 부분만 (Locust --host와 조합 시 사용)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# CF bypass path — SSAI(MediaTailor)를 우회해서 ALB→openresty→MediaPackage 경로 측정
#   - sogne-live.tving.com 은 MediaTailor SSAI → Cache-Control: no-store (캐시 불가)
#   - d3sllikso8yl5i.cloudfront.net + Host: aws-kbo-smart-cloudfront.tving.com
#     → ALB listener rule 매칭 → openresty(max-age=1) → MediaPackage (HIT 가능)
# ---------------------------------------------------------------------------
_DEFAULT_BASE = "https://d3sllikso8yl5i.cloudfront.net/out/v1/kbo2026/kbo_t3_01/vc12"

BASE_URL = os.getenv("BASE_URL", _DEFAULT_BASE)

# 스트림 경로만 분리 (Locust --host 기준 상대경로로 사용)
STREAM_PATH = os.getenv("STREAM_PATH", urlparse(BASE_URL).path)

# CF → ALB 라우팅을 위한 Host 헤더 오버라이드 (빈 문자열이면 비활성)
# 이 값 없이 CF 호출 시 ALB listener가 거부 → 502 Bad Gateway
HOST_HEADER = os.getenv("HOST_HEADER", "aws-kbo-smart-cloudfront.tving.com")

# Legacy — still used for master playlist fetch
BASE_PLAYLIST_URL = f"{BASE_URL}/playlist.m3u8"

# ---------------------------------------------------------------------------
# 인증 토큰 — 모든 요청에 ?token=... 으로 붙음 (UI에서 변경 가능)
# ---------------------------------------------------------------------------
AUTH_TOKEN = os.getenv(
    "AUTH_TOKEN",
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJleHAiOjE3Nzc1MjE1OTksInN1YiI6IioiLCJkbXQiOiJzdCJ9.4LTY8wST8hjdWuHuKREf5H5TvYlOK9ZVKnzHTlDfHHo",
)

# ---------------------------------------------------------------------------
# TimeMachine 파라미터 포맷
#   "minus"  → ?token=...&minus=300         (sogne 방식)
#   "aws"    → ?aws.manifestsettings=time_delay:300  (MediaPackage 방식)
# ---------------------------------------------------------------------------
TM_PARAM_MODE = os.getenv("TM_PARAM_MODE", "aws")  # "minus"=sogne-live SSAI, "aws"=MediaPackage direct

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

# Average request cycle time (seconds) — used to calculate target RPS
# = playlist refresh + wait_time (constant_pacing) + request overhead
# LiveUser/TimeMachineUser use constant_pacing(1), so wait_time ≈ 1.0
AVG_CYCLE_SEC = PLAYLIST_REFRESH_INTERVAL + 1.0 + 0.1

# ---------------------------------------------------------------------------
# Response-time bucket config (1초 단위 막대차트용)
# "1초 이내 / 1~2초 / 2~3초 / 3~4초 / 4초+" 로 master/child m3u8 응답 분포 집계
# ---------------------------------------------------------------------------
RESP_BUCKETS_MS = [1000, 2000, 3000, 4000]
RESP_BUCKET_LABELS = ["≤1s", "1~2s", "2~3s", "3~4s", "4s+"]

# ---------------------------------------------------------------------------
# Renditions — MediaPackage V2 output 기준
# name = master playlist에서 파싱된 렌디션 인덱스 (자동 매칭)
# master playlist가 제공하는 렌디션만 실제 사용됨 (없으면 fallback)
# ---------------------------------------------------------------------------
RENDITIONS = [
    {"name": "1",  "codec": "AVC",  "res": "1920x1080", "bw": "5.0Mbps",  "weight": 5,  "label": "AVC 1080p 5.0M"},
    {"name": "2",  "codec": "AVC",  "res": "1600x900",  "bw": "3.5Mbps",  "weight": 5,  "label": "AVC 900p 3.5M"},
    {"name": "3",  "codec": "AVC",  "res": "1280x720",  "bw": "2.4Mbps",  "weight": 15, "label": "AVC 720p 2.4M"},
    {"name": "4",  "codec": "AVC",  "res": "960x540",   "bw": "1.75Mbps", "weight": 10, "label": "AVC 540p 1.75M"},
    {"name": "5",  "codec": "AVC",  "res": "640x360",   "bw": "1.3Mbps",  "weight": 10, "label": "AVC 360p 1.3M"},
    {"name": "6",  "codec": "AVC",  "res": "480x270",   "bw": "1.0Mbps",  "weight": 5,  "label": "AVC 270p 1.0M"},
    {"name": "7",  "codec": "AVC",  "res": "480x270",   "bw": "760Kbps",  "weight": 5,  "label": "AVC 270p 760K"},
    {"name": "8",  "codec": "HEVC", "res": "1280x720",  "bw": "1.86Mbps", "weight": 10, "label": "HEVC 720p 1.86M"},
    {"name": "9",  "codec": "HEVC", "res": "960x540",   "bw": "1.3Mbps",  "weight": 5,  "label": "HEVC 540p 1.3M"},
    {"name": "10", "codec": "HEVC", "res": "640x360",   "bw": "760Kbps",  "weight": 5,  "label": "HEVC 360p 760K"},
    {"name": "11", "codec": "AVC",  "res": "1920x1080", "bw": "9.0Mbps",  "weight": 5,  "label": "AVC 1080p 9.0M"},
]

# Master playlist 세션 갱신 주기 (초) — 세션 URL은 시간 제한이 있을 수 있음
SESSION_REFRESH_INTERVAL = int(os.getenv("SESSION_REFRESH_INTERVAL", "60"))

# 세그먼트 CF 캐시 샘플링 — rendition m3u8에서 세그먼트 URL을 HEAD 요청하여 HIT/MISS 측정
# 매 사이클마다 N개 세그먼트를 HEAD 요청 (0이면 비활성)
SEGMENT_CACHE_SAMPLE = int(os.getenv("SEGMENT_CACHE_SAMPLE", "0"))

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
    # Stage 1: Warm-up  0 → 2K       (4코어/16GB 기준)
    {"duration": 120,  "users": 2000,   "spawn_rate": 50,   "label": "S1 Warm-up"},
    # Stage 2: Stabilize 2K → 5K
    {"duration": 180,  "users": 5000,   "spawn_rate": 100,  "label": "S2 Stabilize"},
    # Stage 3: Peak     5K → 10K
    {"duration": 180,  "users": 10000,  "spawn_rate": 100,  "label": "S3 Peak"},
    # Stage 4: Sustain  10K hold
    {"duration": 600,  "users": 10000,  "spawn_rate": 0,    "label": "S4 Sustain"},
]

# Enable/disable shape class (set False to use manual Web UI control)
USE_LOAD_SHAPE = os.getenv("USE_LOAD_SHAPE", "false").lower() in ("true", "1", "yes")

# ---------------------------------------------------------------------------
# PASS/FAIL criteria
# ---------------------------------------------------------------------------
PASS_CRITERIA = {
    # SSAI 환경 — master/child m3u8는 세션별 URL이라 캐시 공유 불가 → latency 백분위가 핵심 KPI
    "master_p50":       {"pass": 150,  "fail": 500,  "unit": "ms", "label": "Master p50"},
    "master_p95":       {"pass": 400,  "fail": 1000, "unit": "ms", "label": "Master p95"},
    "master_p99":       {"pass": 800,  "fail": 2000, "unit": "ms", "label": "Master p99"},
    "child_p50":        {"pass": 150,  "fail": 500,  "unit": "ms", "label": "Child p50"},
    "child_p95":        {"pass": 400,  "fail": 1000, "unit": "ms", "label": "Child p95"},
    "child_p99":        {"pass": 800,  "fail": 2000, "unit": "ms", "label": "Child p99"},
    "error_rate":       {"pass": 0.1,  "fail": 1.0,  "unit": "%",  "label": "에러율"},
    # segment 기준 (CDN 캐시) — 참고 지표로 유지
    "cache_hit_rate":   {"pass": 95.0, "fail": 80.0, "unit": "%",  "label": "Segment Cache Hit Rate"},
    "hit_avg_latency":  {"pass": 100,  "fail": 500,  "unit": "ms", "label": "HIT avg 응답속도"},
    "miss_avg_latency": {"pass": 500,  "fail": 2000, "unit": "ms", "label": "MISS avg 응답속도"},
}
