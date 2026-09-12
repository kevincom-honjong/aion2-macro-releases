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
| 에러 | 항상 `{"error": "...", "code": <HTTP 코드>}` |
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
        "uptime_hours": 3.4,
        "deaths_30m": 0,
        "abyss_kina": null,
        "trade_kina": 12345678,
        "gakin_kina": 23456789,
        "odd_energy": 1495,
        "awakening_ticket": 3
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
      "corridor_remaining": 0
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
- **`online`** 은 `status != "offline"` 이다. 「매크로가 살아 있나」를 더 엄격히 보려면
  `silent_s`(마지막 보고 이후 초)를 직접 문턱 잡아 쓰는 게 정확하다.
- **`alerts`** 는 서버 메모리 링버퍼(최근 200건 중 30건)다. **서버가 재시작하면 비워진다.**
  대시보드 배너는 원래 휘발이라 그걸 폴링으로도 보게 하려고 새로 담은 것이다.
- **`notice`** 는 설정 키 `notice` 값이다. 안 넣어놨으면 `null` 이다.
- **`server.disk_persisted`** 가 `false` 면 재시작마다 DB·스샷이 날아간다(위 샘플은 로컬 테스트라 false).
- **캐시** — 3초 안에 다시 부르면 같은 본문에 `cached: true`, `cache_age_s` 가 붙어 온다.

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

## 5. 에러 (전부 실제 원문)

| 상황 | HTTP | 본문 |
|---|---|---|
| `FV_TOKEN` 미설정 | 404 | `{"error": "Not Found", "code": 404}` |
| 토큰 없음/틀림 | 401 | `{"error": "X-FV-Token 이 올바르지 않습니다", "code": 401}` |
| 없는 카드 | 404 | `{"error": "카드 'PC-99' 가 없습니다", "code": 404}` |
| 모르는 명령 | 400 | `{"error": "모르는 cmd 'no_such_command' — GET /api/fv/command 로 목록을 보십시오", "code": 400}` |
| `since` 형식 오류 | 400 | `{"error": "since 가 ISO8601 이 아닙니다 (예: 2026-09-08T05:00:00Z)", "code": 400}` |
| 본문 없음/객체 아님 | 400 | `{"error": "JSON 본문이 필요합니다", "code": 400}` |
| 스냅샷 조립 실패 | 500 | `{"error": "스냅샷 조립 실패: …", "code": 500}` |

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
- ★토큰은 대시보드 비번과 다른 값으로★ — 이 토큰은 락아웃이 없다.

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
| 3 | `POST /api/fv/command` | `pc:"all"` 은 **400** — 목록을 펼쳐 보내고 8대 이상이면 `confirm_fleet:true` 를 같이. 응답 `ok` 는 전부 성공일 때만, 일부는 `partial:true` |
| 4 | `GET /api/fv/events` | `truncated:true` 면 `next_since` 로 **쉬지 않고 다음 장**(팜뷰 `pull_events` 최대 5장). 같은 초 유실 없음(서버가 경계 초를 다음 장으로 미룬다) |
| 5 | `today.slots_done` · `global.totals.bugs` | 서버 값이 정본 — `slots_done` 은 `completed && today!==false`, `totals.bugs` 는 물리 PC 단위 합. 팜뷰의 자체 재계산(`slots_today`)은 같은 규칙이라 그대로 둔다 |

이 문서는 `updater/FV_API.md` 가 정본이고 `farmview/docs/FV_API.md` 는 바이트 동일 사본이다 — `src/verify_all.py` 가 diff 로 지킨다.

## 2026-09-09 추가 — 캐릭터 합 4종
`progress.trade_kina`(거래키나) · `progress.gakin_kina`(각인키나) · `progress.odd_energy`(오드에너지, `"300(+1,195)/840"` → 300+1195 규칙) · `progress.awakening_ticket`(각성전 티켓) — 그 카드(pc_id) 캐릭터들의 **합**, 없으면 0. `global.totals` 에도 같은 이름으로 전체 합. `total_kina` 는 그대로(계정 창고값, 캐릭마다 중복이라 합치지 않는다). 서버 `main.py _fv_char_agg`.
