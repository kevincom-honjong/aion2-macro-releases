## ★OCR 라벨링 (2026-09-23 · 주인님 장부 #108 · 팜뷰 #125)★ — 매크로 ↔ 대시보드

> 2026-09-24 src 로 옮김(아이온2 재지시 #142 — 임시폴더는 사라진다). 코드에 넣었다(미배포). 서버 코드 = `server/ocr_label.py`(라우터, main.py 에는 연결 세 줄),
> 지키는 시험 = `server/tests/test_ocr_label.py`. 팜뷰 쪽 `/api/fv/ocr/*` 의 정본은 `updater/FV_API.md`
> «2026-09-23 (밤) 추가 › D» — 이 문서는 그걸 되풀이하지 않는다.
>
> ★**정본은 이 문서 하나**(2026-09-24 아이온2 결정)★ — 매크로 `lc/ocr_label.py`·`SHARED_ISSUES_아이온2.md` #108 은 여기를 가리킨다.
> ★**두 형식을 다 받는다(더하기만)**★: **형식 A** = 이 문서가 처음 정한 모양(dhash 16자리 · `/ocr/labels?since=<정수>&epoch=`) ·
> **형식 B** = 매크로 `lc/ocr_label.py`(1.1.1004 WIP)가 실제로 보내는 모양(dhash 64자리 · `prompt_sha1`·`sha1`·`phash` · `/ocr/labels?since=<실수 ts>` → §3-b).
> 형식 B 를 지키는 시험 = `server/tests/test_ocr_dual.py` — ★매크로 코드를 그대로 불러★ `before/after` → `ocr_label_net._send` → 서버 200 →
> 팜뷰 라벨 → `poll_now`(requests 가 실제로 만드는 since 글자) → `_merge` → `before()` 적중까지 왕복한다.

### 1. 무엇을 하나
매크로가 **제미나이에 보내는 이미지를 그 순간 서버에도 한 장 보낸다** → 주인님이 관제컴·폰의 `/ocr/label`(또는 팜뷰 «OCR» 탭)에서
정답을 치고 Enter → 라벨이 쌓인다 → 매크로가 `/ocr/labels` 로 끌어가 다음부터 쓴다. [잘못된 이미지] 는 «나쁨» 목록으로.

### 2. `POST /ocr/submit` — 매크로가 보낸다 (헤더 `X-Api-Key`, 테넌트는 키가 정한다)
```json
{"pc_id": "PC-07", "site": "odd_energy", "prompt": "숫자만 읽어라",
 "img_b64": "<PNG 또는 JPEG 바이트의 base64 — data:image/png;base64, 앞머리 허용>",
 "gemini_answer": "1,234/840", "local_answer": "1234/840",
 "dhash": "0f1e2d3c4b5a6978", "ts": 1790164002.4}
```
형식 B(매크로 `lc/ocr_label.after` → `ocr_label_net._send` 가 실제로 보내는 본문):
```json
{"site": "info_collector.py:read_kina", "line": 812, "prompt": "숫자만 읽어라", "prompt_sha1": "a1b2c3d4e5f6",
 "img_b64": "<base64>", "sha1": "<sha1(img_b64 문자열)>", "gemini_answer": "1,234/840", "local_answer": null,
 "dhash": "<64자리>", "phash": "<16자리>", "ts": 1790164002.4, "fn": "ocr", "pc_id": "PC-07"}
```
| 칸 | 규칙 |
|---|---|
| `site` | 이미지 종류 이름. 서버가 `[A-Za-z0-9가-힣_-]` 밖의 글자를 `_` 로 바꾸고 앞뒤 `_-` 를 걷은 것이 **저장 키**(최대 48자). 매크로는 처음부터 그 글자만 쓸 것 — 그래야 `/ocr/labels` 의 키가 보낸 이름과 같다. ★형식 B★ 는 `"파일.py:함수"` 원문을 보내도 된다 — 저장 키가 `파일_py_함수` 라 **두 표기가 같은 줄**(정확 일치·묶음이 같이 된다)이고, 원문은 `site_raw` 로 따로 남아 §3-b 행의 `site` 로 **글자 그대로** 돌아간다(제어문자만 빼고 200자). |
| ★캡차★ | `site` **원문 전체**(자르기 전)를 NFKD → 결합부호 제거 → NFC → casefold → 키릴·그리스 닮은꼴(а·с·р·е·о·х·т·һ·α·ο…)을 라틴으로 → **글자·숫자 아닌 것 전부 제거**(영폭·공백·구두점·밑줄) 한 뒤, `captcha`·`캡차`·`캡챠` 가 **어디든** 들어 있으면(`recaptcha`·`hCaptcha`·`login_captcha`·`캡 차` 포함) **400, 한 바이트도 저장 안 함**(디코드 전). ★캡차 이미지는 아예 보내지 않는다★(매크로 쪽에서도 막을 것). `capture`·`캡쳐` 는 캡차가 아니다. |
| `img_b64` | PNG(`89 50 4E 47 0D 0A 1A 0A`) 또는 JPEG(`FF D8 FF`) 만. **디코드 뒤 512KB 까지**. base64 글자 수가 상한을 넘으면 디코드 전에 413. 본문이 약 748KB(OCR_BODY_CAP) 를 넘으면 413 — Content-Length 가 있으면 읽기 전에, 없으면(청크 전송) **읽으면서 세다가 넘는 순간**. |
| `dhash` | **16자리(64비트) 또는 64자리(256비트) 16진수**(대문자는 소문자로 받는다) — 그 밖(길이·글자·꼬리 줄바꿈)은 400. 길이는 줄에 `dhash_bits`(64·256)로 따로 적는다. **형식 A** 계산법(매크로 전 PC 가 같아야 묶음이 맞는다): 회색조 → **9×8 로 축소(PIL `LANCZOS`)** → 각 줄에서 `왼쪽 > 오른쪽` 이면 1 → 줄 순서·왼쪽부터 **MSB 먼저** 64비트 → `'%016x'`. **형식 B** = 매크로 `lc/ocr_label.hashes`(회색조 → cv2 `INTER_AREA` 17×16 → `오른쪽 > 왼쪽` → 256비트 → `'%064x'`). ★**길이가 다른 둘은 절대 한 묶음이 안 된다**★(해밍 = 아주 먼 거리). 256비트는 **같은 dhash 만** 묶는다(`OCR_NEAR_MAX_256`=0 — 매크로 리뷰 «한 자리 차이가 4~5비트», 문턱 실측 전). |
| `prompt_sha1`·`sha1`·`phash` (형식 B) | 매크로 열쇠 — `prompt_sha1` = `sha1(prompt)[:12]`(16진 6~40자) · `sha1` = **`sha1(img_b64 문자열)`**(40자 — 서버가 파일에 쓰는 바이트 sha1 과 **다른 값**, 줄에 `csha1` 로) · `phash` = 16자리. **틀려도 거부하지 않고 빈칸**(형식 A 제출도 그대로 200). `sha1` 을 안 보내면 서버가 보낸 `img_b64` 글자로 같은 식으로 잰다. `line`·`fn` 은 무시. 정확 일치 재제출은 **비어 있던 칸만** 채운다 — ★알려진 틈★ 같은 그림·같은 site 를 **다른 프롬프트**로 보내면 처음 `prompt_sha1` 이 남아 두 번째 열쇠로는 적중이 안 난다. |
| `pc_id` | 필수. `clean_pc_id` 로 소독. 속도 상한 키는 **원문**을 NFKC·casefold·글자/숫자만 남기고 숫자 뒤 계정 접미사 한 글자(b~z)를 뗀 값(`PC-22`·`pc 22`·`PC-22d` = 한 대). |
| `prompt`·`gemini_answer`·`local_answer` | 문자열(아니면 빈칸). 각 2000·200·200자에서 자른다. `local_answer` 는 로컬 OCR 이 없으면 `""`. |
| `ts` | 매크로 시각(유닉스 초). 없어도 된다. |

**응답 200**
```json
{"ok": true, "id": 188, "cluster": 41, "dup": "new|exact|near", "near_dist": null,
 "status": "pending|labeled|bad", "label": null, "label_match": "exact|near|null", "count": 1, "cluster_count": 7}
```
- `dup:"exact"` = 같은 site 에서 **바이트가 똑같은**(sha1) 이미지가 이미 있다 → `count` 만 올리고 대기열에 새로 안 넣는다.
  그 이미지 파일이 디스크 상한 때문에 비워져 있었으면 **파일을 되살린다**(전체가 가득이면 되살리기만 건너뛴다 — 그래도 200).
- `capped: true` (있을 때만) = 이 묶음이 줄 상한(`OCR_CLUSTER_ROWS_MAX`, 64)에 찼다 — 새 줄·파일을 안 만들고 가장 가까운
  구성원의 `count` 만 올렸다. `id` 는 그 구성원, `dup:"near"`, `label_match` 는 `"near"`(대표여도 — 이 이미지는 사람이 본 그 한 장이 아니다) → 힌트로만.
- `dup:"near"` = 같은 site 에서 **dHash 해밍 거리 ≤ 4** 인 묶음 대표에 붙었다(`near_dist` = 거리). 라벨 하나가 묶음 전체에 붙는다.
- `label_match` = 이 라벨이 **이 이미지에** 붙은 방식. `"exact"` = 이 이미지가 묶음 **대표**(주인님이 실제로 본 그 한 장) →
  **정답으로 바로 써도 된다**. `"near"` = 대표가 아니다(비슷해서 묶였을 뿐 — 12 ↔ 13 도 dHash 1 차이일 수 있다) → **힌트로만**.
  구성원이 정확히 다시 와도(`dup:"exact"`) `label_match:"near"` 이고 `near_dist` 는 대표와의 거리. 라벨이 없으면 `null`.

**에러**(`{"detail": "..."}`): 400 캡차·형식·빈 site·dhash · 403 키 · 413 크기 · **429 = PC 한 대당 분당 30장 초과 또는
테넌트 합 분당 300장 초과** · **507 = 저장 공간이 가득(전체 HARD_CAP 을 넘는데 이 테넌트가 지울 수 있는 게 없다)**.
매크로는 **429·413·507·5xx·연결 실패를 조용히 버린다**(재시도 없음) — 이 제출은 학습용이지 게임 진행의 일부가 아니다.

### 3. `GET /ocr/labels?since=<cursor>&epoch=<epoch>` — 매크로가 끌어간다 (헤더 `X-Api-Key`)
```json
{"labels":       {"odd_energy": {"<대표 sha1>": "1234/840"}},
 "near":         {"odd_energy": {"<dhash>": "1234/840"}},
 "bad":          {"odd_energy": {"<sha1>": "<dhash>"}},
 "cleared":      {"odd_energy": ["<sha1>"]},
 "cleared_near": {"odd_energy": ["<dhash>"]},
 "cursor": 5120, "more": false, "reset": false, "epoch": "9f3c0a7be21d4c55"}
```
- **증분**: `since` 이후에 바뀐 이미지 줄만 온다. 처음엔 `since=0`(전부). 받은 `cursor` 와 `epoch` 를 **둘 다 파일에 저장**하고
  다음에 `since=<cursor>&epoch=<epoch>` 로 그대로 보낸다.
- `more:true` 면 한 번에 5,000줄까지만 왔다 — 곧바로 `since=cursor` 로 이어 읽는다.
- ★`labels` = **정답**. 묶음 **대표**(주인님이 실제로 본 그 이미지)의 sha1 과 그 정확한 재제출만 들어간다.★
  묶음의 다른 구성원(비슷해서 붙었을 뿐, 사람이 본 적 없는 이미지)은 **절대 `labels` 에 안 나가고** `near`(dhash → 라벨)에만 나간다.
- `near` = 묶음 구성원 전부(대표 포함)의 dhash → 라벨. **힌트 전용**(§4-3).
- `epoch` = **DB 세대**. 서버가 OCR 표를 처음 만들 때 한 번 정하는 난수(표 `ocr_meta`). 같은 DB 인 동안은 재시작해도 같다.
- `reset:true` = 다음 중 하나 → **가진 사전(labels·near·bad)을 통째로 버리고** 이 응답(과 `more` 이어 읽기)으로 다시 채운다.
  ① `since>0` 인데 `epoch` 가 **없거나 서버 것과 다르다**(서버 DB 가 새로 생김 — 새 DB 의 커서가 옛 커서를 이미 넘었어도 잡는다)
  ② 매크로 커서가 서버 커서보다 앞이다. `since=0` 은 `epoch` 없이 보내도 reset 이 아니다(처음 받기).
- `cleared` = 주인님이 **되돌리기** 해서 라벨이 없어진 이미지의 sha1 → 매크로 `labels` 사전에서 지운다.
  `cleared_near` = 같은 이미지들의 **dhash** → 매크로 `near` 사전에서도 지운다(안 지우면 되돌린 답이 힌트로 영영 남는다).
  `bad` 의 값(dhash)도 `near` 사전에서 지운다(라벨 → 나쁨으로 고친 경우).
- 되돌린(한 번이라도 라벨·나쁨 된) 묶음은 디스크 비우기가 **지우지 않는다** — `cleared` 가 반드시 한 번은 나간다.
- 같은 sha1 이 여러 번 바뀌면 마지막 상태만 온다(라벨 → 고치기 → 나쁨 …). 한 응답 안에서 sha1 은 `labels`·`bad`·`cleared` 중
  많아야 한 곳(대표가 아닌 라벨 구성원은 셋 어디에도 없고 `near` 에만).
- 라벨은 **UTF-8 그대로**(한글·기호, 앞뒤 공백 걷고 NFC, 최대 200자). ★라벨을 파일 이름·경로에 넣지 말 것★(옛 사고: 파일명 경로가 한글을 x 로 바꿨다) — 매크로 캐시도 JSON 한 파일에 `ensure_ascii=False`·`encoding="utf-8"` 로.
- 400 = `since` 가 0 이상의 정수(64비트)가 아님. ★단 `since` 에 `.`·`e` 가 있거나 `mode=ts` 면 §3-b(형식 B)★ — 2026-09-24 전엔 `since=1.5` 가 400 이었다.

### 3-b. `GET /ocr/labels?since=<ts>` — 형식 B (매크로 `lc/ocr_label_net.poll_now`, 2026-09-24)
- **알아보기**: `mode=ts` 이거나 `since` 에 `.`·`e` 가 있다. 매크로 since 는 파이썬 float 라 requests 가 늘 `0.0`·`1790164002.41` 꼴로 보낸다(시험이 requests 로 URL 을 만들어 확인).
```json
{"labels": [{"site": "info_collector.py:read_kina", "prompt_sha1": "a1b2c3d4e5f6", "sha1": "<sha1(img_b64)>",
             "label": "1,234/840", "ts": 1790164100.52, "deleted": false,
             "dhash": "<64자리>", "phash": "<16자리>", "status": "labeled"}],
 "since": 1790164100.52, "more": false, "mode": "ts", "epoch": "9f3c0a7be21d4c55"}
```
- 행 = ★**묶음 대표 한 장**★(사람이 본 그 그림 — 반증 R1)이면서 매크로 열쇠(`prompt_sha1`·`sha1`)가 있는 줄만. 구성원·열쇠 없는 줄(형식 A·/bugs 씨앗)은 안 나간다(§3 커서 방식엔 그대로 나간다).
- `status:"labeled"` → `label` · `"bad"` → `label:"bad image"`(매크로 `BAD_LABELS`) · 되돌려 대기로 → `deleted:true, label:""`(매크로가 그 열쇠를 지운다).
- `ts` = 그 줄의 라벨이 **서버에서 바뀐 시각**(줄 `lts`) — 테넌트마다 ★**늘 앞선 값보다 크다**★(`ocr_meta` `lts:<tenant>`: 같은 순간 두 번·시계가 뒤로 가도).
  `ts > since` 인 줄만 ts 순으로. 매크로는 받은 최대 ts 를 다음 since 로 보낸다(지금 매크로 코드 그대로). 응답 `since` = 이번에 준 최대 ts.
- 한 번에 5,000줄(`OCR_LABELS_PAGE`). 넘으면 `more:true` 이고 **마지막 ts 가 쪽 경계에서 갈리지 않게** 자른다(한 ts 가 한 쪽보다 크면 그 ts 는 통째로).
- 옛 형식으로 먼저 와서 라벨 된 대표에 매크로가 **같은 바이트**를 보내 열쇠가 채워지면 그때 ts 가 올라가 행이 나간다.
- ★옛·새 프롬프트 열쇠(매크로 1.1.1006 예시값 교체)★ — `ocr_label.PROMPT_SHA1_OLD_NEW`(20쌍, 정본 표 = `src/SHARED_ISSUES_아이온2.md` «제미나이 프롬프트 예시값 교체», 시험 A-1 이 두 표를 대조)에 있는 `prompt_sha1` 이면 같은 행을 **다른 판 열쇠로도 하나 더** 보낸다(`alias_of` = 원래 열쇠, 같은 sha1·라벨·ts·deleted). 새 키 `62fd6da2e23d`(키나 세 칸 합침)는 옛 키 셋 모두로. 함대가 옛·새 판 섞여 돌아도 어느 쪽 라벨이든 적중.
- ★알려진 틈★ 매크로 `LABEL_MAX`=64, 서버 라벨 상한 200 — 65자 이상 라벨은 매크로가 버리고 `too_long` 으로 센다.
- ★알려진 틈 (2026-09-24 아이온2 반증)★ /bugs 씨앗(`ocrdiff_*`·`oddfail_*`)으로 들어온 그림은 `prompt_sha1` 이 없다 — 그 라벨은 형식 B 로 **안 나간다**(열쇠를 못 만든다). 매크로가 같은 그림을 형식 B 로 제출해 열쇠가 채워지면 그때 나간다.
- ★알려진 틈★ 이 기능 배포 **전에** 붙은 라벨은 `lts` 가 NULL 이라 형식 B 로 안 나간다 — 매크로가 그 그림을 다시 제출해 빈 칸이 채워질 때(`_fill_extra`, 라벨·bad 묶음이면 lts 올림) 나간다.
- 400 = ts since 가 0 이상 유한한 수(< 1e11)가 아님(`nan`·`inf`·음수·`1.2.3`).

### 4. 매크로가 쓰는 법 (권장)
1. 제미나이에 보낼 이미지가 생기면 **먼저 로컬 사전을 본다**: 같은 site 의 `labels[sha1]` 가 있으면 그게 정답 → 제미나이를 안 부른다.
   (`labels` 에는 주인님이 본 대표 이미지만 있으니 정확 일치 = 사람이 확인한 답이다.)
2. `bad[sha1]` 에 있으면 그 이미지는 «주인님이 잘못된 이미지라 한 것» → 제미나이도 믿지 말고 다시 찍는 쪽으로.
3. `near` 는 **힌트로만** 쓴다(작은 숫자 글자는 12↔13 도 dHash 가 4 이내일 수 있다). 제미나이 답과 `near` 라벨이 같으면 확신 +, 다르면 제미나이/로컬을 믿고 로그 한 줄.
   `/ocr/submit` 응답의 `label` 도 `label_match:"exact"` 일 때만 정답, `"near"` 면 똑같이 힌트로만.
4. 사전에 없으면 제미나이를 부르고, **같은 스레드를 막지 않게** 백그라운드로 `/ocr/submit`(타임아웃 3초).
5. 동기화는 5분마다 + 부팅 때 한 번. `cursor`·`epoch` 를 한 파일에 같이 저장한다. `reset:true` 면 사전을 비우고 다시 채운다.
   `cleared` → `labels` 에서, `cleared_near` → `near` 에서 지운다. 서버가 죽어 있으면 가진 사전 그대로 쓴다.

### 5. 서버 쪽 사실 (바꾸면 매크로가 틀린다)
| 상수(`ocr_label.py`) | 값 | 뜻 |
|---|---|---|
| `OCR_IMG_MAX` | 512KB | 한 장 상한(디코드 뒤) |
| `OCR_RATE_PER_MIN` | 30 | PC 한 대당 1분 미끄럼 창. 키 = (테넌트, **정규화한 기본 pc id** — §2 `pc_id`) |
| `OCR_RATE_TENANT_PER_MIN` | 300 | 테넌트 한 곳 합계 1분 미끄럼 창(pc id 를 바꿔 끼워도 여기서 막힌다) |
| `OCR_BODY_CAP` · `OCR_SMALL_BODY_CAP` | 약 748KB · 16KB | `/ocr/submit` · 사람 쪽 POST(label·skip·bad) 본문 상한 — 청크 전송도 읽으면서 센다 → 413 |
| `OCR_NEAR_MAX` | 4 | 묶음 해밍 거리(대표와만 잰다 — 사슬로 번지지 않게) — 64비트(16자리) dhash |
| `OCR_NEAR_MAX_256` | 0 | 256비트(64자리) dhash 묶음 거리 — 같은 값만(문턱 실측 전, §2 `dhash`) |
| `OCR_DISK_CAP` | 500MB (env `OCR_DISK_CAP_MB`) | **테넌트마다**. 제출한 테넌트의 합이 넘으면 **그 테넌트의** 한 번도 라벨 안 된 대기 묶음을 오래된 것부터 통째로(파일·줄) → 그래도 넘으면 **그 테넌트** 나쁨의 파일만(줄 유지). 되돌리기로 대기에 돌아간 묶음(`ocr_hist` 있음)은 안 지운다 |
| `OCR_DISK_HARD_CAP` | 1000MB (env `OCR_DISK_HARD_CAP_MB`) | **전체 합**. 이걸 넘을 때만 **제출한 테넌트의** 라벨 된 이미지 파일을 지운다(대표 아닌 것 먼저). 그 테넌트 것을 다 지워도 모자라면 **아무것도 안 지우고 507**. **라벨 줄은 절대 안 지운다** · **남의 테넌트 줄·파일은 절대 안 건드린다** |
| id | 부호 있는 64비트 | 본문 `id` 가 범위 밖이면 400, 경로 `/ocr/img/{id}` 면 404 (웹·팜뷰 7 라우트 전부) |
| `OCR_LABELS_PAGE` | 5000 | `/ocr/labels` 한 번의 줄 수 |
| 저장 위치 | env `OCR_DIR`, 없으면 `DB_PATH` 옆 `ocr/` | `<site>/<sha1>.png|.jpg`(main 테넌트) · `@<tenant>/<site>/…`(테넌트 이름이 소문자 ASCII `[a-z0-9_-]{1,32}` 일 때) · 그 밖(한글·대문자·기호)은 `@@<sha1(tenant) 20자>/<site>/…` — 두 테넌트가 한 폴더를 쓰는 일이 없다. 한글 site 는 폴더 이름만 `u<해시>` |
| 표 | `ocr_cluster`·`ocr_img`·`ocr_hist`·`ocr_seq`·`ocr_meta`(epoch·`lts:<tenant>`) (같은 DB_PATH, 처음 쓸 때 `CREATE TABLE IF NOT EXISTS`). `ocr_img` 에 2026-09-24 열 여섯 더함(`site_raw`·`prompt_sha1`·`csha1`·`phash`·`dhash_bits`·`lts`) — 옛 DB 는 처음 쓸 때 `ALTER`(있으면 건너뜀), 옛 줄은 NULL | 모든 줄에 `tenant`, 모든 조회가 tenant 로 갇힌다 |
| 라벨 | NFC · 서식 문자(Cf: 영폭 공백·ZWJ·BOM·방향 표시)와 한글 채움 문자 제거 · 앞뒤 공백 제거 · 200자 | 그 뒤 비면 400 «라벨이 비었습니다» |

### 6. 사람 쪽 (참고 — 매크로가 부르지 않는다)
- 대시보드 세션: `GET /ocr/label`(화면, 로그인 안 했으면 `/login`) · `GET /ocr/queue` · `GET /ocr/img/{id}` · `POST /ocr/label {id,label}|{id,bad:true}` ·
  `POST /ocr/skip {id}` · `POST /ocr/undo` · `GET /ocr/history` · `GET /ocr/stats`. 에러 `{"detail"}`.
- 팜뷰 `X-FV-Token`: `/api/fv/ocr/{queue,img/{id},label {id,text},bad {id},skip {id},undo,history,stats}` — **같은 저장소·같은 핵심 함수**,
  에러 `{"ok":false,"error","err","code"}`(= `main._fv_err`, 2026-09-24 v4), 테넌트 = `FV_TENANT`. 정본 `updater/FV_API.md` D.
- **/bugs 씨앗(2026-09-24 #125)**: `POST /ocr/seed_bugs`(세션) · `POST /api/fv/ocr/seed_bugs`(팜뷰) · 큐 첫 조회 자동 · `/bugs` 업로드 훅(`main.upload_bug` → `ocr_label.seed_upload_hook`). 전부 `submit_core`(= `/ocr/submit` 과 같은 저장 길 — 정확 일치·묶음·줄 상한·비우기·507)로 넣는다. dHash 는 서버가 순수 파이썬으로 잰다(`png_dhash` — 9x8 칸 평균, 64비트). 같은 sha1 이 이미 있으면 건너뛴다(count 안 올림). 시험 `tests/test_ocr_seed.py`.
- 두 쪽의 되돌리기는 **한 줄 기록(ocr_hist)** 을 공유한다 — 팜뷰에서 되돌리면 웹에서 단 마지막 라벨도 되돌아간다(같은 테넌트).
- 통계 `gemini_disagree_rate` = 라벨 된 이미지 중 제미나이 답이 비지 않은 것에서 `NFKC·소문자·공백 제거` 후 라벨과 다른 비율. `local_*` 도 같다.
