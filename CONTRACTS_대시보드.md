# CONTRACTS_대시보드.md — 다른 영역이 쓰는 인터페이스 중 ★바꾸고 싶지만 안 바꾼 것★

작성: 대시보드 세션, 2026-09-12. 기존 인터페이스는 전부 ★그대로 유지★ 했다.
한 항목 = 대상 영역 / 현재 형태 / 원하는 형태 / 이유.

---

## 1. 매크로 ack 에 상태·사유를 실을 수 있게 (유실·거부를 대시보드에 보이기)
> **처리 완료: 2026-09-12 양쪽 구현(서버 ack_cmd · 매크로 notify_dropped). INTEGRATION_LOG C1**
- 대상 영역: 아이온2 매크로 (`src/lc/report_module.py`, `loot.py`)
- 현재 형태: `POST /command/{pc}/ack/{id}` 본문 없음 → 서버는 무조건 `acked`. WS 는 `{"type":"ack","command_id":N}`.
- 원하는 형태: 본문(또는 WS 프레임)에 선택 필드 `{"status":"acked"|"cancelled"|"rejected", "why":"..."}`. 없으면 지금처럼 `acked`. 서버는 `cancelled`/`rejected` 를 `status` 칼럼에 그대로 적고 이력 방송 → 대시보드가 이미 `cancelled` 를 ⛔ 로 그린다.
- 이유: 매크로가 「이전 명령 처리 중」으로 거부하거나 오버라이드로 큐를 버릴 때 서버·대시보드가 그걸 ★전혀 못 본다★(사고 523·NOW.md 0-Q). 서버 쪽은 하위 호환으로 받을 준비를 할 수 있고, 매크로가 보내기 시작하면 바로 보인다.

## 2. 파섹 주소록 쓰기 인증을 API 키에서 떼기
> **처리 완료: 2026-09-13 세션 로그인만(API 키·토큰 401). 도구 parsec_multi.py 가 비번으로 로그인해 보낸다. INTEGRATION_LOG D5**
- 대상 영역: 관제컴 도구 `src/updater/parsec_multi.py` (대시보드 영역이 아님 — 팜뷰/운영 쪽)
- 현재 형태: `POST /parsec/map` 을 `X-Api-Key`(매크로 공용 키)로 부른다. 서버는 `check_api_key or check_session` 둘 다 허용.
- 원하는 형태: 세션 쿠키(대시보드 로그인) 또는 쓰기 전용 토큰(`X-Parsec-Token`, 서버 env). `GET` 은 그대로.
- 이유: 매크로 API 키는 공개 exe 에 각인돼 유출 전제다. 그 키로 주소록을 덮으면 다음 `switch_launcher` 가 ★남의 호스트★ 로 파섹 접속을 시도한다. 서버가 먼저 바꾸면 이 도구가 깨지므로 도구가 새 인증을 쓰기 시작한 뒤에 서버가 API 키 경로를 닫는다.

## 3. FarmView 일괄 명령의 `pc:"all"` 을 서버가 거부하게
> **처리 완료: 2026-09-12 맨몸 all 400 · 8대↑ `confirm_fleet`(FV_FLEET_N) · 팜뷰 `send_cmd` 가 싣는다. 시험 I4·J2. INTEGRATION_LOG B3**
- 대상 영역: 팜뷰 (`src/farmview/fvdash.py` `/dash/cmd`, `index.html` confirm)
- 현재 형태: `POST /api/fv/command {"cmd":"stop","pc":"all"}` 이면 서버가 카드 목록으로 펼쳐 한 대씩 보낸다. 응답 `ok` 는 이제 ★전부 성공일 때만★ true, 부분 성공은 `partial:true`(2026-09-12 변경, 하위 호환: 키 추가만).
- 원하는 형태: 팜뷰가 목록을 펼쳐 `pc:["PC-01","PC-02",...]` 로 보내고, 서버는 `"all"` 을 400 으로 거부. 대상 수 상한(예: 24)과 `confirm_fleet:true` 필드.
- 이유: A7(함대 전체 명령 금지)이 대시보드 큐에는 걸려 있는데 FV 경로는 토큰 하나로 72장이 나간다. 판정이 서버 한 곳에 있어야 「글자는 지키고 뜻은 어기는」 우회가 없다. 팜뷰 UI 가 confirm 을 두고 있어 지금 당장 위험하진 않다.

## 4. FarmView snapshot 의 `raw` 제거 또는 옵션화
> **처리 완료: 2026-09-12 `?raw=1` 일 때만. FV_API.md 갱신. 시험 I3. INTEGRATION_LOG B4**
- 대상 영역: 팜뷰 (`fvdash.py` — 받자마자 `c.pop("raw")`)
- 현재 형태: 카드마다 `raw`(59필드, 응답의 약 53%, 계정 이메일·전화·peer_id·lan_url 포함)를 항상 보낸다.
- 원하는 형태: 기본에서 빼고 `?raw=1` 로만. 또는 화이트리스트(`last_active`·`acct_nick`·`_rot`).
- 이유: 팜뷰가 받자마자 버리므로 폴링 10초마다 182KB 를 만들고 압축하고 버린다. 정적 토큰 하나로 열리는 경로에 PII 를 실을 이유가 없다. `FV_API.md:166` 의 「안전망」 문장도 같이 고쳐야 한다.

## 5. FarmView 값 뜻 정정 (이번에 서버가 고침 — 팜뷰가 알아야 할 것)
> **처리 완료: 2026-09-12 팜뷰 `pull_events` 가 truncated 를 이어 읽는다(최대 5장, 시험 J3). `slots_today` 재계산은 같은 규칙이라 유지. INTEGRATION_LOG B5**
- 대상 영역: 팜뷰
- 현재 형태(2026-09-12 이후): `today.slots_done` = `completed && today!==false` (대시보드와 같은 규칙, 어제 완주를 오늘로 안 센다). `global.totals.bugs` = ★물리 PC 단위★ 합(카드 합산 3배 부풀림 제거). `/api/fv/events` 는 잘릴 때 경계 초를 통째로 다음 장으로 미룬다(같은 초 유실 없음).
- 원하는 형태: 팜뷰가 자체 재계산(`slots_today`, PC 단위 bugs)을 걷어내고 서버 값을 그대로 써도 된다. `truncated:true` 면 쉬지 말고 다음 장을 부른다(문서 규약인데 지금 안 지킨다).
- 이유: 같은 값을 두 곳에서 다른 규칙으로 세면 답이 갈린다(실제 사고: 던전 탭만 분모 840). 서버가 정본이다.
