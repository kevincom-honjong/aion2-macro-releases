# SHARED_ISSUES_대시보드.md — 대시보드 세션이 발견했지만 ★담당 경로(server/) 밖★ 이라 고치지 않은 것

작성: 대시보드 세션, 2026-09-12. 한 항목 = 파일 / 문제 / 재현 조건 / 제안 네 줄.
다른 세션은 읽기만. 이 파일은 대시보드 세션만 쓴다.

---

## 1. 매크로가 명령을 ★실행 전에★ ack 한다 → 서버·대시보드가 「전달됨」을 「실행됨」으로 읽는다
> **처리 완료: 2026-09-12 서버 `ack_cmd` 본문 `{status,why}`→cancelled + 매크로 `notify_dropped`(부팅 드레인·오버라이드·사고 523 자리). 시험 I1·게이트 52. INTEGRATION_LOG C1**
- 파일: `src/lc/report_module.py` (WS `on_message` 의 ack, HTTP 폴링 drain 의 ack) · `src/lc/loot.py` (`drop_pending_commands`, `enqueue_command`)
- 문제: 매크로는 받자마자 ack 를 보내고, 그 뒤 새 사람 명령이 오면 `drop_pending_commands` 가 대기 큐를 통째로 버린다(주인님 오버라이드 지시라 설계). 서버는 이미 `acked` 라 재전송도 없고 대시보드에도 아무 표시가 없다 = ★유실이 안 보인다★.
- 재현 조건: WS 재접속 직후 서버가 밀린 명령 2건 이상을 한 묶음으로 보낸 뒤 4초 안에 사람이 다른 명령을 누른다. 뒤 명령이 앞 묶음의 나머지를 버린다.
- 제안: 매크로가 「버렸다」를 서버에 알리는 창구를 쓴다 — 서버 `POST /command/{pc}/ack/{id}` 에 `{"status":"cancelled","why":"..."}` 를 받게 넓힐 준비가 돼 있다(대시보드는 이미 `cancelled` 를 ⛔ 로 그린다). 매크로 쪽에서 `drop_pending_commands` 가 버린 id 마다 그걸 부르면 끝난다. 서버 쪽 확장은 CONTRACTS_대시보드.md #1.

## 2. 매크로가 ⛔ 알람 중계 응답의 `ok:false` 를 보지 않는다
> **처리 완료: 2026-09-12 매크로 `_tg_body_ok`(send·photo). INTEGRATION_LOG C2**
- 파일: `src/lc/report_module.py` `telegram_send` (`return bool(r.status_code == 200)`) · `src/lc/config.py` (「[텔레그램] 중계 전송」 로그)
- 문제: 서버가 음소거로 생략하면 이제 `{"ok":false,"reason":"muted"}` 를 준다(2026-09-11 변경). 매크로는 HTTP 200 만 보고 성공으로 찍는다 → 로그가 「보냈다」로 거짓말한다.
- 재현 조건: 대시보드에서 PC 를 음소거한 뒤 그 PC 가 일반 알람을 보낸다. 서버 로그엔 「중계 생략(음소거)」, 매크로 로그엔 「중계 전송」.
- 제안: `telegram_send` 가 본문의 `ok` 를 보고 False 를 돌려주게. ⛔·🚨 는 서버가 음소거를 뚫으므로 그쪽은 영향 없다.

## 3. HTTP 로그 폴백이 51번째 줄부터 잃는다 — 매크로가 버퍼를 먼저 비운다
> **처리 완료: 2026-09-12 매크로 `_flush_logs` 50줄씩 + dropped 경고(`LOG_BATCH_MAX` 짝은 verify_all C). INTEGRATION_LOG C3**
- 파일: `src/lc/report_module.py` `_flush_logs` (`_log_buffer.clear()` 뒤 `_post`)
- 문제: 서버는 배치당 50줄만 저장하고 이제 `{"count":50,"received":70,"dropped":20}` 로 정직하게 답한다(2026-09-11). 매크로는 post 전에 버퍼를 비우고 응답을 안 보므로 버려진 줄은 어디에도 없다. 이 경로는 WS 가 끊긴 구간 전용 = ★사고 구간의 로그가 정확히 여기서 잘린다★.
- 재현 조건: WS 가 60초 이상 끊긴 채 로그가 51줄 넘게 쌓였다가 재접속.
- 제안: 배치를 50줄씩 나눠 보내거나, 응답의 `dropped` 가 0 이 아니면 그 줄들을 다시 버퍼에 넣는다.

## 4. `web/server/main.py` 미러가 낡는다
> **처리 완료: 2026-09-12 미러를 없앴다(`web/server/main.py.mirror_removed_20260912` 보관), 문서 5곳 정본 경로로. verify_all C 가 부재를 지킨다. INTEGRATION_LOG D4**
- 파일: `src/web/server/main.py`
- 문제: 배포되는 정본은 `src/updater/server/main.py` 다. `web/server/main.py` 는 손으로 복사하는 미러인데(HANDOFF 규칙) 대시보드 세션은 이번 작업부터 담당 경로 밖을 안 고치므로 ★이 미러가 낡은 채로 남는다★.
- 재현 조건: 2026-09-12 이후 `diff src/updater/server/main.py src/web/server/main.py`.
- 제안: 미러를 버리거나(정본 하나면 충분하다), web 쪽 도구가 필요할 때 `updater/server/main.py` 를 직접 읽게 한다.

## 5. 관제컴 `parsec_multi.py` 가 API 키로 주소록을 덮어쓴다
> **부분: 2026-09-12 서버에 `X-Parsec-Token`(env `PARSEC_MAP_TOKEN`) 길을 열었다(시험 I5). env→도구→API 키 길 닫기 순서는 주인님. INTEGRATION_LOG D5**
- 파일: `src/updater/parsec_multi.py` (382행 근처, `X-Api-Key` 로 `POST /parsec/map`)
- 문제: 매크로 API 키는 공개 exe 에 각인돼 유출 전제다. 그 키로 파섹 주소록(peer_id)을 덮을 수 있으면 다음 계정전환이 ★공격자 호스트로 접속★ 한다. 서버 쪽에서 세션 전용으로 바꾸면 이 도구가 깨진다.
- 재현 조건: 유출 키로 `POST /parsec/map {"map":{"8":"<남의 peer_id>"}}`.
- 제안: CONTRACTS_대시보드.md #2 — 쓰기 전용 별도 토큰 또는 세션 로그인으로 바꾸는 계약. 그 전까지 서버는 읽기·쓰기 모두 API 키를 허용한 채로 둔다.

## 6. 업데이터 명령은 만료·중복 방지가 없다
> **부분: 2026-09-12 클라이언트 `_done_cmd_ids` dedup + ack 실패 로그(시험 cmd_dedup_test). 서버 만료는 주인님 결정. INTEGRATION_LOG C4**
- 파일: `src/updater/client/updater.py` (폴링 → `handle_command`, ack 실패를 `except: pass`) · 서버 `database.py get_pending_updater_command`
- 문제: ack POST 가 실패하면(서버 굳음·재배포 창) 10초마다 같은 `update` 를 다시 실행한다(stop→start 반복). 꺼진 PC 에 넣은 `update` 는 며칠 뒤 부팅해도 실행된다. 매크로 큐엔 15분 만료·id dedup 이 있는데 업데이터엔 둘 다 없다.
- 재현 조건: 업데이터가 명령을 받은 직후 서버가 재배포된다.
- 제안: 서버가 만료(15분)와 `delivered_at` 표시를 넣을 수 있지만 「꺼진 PC 가 켜지면 밀린 업데이트를 받는다」가 의도된 동작일 수 있어 주인님 결정이 필요하다. 클라이언트는 최소한 같은 id 를 두 번 실행하지 않게.

## 7. 공용 exe 안의 비밀 11개 (게이트 `secret_audit` 빨간불)
> **미해결: 2026-09-12 주인님 결정. INTEGRATION_LOG D3**
- 파일: `src/updater/exe/혼종_통합_자동.exe` · `src/lc/config.py` (secrets_default)
- 문제: 공개 저장소에 올라가는 exe 에서 API 키·텔레그램·OCR 키가 추출된다. 서버는 그래서 API 키를 「유출 전제」로 다룬다. 대시보드 쪽에서 막을 수 없다.
- 재현 조건: `python -X utf8 secret_audit.py --gate` (web/.claude/ops).
- 제안: 이미 NOW.md 0-L 에 있는 그대로 — 빌드 때 secrets_default 를 비우고 각 PC info.txt 로 키를 넣는다. 24대 순서가 있어 주인님 몫.
