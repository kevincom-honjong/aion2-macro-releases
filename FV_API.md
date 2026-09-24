# FarmView 연동 API — `/api/fv/*`

만든 날 2026-09-08 · 서버 `server/main.py` (FastAPI 단일 파일)
**아래 JSON 은 전부 로컬에서 서버를 실제로 띄워 받은 응답 원문이다.** 손으로 지어낸 것이 아니다.
(테스트 계정·토큰·이메일은 예시값으로 바꿨다)

---

## 0. 30초 요약

| | |
|---|---|
| 인증 | 헤더 **`X-FV-Token`** 하나. 환경변수 `FV_TOKEN` 과 상수시간 비교 |
| 세션·쿠키·429 | **전부 안 본다.** 폴링해도 락아웃에 안 걸린다 |
| `FV_TOKEN` 미설정 | **`/api/fv/*` 전체가 404** 로 숨는다 |
| 에러 | 항상 `{"ok": false, "error": "...", "err": "...", "code": <HTTP 코드>}` — `err` 는 `error` 와 같은 글. ★배포된 팜뷰(5.54 `ui/index.html` dashCmd)는 `j.ok===false` 일 때만 실패로 그리고 문구는 `j.err` 에서 읽는다★ (2026-09-24 v3 델타 반증 #1 — 그 전엔 `{error, code}` 뿐이라 함대 창 400 이 «보냈습니다»+성공음이었다). 옛 칸 `error`·`code` 는 그대로(더하기만) |
| gzip | `Accept-Encoding: gzip` 이고 본문 1KB 초과면 압축. **실측 51,488 B → 1,448 B** |
| 스냅샷 캐시 | **3초**. 응답에 `cached`/`cache_age_s` 가 실린다 |
| 시각 형식 | `YYYY-MM-DDTHH:MM:SS` **UTC naive** (DB 형식 그대로). `since` 는 `Z`·오프셋도 받는다 |

```bash
curl -H "X-FV-Token: $FV_TOKEN" --compressed \
     https://web-production-8d4c.up.railway.app/api/fv/snapshot
```

---

## 1. `GET /api/fv/snapshot`

대시보드 화면에 뜨는 것을 PC별로 전부 모아 한 방에. **페이지네이션 없음.**
데이터 출처는 대시보드가 쓰는 바로 그 함수(`_build_full_state`)라 화면과 어긋나지 않는다.

### 응답 (실제 원문 — 카드 2장 기준)

```json
{
  "ts": "2026-09-08T02:15:00",
  "pcs": {
    "PC-01": {
      "pc_id": "PC-01",
      "online": true,
      "status": "hunting",
      "last_report": "2026-09-08T02:15:00",
      "silent_s": 1,
      "ws_live": false,
      "macro_version": "1.1.926",
      "doing": {
        "status": "hunting",
        "switch_step": "15 게임 실행",
        "switch_mark": "✔",
        "map": "spirit",
        "map_name": "엘테넨",
        "slot": 1,
        "wait_slot": null,
        "stopped_by": null,
        "stopped_why": null
      },
      "account": {
        "num": 1,
        "id": "eagle900@example.com",
        "total": 5,
        "server": null,
        "ids":       {"1": "eagle900@example.com", "2": "b@example.com"},
        "platforms": {"1": "NC", "2": "NC"},
        "servers":   {"1": "네자칸"},
        "names":     {"1": {"1": "러닝", "2": "세찬ss2"}}
      },
      "character": {
        "name": "러닝",
        "class": "궁성",
        "chars": ["러닝", "세찬ss2"]
      },
      "progress": {
        "hunt_progress": 42.5,
        "efficiency": 31.2,
        "kina": 812340,
        "kina_rate": 145000,
        "total_kina": 130801794,
        "kina_age_s": 5210,
        "kina_read_age_s": 312,
        "uptime_hours": 3.4,
        "deaths_30m": 0,
        "abyss_kina": null,
        "trade_kina": 12345678,
        "gakin_kina": 23456789,
        "odd_energy": 1495,
        "awakening_ticket": 3,
        "subscribed": true
      },
      "today": {
        "slots_done": 1,
        "slots_total": 2,
        "slots_left": 1,
        "daily_progress": [
          {"slot": 1, "name": "러닝",    "completed": true,
           "completed_time": "2026-09-08T02:10:00"},
          {"slot": 2, "name": "세찬ss2", "completed": false}
        ],
        "dungeon_done_at": null
      },
      "errors": [],
      "bugs": 0,
      "updater": {
        "state": "running",
        "version": "3.1.13",
        "age_s": 0,
        "view_url": "http://192.168.1.11:8767/frame.jpg?k=yyy"
      },
      "links": {
        "lan_url":  "http://172.30.1.11:8765/?k=xxx",
        "view_url": "http://192.168.1.11:8767/frame.jpg?k=yyy"
      },
      "cdp":  {"ok": true, "at": null, "tabs": 2},
      "wifi": {"quiet": 0, "stream_ago": 12, "probe_fail": 0, "verdict": "", "on": true},
      "raw":  { "…대시보드 /status 가 주는 그 줄 전부…" }
    },

    "PC-02b": {
      "pc_id": "PC-02b",
      "online": false,
      "status": "offline",
      "macro_version": "1.1.923",
      "doing": {"status": "offline", "switch_step": "05 파섹 접속", "switch_mark": "✘",
                "map_name": "마을", "slot": 0},
      "errors": ["진행도 판독 불가 10분째"],
      "today": {"slots_done": 1, "slots_total": 1, "slots_left": 0},
      "…": "나머지 키는 PC-01 과 같은 모양"
    }
  },

  "global": {
    "tenant": "main",
    "counts": {
      "total": 2,
      "online": 1,
      "offline": 1,
      "by_status": {"hunting": 1, "offline": 1}
    },
    "totals": {
      "total_kina": 130801794,
      "bugs": 0,
      "corridor_remaining": null,
      "corridor_detail": {"fresh_n": 0, "fresh_left": 0, "stale_n": 0, "stale_left": 0},
      "subscribed": {"sub": 1, "nosub": 0, "unknown": 0},
      "trade_kina": 12345678, "gakin_kina": 0, "odd_energy": 1495, "awakening_ticket": null
    },
    "versions": {"1.1.926": 1, "1.1.923": 1},
    "rotate_armed": [],
    "corridor": {},
    "alerts": [
      {
        "at": "2026-09-08T02:15:00",
        "pc": "PC-01",
        "kind": "progress",
        "message": "진행도를 10분째 읽지 못하고 있습니다",
        "say": "진행도 판독 불가"
      }
    ],
    "notice": null,
    "server": {
      "boot": "2291b3c7",
      "code": "2a214cf8",
      "uptime_s": 1,
      "disk_persisted": false,
      "serving_exe": "1.1.926"
    }
  },

  "cached": false,
  "cache_age_s": 0
}
```

### 알아둘 것

- **`raw`** 는 ★기본에서 빠진다★ (통합 2026-09-12, CONTRACTS_대시보드 #4). `?raw=1` 을 붙였을 때만
  `_build_full_state` 원본 줄(59필드·계정 이메일·전화·peer_id·lan_url 포함)이 통째로 온다.
  팜뷰는 받자마자 버리므로 폴링 10초마다 182KB 를 만들고 압축하고 버리던 것을 없앴다.
- ★**`status: "surface"`** (2026-09-25 #208, 더하기만)★ — 표층 세션 중. 대시보드 카드 «표층 진입중»(회랑과 같은 색). 매크로가 회랑(`corridor`)과 같은 방식으로 보낸다(옛 판 매크로는 안 보낸다 — 그땐 `idle`·`abyss` 등).
  서버 순환은 회랑처럼 «세션 중 — 끝나길 기다린다»(사냥 확인 아님, 명령 안 보냄). `by_status` 에도 이 이름으로 센다. 명령표의 `start`·`surface` `expect_status` 에 `surface` 가 들어갔다.
  ★팜뷰가 «하는 중» 상태 목록을 따로 두면(`fvdash.ACT_ST` 등) `surface` 를 `corridor` 옆에 넣어야 한다★ — 안 넣으면 표층 도는 PC 가 노는 PC 로 보인다.
- **`online`** 은 `status != "offline"` 이다. 「매크로가 살아 있나」를 더 엄격히 보려면
  `silent_s`(마지막 보고 이후 초)를 직접 문턱 잡아 쓰는 게 정확하다.
- **`alerts`** 는 서버 메모리 링버퍼(최근 200건 중 30건)다. **서버가 재시작하면 비워진다.**
  대시보드 배너는 원래 휘발이라 그걸 폴링으로도 보게 하려고 새로 담은 것이다.
- **`notice`** 는 설정 키 `notice` 값이다. 안 넣어놨으면 `null` 이다.
- **`server.disk_persisted`** 가 `false` 면 재시작마다 DB·스샷이 날아간다(위 샘플은 로컬 테스트라 false).
- **캐시** — 3초 안에 다시 부르면 같은 본문에 `cached: true`, `cache_age_s` 가 붙어 온다.
  - ★조립이 실패하면 (2026-09-24)★ 마지막 성공본이 ★120초 안★이면 500 대신 그것을 `cached: true` + 진짜 나이 `cache_age_s`(3보다 클 수 있다)로 준다. 120초가 넘었거나, 그 사이 창고키나가 한 번이라도 바뀌었으면(판매 차감 `kina_adjust`·새 판독 `/char_info`·장부 재적용 — 서버 `database.char_info_gen`) 예전대로 500. ★`cache_age_s` 가 크면 낡은 화면이다★ — 창고키나로 파는 계정을 고를 땐 이 값도 본다.

---

## 2. `GET /api/fv/events?since=<ISO8601>&limit=500`

`since` **이후**의 로그·명령·버그스샷을 **시간순 한 배열**로.

| 파라미터 | 기본 | 설명 |
|---|---|---|
| `since` | **10분 전** | `2026-09-08T05:00:00Z` · `2026-09-08T05:00:00+09:00` · `2026-09-08T05:00:00` 다 받는다 |
| `limit` | 500 | 1~2000 로 자른다 |

### 응답 (실제 원문 — 세 종류가 다 나온 판)

```json
{
  "since": "2026-01-01T00:00:00",
  "next_since": "2026-09-08T02:14:41",
  "count": 3,
  "truncated": false,
  "events": [
    {
      "type": "bug",
      "at": "2026-09-08T02:14:41",
      "pc": "PC-01",
      "filename": "PC-01_20260908_021441_PC-01_20260908_021500_lc15-switch-run-fail.png",
      "size": 70,
      "url": "/bugs/image/PC-01_20260908_021441_PC-01_20260908_021500_lc15-switch-run-fail.png"
    },
    {
      "type": "command",
      "at": "2026-09-08T02:14:41",
      "pc": "PC-01",
      "command": "stop",
      "status": "pending",
      "id": 1,
      "args": "{}"
    },
    {
      "type": "log",
      "at": "2026-09-08T02:14:41",
      "pc": "PC-01",
      "level": "info",
      "message": "[런처] 15 OK 게임 실행",
      "id": 1
    }
  ]
}
```

### 이벤트 타입

| `type` | 나오는 필드 | 출처 |
|---|---|---|
| `log` | `level` · `message` · `id` | `logs` 표 |
| `updater_log` | 같음 (업데이터 로그. `pc` 는 `.upd` 를 뗀 이름) | `logs` 표 |
| `command` | `command` · `status`(`pending`/`acked`/`expired`/`cancelled`) · `id` · `args` | `commands` 표 |
| `bug` | `filename` · `size` · `url` | `BUGS_DIR` 파일 |

### 폴링하는 법

```python
since = None
while True:
    q = f"?limit=500" + (f"&since={since}" if since else "")
    d = (await sess.get(BASE + "/api/fv/events" + q, headers=HDR)).json()
    for ev in d["events"]:
        handle(ev)
    since = d["next_since"]          # ★그대로 다음 요청에 넣는다★
    if not d["truncated"]:
        await asyncio.sleep(5)       # 덜 받았으면 쉬지 말고 바로 다음 장
```

- **`next_since` 는 마지막 이벤트의 `at`** 이다. 이벤트가 없으면 `since` 를 그대로 돌려준다.
- **`truncated: true`** 면 `limit` 에 걸려 잘린 것이다. **쉬지 말고 바로 다음 장을 받아라.**
- ★같은 초에 여러 건이 있으면 다음 요청에서 한 번 더 올 수 있다★ — `at` 이 초 단위라
  `> since` 로 자르기 때문이다. **`type`+`id`(버그는 `filename`)로 중복을 걸러라.**

---

## 3. `GET /api/fv/pc/<pc_id>?logs=300`

스냅샷 항목 **전부 + 이력**. `logs` 는 1~2000(기본 300).

응답은 §1 의 PC 항목과 **같은 키**에 아래가 더 붙는다.

```json
{
  "…§1 의 PC-01 항목 전부…": "…",
  "ts": "2026-09-08T02:15:00",
  "logs": [
    {"level": "info", "message": "[런처] 15 OK 게임 실행 — 클릭 (1043, 577)",
     "created_at": "2026-09-08T02:15:00"},
    {"level": "warn", "message": "[분석] 진행도를 10분째 못 읽는다",
     "created_at": "2026-09-08T02:15:00"}
  ],
  "updater_logs": [],
  "commands": [
    {"id": 1, "pc_id": "PC-01", "command": "stop", "args": "{}",
     "status": "acked", "created_at": "2026-09-08T02:15:01",
     "updated_at": "2026-09-08T02:15:01"}
  ],
  "bugs": [
    {"filename": "PC-01_20260908_021441_…_lc15-switch-run-fail.png", "size": 70}
  ],
  "char_info": {
    "total_kina": 130801794,
    "chars": [{"name": "러닝", "class": "궁성", "gear_power": 3452}],
    "collected_at": "2026-09-08T02:15:00"
  },
  "nightmare": null,
  "corridor": null
}
```

없는 카드:
```json
{"error": "카드 'PC-99' 가 없습니다", "code": 404}
```

> `pc_id` 는 `clean_pc_id()` 로 소독한다. **접미사(`PC-01b`)까지 정확히** 줘야 한다 —
> 물리 PC 이름(`PC-01`)만 주면 그 이름의 카드가 없을 때 404 다.

---

## 4. 명령

### 4-1. `GET /api/fv/command` — 지원 목록

**대시보드의 `CMD_TRACK`/`CMD_SILENT` 표를 그대로 읽어서** 준다.
목록을 여기 따로 적지 않는다 — 두 벌이면 어긋나기 때문. 화면에 버튼이 늘면 여기도 자동으로 는다.

```json
{
  "count": 37,
  "source": "대시보드 CMD_TRACK/CMD_SILENT (server/main.py 내 HTML_DASHBOARD)",
  "rotatable": ["daily_dungeon", "nightmare", "awakening", "corridor", "collect_info"],
  "commands": {
    "start": {
      "label": "▶ 사냥 시작",
      "ttl_ms": 180000,
      "expect_status": ["hunting", "moving", "selling", "subquest", "dungeon",
                        "nightmare", "awakening", "corridor", "abyss"],
      "rotatable": false,
      "silent": false
    },
    "stop":            {"label": "■ 정지", "ttl_ms": 90000,
                        "expect_status": ["idle", "paused"], "rotatable": false, "silent": false},
    "switch_launcher": {"label": "🔄 계정 전환(본컴+원격컴)", "ttl_ms": 420000,
                        "expect_status": [], "rotatable": false, "silent": false},
    "collect_info":    {"label": "📋 정보수집", "ttl_ms": 180000,
                        "expect_status": ["collecting"], "rotatable": true, "silent": false},
    "captcha_code":    {"label": "captcha_code", "ttl_ms": null,
                        "expect_status": [], "rotatable": false, "silent": true},
    "…": "총 37개"
  },
  "note": "pc 는 'PC-01' · ['PC-01','PC-02'] · 'all' 셋 중 하나. 'all' 은 서버가 실제 카드 목록으로 펼쳐 한 대씩 보낸다."
}
```

필드 뜻
- **`label`** — 대시보드 버튼에 적힌 글자 그대로. FarmView UI 에 그대로 써도 된다.
- **`ttl_ms`** — 대시보드가 「이 시간 넘으면 응답 없음」으로 보는 값. 기대 시간의 근거로 쓰면 된다.
- **`expect_status`** — 그 명령이 먹히면 카드 `status` 가 이 중 하나가 된다.
  **비어 있으면 status 로는 확인이 안 되는 명령**이다(계정번호가 바뀌는 것 등).
- **`rotatable`** — `args:{"rotate":true}` 를 실으면 전 계정 순환으로 도는 명령.
- **`silent`** — 대시보드가 진행 표시를 안 띄우는 것(라이브 화면·로그 요청 같은 즉답형).
- ★2026-09-25 #208★ 줄 끝에 `// 주석` 이 달린 `CMD_TRACK` 줄이 이 목록에서 통째로 빠지던 것을 고쳤다 — 그동안 ★`surface`(🏔 표층)★ 가 없었다. 이제 `CMD_TRACK` 의 명령은 전부 나온다(개수 +1).

### 4-2. `POST /api/fv/command` — 보내기

```json
{"pc": "PC-01", "cmd": "stop", "args": {}}
{"pc": ["PC-01", "PC-02b"], "cmd": "collect_info", "args": {}}
{"pc": "all", "cmd": "find_host", "args": {}}
```

응답 (한 대 — 실제 원문)
```json
{
  "ok": true,
  "cmd": "stop",
  "targets": 1,
  "sent": 1,
  "results": [
    {"pc": "PC-01", "ok": true, "id": 1, "ws": false}
  ]
}
```

응답 (여러 대 — 실제 원문)
```json
{
  "ok": true,
  "cmd": "collect_info",
  "targets": 2,
  "sent": 2,
  "results": [
    {"pc": "PC-01",  "ok": true, "id": 2, "ws": false},
    {"pc": "PC-02b", "ok": true, "id": 3, "ws": false}
  ]
}
```

`results[]` 필드
- **`id`** — 명령 행 id. `/api/fv/events` 의 `command` 이벤트에서 같은 id 로 `acked` 를 기다리면 된다.
- **`ws`** — `true` 면 매크로 WebSocket 으로 **즉시** 밀어넣었다는 뜻. `false` 면 큐에 넣었고
  매크로가 폴링으로 가져간다(WS 미연결). **둘 다 정상이다.**
- **`armed`/`why`/`queue`** — `start` 에 `args:{"rotate":true}` 를 실었을 때만 붙는다(순환 무장 결과).
- 실패한 대상은 `{"pc": …, "ok": false, "error": "…"}`.

### ★반드시 알아야 할 것★

- **`"all"` 은 함대 전체를 움직인다.** 서버가 카드 목록으로 펼쳐 **한 대씩** 보낸다
  (브로드캐스트 키를 쓰지 않으므로 서버의 A7 가드는 그대로 살아 있다).
  ★FarmView 쪽에 확인 절차를 두는 걸 권한다.★
- **명령 주입의 몸통은 대시보드와 ★같은 함수★(`_dispatch_macro_command`)다.**
  그래서 비밀 마스킹(파섹 비번·peer_id 는 DB·이력에 안 남는다) · 순환 무장/해제 ·
  WS 즉시전달 · 명령 이력 브로드캐스트가 **대시보드에서 누른 것과 똑같이** 동작한다.
- **모르는 `cmd` 는 400 으로 막는다** — 오타로 큐를 더럽히지 않게.

---

## 4-3. 업데이트/재시작 큐 (2026-09-22 추가 — 「대시보드 업데이트·재시작은 잘 안 된다, 팜뷰 방식으로」)

대시보드에서 사람이 「업데이트」·「재시작」을 누르면(카드 메뉴·스프레드행·다중선택 셋 다
★서버는 한 엔드포인트로 모인다★ — `POST /updater/command/{pc_id}`) 이 큐에도 같이 쌓인다.
★기존 `/updater/command` 큐(매크로가 직접 폴링/WS로 받는 것)는 그대로 살아 있다★ —
여기는 팜뷰가 받아가는 ★창구★일 뿐, 팜뷰가 60초 안에 못 받아가도 기존 경로로 결국 배달된다.

**PC당 최신 1건만** — 같은 PC 에 두 번 누르면 앞 것을 덮어쓴다(기존 명령 큐의 오버라이드
규칙과 동일). `act` 는 두 가지뿐: `updater`(업데이트+재시작) · `restart`(재시작만,
업데이트 확인 없이 껐다 켬). `update_only`(⬆ 업데이트만, 카드 메뉴에만 있음)는
이 큐 대상이 ★아니다★ — 기존 큐로만 간다.

### `GET /api/fv/updcmd?since=<id>`

`since` 는 마지막으로 받은 `id`(정수, 생략하면 0 = 전부). **ack 전까지는 폴링마다
계속 나온다** — 재전송 안전(at-least-once). 큐가 비어 있으면 `cmds: []`.

실제 원문(로컬, 업데이트 1건 대기 중):
```json
{"cmds": [{"id": 1, "pc": "PC-05", "act": "updater", "at": "2026-09-22T11:43:39"}]}
```

### `POST /api/fv/updcmd/ack`

```json
{"id": 1, "pc": "PC-05", "ok": true, "why": "done"}
```

`id` 가 지금 큐에 있는 값과 **일치할 때만** 지운다 — ack 이 오가는 사이 사람이 같은 PC 에
다시 눌러 새 `id` 가 들어와 있으면(더 최신 요청) 그건 안 건드리고 살려둔다.
`ok`/`why` 는 서버 콘솔 로그 한 줄로만 남는다(`[FV] updcmd ack pc=... id=... ok=... why=...`)
— 대시보드 화면엔 안 보인다.

실제 원문:
```json
{"ok": true, "removed": true}
```

### 대시보드 쪽 결과 표시(참고 — 팜뷰가 직접 쓰는 API 는 아니다)

카드의 업데이터 정보 줄에 결과만 뜬다: 버튼을 누른 시점의 `updater.version` 스냅샷과
비교해 **바뀌면 ✓, 3분 안에 안 바뀌면 ✗** — 색만 다르고 글자 설명은 없다(hover 로만).
팜뷰가 이 큐를 처리하는 방식과 무관하게, 실제로 버전이 바뀌었는지만 본다.

---

## 5. 에러 (전부 실제 원문)

| 상황 | HTTP | 본문 |
|---|---|---|
| `FV_TOKEN` 미설정 | 404 | `{"error": "Not Found", "code": 404}` |
| 토큰 없음/틀림 | 401 | `{"error": "X-FV-Token 이 올바르지 않습니다", "code": 401}` |
| 없는 카드 | 404 | `{"error": "카드 'PC-99' 가 없습니다", "code": 404}` |
| 모르는 명령 | 400 | `{"error": "모르는 cmd 'no_such_command' — GET /api/fv/command 로 목록을 보십시오", "code": 400}` |
| `since` 형식 오류 | 400 | `{"error": "since 가 ISO8601 이 아닙니다 (예: 2026-09-08T05:00:00Z)", "code": 400}` |
| 본문 없음/객체 아님 | 400 | `{"error": "JSON 본문이 필요합니다", "code": 400}` |
| 스냅샷 조립 실패(마지막 성공본이 없거나 120초 넘음) | 500 | `{"error": "스냅샷 조립 실패: …", "code": 500}` — 120초 안이면 200 `cached:true` 로 성공본(위 «캐시») |
| `kina_adjust` 카드는 있는데 창고 기록이 없음 | 404 | `{"error": "카드 'PC-07' 의 창고 키나 기록(char_info)이 없습니다", "code": 404}` |
| `kina_adjust` 본문 오류 | 400 | `{"error": "why.tid 가 필요합니다(거래 id — 같은 tid 는 두 번 빼지 않습니다)", "code": 400}` 등 |
| `kina_adjust` 일시 오류(DB 바쁨 등) | 503 | `{"error": "일시 오류(OperationalError) — 뺐는지 확인 못 했습니다. 같은 tid 로 다시 보내면 두 번 빼지 않습니다", "code": 503, "retry": true}` — ★같은 tid 로 다시 보내라★(2026-09-24) |

(위 본문은 줄여 적었다 — 실제로는 전부 `"ok": false` 와 `"err"`(= `error`) 가 같이 온다, 2026-09-24.)

**`FV_TOKEN` 미설정(404) 과 토큰 오류(401) 를 갈라 보라** — 404 면 서버에 환경변수를 안 넣은 것이다.

---

## 6. 환경변수

`.env.example` 에 추가돼 있다.

| 키 | 필수 | 기본 | 설명 |
|---|---|---|---|
| `FV_TOKEN` | **예** | (없음) | 이 값이 없으면 `/api/fv/*` 전체가 404. 32자 이상 랜덤 권장 |
| `FV_TENANT` | 아니오 | `main` | 어느 테넌트의 데이터를 줄지. 혼자 쓰면 건드릴 필요 없다 |

### Railway 에 넣을 것

Railway → 프로젝트 → **Variables** 에 **하나만** 추가하면 된다.

```
FV_TOKEN = <랜덤 32바이트 hex>
```

만드는 법 (아무 데서나)
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

- 넣고 저장하면 Railway 가 **자동으로 재배포**한다(변수 변경은 재시작을 부른다).
- 되는지 확인:
```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  -H "X-FV-Token: <넣은 값>" \
  https://web-production-8d4c.up.railway.app/api/fv/snapshot
```
  **200** 이면 됐다. **404** 면 변수가 아직 안 붙은 것(재배포 대기), **401** 이면 값이 다른 것.
- `FV_TENANT` 는 여러 고객을 나눠 쓰는 게 아니면 **넣지 마라**(기본 `main`).
- ★토큰은 대시보드 비번과 다른 값으로★ — 틀림 잠금은 대시보드 비번과 칸이 따로다(아래 «토큰 틀림 잠금»).

---

## 7. FarmView 쪽 최소 클라이언트 (aiohttp)

```python
import aiohttp, asyncio

BASE = "https://web-production-8d4c.up.railway.app"
HDR  = {"X-FV-Token": FV_TOKEN}

async def main():
    # 자동 gzip: aiohttp 는 Accept-Encoding 을 기본으로 붙이고 알아서 푼다
    async with aiohttp.ClientSession(headers=HDR) as s:
        async with s.get(f"{BASE}/api/fv/snapshot") as r:
            if r.status == 404:
                raise SystemExit("서버에 FV_TOKEN 이 없다")
            if r.status != 200:
                raise SystemExit((await r.json())["error"])
            snap = await r.json()
        print(snap["global"]["counts"])
        for pc, v in snap["pcs"].items():
            print(pc, v["status"], v["today"]["slots_left"], "슬롯 남음")

        since = None
        while True:
            q = {"limit": 500} | ({"since": since} if since else {})
            async with s.get(f"{BASE}/api/fv/events", params=q) as r:
                d = await r.json()
            for ev in d["events"]:
                print(ev["at"], ev["type"], ev.get("pc"), ev.get("message") or ev.get("command"))
            since = d["next_since"]
            await asyncio.sleep(0 if d["truncated"] else 5)

asyncio.run(main())
```

폴링 주기 권장
- `snapshot` — **3~5초**. 서버가 3초 캐시라 더 자주 불러도 같은 것이 온다.
- `events` — **5초**, `truncated` 면 즉시 다음 장.

---

## 8. 서버에서 무엇을 건드렸나 (정직하게)

「추가만 하라」는 지시였고, **기존 라우트·화면·DB 스키마는 하나도 안 바꿨다.**
다만 **「복붙하지 마라」를 지키려고 두 곳을 옮기거나 한 줄 붙였다.**

| 파일 | 무엇 | 왜 |
|---|---|---|
| `server/main.py` | `POST /command/{pc_id}` 의 몸통을 **`_dispatch_macro_command()` 로 옮김** | 그 안에 순환 무장·비밀 마스킹·WS 전달·이력 브로드캐스트가 전부 있다. FV 가 복붙하면 규칙이 둘로 갈려 한쪽만 고쳐진다. **경로·인증·A7 가드·응답 모양은 한 글자도 안 바뀌었다** — 라우트는 이제 그 함수를 부른다 |
| `server/main.py` | `push_alert()` 끝에 **한 줄** — 알림을 링버퍼에 남김 | 화면 배너는 휘발이라 폴링으로 오는 FarmView 가 알림을 영영 못 본다. 브로드캐스트 동작은 그대로 |
| `server/main.py` | 파일 끝에 **FV 블록 신설** | `/api/fv/*` 전부 |
| `server/database.py` | `get_logs_since()` · `get_commands_since()` **새 함수 2개** | 「이 시각 이후 전부」가 필요한데 기존 `get_logs` 는 PC 하나씩이라 72대면 쿼리가 72번. **읽기 전용이고 `CREATE TABLE` 은 11개 그대로** |

### 확인한 것

- **`CREATE TABLE` 개수 11개 → 11개** (스키마 불변)
- **기존 경로 정상** — 로컬 부팅 후 `/health` 200 · `/login` 200 · `/ping` 200 ·
  `/` 307(세션 없으면 로그인으로) · `/status` 401(세션 없으면 거부)
- **`FV_TOKEN` 미설정이면 5개 엔드포인트 전부 404**, 그 상태에서도 기존 경로는 200
- **gzip 실측** 51,488 B → 1,448 B
- **명령 전송 3형태(한 대·여러 대·`all`) 전부 200**, 큐에 실제로 들어감

### 안 한 것 · 못 잰 것

- **실서버(Railway)에서는 아직 안 돌려봤다.** 위 확인은 전부 로컬 부팅 기준이다.
- **부하** — 카드 72장 실데이터 기준 `snapshot` 크기·응답시간은 **안 쟀다**(로컬 24장이 51KB 였다).
- `events` 의 로그 조회는 **테넌트를 DB 에서 못 좁힌다**(`main` 은 접두사가 없어서).
  `limit` 만큼 받아 파이썬에서 거르므로, **다른 테넌트 로그가 섞이면 그만큼 덜 온다.**
  혼자 쓰는 서버면 무해하고, 여러 테넌트면 `limit` 을 넉넉히 주는 게 좋다.
- **웹소켓은 안 만들었다** — 지시에 없었다. 실시간이 필요하면 `events` 폴링으로 충분한지 먼저 재보라.

## 2026-09-12 통합 — 계약 5건 (통합 세션, 양쪽 시험이 지킨다)
서버 시험 `updater/server/tests/test_integration_contracts.py` · 팜뷰 시험 `farmview/tests/test_integration_contracts.py`.

| # | 무엇 | 지금 형태 |
|---|---|---|
| 1 | `pcs[].silent_s` · `pcs[].updater.age_s` | **항상 정수.** 모르면 `1000000000`(10^9). 예전엔 `-1` / `null` 이 나가 팜뷰 대표 카드(`silent_s` 최소)가 «모름» 카드에 뺏겼다 |
| 2 | `raw` | 기본 없음. `?raw=1` 일 때만 |
| 3 | `POST /api/fv/command` | `pc:"all"` 은 **400** — 목록을 펼쳐 보내고 ★물리 PC★ 8대 이상이면 `confirm_fleet:true` 를 같이(요청 하나의 셈도 물리 PC — 카드 8장·물리 2대는 함대가 아니다, v4 반증 B3). ★8대는 요청 하나가 아니라 최근 15분 합★(2026-09-24 — 7대+7대로 지나가던 것): 15분 안에 ★확인 없이★ 명령을 받은 PC 에 이번 요청의 ★새★ PC 를 더해 8대 이상이면 (★PC 는 물리 PC 로 센다★ — 계정 카드 `PC-20b`·`PC-20c` 는 `PC-20` 한 대. A7 의 «대» 는 기계 수이고, 카드 셋을 따로 세면 물리 3대에 8번째 카드로 막혔다 — v3 델타 반증 X7. 창 시계는 그 PC 에 ★처음★ 보낸 시각이라 다시 보내도 15분이 늘지 않는다) `confirm_fleet:true` 없이 400(이미 창에 있는 PC 에 다시 보내는 것은 통과). 거부된 요청은 합에 안 센다. `confirm_fleet:true` 로 보낸 PC 는 15분 동안 창에 안 센다(뒤이은 한 대 명령 통과) — ★단, 창 면제 명령(아래 정지·보기 전용)에 붙은 확인은 치지 않는다★(8대↑ `get_logs` 에 팜뷰가 자동으로 붙인 확인 하나로 그 PC 들의 start 가 15분 창을 빠져나갔다, v4 반증 B2). 정지 쪽(`stop`·`stop_tour`·`stop_nightmare`·`stop_corridor`·`stop_surface`)과 보기 전용(`chrome_view`·`live_on`·`live_off`·`get_logs`·`request_logs`·`netprobe`)은 세지도 막지도 않는다. ★팜뷰 할 일★: 한 대씩 눌러 8대째에서 이 400 이 오면 사람에게 확인을 받아 `confirm_fleet:true` 로 다시 보낸다(지금 `fvdash` 는 한 요청이 8대 이상일 때만 붙인다). 이 400 본문도 `ok:false`·`err` 를 실어 5.54 는 실패 토스트로 그린다. 응답 `ok` 는 전부 성공일 때만, 일부는 `partial:true` |
| 4 | `GET /api/fv/events` | `truncated:true` 면 `next_since` 로 **쉬지 않고 다음 장**(팜뷰 `pull_events` 최대 5장). 같은 초 유실 없음(서버가 경계 초를 다음 장으로 미룬다) |
| 5 | `today.slots_done` · `global.totals.bugs` | 서버 값이 정본 — `slots_done` 은 `completed && today!==false`, `totals.bugs` 는 물리 PC 단위 합. 팜뷰의 자체 재계산(`slots_today`)은 같은 규칙이라 그대로 둔다 |

이 문서는 `updater/FV_API.md` 가 정본이고 `farmview/docs/FV_API.md` 는 바이트 동일 사본이다 — `src/verify_all.py` 가 diff 로 지킨다.

## 2026-09-13 추가 — «팔린 만큼 줄인다» (CONTRACTS_팜뷰, 주인님 지시)

### `progress.kina_age_s` — 창고 키나를 잰 지 몇 초인가
정수(초). `char_info.collected_at`(매크로 전체수집 시각) 기준. **모르면 `1000000000`(10^9)** — `null` 이 아니다.
팜뷰는 이 값으로 «창고키나 N시간 전» 을 그리고, 낡은 계정을 파는 계정으로 안 고른다. `kina_adjust` 는 이 시각을 **안 바꾼다**.

### `POST /api/fv/kina_adjust` — 매니아에서 팔린 만큼 창고 키나를 뺀다
```json
{"pc_id": "PC-04b", "delta_kina": -210000000,
 "why": {"tid": "2026090603221474", "server": "챈가룽", "man": 21000, "won": 84000, "src": "itemmania"}}
```
응답
```json
{"ok": true, "pc_id": "PC-04b", "before": 500000000, "after": 290000000, "dup": false}
```
- `after = max(0, before + delta_kina)`. `char_info.total_kina` 를 그 값으로 쓴다. 다음 `/char_info` 수집이 오면 진짜 값이 덮는다(그게 «맞춰 나가기»).
- **같은 `why.tid` 는 두 번 빼지 않는다** → `{"ok": true, "dup": true, "before": <지금>, "after": <지금>}`, 값 그대로. 재시도는 안전하다.
- 기록 표 `kina_adjust(tid PK, pc_id, delta, before, after, why, at)`. Railway 재배포로 SQLite 가 비면 기록도 사라진다(팜뷰가 감수하기로 함).
- 거부: `delta_kina` 정수 아님(bool 포함)·상한 ±1조 초과·`why.tid` 없음·`pc_id` 가 `all` → 400. 카드/창고 기록 없음 → 404. 토큰 → 401.
  - ★일시 오류 (2026-09-24, 팜뷰 #201 부류)★ — DB·OS 오류(잠김·디스크)면 503 `{ok:false, err, retry:true}`(예전엔 맨 500 글자). 그 밖의 예외(서버 버그)는 500 그대로 — 다시 보내도 소용없다. 뺐는지 서버도 확인 못 한 상태이므로 ★같은 `why.tid` 로 다시 보내면 된다★(뺐으면 `dup:true`, 안 뺐으면 지금 뺀다). 사람을 부를 일이 아니다. 첫 답이 503 이었던 판매는 재전송(`dup:true`) 때 그 PC 로그줄 «… — 재전송 때 남김» 이 장부에서 되살아난다. 차감이 커밋된 ★뒤★ 로그줄 쓰기가 실패해도 답은 200 ok 다(예전엔 500 이라 «차감 대기» 로 보였다).
  - (2026-09-23 밤 반증 D) **`tid` 는 카드 하나에만** — 다른 카드에서 이미 쓴 `tid` 면 409 `{"error":"tid '…' 는 이미 다른 카드(PC-..)에서 뺐습니다 …"}`, 아무것도 안 뺀다(예전엔 `dup:true, ok:true` 로 «뺐다» 고 답했다). 결과가 상한(`KINA_MAX` = 10^15)을 넘으면 400.
  - 토큰 틀림 잠금(5분 429)은 **팜뷰 칸이 따로다** — 같은 IP 에서 API 키를 30번 틀려도 팜뷰 토큰은 안 잠긴다. 토큰 헤더가 ★비어 있으면★ 401 이지만 잠금 횟수엔 안 센다. ★칸은 IP + 보낸 토큰★(2026-09-24): 같은 틀린 토큰 30번은 ★그 토큰만★ 잠그고 올바른 토큰은 계속 통과(낡은 토큰을 든 다른 팜뷰가 진짜를 멈추지 않게). 한 IP 에서 서로 다른 틀린 토큰이 5분에 300번(`FV_IP_MAX_FAILS`)이면 그 IP 전체(올바른 토큰도) 5분 429.
  - 모든 엔드포인트: 문자열에 짝 없는 UTF-16 대리 문자(`\ud800`~`\udfff`)가 있으면 400 `{ok:false, error, err, code:400, detail}`(예전 500 · `ok`·`err` 는 2026-09-24 v4 반증 B5).
- 차감 1건마다 그 PC 로그에 `[팜뷰] 창고 키나 -210,000,000 (챈가룽 21000만 → 84000원, tid …) 500,000,000 → 290,000,000` 한 줄이 남는다 — `/api/fv/events` 의 `log` 로도 보인다. 스냅샷 캐시(3초)는 즉시 비운다.
- ★`why.hand_at` (2026-09-24 팜뷰 #201 r3e p6, 선택·더하기만)★ — 그 판매의 ★게임 안 인계 시각★, epoch 초(숫자, 소수 가능). 모르면 빼고 보낸다(`null` 도 «모름»).
  서버는 창고 판독이 새로 들어오면 «판독 시각 뒤에 ★기록된★ 차감» 을 그 판독 값에서 다시 뺀다(판독이 모르는 판매, P0 v3 반증 ③). 그런데 인계 → 창고 판독(판매 반영) → 차감 기록 → 판독 도착(수집 중이던 것) 순서면 두 번 빠졌다. `hand_at` 이 판독 시각(서버 시계로 옮긴 `kina_read_at`)과 ★같은 초이거나 앞이면★ 다시 안 뺀다. 없으면 예전대로 뺀다(`database.KINA_UNKNOWN_HAND_DEDUCT`). 모양이 틀리면(글자·bool·16억 미만·밀리초·지금+하루 넘음) 400 — 조용히 버리면 두 번 빼기가 조용히 돌아온다. 장부 `why` 에 그대로 남는다.

## 2026-09-09 추가 — 캐릭터 합 4종
`progress.trade_kina`(거래키나) · `progress.gakin_kina`(각인키나) · `progress.odd_energy`(오드에너지, `"300(+1,195)/840"` → 300+1195 규칙) · `progress.awakening_ticket`(각성전 티켓) — 그 카드(pc_id) 캐릭터들의 **합**, 없으면 0. `global.totals` 에도 같은 이름으로 전체 합 — ★단 `global.totals` 쪽은 합산 대상 카드 중 그 칸을 한 번이라도 읽은 캐릭이 없으면 `null`(모름)★(2026-09-23, 0 = 「읽었는데 0」 만). `total_kina` 는 그대로(계정 창고값, 캐릭마다 중복이라 합치지 않는다). 서버 `main.py _fv_char_agg`.

## 2026-09-23 추가 — `global.totals` 계산을 대시보드 화면과 한 곳으로 모았다
주인님: 「팜뷰 전광판은 대시보드 전광판이 반영 안 되냐, 숫자가 왜 이렇게 다르냐」.
그날 대시보드 화면 JS 에만 넣었던 보정(은퇴·계정없음 PC 제외 · 각성전 3/3 주간
리셋 보정)이 `/api/fv/snapshot` 에는 안 실려 있었다 — 서버 함수 `_fv_build_snapshot`
(+ `_fv_char_agg`) 한 곳으로 계산을 모으고, 대시보드는 세션 인증 `GET /summary`(같은
함수 결과, 팜뷰 전용 아님 — 화면 전용) 로 이 값을 받아 표시하게 바꿨다. 실측(같은
서버, 같은 순간) 두 응답의 `global.totals`/`/summary` 가 **바이트까지 동일**함을
확인했다(로컬 재현).

- **`global.totals.subscribed`**(신설) — `{"sub": N, "nosub": N, "unknown": N}`. 대시보드
  구독 O/X 집계(`dkSubCount`)와 같은 값. 은퇴·계정없음 PC 는 어느 쪽에도 안 세진다
  (예전엔 `subscribed` 총계 자체가 없었다 — 카드별 `progress.subscribed` 만 있었다).
- **은퇴·계정없음·검증용 가짜 PC 제외** — `total_kina`·`corridor_remaining`(+`corridor_detail`)·
  `subscribed`·캐릭터 합 4종(`trade_kina`·`gakin_kina`·`odd_energy`·`awakening_ticket`) 전부
  은퇴(카드 삭제)·계정없음(no_account)·`PC-TEST`/`PC-DEMO`(배포 검증용, 대시보드가 카드로 안
  그림) 를 뺀다. 판정은 서버 `_fv_pc_excluded` — `RETIRED_PCS`/`NO_ACCOUNT_PCS`(대시보드
  `/admin/retire`·`/admin/no_account` 로 관리) + 가짜 PC 이름.
  - **제외 PC 의 카드 `pcs[pid]` 는 스냅샷에 남되, 캐릭터 합은 비운다** — 그 카드의
    `progress.trade_kina`·`gakin_kina`·`odd_energy`·`awakening_ticket` 은 `0`,
    `progress.subscribed` 는 `null` 이다(`_fv_char_agg` 가 제외 PC 를 아예 안 모은다 —
    계정없음 카드는 옛 계정의 캐릭터 값을 보여 주면 안 되므로). 계정없음 카드는 추가로
    `total_kina`·캐릭터·맵·진행도도 비운다(`status: "no_account"`). 은퇴 PC 는
    `/admin/retire` 가 카드 데이터를 지우고 이후 보고도 버리므로 보통 `pcs` 에 아예 없다.
  - **`global.counts` 는 제외를 안 한다** — `total`·`online`·`offline`·`by_status` 는 `pcs`
    전부(가짜 PC·계정없음 카드 포함)를 센다. `online` 은 `status != "offline"` 이라
    `no_account`·`other_account` 카드도 온라인으로 센다(대시보드 화면의 온라인 수와 다르다).
- **모름은 `null`** — `global.totals` 의 캐릭터 합 4종은 합산 대상 카드 중 그 칸을 한 번도
  못 읽었으면 `null`(대시보드 「–」). `corridor_remaining` 도 회랑 스냅샷이 하나도 없으면
  `null`. `total_kina`·`bugs` 는 여전히 정수. ★카드별 `progress.*` 4종은 여전히 「없으면 0」★
  이다 — 팜뷰가 카드 값을 다시 더해 전광판을 만들면 모름이 0 으로 보인다(`global.totals` 를 읽을 것).
- **`global.totals.corridor_detail`**(신설) — `{"fresh_n", "fresh_left", "stale_n", "stale_left"}`.
  `corridor_remaining = fresh_left + stale_left`. 이번 판에 보고한 PC 수·남은 수 / 리셋 뒤
  아직 시작 안 한(낡은 스냅샷) PC 수·지난 판 정원. 대시보드 회랑 타일 툴팁이 숫자와 같은
  출처를 쓰게 넣었다.
- **각성전 티켓 주간 리셋 보정** — `progress.awakening_ticket`·`global.totals.awakening_ticket`
  둘 다, 그 캐릭터 정보수집 시각이 가장 최근 수요일 05:00(KST) 이전이면 raw 값 대신
  ★3(가득 참)★ 을 합산한다(게임이 리셋으로 3/3 이 됐는데 다음 정보수집 전까지 옛 낮은
  값을 들고 있던 것 — DB 원본은 안 바뀐다). 일일던전(`daily_ticket`)·성역(`sanctuary`)
  은 캐릭터 테이블 컬럼일 뿐 `global.totals`/`progress` 에 없어 이 계산 밖이다(대시보드
  화면에서만 같은 규칙으로 보정).
- 악몽 티켓(`nightmare_ticket`)은 리셋 주기가 아직 실측 확정 전이라 이 보정 대상이
  ★아니다★ — 값을 그대로 준다.


---

## 2026-09-23 (밤) 추가 — 명령 시간 상한 · 업데이트 큐 «한 길» · OCR 라벨 (대시보드 세션, 아이온2 요청)

### A. `POST /api/fv/command` — 8초 안에 답한다 (더하기만, 옛 모양 그대로)
서버는 **`FV_CMD_DEADLINE_S` = 8초** 안에 답한다(팜뷰 12초 끊김보다 짧게). ★팜뷰 끊김은 관제컴 `dashapi.json` 의 `timeout` 으로 사람이 바꿀 수 있다(최소 1)★ — 11초(= 8 + 3) 아래로 두면 서버가 답하기 전에 끊겨 «실패» 로 보이고 사람이 다시 누른다(이미 나간 PC 에 두 번). 팜뷰는 11초 아래 값을 11초로 올려 쓸 것(SHARED_ISSUES FC1-4 ⑧). 서버 시험(M16)은 `fvdash.py` 기본값만 본다. 그 안에 못 끝난 만큼은:

| 칸 | 뜻 |
|---|---|
| `timeout` (bool, 늘 있음) | 상한에 걸렸나 |
| `partial` (bool) | 일부만 됐거나 `timeout` |
| `err` (문자열, **실패일 때만**) | 사람에게 보일 한 줄. 옛 팜뷰 토스트가 `j.err` 만 쓴다. 예: `"8초 초과 — 1대는 닿았는지 모름, 다시 누르지 마십시오 (0/1대 확인)"` · `"8초 초과 — 2대는 보내지 않았습니다(그 PC 만 다시 보내도 됩니다)"` · `"3대 중 1대 실패"` |
| `results[].sent:false` | 시작도 못 했다 — **그 PC 는 다시 보내도 된다** |
| `results[].ok:null` + `unknown:true` | 시작했는데 안 끝났다 — 서버 뒤에서 끝까지 넣는다. **닿았는지 모름, 다시 누르지 말 것** |
| 카드 목록 만들기에서 시간초과 | `{"ok":false,"timeout":true,"targets":0,"sent":0,"results":[],"err":"…아무 PC 에도 보내지 않았습니다…"}` |

같은 PC 로 가는 명령은 서버가 **도착 순서대로** 넣는다(시간초과로 뒤에서 도는 stop 을 다음 start 가 앞지르지 않는다).
성공 응답엔 `err` 가 없다.

### B. §4-3 업데이트/재시작 큐 — «한 명령은 한 길로만» (대시보드 업데이터 큐와의 관계)
`/api/fv/updcmd` 에 나온 명령은 짝이 되는 업데이터 큐 행(`/updater/command`)과 묶여 있다. **먼저 집은 쪽만 실행한다.**
- 팜뷰 목록에 나오는 순간 그 행은 `fv_claimed` — 업데이터 폴링에 안 나간다. 업데이터가 먼저 집었으면 팜뷰 목록에서 빠진다.
- **업데이터가 같은 PC·같은 종류를 방금(90초 안, `UPDCMD_UP_BUSY_SEC`) 받아 갔으면** 그 사이 다시 누른 같은 종류 명령은 팜뷰 목록에 안 나오고 업데이터가 차례로 받는다(두 길이 겹쳐 돌지 않게). 90초가 지나 다시 누른 것은 팜뷰가 받는다(멈춘 업데이터를 에이전트로 살리는 길). 다른 종류(`update` 중 `restart`)는 막지 않는다.
  - 90초 안에 거절된 명령은 **버리지 않고 미룬다** — 업데이터가 그 사이 안 가져가면 90초 뒤 팜뷰 목록에 나온다(반증 3차 H1).
  - 이 규칙은 **재배포 뒤에도 산다** — DB 에서 «같은 PC·같은 종류가 90초(`UPDATER_BUSY_SEC`) 안에 `acked`, 또는 업데이터에 내준 지(`handed_at`) 90초 안인데 아직 ack 전» 을 본다(H5, 2026-09-24 v2 반증 1부).
  - ★업데이터에 내준 명령은 `superseded` 로 안 바뀐다★(2026-09-24) — ack 가 사라진 사이 같은 종류를 다시 눌러도 앞 것은 대기로 남아 늦은 ack 가 `acked` 로 적고, 새로 누른 것은 그 뒤에 차례로 돈다(예전: 돈 명령이 «안 돎» 으로 적혔다). 팜뷰도 업데이터에 내준 행은 집지 않는다(거절하면 그 행의 팜뷰 소유도 푼다 — 업데이터 큐가 막히지 않는다, v3 델타 반증 #4). 내준 뒤 ack 없이 치워지는 행(더 새 같은 종류가 먼저 가거나 10분 만료)은 `superseded`/`expired` 가 아니라 `handed_noack`(«받아감 — ack 없음, 돌았을 수 있음») 이고, 늦은 ack 는 그래도 `acked` 로 적는다(v3 델타 반증 #3).
- **거꾸로도 같다 (H8)** — 팜뷰가 같은 PC·같은 종류를 집고 아직 ack 전(`fv_claimed`)이면, 그 뒤 다시 누른 같은 종류 명령은 **업데이터가 안 가져간다**. 팜뷰가 ack 한 뒤 다음 목록에서 팜뷰가 받는다(두 길 겹침 방지). 팜뷰가 조용해 앞 것이 `fv_unknown` 이 되면 그때는 업데이터가 받는다.
- **ack 는 팜뷰 `id` 로 짝 행을 찾는다** — 서버가 집을 때 `팜뷰 id → 업데이터 행` 을 DB 에 적어 둔다. 그래서 ack 전에 사람이 같은 PC 를 다시 눌러 칸이 덮였거나, 서버가 재배포돼 메모리가 비었어도 ack 는 제 명령에 닿는다.
- **재배포 뒤 목록 복구** — 서버가 새로 뜨면 첫 목록 요청 때 «팜뷰가 집었는데 ack 가 안 온»(10분 안) 명령을 DB 에서 다시 세운다. **같은 팜뷰 `id` 그대로**라 팜뷰는 기억한 결과(`UPD_DONE`)로 ack 만 다시 보내면 된다(다시 실행하지 말 것). 같은 PC 칸이 새 명령으로 차 있으면 둘 다 목록에 나온다(PC 당 1건이 아닐 수 있다 — `id` 로 구분). 재건이 한 번 실패(DB 잠김 등)하면 다음 목록에서 다시 한다(H3), 재건 도중 ack 된 명령은 되살리지 않는다(H4).
- **집힌(ack 전) 명령은 `since` 와 무관하게 매번 나온다 (H6, 2026-09-23 밤)** — 팜뷰 커서는 «ack 성공한 가장 큰 id» 라, 더 작은 id 의 ack 가 재배포 중 사라지면 예전엔 커서 아래로 숨어 영영 안 닫혔다. 이제 `since` 는 **아직 안 집힌** 명령만 거른다. 팜뷰는 이미 실행한 id(`UPD_DONE`)면 다시 실행하지 말고 기억한 결과로 ack 만 보낸다. ⚠ 팜뷰 `UPD_DONE` 을 500개에서 통째로 비우면 다시 나온 집힌 명령이 두 번 실행될 수 있다 — SHARED_ISSUES_대시보드 FC1-4.
- **같은 PC 에 새 명령이 와도 집힌 명령은 목록에서 안 사라진다 (H2)** — 팜뷰가 집었는데 응답이 전선에서 사라진 판에 다른 종류가 눌려도 둘 다 나온다(곁칸, `id` 로 구분).
- **ack 규칙** (`POST /api/fv/updcmd/ack {id, pc, ok, why, reached?}`):
  - `ok:true` → `fv_done`. 업데이터는 절대 안 받는다.
  - `ok:false` 이고 **명령이 PC 에 안 간 게 확실할 때만** → 업데이터 큐로 되돌린다(`pending`, 업데이터가 정확히 한 번 받는다). «확실» = `reached:false` 이거나, `reached` 가 없고 `why` 에 팜뷰 `macro_act` 의 안-감 문구가 있을 때: `없는 PC` · `이 PC 는 매크로 대상이 아닙니다`(nobulk) · `에이전트 없음` · `act 는` · `Cannot connect to host` · `Connect call failed` · `Connection refused`.
  - 그 밖의 `ok:false`(읽기 시간초과 `Timeout on reading…`, 빈 예외 `실패`, `결과 기록 없음`, `reached:true`) → `fv_failed`. 에이전트가 이미 받았을 수 있어 되돌리면 재시작이 두 번 난다. 대시보드 «업데이터 명령» 내역에 `팜뷰 실패` 로 보인다(조용히 사라지지 않는다).
  - **팜뷰 요청(선택, 더하기만)**: ack 에 `reached` 를 명시해 주면 문구 추측 대신 그걸 쓴다 — 연결조차 못 했으면 `false`, 에이전트에 요청을 보냈으면(응답이 실패·시간초과여도) `true`.
  - 되돌릴 때 같은 PC·같은 종류의 **더 새 명령**이 이미 대기·실행 중이거나 끝났으면 옛 것은 되살리지 않고 `superseded`(옛 restart 가 새 restart 뒤에 도는 일 없음).
- **팜뷰가 집고 ack 가 안 올 때 — 업데이터에 넘기지 않는다** (2026-09-23 배포 반증 3차 #1). 팜뷰가 에이전트로 이미 실행하고 인터넷만 끊겼을 수 있어, 넘기면 재시작이 두 번 난다. 팜뷰가 목록·ack 를 **60초(`FV_QUIET_SEC`) 동안 한 번도 안 불렀고** 집은 지 90초(`FV_CLAIM_ACK_SEC`)가 넘었거나, 집은 채 10분이 지나면 → `fv_unknown`(«실행됐는지 모름») + 그 PC 로그에 WARNING 한 줄. 필요하면 사람이 다시 누른다. ★그 순간 팜뷰 목록에서도 빠진다★(반증 v2 #2 — 재시작한 팜뷰가 옛 것과 다시 누른 것을 둘 다 돌리지 않게). 목록에 없어도 돌아온 팜뷰의 늦은 ack 는 `id` 로 짝 행을 찾아 닫는다: `ok:true` → `fv_done` · 안-감 확실한 실패 → 업데이터가 한 번 받음 · 그 밖의 실패 → `fv_failed`. 팜뷰가 살아서 차례로 돌리는 중이면(60초 안에 목록·ack) 90초가 넘어도 그대로 둔다.
  - 이 판정은 **30초마다 서버가 스스로** 돈다(H7) — 업데이터가 죽어 폴링이 없어도 `fv_claimed` 가 영영 남지 않는다. `fv_unknown` 은 재배포 뒤에도 다시 세우지 않는다(«다시 보내지 않았습니다» 그대로). 안전망: 집은 지 11분이 넘은 항목은 청소가 못 돌았어도 목록에 다시 안 나온다.
- 대기(`pending`) 명령은 유효기간 10분(`UPDATER_COMMAND_MAX_AGE_SEC`)이 지나면 `expired`(되살리지 않는다).
- **덮어쓰기는 같은 종류만** — `update` 두 번 → 앞 것 `superseded`(앞 것을 업데이터가 아직 안 받았을 때만 — 받았으면 둘 다 차례로). `update` 뒤 `restart` 는 **둘 다** 실행된다(팜뷰 큐는 PC 당 최신 1건이라 `restart` 는 팜뷰가, `update` 는 업데이터가 받는다).
- 업데이터 큐 행의 상태는 대시보드 «최근 명령 내역 › 업데이터 명령» 과 `GET /updater/commands/recent` 에 보인다:
  `pending`(대기) · `acked`(업데이터 받음) · `fv_claimed`(팜뷰 처리 중) · `fv_done` · `fv_failed` · `fv_unknown`(팜뷰 응답 없음 — 실행됐는지 모름) · `superseded`(같은 명령 다시 누름 — 업데이터가 안 받아 간 것만) · `expired`(10분 — 안 받아 간 것만) · `handed_noack`(업데이터가 받아 갔는데 ack 없이 치워짐 — 돌았을 수 있음).
- 응답 모양 `{id,pc,act,at}` 은 그대로. `id` 는 벽시계 ms 기반 정수(≈1.79×10^12, 재배포 뒤에도 커진다).
- **`redeliver: true` (2026-09-23 밤, 더하기만)** — 이미 한 번 준(집힌, ack 전) 항목을 다시 줄 때만 붙는다. 팜뷰 규칙: 그 `id` 를 기억(`UPD_DONE`)하면 기억한 결과로 ack 만 · ★기억이 없으면 실행하지 말고★ `{ok:false, reached:true, why:"재시작해 결과 모름"}` 으로 ack(→ `fv_failed`, 사람이 본다). 처음 주는 항목엔 이 칸이 없다 — 옛 팜뷰는 무시해도 지금처럼 돈다.

### C. 알아둘 것 — `global.totals.corridor_remaining` 은 `null` 일 수 있다
회랑 스냅샷이 하나도 없으면 `null`(모름). 옛 팜뷰(fvdash-1.20 이하)는 이걸 `0` 으로 보인다 — 1.21 부터 `null` 을 통과시킨다.

### D. OCR 라벨 — `/api/fv/ocr/*` (주인님 장부 #125 · 팜뷰 «OCR» 탭)
> 배포됨(v4 9f27627, 2026-09-24 `server/ocr_label.py`). 에러 모양은 다른 `/api/fv/*` 와 같다(`{ok:false, error, err, code}`). `seed_bugs`·자동 씨앗은 **코드에 넣었다(미배포, 2026-09-24)** — 배포 전엔 `seed_bugs` 가 404.
대시보드 `/ocr/label` 화면(세션 로그인, 관제컴 브라우저·폰)과 **같은 저장소**를 쓴다 — 어느 쪽에서 라벨을 달아도 같이 세진다.
인증은 다른 `/api/fv/*` 와 같다(`X-FV-Token`, 테넌트 = `FV_TENANT`). 캡차 이미지는 저장소에 아예 없다(서버가 `site=captcha*` 제출을 거절).

**묶음(cluster)** — 같은 사이트에서 거의 같은 이미지(dhash 해밍 ≤ 4)는 한 묶음이다. 라벨·나쁨·건너뛰기는 **묶음 id** 에 건다. `count` 는 그 묶음에 들어온 제출 수(×N 표시용), `members` 는 서로 다른 이미지 수.

| 메서드 · 경로 | 본문 / 쿼리 | 응답 |
|---|---|---|
| `GET /api/fv/ocr/queue?limit=30` (1~100) | — | `{"pending": 57, "items": [Item…], "suggest": {"<site>": ["라벨", …]}, "now": 1790164002.4}` — 대기 묶음을 처리할 순서대로 |
| `GET /api/fv/ocr/img/<img_id>` | — | 이미지 바이트(`image/png` 등). 없으면 404 |
| `POST /api/fv/ocr/label` | `{"id": <묶음 id>, "text": "라벨"}` | `{"ok": true, "id", "status": "labeled", "label", "prev_status", "prev_label", "members", "pending"}` — 이미 라벨 된 묶음에 다시 보내면 **고치기** |
| `POST /api/fv/ocr/bad` | `{"id": <묶음 id>}` | 같은 모양, `status: "bad"`, `label: null` («잘못된 이미지») |
| `POST /api/fv/ocr/skip` | `{"id": <묶음 id>}` | `{"ok": true, "id"}` — 대기열 맨 뒤로. 대기 아닌 묶음이면 409 |
| `POST /api/fv/ocr/undo` | `{}` | `{"ok": true, "id", "status", "label"}` — **이 테넌트의** 마지막 저장/나쁨/고치기 하나를 되돌린다(웹 화면에서 한 것도 포함). 되돌려 대기가 되면 대기열 맨 앞. 되돌릴 게 없으면 404 |
| `GET /api/fv/ocr/history?limit=30` (1~200) | — | `{"items": [Item…]}` — 최근 라벨/나쁨, 새것부터 |
| `GET /api/fv/ocr/stats` | — | `{"sites": {"<site>": {"pending","labeled","bad","images","hits","gemini_compared","gemini_disagree","gemini_disagree_rate","local_compared","local_disagree","local_disagree_rate"}}, "disk_bytes", "disk_cap", "disk_hard_cap"}` — `*_rate` 는 비교한 게 없으면 `null` |
| `POST /api/fv/ocr/seed_bugs` (2026-09-24) | `{}` | `{"ok": true, "scanned", "added", "exists", "skipped": {"<까닭>": n}, "full", "more"}` — `/bugs` 의 `ocrdiff_*`·`oddfail_*` 크롭을 큐로(아래 «씨앗»). 몇 번 불러도 같다(`added` 0 · `exists` n). 같은 테넌트 씨앗이 도는 중이면 `{"ok": false, "busy": true, …}` |

`Item`:
```json
{"id": 41, "img": 188, "site": "odd_energy", "status": "pending", "label": null,
 "created": 1790160000.1, "at": null, "count": 7, "members": 3,
 "prompt": "숫자만 읽어라", "gemini": "1,234/840", "local": "1234/840", "pc": "PC-07",
 "thumbs": [187, 181]}
```
- `img`·`thumbs` 는 이미지 id — `GET /api/fv/ocr/img/<id>` 로 받는다. 대표 이미지 파일이 디스크 상한으로 지워졌으면 `img: null`.
- 시각(`created`·`at`)은 유닉스 초(UTC).
- `label` 은 앞뒤 공백을 걷고 NFC 로 맞춘 글자(최대 200자). 빈 `text` 는 400.
- 에러 모양은 다른 `/api/fv/*` 와 같다: `{"ok": false, "error": "...", "err": "...", "code": N}` — 400(본문·id)·404(없는 묶음·이미지)·409(skip 대상 아님).
- 실시간: 대시보드 화면은 `queue` 를 2초마다 다시 읽는다. 팜뷰도 같은 폴링이면 된다(`pending` 이 줄면 다른 쪽에서 단 것).
- **씨앗(2026-09-24, 주인님 #125 «탭이 뜨자마자 판별»)** — 매크로가 예전부터 `/bugs` 로 올리던 로컬 OCR 불일치 크롭이 판별 거리다. ① `queue` 를 서버가 켜진 뒤 ★처음★ 읽을 때 씨앗이 저절로 돈다(그 응답에 이미 들어 있다) ② `seed_bugs` 로 다시 돌릴 수 있다 ③ 그 뒤 `/bugs` 로 새로 올라오는 것은 올라오는 순간 큐로 들어간다. site = 파일 이름의 항목(`ocrdiff_<site>_L<로컬>_G<제미나이>` · `oddfail_narrow` → `odd_energy` · `oddfail_wide` → `odd_energy_wide`), `gemini`·`local` 은 파일 이름에서 되읽은 **힌트**(모르는 글자 `?`), `prompt` 는 `seed:/bugs <파일 이름>`, `pc` 는 올린 PC. 캡차 이름·깨진 PNG 는 넣지 않는다. 같은 그림은 두 번 안 들어간다.

## 2026-09-23 (밤3) 추가 — `[알람]` 이벤트 (주인님 #128 팜뷰 알람 목소리, 대시보드 세션)
매크로가 `/telegram/send/{pc}`·`/telegram/photo/{pc}` 로 알람을 보내면, 서버가 ★텔레그램으로 실제로 내보내는 순간★ 그 PC 로그에 한 줄을 쓴다 → `/api/fv/events` 에 `type:"log"` 로 나온다(더하기만, 새 엔드포인트 없음).
```json
{"type": "log", "at": "2026-09-23T14:57:00", "pc": "PC-14b", "level": "info",
 "message": "[알람] PC-14b | 🚨 캡차 자동해결 실패(3회) — 코드를 답장해 주세요", "id": 12345}
```
- 모양: `[알람] <pc> | <본문 300자까지>` — 본문의 줄바꿈(CR·LF)은 빈칸 하나로(한 줄) · 사진은 `[알람] <pc> | <캡션> (사진)`(캡션이 없으면 `(사진)`). 머리 상수는 서버 `main.ALARM_EVENT_PREFIX`.
- ★음소거로 생략한 알림은 안 쓴다★(안 나간 알람을 말하지 않게). `⛔`·`🚨` 로 시작해 음소거를 뚫는 것은 쓴다. 텔레그램 전송 ★뒤★ 에 쓴다 — 전송이 실패(502)해도 쓰되 끝에 ` (텔레그램 실패)` 를 붙인다(2026-09-24 아이온2 반증: 목소리가 텔레그램보다 조용히 더 많이 말하지 않게). 팜뷰 `summarize` 는 «(» 에서 자르므로 말은 같고, 로그·events 에서 실패가 보인다.
- 늦음: 서버가 텔레그램에 보낸 즉시(보통 1초 안) DB → events 정착 2초(`FV_EVENT_SETTLE_S`) + 팜뷰 폴링 5초 = 보통 5초 안, 최악 ~8초. 예전 길(매크로 `[텔레그램] 중계 전송:` info 줄, 하트비트 30초)은 ~35초였다 — 그 줄도 계속 온다. 팜뷰 `alarmvoice.py` 는 둘 다 받고 2분 중복 억제로 한 번만 말한다.
- 텔레그램이 꺼진 테넌트(봇 토큰·chat_id 없음, 503)는 안 쓴다.
- ★2026-09-24 더하기 — 지연 꼬리(#124, 아이온2 결정)★: 줄 끝에 ` (⏱ 발생→수신 X초 · 수신→전송 Y초 · 텔레그램 HH:MM:SS)` 가 붙는다(`(텔레그램 실패)` 뒤). 발생→수신 = 서버 수신 시각 − 매크로 본문 `t0`(epoch 초, ★매크로 시계★ — 음수면 시계 차) · t0 가 없거나 숫자가 아니거나 하루 넘게 어긋나면 이 칸만 빠진다 · 수신→전송 = 텔레그램 호출이 끝날 때까지(실패·429 대기 포함) · 텔레그램 = 텔레그램 응답 `date`(KST, 초) — 실패면 빠진다. 사진은 `/telegram/photo` 폼 칸 `t0`(수신 = 업로드를 다 받은 뒤). «(» 로 시작하므로 팜뷰 `summarize` 가 말에서 자른다(말은 그대로 — 서버 시험 A-9b 가 팜뷰 실물 alarmvoice 로 확인). 꼬리를 떼려면 `\s\(⏱ [^()]*\)$`.
- 시험: 서버 `tests/test_alarm_event.py`(22건 — 모양·줄바꿈·음소거·실패 표시·사진·events 로 나옴 · 지연 꼬리 A-7~A-9).

## 2026-09-24 추가 — 업데이트 큐 T1·T2 로그 (#64, 대시보드 세션 · 더하기만)
§4-3 큐의 왕복을 사후에 잴 수 있게 그 PC 로그(= `/api/fv/events` `type:"log"`)에 두 줄을 쓴다. 응답 모양은 그대로.
- T1 `[업데이트큐] 팜뷰가 가져감 id=<id> act=<updater|restart> (누른 뒤 N초)` — `GET /api/fv/updcmd` 가 그 id 를 ★처음★ 줄 때 한 번(ack 전 재폴링엔 안 쓴다).
- T2 `[업데이트큐] 팜뷰 ack id=<id> ok=<True|False> why=<80자> (누른 뒤 N초)` — `POST /api/fv/updcmd/ack` 마다. 큐에 없는 id 면 `(누른 뒤 ?) (이미 없음·id 불일치)`.
- 나머지 시각: T0 = `updater_commands.created_at`(누른 시각) · T3 = 카드 `updater.version` 이 바뀐 시각 · T4 = 매크로 보고 재개. 로그 쓰기 실패는 큐를 막지 않는다.
- 시험: 서버 `tests/test_audit_0924.py` Q-1~Q-6.

## 2026-09-24 추가 — 어비스 수익 (주인님 장부 #104·#297·#302, 대시보드 세션 · 더하기만)
> **코드에 넣었다(미배포).** 매크로 1.1.1004 와 같은 날 배포. 기존 키는 그대로 — 옛 팜뷰는 무시해도 지금처럼 돈다.
- `snapshot.pcs[].progress` 에 다섯 칸: `abyss_kina_state` (`"ok"`|`"waiting"`|null) · `abyss_kina_gain`(이 구간 번 키나) ·
  `abyss_kina_rate`(시간당 키나) · `abyss_kina_since`(구간 시작 epoch 초 — ★바뀌면 새 측정★) · `abyss_kina_mins`(잰 분). 전부 정수 또는 null
  (옛 매크로·이상값 = null). `waiting` = 매크로가 아직 10분 안 쟀다 → 시간당을 믿지 말 것.
- `snapshot.global.totals.abyss` = `{"day", "today", "today_pcs", "rate_sum", "rate_avg", "rate_n", "wait_n", "red", "red_rate", "min_mins", …}`
  - `today` = 함대 KST 하루 누적(★구간이 바뀌거나 매크로가 재시작해도 줄지 않는다★ — 앞 구간 몫을 서버가 적립). `null` = 측정 대기(0 과 다르다).
  - `error: true` (있을 때만) = 서버가 전광판 계산에 실패했다(저장본 손상 등) — `today`·`rate_*` 는 `null`, `red:false`. 스냅샷 나머지는 그대로 온다(v4 반증 3차, 코드에 넣었다·미배포).
  - `rate_sum` = 함대 시간당 합 · `rate_avg` = 대당 시간당 평균(`rate_n` 대) · `wait_n` = 아직 재는 중인 대수.
  - `red` = `rate_avg < red_rate`(기본 1,000,000, 설정 `abyss_red_rate`). 카드 빨강은 그 PC `abyss_kina_rate < red_rate` 이고
    `abyss_kina_mins >= min_mins`(기본 5, 설정 `abyss_min_mins`) 일 때만 — `since` 가 바뀌면 새 구간은 `waiting` 으로 시작해 빨강이 풀린다.
- 시험: 서버 `tests/test_abyss_kina.py`(A8-e·A8-e2 = 이 칸들, S2-a·S2-a2 = 오늘 누적 안 줄어듦, S2-b·S2-b2 = since 바뀌면 빨강 해제).

## 2026-09-24 추가 — 팜뷰 #201 r3d: `progress.kina_read_age_s` · `POST /api/fv/notify` (대시보드 세션 · 더하기만)
> 코드에 넣었다(미배포). 시험: 서버 `tests/test_fv_r3d.py`(KR-*·N-*) · `tests/test_contracts.py`(모든 카드에 정수).

### `progress.kina_read_age_s` — 창고를 ★실제로 읽은★ 지 몇 초
- 정수(초). **모르면 `1000000000`(10^9)** — `kina_age_s` 와 같은 규칙. 모든 카드에 온다.
- 기준 = 서버가 ★새 판독으로 받아들인★ 마지막 `kina_read_at`(매크로가 창고를 읽은 순간, 서버 시계로 옮긴 값 — 서버 표 `kina_read.read_srv`).
- `kina_age_s`(전체수집 시각 `collected_at`)와 다른 점: ★merge 판독에도 바뀐다★ — 사냥 끝 창고 읽기(매크로 `info_collector.refresh_total_kina_after_deposit`, 사고 569)는 `merge:true` 라 `collected_at` 을 안 바꾼다. 거래 끝 뒤에 창고를 다시 읽었는지는 ★이 칸★ 으로 본다(`body_ts − kina_read_age_s` 가 거래 끝보다 뒤면 그 판독이 판매를 이미 반영했다).
- **안 바뀌는 것**: 같은 판독의 재전송(부팅·WS 재연결 — 판독 시각이 같다) · 서버가 버린 옛 판독 · `kina_read_at` 이 없는 보고(KF1-c 전 매크로·로컬 json) · `kina_adjust`(판매 차감). ★재전송된 옛 값을 «방금 읽음» 으로 보이면 팜뷰가 반영 안 된 판매를 건너뛴다★ — 그래서 판독 시각이 있는 새 판독만 센다.
- 판독 표식을 한 번도 안 보낸 카드(옛 매크로)는 10^9 로 남는다 — 그땐 `kina_age_s` 와 팜뷰 자체 관찰로 판단.
- ★«마지막으로 잰 때» = `kina_age_s` 와 `kina_read_age_s` 중 ★작은 쪽(늦은 쪽)★★ — 판독 시각 없는 옛 매크로의 전체수집은 창고키나를 바꾸고 `kina_age_s` 만 새로 한다(`kina_read_age_s` 는 그대로). 한 칸만 보면 그 판을 못 보고 두 번 뺀다(반증 #1). 판독 시각 없는 옛 매크로의 ★merge★ 보고는 어느 칸도 안 바꾼다(서버가 읽은 때를 모른다).
- 두 나이 모두 ★본문을 만든 때★ 기준이다 — 3초 캐시·120초 폴백 본문이면 `cache_age_s` 만큼 더한다(받은 때 − `cache_age_s` = 본문 시각).
- 판독이 들어오면 스냅샷 3초 캐시는 건너뛴다(창고키나 세대 — 캐시·폴백 절 참고). 카드를 지우면 판독 시각도 지워진다.

### `POST /api/fv/notify` — 팜뷰 → 주인님 텔레그램 한 통 (드문 알림 전용)
```json
{"key": "sold_fail:2026090603221474", "text": "차감 24시간 실패 — 챈가룽 2100만, 주인님 확인 필요", "pc_id": "PC-04b"}
```
- `key` 필수(1~200자) — ★같은 key 는 한 번만 보낸다★. `text` 필수(1000자에서 자름). `pc_id` 는 선택(카드 하나, `all` 은 400) — 주면 메시지 앞에 붙고 그 카드 로그에 `[팜뷰 알림] … (key …, 텔레그램 <id>)` 한 줄(`/api/fv/events` 의 `log`). 텔레그램 글은 `[팜뷰] PC-04b | <text>`.
- 음소거(`/telegram/mute`)는 안 본다 — 팜뷰 알림은 카드 알림이 아니다. 대신 상한이 있다.
- ★적어도 한 번★ 이다(정확히 한 번이 아니다) — 텔레그램이 받았는데 답이 늦어(10초) 서버가 502 로 답하면 재전송이 한 번 더 보낼 수 있다. 200 뒤로는 절대 다시 안 간다.

| 답 | 뜻 | 팜뷰가 할 일 |
|---|---|---|
| 200 `{ok:true, key, sent:true, message_id}` | 보냈다 | 끝 |
| 200 `{ok:true, key, dup:true, sent_at}` | 이 key 는 이미 보냈다(`sent_at` 은 서버가 장부를 못 쓴 드문 판에 `null`) | 끝 — 재시도가 안전하다 |
| 429 `{ok:false, limited:true, retry:true, retry_after_s}` | 상한 — 텔레그램까지 간 ★시도★(실패 포함) 1분 3 · ★실제로 보낸★ 알림 1시간 20, 그중 5칸은 `sold_fail:` 로 시작하는 key 몫(다른 key 는 15에서 멈춘다) | `retry_after_s` 뒤 ★같은 key★ 로 |
| 409 `{ok:false, busy:true, retry:true}` | 같은 key 를 지금 보내는 중 | 잠시 뒤 같은 key 로(두 번 안 간다) |
| 502 `{ok:false, reason:"send_failed", retry:true}` | 텔레그램 전송 실패 — key 는 안 쓰였다 | 같은 key 로 다시 |
| 503 `{ok:false, retry:true}` | 서버 장부(DB) 일시 오류 — 안 보냈다 | 같은 key 로 다시 |
| 503 `{ok:false, reason:"disabled", retry:false}` | 서버에 텔레그램 설정 없음 | 다시 보내도 소용없다 — 화면에만 |
| 400 / 401 | 본문(key·text 없음, 짝 없는 대리 문자는 `?` 로 바꿔 받는다) / 토큰 | 고친다 |

- 중복 막기·감사 장부는 서버 표 `fv_notify(key PK, pc_id, text, status, n, first_at, last_at, sent_at, message_id)` — 재배포에도 남는다(볼륨). `status` = `sent`|`dup`|`limited`|`send_failed`|`disabled`(마지막 결과, 단 `sent` 는 뒤 결과가 안 덮는다), `n` = 장부에 닿은 요청 수 — ★이미 보낸 key 의 dup 은 서버 기억으로 답하고 장부를 안 쓴다★(상한이 없는 dup 이 DB 를 두드리지 않게; 재시작 뒤 첫 dup 만 한 번 센다). 30일 지난 key 는 지운다(그 뒤 같은 key 는 새 알림).
- `GET /api/fv/notify?limit=50`(1~200) → `{ok:true, items:[행…]}` 새것부터 — 감사용 읽기.
- 상한·«지금 보내는 중»·보낸 key 기억은 서버 프로세스 안이다(재배포 때 비워진다 — 보낸 key 는 장부 표가 계속 막는다; 워커 하나 전제 — 여러 워커면 서버가 시작할 때 크게 경고한다).
