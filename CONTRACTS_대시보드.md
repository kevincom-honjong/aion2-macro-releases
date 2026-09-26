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

## 6. 업데이터 큐 + 팜뷰 큐로 같은 재시작이 두 번 (B-CQ5)
> **처리 완료: v4 9f27627 — 서버 혼자 막는다. 팜뷰 계약 변경 없음 (2026-09-24 확인)**
- 대상 영역: 팜뷰 (`/api/fv/command` 업데이트 큐 ↔ 업데이터 `/updater/command`)
- 현재 형태: 대시보드가 업데이터 행(`ucmd_id`)과 팜뷰 큐 항목(`ucmd`, 밖으로는 안 나감)을 짝으로 만든다. ★먼저 집는 쪽만★ 가져간다 — `main._updcmd_take`: 팜뷰가 목록에서 집으면 업데이터 행은 `fv_claimed` 로 빠지고, 업데이터가 먼저 가져가면 팜뷰 큐에서 뺀다(`_fv_q_drop_ucmd`). 재배포 뒤엔 DB 에서 복구(`fv_id`).
- 원하는 형태: 없음(제안 1 «id 공유 + 먼저 받은 쪽만» 을 서버 안에서 했다 — 팜뷰는 따로 claim 을 부르지 않는다).
- 지키는 시험: `server/tests/test_fv_found.py`(R-5 등) · `test_breaker_v2.py` · `test_refute_v2p*`·`v3`.

## 7. 텔레그램 중계 응답 — «처리됨» 과 «실패» 를 가른다 · 음소거 뚫기 (TG7 후속, 2026-09-24 주인님 결정)
> **서버는 코드에 넣음(미배포) — 시험 `server/tests/test_tg_hard.py` 25 · 변이 7/7(hard) + 7/7(차단 reason) + 5/5(은퇴 id, #114). 매크로 쪽 짝은 CONTRACTS_아이온2 C4-b·C4-c(lc 2a448ed)**
- 대상 영역: 아이온2(매크로 `lc/report_module._tg_body_ok` · `lc/config.py` 텍스트·캡차 사진 폴백)
- 응답 뜻 (`POST /telegram/send/{pc}` · `POST /telegram/photo/{pc}`):
  - `200 {ok:true, message_id}` = 보냈다.
  - `200 {ok:false, muted:true, reason:"muted", minutes_left}` = ★처리됨(보내지 않기로 결정됨)★ — 클라이언트는 ★자기 봇으로 직접 보내지 않는다★. 생략은 그 PC 로그에 «[텔레그램] 중계 생략(음소거 N분 남음)».
  - ★`403 {detail:"차단 상태에서는 정지 안내만 전송됩니다", reason:"blocked"}`★ (차단 테넌트 — 만료·킬스위치) = ★처리됨★, 직접 보내지 않는다. 텍스트·사진 ★같은 문구·같은 reason★(2026-09-24 — 예전 사진은 detail 없는 `Forbidden` 이라 키 오류와 못 갈라 매크로가 `/telegram/status` 를 다시 물었다). 차단 테넌트는 ⛔ 로 시작하는 정지 안내 ★텍스트★만 통과(사진은 늘 이 403).
  - ★`429 {detail:"정지 안내 전송 상한", reason:"blocked_cap"}`★ = 차단 테넌트의 ⛔ 정지 안내 시간당 3 초과 — 이것도 차단이다(처리됨). 자기 봇으로 폴백하면 상한이 뚫린다.
  - ★`reason` 글자 셋 = 계약: `"muted"` · `"blocked"` · `"blocked_cap"`★ (lc e32c01f `report_module.TG_MUTED`·`TG_BLOCKED`·`TG_BLOCKED_CAP`) — 매크로는 ★대소문자까지 그대로★ 맞춰 보고, 빈 reason 은 «없음». 서버 정본 `main._TG_REASON_*` · 시험 `tests/test_contracts.py` t_tg_reasons(값을 못 박음, 변이 7/7).
  - ★한국어 `detail` 문구는 그대로 둔다★ — 1.1.1006 이전 판 매크로가 문구로 맞춰 본다(`lc/report_module._TG_BLOCKED_DETAIL`). 새 판은 `reason` 을 먼저 본다.
  - 그냥 `403 {detail:"Forbidden"}`(reason 없음) = ★키 문제★(미등록 키·probe 잠금 IP) — 차단이 아니다 → 직접 전송 폴백. probe 잠금 IP 에는 차단 키라도 reason 을 안 준다(키 추측 오라클 방지, 2026-08-06).
  - ★은퇴 id (2026-09-24 #114 B-TG5)★ — 보통 알림은 `200 {ok:false, muted:true, reason:"muted", retired:true, minutes_left:0}` = ★처리됨★(음소거와 ★같은 reason★ — 새 글자를 만들면 옛·새 매크로가 «그 밖의 ok:false» 로 읽고 직접 보내 은퇴가 뚫린다). 강제 알람(⛔·🚨·`hard`)·캡차 사진은 ★그대로 중계★(살아 도는 기계가 사람을 부르는 중). 어느 쪽이든 카드·이벤트 행은 안 만든다(`/alert` 와 같은 규칙). 시험 `tests/test_tg_hard.py` R-1~R-4.
  - 직접 전송 폴백은 ★그 밖★ 만: 네트워크 오류·타임아웃·reason 없는 403·다른 4xx·`503 {reason:"disabled"}`(중계 꺼짐)·`502 {reason:"send_failed"}`·그 밖의 5xx.
- ★C4-b 문구 (SHARED_ISSUES_아이온2 «TG7 … C4-b 에 넣을 문구» 를 여기 정본으로)★: 매크로는 `send_telegram_text(force=True)` 와 답 기다리는 사진(캡차 `expect_reply=1`)에만 `hard` 를 싣는다(텍스트 본문 `true` / 사진 폼 `"1"`), 그 밖엔 칸 자체가 없다. 매크로가 «처리됨» 으로 보고 폴백하지 않는 응답 = 200 + (`reason:"muted"` 또는 `muted:true`) → «음소거로 생략» · 403 + `reason:"blocked"` / 429 + `reason:"blocked_cap"` (옛 판은 텍스트 403 정지 안내 문구) → «차단으로 생략». 그 밖은 직접 전송 폴백. 서버 판정 한 곳: `main._tg_hard`(음소거 뚫기) · `main._tg_blocked_tenant`·`_tg_blocked_resp`(차단 응답).
- 음소거를 뚫는 것 — 규칙은 서버 `main._tg_hard` 한 곳(두 창구 같이):
  - 본문(사진은 캡션)이 `⛔`·`🚨` 로 시작 (2026-09-11부터).
  - ★`hard:true`★ — 텍스트는 JSON 본문 `"hard": true`(1·"1"·"true"·"yes" 도), 사진은 폼 `hard=1`. 주인님 «음소거라도 올리기» — 매크로 1.1.1006 이 강제 알람(지역차단·재접속 중 캡차·스트리밍 비번 등)에 싣는다. false·0·"0"·"false"·빈 값은 못 뚫는다.
  - ★사진 `expect_reply=1`(캡차) 은 늘★ — 주인님 «항상 올리기». 예전엔 사진 창구에 음소거 확인이 아예 없어 ★우연히★ 나갔다 — 이제 명시 규칙. 대신 ★답을 안 기다리는 사진은 음소거면 텍스트와 같은 200 muted★(2026-09-24 현재 매크로 사진 호출부는 캡차 하나뿐, `lc/config.py:4504`).
- ★차단이 hard 보다 먼저★ (아이온2 권고 채택): 차단 테넌트는 hard·expect_reply 로도 못 뚫는다 — ⛔ 정지 안내만. 사진은 차단이면 늘 403.
- 이유: TG7 — 매크로가 muted 를 「중계 실패」로 읽어 PC 옛 토큰으로 직접 보내 음소거가 뚫렸다(주인님이 끄라신 PC 에서 알림이 계속 옴). 강제 알람은 반대로 음소거에 먹히면 안 된다(사람을 세워놓는 알람).

## 8. 매크로 새 status `surface` (#208, 2026-09-25 주인님 «표층 중인데 대기라고 뜬다»)
> **서버는 코드에 넣음(미배포) — 시험 `server/tests/test_surface_208.py` 15 · 변이 10/10. 매크로 짝은 프로그램 방(`report_status("surface")`, 회랑과 같은 방식)**
- 대상 영역: 아이온2(매크로 `/status`) · 팜뷰(스냅샷 `status`·`by_status`·명령표 `expect_status`)
- 형태: `status: "surface"` = 표층 세션 중. 대시보드 카드 «표층 진입중», 모양은 회랑과 같다(`STATUS_CFG.surface`). 서버 순환은 `ROT_SESSION` — 사냥 확인이 아니라 세션 대기(명령 안 보냄, 90분 상한), 무보고 300초면 박제(회랑과 같다). 순환 말 «표층 진입중»(`ROT_ST_KOR`). 노는 PC 자동진행은 건너뛴다(`AUTO_IDLE_BUSY`). 명령 추적 `start`·`surface` 의 기대 상태에 `surface`.
- 순환 작업(`ROT_TASKS`)이 아니므로 `ROT_TASK_BUSY_ST` 에는 안 넣었다 — 작업 순환 중 표층이면 «딴 일»(20분 대기) 로 읽는 게 맞다.
- 팜뷰가 할 일: 자기 «하는 중» 목록(`farmview/fvdash.py ACT_ST`)에 `surface` 를 `corridor` 옆에(FV_API «알아둘 것»).
- 같이 고친 것: FV 명령표 파서가 줄 끝 `// 주석` 달린 `CMD_TRACK` 줄을 버려 `surface` 명령이 팜뷰 명령 목록에 없었다(S-15 가 키 수를 본다).

## 10. `/bugs/image/{fn}` 보기용 JPEG (2026-09-27 Railway 나가는 바이트 1위 — 한 장 ≈1.1MB PNG)
- **기본은 그대로** — 쿼리 없으면 원본 PNG 바이트(에이전트 `bugpull`·템플릿 수확은 무손실이 필요하다).
- 새 선택: `?fmt=jpg[&q=40..90(기본 80)][&w=160..1280]` → `image/jpeg`. 실측 1280x720: q80 ≈ 1/9, w=640 q=70 ≈ 1/33.
  JPEG 이 원본보다 크거나(작은 단색 크롭) 변환을 못 하면 원본 PNG(못 한 때만 `X-Fmt-Fallback: png`).
- 모든 응답 `Cache-Control: private, max-age=604800, immutable` (파일명에 시각 — 안 바뀐다).
- 대시보드 버그 패널 썸네일은 `?fmt=jpg&q=70&w=640`, 클릭은 원본. **보기만 하는 에이전트(alarmshot·bugshots)는 `?fmt=jpg` 를 붙이면 된다(프로그램 방).**
- 지키는 시험: `tests/test_bugimg_mem.py` B-1..B-14.

## 11. 버그스샷 정리(나이) · 숨은 대시보드엔 안 보낸다 (#271, 2026-09-27 주인님 «쓸모없이 쌓인다» · «라이브 안 쓰는데»)
- **정리** (업로드 때 그 pc_id + 30분마다 전체, 첫 판은 부팅 30분 뒤):
  사고 증거 = 48시간 지나면 지움(최신 6장·40장 상한은 그대로) · 수확 재료(`plrowfull`·`plrowlist`·`profcardN`·`autorowN`·`plrowmissN`·`*_CAND`·`flyout`)
  = (pc_id, 종류) 최신 6장 **나이 무관 영구** · 학습(`ocrlearn_`·`ocrdiff_`·`oddfail_`) = 120장 + 7일 · 이름에서 시각을 못 읽으면 나이로 안 지움.
- 핀: `POST /bugs/pin/{fn}?on=1|0` (main 세션) → settings `bug_pins`, 정리가 절대 안 지운다. 핀을 못 읽으면 그 판은 안 지운다.
- 미리보기: `GET /diag/bugs_prune` (main) — 지금 훑으면 지울 수·바이트(종류별). ★배포 직후 30분 안에 읽어 본다.★
- **WS `/ws`**: 접속 주소에 `?h=1|0&e=1|0`(숨김·iframe). 화면은 숨음/보임이 바뀔 때 `{"t":"vis","h":0|1}` 을 보낸다
  (`document.hidden` 또는 창 크기 0). 숨은 소켓엔 `state`·`state_diff`·`log`·`cmd_history`·`char_info`·`corridor_progress`·`nightmare_progress` 를 안 보내고
  `alert`(소리)·`ping` 만 보낸다. 보이는 소켓이 하나도 없으면 상태를 만들지도 않는다. 보이게 되면 새 판을 만들어 보낸다.
- `/diag/egress` `ws_clients`: 소켓마다 ip(첫 홉)·ua·origin·embedded·hidden·tx·age_s — 누가 붙어 있는지 여기서 본다.
- 지키는 시험: `tests/test_bug_retention_ws.py` R-1..R-19 · W-1..W-11.
