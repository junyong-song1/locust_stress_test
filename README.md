# TVING CloudFront HLS Stress Test

TVING OTT 플랫폼의 CloudFront HLS 타임머신(TimeMachine) 스트리밍 캐시 성능을 측정하는 부하 테스트 도구.

## 주요 기능

- **FastHttpUser** 기반 고성능 부하 생성 (geventhttpclient)
- **Live + TimeMachine** 듀얼 모드 사용자 시뮬레이션
- **14개 렌디션** 가중치 랜덤 선택 (AVC/HEVC, 270p~1080p)
- **실시간 캐시 대시보드** — HIT/MISS율, 응답속도, PASS/FAIL 판정
- **요청 로그** — CF 캐시 상태, POP, PDT, 응답시간 (샘플링)
- **분산 모드** — `--processes -1` (전체 코어 자동 활용)
- **Phase 프리셋** — P1(TM Only), P2(Mixed), P3(Live Only) 실시간 전환
- **CF Sampler** — SamplerUser 기반 CF 실제 응답속도 측정 (threshold 필터링 + EMA)
- **orjson** — JSON 직렬화 7.8x 고속화

## 파일 구조

```
locustfile.py      # 메인 (User 클래스, 대시보드, 분산 동기화)
config.py          # 설정 (렌디션, Phase, PASS/FAIL 기준값)
cache_metrics.py   # CF 캐시 메트릭 수집 (CacheMetricsCollector)
hls_client.py      # m3u8 파싱 유틸리티
requirements.txt   # Python 의존성
cf-sampler/        # Go CF Sampler (참고용, 현재 미사용)
docs/              # 테스트 보고서
```

## 실행 방법

### 설치

```bash
python3 -m venv venv
source venv/bin/activate
pip install locust geventhttpclient m3u8 orjson
```

### 단일 모드

```bash
locust --host https://your-cloudfront-url.com
```

### 분산 모드 (권장)

```bash
# 전체 코어 자동 활용 (master 1 + worker N)
ulimit -n 65535
locust --processes -1 --host https://your-cloudfront-url.com
```

### 멀티 서버 분산

```bash
# 서버 A (master)
locust --master --host https://your-cloudfront-url.com

# 서버 B (worker만, locustfile 자동 전달)
locust -f - --worker --master-host=<서버A IP> --processes -1
```

### 서버 OS 튜닝 (50K+ CCU)

```bash
# fd limit
ulimit -n 65535

# TCP 최적화
sudo sysctl -w net.core.somaxconn=65535
sudo sysctl -w net.core.netdev_max_backlog=50000
sudo sysctl -w net.ipv4.tcp_fin_timeout=15
sudo sysctl -w net.ipv4.ip_local_port_range="1024 65535"
sudo sysctl -w net.ipv4.tcp_tw_reuse=1
sudo sysctl -w net.core.rmem_max=16777216
sudo sysctl -w net.core.wmem_max=16777216
```

## 대시보드

- **Locust UI**: `http://<host>:8089`
- **캐시 대시보드**: `http://<host>:8089/cache-dashboard`
- **요청 로그**: `http://<host>:8089/request-log-view`

### 대시보드 차트

| 차트 | 내용 |
|------|------|
| 1. 전체 RPS | 실측 vs 목표 RPS |
| 2. 캐시 히트율 | HIT/MISS 비율 추이 |
| 3. 구간별 호출수 | CF MISS + 오리진 도달 호출수 |
| 4. 응답속도 | CF HIT/MISS 응답시간 (Sampler EMA + Locust 측정) |
| 5. 에러율 | 4xx/5xx 모니터링 |

### PASS/FAIL 기준

| 항목 | PASS | WARN | FAIL |
|------|------|------|------|
| Cache Hit Rate | >= 95% | 80~95% | < 80% |
| HIT 응답속도 | <= 100ms | 100~500ms | > 500ms |
| MISS 응답속도 | <= 500ms | 500~2000ms | > 2000ms |
| 에러율 | <= 0.1% | 0.1~1.0% | > 1.0% |

## 아키텍처

### on_request 경량화

Locust 공식 예제(`web_ui_cache_stats.py`) 패턴 적용:
- `on_request` 리스너: **cache hit/miss 카운트만** (lock 0개)
- `_watch_stream`: resp_tracker, period_tracker, origin_tracker, request_log 직접 처리
- request_log: **10번에 1번 샘플링**

### CF Sampler (SamplerUser)

| 항목 | 설명 |
|------|------|
| 방식 | SamplerUser (FastHttpUser, fixed_count=1) |
| 연결 | Locust **connection pool 공유** (추가 TCP 없음) |
| 필터링 | HIT >300ms, MISS >400ms 버림 (greenlet 노이즈 제거) |
| 스무딩 | EMA (alpha=0.3) |
| 분산 동기화 | worker→master raw 값 전달 → master EMA 적용 |

### 분산 모드 데이터 동기화

Worker → Master 커스텀 데이터 전달:
- `report_to_master`: cache_stats, resp_tracker, period_tracker, cf_sampler 전송 후 리셋
- `worker_report`: Master에서 weighted average 병합
- `_is_master()`: `--processes` 모드에서도 MasterRunner 자동 감지

### TimeMachine time_delay 슬라이딩 윈도우

```
Phase 1 (elapsed < TIME_DELAY_MAX):
  time_delay = random(MIN, elapsed)    DVR 윈도우 성장

Phase 2 (elapsed >= TIME_DELAY_MAX):
  time_delay = random(MIN, MAX)        풀 DVR, 콘텐츠 윈도우 자동 슬라이딩
```

### User 클래스 설정

| 항목 | 값 |
|------|-----|
| wait_time | `constant_pacing(2)` (open workload) |
| concurrency | 10 (connection pool) |
| max_retries | 0 |
| max_redirects | 0 |
| connection_timeout | 30초 |
| network_timeout | 30초 |

## 환경 변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `BASE_URL` | CF URL | CloudFront 베이스 URL |
| `TIME_DELAY_MIN` | 1 | 타임딜레이 최소 (초) |
| `TIME_DELAY_MAX` | 10800 | 타임딜레이 최대 (초, 3시간) |
| `LIVE_USER_WEIGHT` | 5 | 라이브 유저 가중치 |
| `TIME_MACHINE_USER_WEIGHT` | 5 | 타임머신 유저 가중치 |
| `FETCH_SEGMENTS` | false | 세그먼트 fetch 여부 |
| `USE_LOAD_SHAPE` | false | 자동 ramp-up 사용 |
| `CACHE_REPORT_INTERVAL` | 1.0 | 콘솔 리포트 주기 (초) |

## 테스트 결과

테스트 종료 시 `results/` 디렉토리에 JSON 자동 저장. 상세 보고서: [docs/50k_test_report.md](docs/50k_test_report.md)

### 50K CCU (32코어 단일 서버, 2026-03-19)

| 항목 | 값 | 판정 |
|------|-----|------|
| CCU | **50,000명** | |
| 총 요청 수 | **11,052,135건** | |
| Cache Hit Rate | **99.9%** | **PASS** |
| HIT 응답속도 (CF Sampler EMA) | **53.4ms** | **PASS** |
| MISS 응답속도 (CF Sampler EMA) | **30.8ms** | **PASS** |
| 에러율 (안정 구간) | **0.0%** | **PASS** |
| RPS (안정 구간 평균) | **12,851/s** | |
| CPU | **38%** | 여유 |
| 메모리 | **49GB / 123GB** | 여유 |
| 테스트 시간 | 14분 38초 | |
