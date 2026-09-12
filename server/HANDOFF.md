# server/HANDOFF.md — 대시보드 영역 인수인계 (2026-09-12)

먼저 `CLAUDE.md`(같은 폴더)를 읽고, 그다음 이 파일. 상태 정본은 `../../web/.claude/ops/NOW.md` 0-AB·0-AC 절(더 길다).

## 이번에 고친 것 (2026-09-10 ~ 09-12, 전부 배포됨 또는 이번 커밋)

### 09-12 (이번 커밋) — 1단계 20건 + 2단계 정리
| 등급 | 무엇 | 자리 |
|---|---|---|
| 치명 | `/img` 가 무인증인데 동기 httpx 3상류×15초로 서버 전체를 세웠다 → 스레드 + 매니페스트 밖 이름 404 + LRU(통째 clear 제거) | `serve_image`·`_fetch_image_upstream` |
| 치명 | `version.json` 갱신이 동기 httpx(최악 14초)였고 `push_state` 경로에서 불렸다 → `_load_version_json_async`(스레드+잠금+이중확인) | 호출부 4곳 |
| 치명 | 렌탈 `/updater.exe` 가 75MB 동기 다운로드+압축(최악 3분) → 스레드 | `download_updater` |
| 심각 | 사람 표식 `_by` 가 첫 WS 배달에만 붙고 DB 엔 없어 ★재배달이 「사람 아님」★ 으로 갔다(사고 523 재현) → DB 에도 저장, 화면엔 `_public_args` 로 숨김 | `_dispatch_macro_command`·`_strip_cmds` |
| 심각 | `/updater/command/{pc}` 에 A7 브로드캐스트 가드가 0줄 | 라우트 |
| 심각 | 텔레그램 음소거 키가 pc_id 뿐이라 테넌트가 섞였다 → `_tg_muted(tenant, pc)` | `_TG_MUTE` 3곳 |
| 심각 | `/setting/{key}` API 키 조회가 부정목록(비밀 2개 제외 전부) → 허용목록 `MACRO_READABLE_SETTINGS` | `get_setting_ep` |
| 심각 | 버그스샷 목록이 481×getsize 를 매번(팜뷰 20초 폴링) → `_bug_scan` 캐시 + scandir | `_list_bug_files` |
| 심각 | `_rot_stop` 이 옛 무장으로 새 무장을 지울 수 있었다(독스트링은 「await 없다」고 했지만 5곳이 await 뒤) → `_ROT_STEP_CUR` 로 자원 쪽에서 확인 | `_rot_stop`·엔진 루프 |
| 심각 | 엔진이 단계 예외를 삼켜 ★상한 알람도 같이 죽었다★ → `err_n` 세고 3틱째 🚨 | `_rot_engine` |
| 심각 | `_rot_load` 복원 실패가 stdout 뿐, 깨진 저장본을 다음 save 가 덮음 → `.broken` 보관 + 텔레그램 | `_rot_load` |
| 심각 | FV `slots_done` 이 `today` 플래그를 안 봐 어제 완주를 오늘로 · `totals.bugs` 카드 합산 3배 · events 같은 초 유실 · 일괄 명령 1대 성공도 `ok` | `_fv_pc_view`·`_fv_build_snapshot`·`fv_events`·`fv_command_send` |
| 심각 | `/check/purge` 아무 테넌트 키 · FV 토큰 추측 무제한 | `check_purge`·`_fv_guard` |
| 경미 | JS `armed` 거부 표시가 start 만 · `_rot_say` 가 보내기 전에 「전송」 기록 + ⛔ 화면 배너 없음 · 재무장 시 옛 해제 사유 잔재 · 내부망 주소 캐시 재배포 소실 | `sendCmd`·`_rot_say`·`_rot_arm`·`_lan_cache_*` |
| 정리 | 상수 5종, 인증 도우미 2종(46곳), `_attach_char_info`, 죽은 줄 1 | — |

### 09-11 (배포 `e8e2a75`·`339e6cc5`) — 전수조사 10축 확정분
저장형 XSS 8곳(`renderRow` 에 esc 0개) · N+1 제거(상태조립 70~106ms → 14ms) · `insert_log` 연결 2→1 · `logs(created_at)` 인덱스+정렬 · `get_pending_commands` 읽기 경로 쓰기락 제거 · 소켓 소유권(`send_command_to_macro`) · `/ws` finally · WS ack 이력 방송 · 음소거가 ⛔ 삼킴 · 로그 배치 유실 표시 · `ack_command` 상태가드 · 키나 0 덮어쓰기 · LIKE 이스케이프.

### 09-10 (배포 `d55be8c`~) — 명령이 느리던 진짜 원인
`macro_websocket` 의 finally 가 무조건 pop 이라 ★재접속한 새 연결을 옛 핸들러가 지웠다★. 서버만 「WS 없음」으로 믿어 폴링 15~60초를 기다렸다. 그 앞에 세운 가설 둘(「경로가 느리다」「루프가 굳는다」)은 계측이 깼다.

## 시도했다가 버린 접근과 이유
- **미러 CSS 를 「가장 큰 `<style>` 블록」으로 뽑기** — JS 주석 안의 `<style` 에서 시작한 4,898줄짜리 가짜를 집어 `dashboard.css` 를 한 번 망가뜨렸다. 진짜는 803줄. → 「함수가 있는가」로 가른다(`verify.py _looks_right`).
- **문자열 grep 으로 「고쳤는지」 검사** — 적대검증이 「진짜 가드를 지우고 주석에만 문구를 남긴 사본」을 20/20 으로 통과시켰다. → 시험은 전부 실호출, `prove_guards.py` 로 되돌려 확인.
- **인덱스만 달기(`logs(created_at)`)** — `ORDER BY id` 가 그대로면 SCAN 그대로(8.96→9.31ms). 정렬을 커서와 맞춰야 0.05ms.
- **`/parsec/map` POST 를 세션 전용으로** — 관제컴 `parsec_multi.py` 가 API 키로 부른다. 서버가 먼저 바꾸면 도구가 깨진다 → `CONTRACTS_대시보드.md` #2.
- **`set_slot_filter` 에 `_by` 달기** — 사람 표식이 붙으면 매크로가 진행 중 워커를 취소한다(`loot.py` `_is_human` 갈래). 설정 명령에 그걸 달면 슬롯 토글이 사냥을 끊는다. 안 달았다.
- **`main.py` 분할·static 서빙 전환** — 주인님 「아직 올리지 마라」 + 라이브 72카드. 미러는 만들어 두되 서빙은 안 바꿨다.

## 남은 TODO (주인님 결정 필요 순)
1. `commands`·`updater_commands` 표에 정리 규칙이 없다(단조 증가, 200k행에서 33ms). 보존 기간을 정해야 한다.
2. 업데이터 명령 만료·dedup(`SHARED_ISSUES` #6) — 「꺼진 PC 가 켜지면 밀린 업데이트를 받는다」가 의도인지.
3. `_rot_step_pc` `starting` 7분 상한이 캡차·재연결을 못 넘긴다(`switching` 은 20분) — 줄기.
4. 재무장이 `tvisit` 을 초기화해 full 순환이 끝낸 계정을 다시 돈다 — 줄기.
5. 단계 전이 절반이 즉시 저장되지 않는다(재배포가 30초 창에 걸리면 옛 단계로 복원).
6. `build_full_state` 합치기(coalesce) — N+1 제거 뒤 14ms 라 급하지 않다. 재고 판단.
7. FV `raw`(응답의 53%, PII) 제거 — `CONTRACTS` #4.
8. `secret_audit` 빨간불(exe 안 비밀 11개) — 주인님 몫.

## 함정 포인트
- **`main.py` 는 LF, `database.py` 는 CRLF.** 치환 스크립트에서 줄바꿈을 한 값으로 뭉뚱그리면 조용히 count=0. 파일마다 잰다.
- **`servercode.py`(web/.claude/ops)가 배포 확인의 정본** — 작업트리 sha256 은 CRLF 로 틀린다. 커밋된 바이트로 잰다.
- **`_rot_engine` 은 부팅 뒤 `ROT_BOOT_GRACE_S`(25초)를 잔다** — 시험은 0 으로 놓는다.
- **매크로는 실행 전에 ack 한다.** 서버 `acked` = 「받았다」지 「했다」가 아니다. 유실·거부는 아직 서버에 안 보인다(`CONTRACTS` #1).
- **한 PC = 한 매크로 = WS 하나.** 같은 pc_id 재접속은 옛 것을 `close(1012)` 로 밀어낸다. 이걸 「연결이 자꾸 끊긴다」로 읽지 마라.
- **`/status` 의 base 카드는 업데이터 기록이 없으면 `offline`** — 시험에서 status 를 볼 땐 `upsert_updater_status` 를 같이 심는다.
- **FV `pcs` 는 dict.** 배열로 순회하면 키 문자열이 나온다.
- **다른 세션이 같은 repo 에 커밋한다** — 작업트리에 `client/updater.py`·`seed_server.py` 가 수정된 채 있었다(내 것 아님). `git add .` 금지, 파일을 골라 스테이징. 빌드 잠금(`web/.claude/ops/room.py lock build`)이 있어도 겹친 적이 있다(2026-09-10).
- **다른 손이 `main.py` 를 고치면 static/ 미러가 낡는다** — `verify.py` 4단계가 잡는다. `--sync-static` 으로 다시 뽑는다.

## 다른 영역에 넘긴 것 (요약 — 원문은 `../SHARED_ISSUES_대시보드.md` · `../CONTRACTS_대시보드.md`)
- **SHARED_ISSUES 7건**: 매크로가 실행 전 ack(유실 안 보임) · `telegram_send` 가 `ok:false` 를 안 봄 · HTTP 로그 폴백 51줄부터 유실 · `web/server/main.py` 미러 낡음 · `parsec_multi.py` 가 API 키로 주소록 씀 · 업데이터 명령 만료 없음 · exe 안 비밀 11개.
- **CONTRACTS 5건**: ack 에 status/why 싣기 · 파섹 주소록 쓰기 인증 분리 · FV `pc:"all"` 서버 거부 · FV `raw` 제거 · FV 값 뜻 정정(`slots_done`·`totals.bugs`·events 잘림 — 이번에 서버가 고침, 팜뷰가 알아야 함).
