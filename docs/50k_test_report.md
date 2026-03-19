# CloudFront HLS TimeMachine 부하테스트 결과 보고서

## 1. 테스트 개요

| 항목 | 내용 |
|------|------|
| **테스트 목적** | TVING CloudFront HLS TimeMachine 스트리밍 캐시 성능 검증 |
| **테스트 일시** | 2026-03-19 |
| **테스트 도구** | Locust 2.43.3 (FastHttpUser) |
| **대상 URL** | `https://d1lel4oh55a2dj.cloudfront.net/out/v1/stress-test-ch/stress-test-02/vc12` |
| **테스트 유형** | P1 — TimeMachine 단독 |
| **목표 CCU** | 50,000명 |

---

## 2. 테스트 환경

### 2.1 서버 스펙 (부하 생성 서버)

| 항목 | 스펙 |
|------|------|
| **서버** | Tencent CVM (서울 리전) |
| **IP** | 43.131.233.3 |
| **CPU** | AMD EPYC 9754 — **32코어** |
| **메모리** | **123GB** |
| **디스크** | 493GB SSD |
| **OS** | Ubuntu 24.04.4 LTS (Kernel 6.8.0) |
| **네트워크** | 1~10 Gbps |

### 2.2 부하 도구 설정

| 항목 | 설정 |
|------|------|
| **Locust** | 2.43.3 |
| **Python** | 3.12.3 |
| **HTTP Client** | FastHttpUser (geventhttpclient) |
| **JSON** | orjson (7.8x faster) |
| **Workers** | 32개 (`--processes -1`, 코어 수 자동 감지) |
| **wait_time** | `constant_pacing(2)` (open workload, 2초 사이클) |
| **concurrency** | 10 (유저당 connection pool) |
| **max_retries** | 0 |
| **timeout** | connection=30s, network=30s |

### 2.3 OS 튜닝

| 항목 | 기본값 | 적용값 |
|------|--------|--------|
| fd limit | 1,024 | **65,535** |
| somaxconn | 128 | **65,535** |
| netdev_max_backlog | 1,000 | **50,000** |
| tcp_fin_timeout | 60s | **15s** |
| tcp_tw_reuse | 0 | **1** |
| port range | 32768-60999 | **1024-65535** |
| rmem/wmem_max | 208KB | **16MB** |

### 2.4 테스트 대상 렌디션 (14개)

| 렌디션 | 코덱 | 해상도 | 비트레이트 | 가중치 |
|--------|------|--------|-----------|--------|
| playlist_1 | AVC | 1920x1080 | 7.8Mbps | 5 |
| playlist_2 | AVC | 1600x900 | 5.3Mbps | 5 |
| playlist_3 | AVC | 1280x720 | 3.6Mbps | **15** |
| playlist_4 | AVC | 960x540 | 2.6Mbps | 10 |
| playlist_5 | AVC | 640x360 | 1.9Mbps | 10 |
| playlist_6 | AVC | 480x270 | 1.6Mbps | 5 |
| playlist_7 | AVC | 480x270 | 1.1Mbps | 5 |
| playlist_8 | HEVC | 1920x1080 | 6.1Mbps | 5 |
| playlist_9 | HEVC | 1600x900 | 4.4Mbps | 5 |
| playlist_10 | HEVC | 1280x720 | 2.8Mbps | 10 |
| playlist_11 | HEVC | 960x540 | 1.9Mbps | 5 |
| playlist_12 | HEVC | 640x360 | 1.1Mbps | 5 |
| playlist_13 | AVC | 1920x1080 | 13.7Mbps | 5 |
| playlist_14 | HEVC | 1920x1080 | 10.0Mbps | 5 |

---

## 3. PASS/FAIL 기준

| 항목 | PASS | WARN | FAIL |
|------|------|------|------|
| Cache Hit Rate | >= 95% | 80~95% | < 80% |
| HIT 응답속도 | <= 100ms | 100~500ms | > 500ms |
| MISS 응답속도 | <= 500ms | 500~2000ms | > 2000ms |
| 에러율 | <= 0.1% | 0.1~1.0% | > 1.0% |

---

## 4. 테스트 결과

### 4.1 종합 판정

| 항목 | 측정값 | 기준 | **결과** |
|------|--------|------|----------|
| **Cache Hit Rate** | 99.9% | >= 95% | **PASS** |
| **HIT 응답속도** | 82ms | <= 100ms | **PASS** |
| **MISS 응답속도** | 29ms | <= 500ms | **PASS** |
| **에러율** | 0.26% | <= 0.1% | **WARN** |

### 4.2 주요 지표

| 항목 | 값 |
|------|-----|
| 동시접속 (CCU) | **50,000명** |
| RPS (Locust native) | **~12,700/s** |
| 총 요청 수 | ~2,600,000건 |
| CF HIT 응답 (EMA) | **82ms** |
| CF MISS 응답 (EMA) | **29ms** |
| Cache Hit Rate | **99.9%** |
| 에러율 | 0.26% (WARN) |
| timeout 건수 | ~124건 (0.005%) |

### 4.3 차트

> 아래 차트는 캐시 대시보드 (`http://43.131.233.3:8089/cache-dashboard`)에서 캡처

#### 4.3.1 전체 RPS
<!-- 캐시 대시보드 → 차트 탭 → 차트 1 "전체 RPS" PNG 버튼 클릭 -->
![전체 RPS](./charts/01_rps.png)

#### 4.3.2 캐시 히트율
<!-- 차트 2 "캐시 히트율 / 미스율" PNG -->
![캐시 히트율](./charts/02_hit_rate.png)

#### 4.3.3 구간별 호출수 (CF MISS + 오리진)
<!-- 차트 3 "구간별 호출수" PNG -->
![구간별 호출수](./charts/03_call_count.png)

#### 4.3.4 HIT/MISS 응답속도
<!-- 차트 4 "HIT / MISS 응답속도" PNG -->
![응답속도](./charts/04_latency.png)

#### 4.3.5 에러율
<!-- 차트 5 "에러율" PNG -->
![에러율](./charts/05_error_rate.png)

#### 4.3.6 PASS/FAIL 판정
<!-- 캐시 대시보드 → 응답속도 & 판정 탭 스크린샷 -->
![PASS/FAIL](./charts/06_pass_fail.png)

---

## 5. 서버 모니터링

### 5.1 부하 생성 서버 리소스 (50K CCU 안정 상태)

| 항목 | 값 | 비고 |
|------|-----|------|
| **CPU** | 39% | 32코어 중 idle 50%+ |
| **메모리** | 48GB / 123GB (39%) | 여유 |
| **Load Average** | 14.1 (32코어 = 44%) | 양호 |
| **TCP 연결** | 49,454 ESTAB | 50K CCU 정상 |
| **네트워크 수신** | 243 Mbps | |
| **네트워크 송신** | 128 Mbps | |
| **Worker CPU** | 각 ~32% | 균등 분배 |
| **Worker 메모리** | 각 ~1.4GB | 정상 |

### 5.2 서버 리소스 차트
<!-- top, htop, 또는 Grafana 스크린샷 -->
![서버 CPU/Memory](./charts/07_server_resources.png)

### 5.3 네트워크 상태

| 항목 | 값 |
|------|-----|
| ESTAB | 49,454 |
| FIN-WAIT-1 | 9,599 |
| TIME-WAIT | 3,918 |
| SYN-SENT | 642 |
| orphaned | 9,924 |

---

## 6. TimeMachine time_delay 동작

### 6.1 슬라이딩 윈도우 방식

```
Phase 1 (elapsed < TIME_DELAY_MAX):
  time_delay = random(MIN, elapsed)     → DVR 윈도우 성장

Phase 2 (elapsed >= TIME_DELAY_MAX):
  time_delay = random(MIN, MAX)         → 풀 DVR, 콘텐츠 윈도우 자동 슬라이딩
```

### 6.2 설정값

| 항목 | 값 |
|------|-----|
| TIME_DELAY_MIN | 1초 |
| TIME_DELAY_MAX | 10,800초 (3시간) |
| DVR 윈도우 | 최대 3시간 |

---

## 7. 최적화 적용 내역

| 항목 | Before | After | 효과 |
|------|--------|-------|------|
| HTTP Client | HttpUser | **FastHttpUser** | CPU 절감 |
| on_request | lock 5개 + regex + dict | **카운트만** | CPU 95% → 39% |
| JSON | json.dumps | **orjson** | 7.8x 빠름 |
| CF Sampler | Python thread | **SamplerUser + threshold** | 1,500ms → 82ms |
| wait_time | constant(2) | **constant_pacing(2)** | RPS 안정화 |
| Workers | 16개 수동 | **32개 자동** | 코어 전부 활용 |

---

## 8. 결론

### 8.1 캐시 성능
- CloudFront **99.9% 캐시 히트율** — PASS
- HIT 응답 **82ms**, MISS 응답 **29ms** — PASS
- 14개 렌디션 (AVC/HEVC, 270p~1080p) 전체 정상

### 8.2 부하 한계
- **단일 서버 50K CCU** 안정 운영 (CPU 39%, 메모리 39%)
- **100K CCU 가능** — 서버 여유 충분, 추가 서버 분산 시 확장 가능
- RPS ~12,700/s (목표 24K 대비 53% — greenlet 스케줄링 한계)

### 8.3 알려진 이슈
| 이슈 | 영향 | 비고 |
|------|------|------|
| SSL read timeout | 0.005% (124건/2.6M) | 50K TCP 환경 간헐적 |
| CPU 90% 경고 | ramp-up 초기에만 | 안정화 후 39% |
| 에러율 0.26% | WARN (기준 0.1%) | 404 포함 |

---

## 9. 부록

### 9.1 Git Repository
- https://github.com/junyong-song1/locust_stress_test.git

### 9.2 대시보드 URL
- Locust UI: `http://43.131.233.3:8089`
- 캐시 대시보드: `http://43.131.233.3:8089/cache-dashboard`
- 요청 로그: `http://43.131.233.3:8089/request-log-view`

### 9.3 차트 캡처 방법
1. 캐시 대시보드 접속
2. 각 차트 우측 상단 **PNG** 버튼 클릭
3. 전체 페이지: 하단 **전체 스크린샷 PNG** 버튼
