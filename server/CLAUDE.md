# server/ — AION2 관제 대시보드 서버 (담당 영역: 대시보드)

## 이 폴더가 하는 일 (3줄)
1. 원격컴 24대의 매크로가 올리는 상태·로그·스샷을 받아 SQLite(`/data`, Railway 볼륨)에 쌓고, 브라우저 대시보드(카드 72장·명령 이력·알림·TTS)에 WebSocket 으로 실시간으로 밀어준다.
2. 사람이 대시보드에서 누른 명령을 큐에 넣어 매크로에 배달하고(WS 즉시 / HTTP 폴링 폴백), 계정 자동순환 엔진(`_rot_*`)이 그 큐로 PC 를 굴린다.
3. 업데이터에 버전·이미지를 알려주고(`/check`·`/img`), 텔레그램 단일봇을 중계하고, 팜뷰(FarmView)에 `/api/fv/*` 로 같은 상태를 준다.

## 복붙하면 되는 명령
```bash
cd C:/Users/USER/Desktop/src/updater/server

# ★코드를 고쳤으면 반드시★ — 문법·pyflakes·JS·미러·시험 110개 (종료코드 0 = 통과)
python -X utf8 verify.py

# 빠른 것만 (시험 제외)
python -X utf8 verify.py --quick

# 정본(main.py 인라인 JS/CSS)에서 static/ 미러를 다시 뽑는다 (JS/CSS 를 고친 뒤)
python -X utf8 verify.py --sync-static

# 시험 하나만
python -X utf8 tests/test_stage1.py        # 2026-09-12 수정분 45검사
python -X utf8 tests/test_regressions.py   # 2026-09-10~11 수정분 35검사
python -X utf8 tests/test_contracts.py     # 다른 영역과의 인터페이스 30검사

# 시험이 진짜로 무는지 (고친 것을 되돌린 사본에서 빨간불이 나는가, 1~2분)
python -X utf8 tests/prove_guards.py

# 로컬로 띄워 보기 (운영 DB 안 건드림)
DB_PATH=/tmp/t.db BUGS_DIR=/tmp/bugs DASHBOARD_PASSWORD=x API_KEY=y python -X utf8 -m uvicorn main:app --port 8791
```
배포는 `git push` → Railway 가 `server/**` 변경만 보고 재배포한다(`railway.toml`). 떠 있는 코드 확인은 `GET /health` 의 `code` = `git show HEAD:server/main.py` 의 sha256 앞 8자(작업트리 파일로 재면 CRLF 때문에 틀린다).

## 파일 구조
| 파일 | 역할 |
|---|---|
| `main.py` (약 13,300줄) | 전부 — FastAPI 라우트, 인라인 대시보드 HTML/CSS/JS(`HTML_DASHBOARD`·`HTML_LOGIN`), 순환 엔진, FV API, 텔레그램 중계, 계측(`/diag/perf`) |
| `database.py` | aiosqlite 헬퍼. 표: pc_status·commands·logs·settings·updater_*·char_info·nightmare_progress·slot_filters·death_events·telegram_map |
| `static/` | main.py 인라인의 ★미러★(아직 서빙 안 됨, 3단계 분리 보류). `verify.py --sync-static` 로만 만든다. 손으로 고치지 않는다 |
| `tests/` | `_harness.py`(임시 DB 발판) · `test_*.py`(실호출 시험) · `prove_guards.py`(되돌리기 증명) |
| `verify.py` | 검증 명령(위) |
| `requirements.txt`·`Procfile`·`railway.toml` | 배포 설정 — ★수정 금지★(공용 설정) |
| `version.json` | Actions 가 자동 갱신. ★수동 편집 금지★ |
| `manual.pdf` | 지인 매뉴얼 |

## 수정 시 규칙 (숫자로)
- **함수 40줄 이하, 중첩 3단 이하**로 새로 쓴다. 기존 긴 함수(`_rot_step_pc` 663줄, `_build_full_state_inner` 280줄)는 ★쪼개지 않는다★ — 아래 「건드리면 안 되는 것」.
- **매직넘버 금지** — 두 번 이상 쓰이면 모듈 상수(`LOG_BATCH_MAX`·`VERSION_CACHE_TTL_S`·`ROT_*`·`WS_RECONNECT_DRAIN` 처럼 대문자, 주석에 왜 그 값인지).
- **인증은 도우미로** — `tenant = _require_session(request)`(401) / `tenant = _require_api_key(request)`(403). 3줄을 손으로 다시 쓰지 않는다.
- **에러 처리** — `except Exception: pass` 금지. 최소 `print(...)` + 사람이 볼 곳(그 PC 로그 `insert_log` 또는 텔레그램)에 남긴다. 「됐다」는 응답은 실제로 된 것만(`ok:false` 를 두려워하지 마라).
- **동기 I/O 를 async 핸들러에서 직접 부르지 않는다** — `httpx.get`·`os.listdir`·큰 `json.dumps` 는 `await asyncio.to_thread(...)` 또는 캐시. 루프가 10초 넘게 막히면 매크로 24대 WS 가 동시에 떨어진다.
- **공유 자원(`macro_ws_connections`·`manager.active`·캐시 dict)을 지울 땐 「내 것인가」를 본다** — `is` 비교. 2026-09-10 사고(옛 핸들러가 새 연결을 지움)의 뿌리.
- **매크로가 보낸 문자열은 `innerHTML` 에 넣기 전에 `esc()`** — 캐릭 이름·서버명·명령·파일명 전부. API 키는 공개 exe 에 각인돼 유출 전제다.
- **테넌트 키** — DB 키는 `ns(tenant, pc_id)`(main 은 접두사 없음). 전역 dict 에 pc_id 만 키로 쓰면 지인과 본판이 섞인다(2026-09-12 `_TG_MUTE` 사고).
- **줄기(순서·정지 조건·「성공」의 뜻)는 상의 뒤에만** — 새 화면·새 상황은 ★가지(알아채고→처리→다음)나 가드★ 로만.
- **문서 정정은 원문을 지우거나 접는다. 덧붙이기 금지.** 코드 주석의 숫자·날짜는 「과거의 주장」 — 근거로 쓰기 전에 센다.

## 절대 건드리면 안 되는 것과 이유
| 대상 | 이유 |
|---|---|
| `_rot_step_pc` 의 단계 순서·상한·정지 조건 | 계정 자동순환의 줄기. 2026-09-09 사고 522 — 18시간 TTL 하나가 멀쩡한 순환을 껐다. `ROT_TTL` 은 없앴고 ★다시 만들지 않는다★(게이트 `rotttl_test`) |
| `_dispatch_macro_command` 의 `_by="human"` 표식 (send_args ★와 DB args 둘 다★) | 없으면 매크로가 사람 명령을 「이전 명령 처리 중」으로 거부한다(사고 523). 재배달(폴링·WS재접속)은 DB 행을 읽는다 |
| `_rot_send` 에는 `_by` 를 달지 않는다 | 엔진 명령이 사람 명령처럼 취급되면 두 워커가 같은 화면을 만진다 |
| `_BROADCAST_IDS` 거부(`/command/all`·`/updater/command/all`) | A7 — 함대 전체 명령은 사람 몫. 두 큐 모두 막혀 있어야 한다 |
| `enrich_cmd_args` 를 안 거치는 배달 경로 추가 | DB 는 마스킹본(`***`)이다. 거치지 않으면 매크로가 `***` 를 비번으로 친다(PC-21 peer_id 사고) |
| `/health`·`/tenants`·`/diag/perf` 의 `== "main"` | 다른 지인 테넌트 이름·함대 규모가 샌다(2026-08-06 감사, 2026-09-11 재발 후 재수정) |
| `railway.toml` 의 `watchPatterns` 에서 `!server/version.json` | 이걸 빼면 매크로 릴리스마다 서버가 재배포돼 `/data` 가 날아간다(2026-07-28) |
| `push_state` 의 「보는 사람 없으면 만들지 않는다」 가드 | 매크로 보고마다(초당 ~1회) 카드 72장을 만들던 낭비. 새 대시보드는 `/ws` 가 직접 만든다 |
| `database.py` 의 `journal_mode=WAL` · `ack_command` 의 `AND status='pending'` | 후자를 빼면 취소한 명령이 뒤늦은 ack 로 되살아난다 |

## 다른 영역과 주고받는 인터페이스 (계약은 `tests/test_contracts.py` 가 지킨다)
| 상대 | 방향 | 무엇 | 계약 |
|---|---|---|---|
| 아이온2 매크로 (`src/lc`) | M→S | `POST /report/{pc}` · `POST /log/{pc}` · `GET /command/{pc}` · `POST /command/{pc}/ack/{id}` · WS `/ws/macro/{pc}` (status/log/ack/pong 프레임) | 인증 `X-Api-Key`. `/log` 응답 `{ok,count,received,dropped}`(count = 저장 수). 폴링 봉투 `{command,args,id}` / 빈 큐 `{command:null}` |
| 아이온2 매크로 | S→M | WS 봉투 `{type:"command", id, command, args}` · 사람 명령은 `args._by="human"` | 재접속 때 밀린 명령 최대 `WS_RECONNECT_DRAIN`(8) 건을 밀리초 간격으로 |
| 업데이터 (`src/updater/client`) | 양방향 | `POST /check` · `GET /img/{fname}`(무인증, 매니페스트 안 이름만) · `/updater/*` | `/img` 는 `version.json` `images` 에 있는 이름만 상류를 찌른다 |
| 브라우저 대시보드 (main.py 인라인 JS) | S→B | WS `/ws`: `state`(카드)·`cmd_history`·`alert`·`log`·`char_info` | `cmd_history` 항목 `{id,pc_id,command,status,created_at,updated_at,args}`, `args` 에서 `_` 키는 걷어낸다. `status` ∈ pending/acked/expired/cancelled |
| 팜뷰 (`src/farmview`) | S→FV | `GET /api/fv/snapshot`·`/events`·`/pc/{id}`·`/tts`, `POST /api/fv/command` | 인증 `X-FV-Token`. `pcs` 는 ★dict★(pc_id 키). `progress.{trade_kina,gakin_kina,odd_energy,awakening_ticket,subscribed,total_kina}` · `today.{slots_done,slots_total,slots_left,daily_progress[].today}` · `global.totals.{total_kina,bugs,…}`. 문서 `../FV_API.md` |
| 관제컴 도구 `../parsec_multi.py` | →S | `POST /parsec/map`(API 키) | 바꾸고 싶은 것은 `../CONTRACTS_대시보드.md` #2 |
| 텔레그램 | S↔TG | 서버가 유일한 폴러(`_tg_poller`). 매크로는 `/telegram/send·photo` 로 중계 | ⛔·🚨 는 음소거를 뚫는다. 생략은 `ok:false` |

## 지시
**코드 수정 후에는 반드시 `python -X utf8 verify.py` 를 실행해 통과시킬 것. 이 폴더 밖은 수정하지 말 것.**
담당 밖에 원인이 있으면 `../SHARED_ISSUES_대시보드.md`, 인터페이스를 바꿔야 하면 `../CONTRACTS_대시보드.md` 에 적는다. 커밋 메시지 앞에 `[대시보드]`.
