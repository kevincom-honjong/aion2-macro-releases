# HANDOFF — AION2 매크로 (인수인계 스냅샷)

작성 시각 기준 실제 파일/‏git 재확인 후 작성. 불확실한 건 **[확인 필요]** 표기.
프로젝트 루트: `C:\Users\USER\Desktop\src\` (하위: `lc`=매크로 소스, `updater`=배포 git repo, `web`=서버 로컬편집본)

---

## ⚠️ 먼저 알아야 할 구조 (중요)
- **git repo는 `src/updater` 하나뿐.** `src/lc`(매크로 파이썬 소스)·`src/web`·`src` 는 **git 추적 안 됨.**
  → 매크로 소스 변경 이력은 git에 없음. 소스의 "현재 상태" = `src/lc/*.py` 파일 그 자체.
  → `updater` repo가 추적하는 건: `exe/혼종_통합_자동.exe`(빌드산출물), `server/`(대시보드), `version.json`.
- **`src/web/server/`와 `src/updater/server/`는 동일 내용(현재 IN_SYNC 확인함).** 서버 수정 시 둘 다 맞춰야 함(한쪽 수정→다른쪽 복사). 실제 배포되는 건 `updater/server`.
- 매크로 진입점: `src/lc/main.py`. 빌드 스펙: `src/lc/혼종_통합_자동.spec` (name=혼종_통합_자동, console=True).

---

## 현재 상태

### git (updater repo)
- `git status`: **clean** (uncommitted 없음)
- `git log -3`:
  - `5cd0e5a` v1.1.214: 판매 큐브 스캔에서 하단경계행(626) 제외 → 헛클릭 감소
  - `58fdc58` v1.1.213: 판매 치명버그 — 팝업없을때 ESC가 거래소창 닫던 것 제거
  - `ad7ae68` v1.1.212: 판매 STEP 플래그 + 전제조건 검증
- **현재 배포 버전: v1.1.214** (`version.json`과 `server/version.json` 둘 다 1.1.214, sha256 `d97a506c…`). Railway 서버도 1.1.214 서빙 확인함.

### 동작 확인된 것
- **대시보드 서버 재시작 자동 새로고침** (`/ping` boot_id 폴링) + **stateless 세션**(HMAC, env `SESSION_SECRET`) — 라이브 검증됨.
- **각 PC 카드 "사망(30분)" 표시** — deaths_30m, death_events 테이블 + 60초 디바운스. 검증됨.
- **정보수집 OCR 수정**(물약/귀환주문서/성역 좌표) — 실제 Gemini OCR로 검증 후 배포(1.1.202).
- **전투 로직 단순화**(전 직업 공통 R-홀드) + 치유성/호법성 버프 별도 스레드 + 카메라 95% + 마우스 직렬화(mouse_lock, 카메라 하늘봄 버그 수정) — 배포됨.
- **판매(sell_all)**: 어느 정도 굴러감(캐릭 전환·출석부·시장진입·골드감지·지정가입력·거래소전환 다 동작). 사용자 실사용 로그로 확인.

### 미완성 / 진행 중 (→ 아래 "중단 지점" 참조)
- **몹 없음 감지(no-mob detection) 신기능** — R&D(색 보정)만 하고 **코드 미작성.** 최우선 재개 대상.

### 알려진 버그 / 미결
- **판매: 판매불가 아이템 헛클릭 잔존 가능성.** 626행 제외로 줄였으나, 등록 후 큐브 **리플로우로 스냅샷 좌표가 밀리면** 엉뚱한 칸 우클릭 가능성 있음(현재는 고정 스냅샷 역순 for). 재현되면 "등록마다 재스캔"으로 전환 검토. → 사용자에게 헛클릭 순간 로그/스샷 요청해둔 상태.
- **`/debug/deaths` 임시 엔드포인트** (`updater/server/main.py:332`, 세션인증) — 진단용으로 넣음. **추후 제거 필요.**
- **거래소 지정가 localStorage 저장** — 서버 DB가 휘발성(재배포 초기화)이라 브라우저 localStorage에 저장하는 방식. PC/브라우저 바뀌면 안 넘어감(의도된 트레이드오프).
- **판매취소 기능** — 사용자가 나중에 스펙 준다고 함(오래된 안 팔린 템 취소). 미착수.
- **좌표 크롬 오프셋 [확인 필요]**: 판매 좌표는 PC-05 스샷 기준. PC마다 크롬 헤더 오프셋으로 미세하게 어긋날 수 있음(과거 이슈). 골드/팝업 감지는 밝기 강건 지표로 바꿔 PC차 대응했지만, 클릭 좌표(토글·확인버튼·숫자패드)는 고정값.

---

## 파일 맵 (이번 세션에 손댄 파일)

### 매크로 소스 (`src/lc/` — git 미추적, 현재 파일이 곧 최신)
| 파일 | 역할 | 현재 상태 |
|------|------|-----------|
| `sale.py` | ★판매 핵심★. `sale_routine`(옛 단일슬롯, 일일종료 판매용·유지) + `sell_all_routine`(신규 전 캐릭 순회) | 완성·배포됨(1.1.214). 헛클릭 잔존 가능성 위 참조 |
| `config.py` | 좌표·이미지·상태·헬퍼. `SALE_*` 좌표, `scan_gold_slots()`(골드감지), `mouse_lock`/`camera_rotate`/`m_click`, `SALE_CUBE_ROWS`(7행, 626제외) | 배포됨 |
| `combat.py` | 전투. `combat_routine`(전직업 공통 R-홀드), `buff_maintenance_loop`(15초 치유성5/호법성7·8), `battle_movement_camera`(카메라95%) | 배포됨 |
| `loot.py` | 원격명령 디스패처 `_handle_remote_command`. `sell_all` 추가, subquest/sealed/wardrobe/mission/shugo 제거 | 배포됨 |
| `main.py` | 진입점·스레드 시작. `buff_maintenance_loop` 스레드 등록 | 배포됨 |
| `info_collector.py` | 정보수집 OCR. 물약(436,662,32,14)/귀환주문서=F4(576,662,32,14)/성역(173,188,52,15) 좌표수정 + `_parse_count` 상한가드 | 배포됨(1.1.202) |
| `navigation.py` | 추출설정 등. "보급 의뢰 제외" 체크→해제로 변경 | 배포됨(1.1.201) |
| `recovery.py` | 복구/감시. confirm_watch·error_close 클릭을 `config.m_click`으로(마우스 직렬화) | 배포됨 |
| `혼종_통합_자동.spec` | PyInstaller 빌드 스펙 | 변경 없음 |

### 배포 repo (`src/updater/` — git 추적)
| 파일 | 역할 | 상태 |
|------|------|------|
| `server/main.py` | FastAPI 대시보드. /ping, stateless세션, deaths_30m, 판매버튼/가격확정, `/debug/deaths`(임시) | 배포됨. **web/server와 동일 유지 필수** |
| `server/database.py` | aiosqlite. `death_events` 테이블 + `upsert_status` dead전환감지 + 60초디바운스 + `get_death_counts_since` | 배포됨 |
| `server/version.json`, `version.json` | 버전/sha256 (둘 다 1.1.214) | 커밋됨 |
| `exe/혼종_통합_자동.exe` | 빌드된 exe (1.1.214) | 커밋됨 |

### 서버 로컬편집본 (`src/web/server/main.py`, `database.py`) — updater/server와 IN_SYNC

---

## 중단 지점 (가장 중요)

### 작업 중이던 것: **몹 없음(no-mob) 감지 신기능**
- 사용자 요구: "사냥 중 몹 없는데 허공에 삽질할 때가 많다. 현재 몹없음 감지가 너무 빨라서 안 됨 → 교체하고 싶다."
- **현재 매크로의 '몹 없음 감지' 메커니즘을 코드에서 못 찾음** → **[확인 필요]**: combat.py/navigation.py/warning.py/config 이미지 다 뒤졌으나 별도 no-mob 감지 없음. 사냥종료 감지는 `finish`/`finish2` 이미지(키나 참→일일종료)뿐. 사용자가 "만들어놨다"는 게 어디인지 재확인 필요.

### 마지막으로 한 것 (코드 아님, R&D)
- 사용자가 스샷 2장 제공: 몹 **선택 안 됨** vs **선택됨**.
  - 스샷 저장: `<scratchpad>/mob_no.png`, `<scratchpad>/mob_yes.png` (scratchpad = `C:\Users\USER\AppData\Local\Temp\claude\C--Users-USER-Desktop-src-web\cada2b4a-edfc-4038-b31f-7b69b34f5786\scratchpad`)
- **차이 발견**: 몹 선택하면 **화면 상단 중앙에 빨간 타겟 체력바** 뜸(몹명 "46 홍옥의 이끼 게"). 빨간 바 ≈ **x 510~755, y 42~52**.
- **색 보정 완료** (샘플박스 `(515,44,600,50)`, 중앙 엠블럼 피함):
  - 선택됨: RGB **(228,32,20)**, redness(R−max(G,B))=**196**, HP빨강픽셀비율=**1.00**
  - 선택안됨(지형): RGB (67,47,45), redness=**19**, 비율=**0.02**
  - → **redness > ~100 (또는 HP빨강비율 > 0.5)** 이면 몹 선택됨으로 명확히 갈림. (이 맵이 붉은 지형인데도 채도 차이로 분리됨)

### 다음 한 스텝
1. **몹 선택 감지 헬퍼 작성** (예: `config.is_mob_targeted()`): 위 박스 영역 캡처 → redness>100이면 True.
   - mss로 `{"left":515,"top":44,"width":85,"height":6}` 캡처, R−max(G,B) 계산.
2. **그 다음 사용자에게 확인**: (a) 현재 "몹없음 감지"가 진짜 어디 있는지, (b) 몹 없을 때 **무슨 동작**을 시킬지(이동? 재타겟? 대기?), (c) "너무 빠르다"의 정확한 증상. → 이거 받고 통합.
   - **아직 사용자가 (b)(c)를 안 줬으므로, 통합 로직은 스펙 받기 전 짜지 말 것.**

---

## 핵심 결정 (새 세션이 모르면 엉뚱하게 갈아엎을 것들)

### 판매(sell_all) 설계
- **노란템(전설 등급)만 판매.** 판단 2중:
  1. `scan_gold_slots()`: 큐브 슬롯 코너의 **금색 배경**(판매등록 화면에선 판매불가=회색이라 자동제외). 지표 **R+G−2B > 95, R>G, R>100** (절대값 아닌 **상대 지표** — PC마다 밝기 달라서. PC-01 어두운 금색130 ~ PC-05 밝은340 다 커버). **절대임계(R>140,B<60)로 되돌리지 말 것 — 어두운 PC 놓침.**
  2. `_sell_popup_is_yellow()`: 우클릭 후 **가격팝업의 아이템 이름 색**(전설=노랑 R−B>40)으로 최종 등급 검증. 큐브 감지가 옆칸 글로우 번짐으로 흰색 오탐해도 여기서 차단. **이 2차 검증 제거 금지**(흰색템 올라가는 버그 재발).
- **가격**: 월드거래소=자동 최저가(그대로 확인), 거래소=대시보드 지정가를 숫자패드로 입력. **지정가 입력은 백스페이스로 지우지 말 것**(필드가 첫 입력에서 자동 교체됨 — 사용자 확인). `_sell_ocr_price_field`로 OCR 검증.
- **팝업 감지** `_sell_dialog_open()`: 확인버튼(722,572) 금색 = R+G−2B>60, R>G. 임계 60은 어두운 PC 대응(팝업無는 R<G라 어차피 제외). **팝업 없으면 ESC 절대 금지**(v1.1.213 치명버그: 팝업없는데 ESC→거래소창 닫힘). 팝업 실제로 떴을 때만 ESC.
- **등록 순회**: 스캔 스냅샷 **역순(뒤→앞) for** (등록 시 뒤 슬롯만 밀려 앞 좌표 유효 + 고정리스트라 무한루프 불가). 각 우클릭 후 **팝업 최대 4s 폴링**(렉 대응).
- **STEP 플래그**: `_sell_current_char`가 STEP0~6 명시 검증(각 단계 화면 확인 통과해야 다음). `_sell_on_register_screen()`=fi("sale_register_tab")로 판매등록 화면 전제 확인. 사용자 요구("단계 진행 플래그 없어 꼬임")로 넣음. **이 검증 구조 유지.**
- **캐릭 전환** `_sell_switch_light`/`_sell_goto_char_select`: 이미 캐릭선택창이면 메뉴버튼 안 누름(캐릭선택창에선 MENU_BTN=닫기버튼이라 창 꺼짐). 인게임이면 메뉴→캐릭선택→팝업.
- **끝나면**: `_sell_goto_char_select()`로 캐릭선택창 이동(사냥 재개 안 함). 사용자 요구.
- **가격 하한** `SELL_MIN_PRICE=1000`: 0/누락이면 무료판매 방지 중단. **제거 금지**(적대적 리뷰 CONFIRMED).

### 버린 접근법 (다시 시도하지 말 것)
- 골드감지 **≥2 코너 요구**: 진짜 금색템도 아이콘이 코너 가려 1코너만 잡히는 경우 있어서 **놓침**. 폐기.
- 골드감지 **절대임계 R>140,B<60**: PC마다 밝기 달라 어두운 PC 금색 놓침. → R+G−2B 상대지표로 대체.
- 세션 서명키를 **비번/API_KEY에서 파생**: 토큰 캡처로 비번 오프라인 크랙 가능(리뷰 CONFIRMED). → env `SESSION_SECRET` 또는 랜덤.
- 옛 단일슬롯 `sell` 명령으로 판매: 등급 안 가리고 고정슬롯 막 등록(흰색템 올라감). → sell_all로 대체(대시보드 버튼 전부 sell_all). 단 `sell`/`sale_routine`은 일일종료 판매 내부용으로 **남겨둠**.

### 배포 아키텍처 (과거 확립, 유지)
- exe(71MB)는 raw.githubusercontent 429 때문에 **GitHub Releases**로 배포. 이미지는 jsDelivr. 새 버전마다 릴리스+에셋 업로드 필수.
- Gemini OCR: `thinkingBudget=0` 필수. config.GEMINI_API_KEY.

---

## 실행 / 검증

### 매크로 빌드 → 배포 (전체 사이클)
```bash
cd /c/Users/USER/Desktop/src/lc
python -c "import py_compile; py_compile.compile('sale.py',doraise=True)"   # 컴파일 체크
pyinstaller 혼종_통합_자동.spec --noconfirm                                  # 빌드(~2-3분)
cp dist/혼종_통합_자동.exe ../updater/exe/혼종_통합_자동.exe                  # exe 복사
cd ../updater
SHA=$(sha256sum exe/혼종_통합_자동.exe | cut -d' ' -f1)
# version.json + server/version.json 둘 다 version/sha256 갱신 (python으로)
git add exe/ server/version.json version.json && git commit -m "vX.X.X: ..."
git pull --rebase origin main && git push origin main
# GitHub Release + 에셋 업로드:
#   scratchpad/upload_release.py 의 tag/asset_name을 새 버전으로 sed 치환 후,
#   GH_TOKEN=$(git credential fill로 추출) 넣고 실행
```
- `upload_release.py` 위치: `<scratchpad>/upload_release.py` (repo=kevincom-honjong/aion2-macro-releases)
- **주의: exe는 `dist/`에 빌드됨 → 반드시 `updater/exe/`로 복사해야 배포됨.**

### 배포 검증
```bash
# 서버가 새 버전 서빙하는지 (POST /check, 구버전으로 위장)
curl -s -X POST "https://web-production-8d4c.up.railway.app/check" \
  -H "Content-Type: application/json" \
  -d '{"exe_version":"1.1.213","image_hashes":{},"updater_version":"3.0.2"}'
# → exe_update.version 이 새 버전이면 배포 성공 (Railway 재배포 ~30초)
```
- 대시보드: `https://web-production-8d4c.up.railway.app` (비번 0602)
- 서버 전용 변경(대시보드)은 exe 빌드 없이 `updater/server/` 커밋+푸시 → Railway 자동 재배포.

### 서버 로컬 스모크 테스트
```bash
cd /c/Users/USER/Desktop/src/updater/server
env DASHBOARD_PASSWORD=0602 API_KEY=k SESSION_SECRET=x DB_PATH=/tmp/t.db BUGS_DIR=/tmp/tb SCREENSHOTS_DIR=/tmp/ts \
  python -m uvicorn main:app --port 8599 --host 127.0.0.1   # --reload 쓰지 말 것(hang)
```

### 버그 스샷 받기
- 사용자가 대시보드 URL(`/bugs/image/...`)로 주면 **재배포 전에** 다운로드할 것(휘발성 저장 `/data/bugs`라 재배포 시 삭제됨). 채팅 첨부가 더 안전.

---

## 다음 할 일 (우선순위 순)

1. **[진행중] 몹 없음 감지 완성.**
   - a. `is_mob_targeted()` 헬퍼 작성(위 색 보정값 사용: 박스(515,44,600,50)ish, redness>100).
   - b. 사용자에게 확인: 현재 감지 위치 / 몹 없을 때 동작 / "너무 빠르다" 증상. **스펙 받고 통합.**
2. **판매 실사용 안정화**: 사용자 로그로 헛클릭·꼬임 재확인. 리플로우 문제면 "등록마다 재스캔"으로 전환.
3. **판매취소 기능**: 사용자 스펙 대기(오래된 안 팔린 템 취소).
4. **`/debug/deaths` 임시 엔드포인트 제거** (`updater/server/main.py`) — 판매/사망 안정화 후 정리.
5. (선택) 매크로 소스(`lc`)를 git repo로 만들지 검토 — 현재 소스 이력이 git에 전혀 없음.

---

## 메모리 파일
- 프로젝트 요약: `C:\Users\USER\.claude\projects\C--Users-USER-Desktop-src-web\memory\project_aion2_macro.md` (v1.1.214까지 일부 반영, **[확인 필요]**: 판매 후반 버전(207~214) 상세는 이 HANDOFF가 더 최신).
- 사용자: 항상 **존댓말**, "주인님" 호칭 (feedback 메모리).
