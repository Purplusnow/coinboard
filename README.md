# COIN BOARD

최종 업데이트: 2026-08-10

업비트 KRW 마켓을 실시간으로 받아 추천 종목을 **전광판**으로 띄우는 GitHub Pages 정적 사이트.
서버 없음, API 키 없음, 비용 0원.

```
GitHub Actions (5분마다)                브라우저 (정적 페이지)
  ├ /ticker/all  1회                      ├ board.json  ──→ 추천 목록·스코어·근거
  ├ /candles     116회 (10req/s)          └ WebSocket   ──→ 초 단위 체결가·등락률
  ├ 지표 계산 (EMA/RSI/ATR/거래대금)
  └ board.json  →  data 브랜치 force-push
```

## 왜 이 구조인가

업비트 REST는 **Origin 헤더 유무로 쿼터 그룹이 달라진다.** 실측:

| 호출 주체 | 쿼터 그룹 | 한도 | 실측 결과 |
|---|---|---|---|
| 브라우저 (Origin 있음) | `group=origin` | 사실상 초당 1회 | 1회 성공 후 **연속 429** |
| Actions/서버 (Origin 없음) | `group=candles` | 600/분 · 10/초 | 15회 연속 전부 200 |

즉 **브라우저에서 116개 종목의 캔들을 긁어 지표를 계산하는 건 불가능**하다.
그래서 무거운 분석은 Actions에서 돌리고, 브라우저는 결과 JSON + WebSocket만 쓴다.

WebSocket(`wss://api.upbit.com/websocket/v1`)은 인증도 CORS 제약도 없어서
정적 페이지에서 그대로 붙는다. 이게 이 프로젝트가 성립하는 이유다.

## 스코어링

116개 유니버스 안에서 **횡단면 백분위**로 각 지표를 0~100 환산한 뒤 가중합한다.
절대 임계값을 쓰면 시장 전체가 오른 날 모두가 만점을 받아 변별력이 사라진다.

| 지표 | 가중 | 의도 |
|---|---|---|
| 거래대금 급증 (최근 3h / 직전 72h) | 26% | 신규 자금 유입. 가장 선행하는 신호 |
| 추세 정렬 (종가>EMA20>EMA60) | 18% | 역추세 반등 노이즈 배제 |
| 4시간 수익률 | 16% | 단기 모멘텀 |
| 24시간 수익률 | 16% | 중기 모멘텀 |
| RSI 품질 | 12% | 60 부근 최고점, 과매수 급감점 |
| 변동성 대비 효율 (수익/ATR) | 12% | 덜 흔들리며 오른 쪽 우대 |

추가 규칙:
- `RSI ≥ 82` → 점수 ×0.75, `24h ≥ +45%` → ×0.85 (추격매수 방지)
- 업비트 **투자유의 종목 제외**, 유의환기 종목은 배지 표시
- 유니버스는 24h 거래대금 3억+ (통계 표본 확보), **추천 노출은 10억+** 만 (슬리피지·작전 위험)

근거 태그는 고정 문구가 아니라 *그 종목이 유니버스 대비 가장 앞선 지표*를 실제 수치와 함께 뽑는다.

## 배포

1. 이 디렉터리를 GitHub 저장소로 push (public 권장 — Actions 무료 무제한)
2. **Settings → Pages** → Source: `Deploy from a branch`, Branch: `main` / `/docs`
3. **Settings → Actions → General** → Workflow permissions: `Read and write`
4. Actions 탭에서 `scan` 워크플로 1회 수동 실행 → `data` 브랜치 생성 확인
5. [docs/config.js](docs/config.js) 의 `BOARD_DATA_URL` 을 본인 저장소 raw URL로 교체

```js
window.BOARD_DATA_URL =
  "https://raw.githubusercontent.com/<OWNER>/<REPO>/data/board.json";
```

5번을 건너뛰어도 저장소에 커밋된 `docs/data/board.json` 으로 동작한다(갱신은 안 됨).
raw URL이 실패하면 자동으로 이 사본으로 폴백한다.

> `data` 브랜치는 매번 히스토리 없는 단일 커밋으로 force-push 한다.
> 5분마다 갱신해도 저장소 용량이 늘지 않는다.

## 로컬 실행

```bash
python3 tools/scan.py            # docs/data/board.json 생성 (약 30초)
python3 -m http.server 8899 --directory docs
open http://127.0.0.1:8899
```

의존성 없음 (Python 표준 라이브러리만).

## 알아둘 것

- **Actions cron은 5분이 최소 간격이고, 혼잡할 때 수 분 밀린다.** 추천 스코어는 5~15분 지연을 전제로 한다.
  다만 화면의 가격·등락률은 WebSocket이라 실제로 초 단위다.
- 60일간 저장소 활동이 없으면 GitHub가 스케줄 워크플로를 자동 비활성화한다.
- 스코어링은 **백테스트로 검증된 전략이 아니다.** 현재는 기술적 지표의 횡단면 랭킹일 뿐이며,
  수익률 검증은 하지 않았다. 실전 판단 근거로 쓰려면 별도 백테스트가 필요하다.

## 고지

공개 시세를 기계적으로 가공한 **정보 제공** 화면이다. 투자 자문·권유가 아니며
매매 판단과 결과의 책임은 이용자 본인에게 있다.
불특정 다수에게 공개 운영할 경우 국내법상 유사투자자문업 신고 대상이 될 수 있다.
