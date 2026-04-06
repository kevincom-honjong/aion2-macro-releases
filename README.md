# 매크로 자동 업데이트 시스템

## 전체 구조

```
[개발 PC] 코드 수정 + exe 빌드
    ↓ git push
[GitHub 레포] macro-releases
    ↓ GitHub Actions 자동 실행
[version.json] 버전/해시 자동 갱신
    ↓
[Railway] FastAPI 서버 (캐시 + diff 계산)
    ↓
[각 PC] updater.exe 실행 → 체크 → 다운로드 → 매크로 실행
```

---

## 초기 세팅 순서

### 1. GitHub 레포 만들기

```
GitHub에서 새 레포 생성: macro-releases
파일 구조:
├── version.json          ← 이 파일 업로드
├── exe/
│   └── macro.exe         ← 빌드된 exe 업로드
├── images2/
│   ├── loot.png          ← C:\auto\images2 파일들 전부 업로드
│   ├── none.png
│   └── ... (90개+)
├── version_scripts/
│   └── update_version.py  ← 이 파일 업로드
└── .github/
    └── workflows/
        └── update_version.yml  ← 이 파일 업로드
```

### 2. version.json 초기 설정

`version.json`에서 `YOUR_GITHUB_USERNAME` 을 실제 GitHub 유저명으로 교체:
```json
"download_url": "https://raw.githubusercontent.com/실제유저명/macro-releases/main/exe/macro.exe"
```

### 3. Railway 서버 배포

1. Railway 가입 (railway.app)
2. 새 프로젝트 → GitHub 연동 → `server/` 디렉토리 배포
3. 환경변수 설정:
   - `GITHUB_OWNER` = 실제 GitHub 유저명
   - `GITHUB_REPO`  = `macro-releases`
   - `GITHUB_BRANCH` = `main`
   - `GITHUB_TOKEN` = (public 레포면 불필요, private이면 필요)
4. 배포 완료 후 Railway URL 확인 (예: `https://xxxxx.railway.app`)

### 4. updater.py 설정

`client/updater.py` 열어서 UPDATE_SERVER 교체:
```python
UPDATE_SERVER = "https://xxxxx.railway.app"  # ← Railway URL로 교체
```

### 5. updater.exe 빌드 및 배포

```cmd
cd client
pip install pyinstaller requests
build_exe.bat
```

생성된 `dist\updater.exe` 를 각 PC의 `C:\auto\` 에 복사.

---

## 업데이트 배포 방법 (이후 루틴)

```
1. 코드 수정 후 exe 빌드 (pyinstaller)
2. git add exe/macro.exe
   git add images2/변경된파일.png   (이미지 변경 있으면)
   git commit -m "v1.0.X 업데이트"
   git push
3. GitHub Actions 자동 실행 → version.json 버전/해시 자동 갱신
4. 끝! 각 PC가 다음 번 updater.exe 실행 시 자동 업데이트
```

---

## 각 PC 사용법

`C:\auto\updater.exe` 를 실행하면:
1. 서버에서 최신 버전 확인
2. exe/이미지 변경분 자동 다운로드
3. 매크로 exe 자동 실행

**Windows 작업 스케줄러에 등록 권장:**
- 트리거: 로그인 시 / 매일 아침 6시
- 작업: `C:\auto\updater.exe` 실행

---

## 안전장치

| 상황 | 동작 |
|------|------|
| 서버 연결 실패 | 기존 버전으로 매크로 실행 |
| exe 다운로드 실패 | 백업(.bak)으로 복구 후 실행 |
| 이미지 일부 실패 | 성공한 것만 적용, 실패분은 다음 기회에 |
| SHA256 불일치 | 임시파일 삭제, 다운로드 재시도 |
| `C:\auto\info.txt` | 절대 건드리지 않음 (PC별 캐릭 설정) |

---

## 디렉토리 구조

```
C:\auto\
├── updater.exe         ← 업데이터 (각 PC 배포)
├── macro.exe           ← 메인 매크로
├── macro.exe.bak       ← 이전 버전 백업 (자동 생성)
├── info.txt            ← PC별 설정 (건드리지 않음)
├── version.json        ← 현재 버전/해시 (updater가 관리)
├── updater.log         ← 업데이터 로그
└── images2/            ← 이미지 템플릿 (updater가 관리)
    ├── loot.png
    └── ...
```
