"""ocr_names.py — #436 지역 이름 목록 + 한 글자 오독 보정 (lc/map_names.py 의 ★사본★)

왜 사본인가: 서버(Railway)는 updater 저장소만 배포해 lc/ 를 못 본다. 정본은 lc/map_names.py(MAP_NAMES·snap) 이고
tests/test_ocr_436.py 가 둘이 같은지 지킨다(lc 가 곁에 있을 때) — 바꾸면 양쪽을 같이.
쓰는 곳: ocr_label.stats_core — 맵 이름 site 의 제미나이 답을 «매크로가 snap 한 뒤» 의 값으로 라벨과 비교한다(사전 원문 오독이 불일치율을 부풀리지 않게).
"""

MAP_NAMES = (
    "홍옥의 섬", "정령의 섬", "베르테론 요새 폐허", "드라나 가공구역", "루브레인 구릉지", "환영신의 정원",
    "엘듄강 중류", "어비스 회랑", "데바 생체 연구기지", "갈라진 남쪽 추락지", "갈라진 북쪽 추락지",
    "라 미렌 요새 남쪽 잔해", "라 미렌 요새 북쪽 잔해", "붉은 가시 왕관섬", "영원의 섬", "아울라우 부락",
    "불멸의 섬",
)


def _key(s: str) -> str:
    return "".join(str(s or "").split())


def lev(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def snap(text: str) -> str:
    """목록 밖 답이 딱 한 이름과 1 편집 안쪽이고 다음 이름이 2 이상이면 그 이름(0 = 띄어쓰기만 다름), 아니면 text 그대로."""
    try:
        if not text or text in MAP_NAMES:
            return text
        k = _key(text)
        if not k:
            return text
        d = sorted((lev(k, _key(n)), n) for n in MAP_NAMES)
        if (d[0][0] == 0 and d[1][0] > 0) or (d[0][0] == 1 and d[1][0] >= 2):
            return d[0][1]
        return text
    except Exception:
        return text
