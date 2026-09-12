# -*- coding: utf-8 -*-
"""[대시보드] ★가드레일이 진짜로 무는지★ 증명 — 고친 것을 일부러 되돌린 사본에서 시험이 빨간불이 되는가.

  python -X utf8 tests/prove_guards.py        (server/ 에서, 1~2분)

왜 있나: 2026-09-11 적대검증이 「소스 문자열만 보는 시험은 주석만 남긴 사본을 통과시킨다」를
실제로 보여줬다. 그래서 시험은 전부 실호출로 썼고, 그것이 사실인지는 ★되돌려 봐야★ 안다.
이 파일은 verify.py 에 포함되지 않는다(느리다). 시험을 크게 고쳤을 때 한 번 돌린다.
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # server/
PY = sys.executable

# (이름, 파일, 되돌리기 전 텍스트, 되돌린 뒤 텍스트, 잡아야 하는 시험 파일)
CASES = [
    ("N+1 되돌리기", "main.py",
     "_attach_char_info(pc, _ci_all.get(ns(tenant, pid)))",
     "_attach_char_info(pc, await get_char_info(ns(tenant, pid)))", "test_regressions.py"),
    ("소켓 소유권 검사 제거", "main.py",
     "            if macro_ws_connections.get(pc_id) is ws:\n                macro_ws_connections.pop(pc_id, None)\n            else:\n                _WS_KEPT[0] += 1",
     "            macro_ws_connections.pop(pc_id, None)", "test_regressions.py"),
    ("ack 상태가드 제거", "database.py",
     "WHERE id=? AND status='pending'\",",
     "WHERE id=?\",", "test_regressions.py"),
    ("업데이터 A7 가드 제거", "main.py",
     "    if str(pc_id).strip() in _BROADCAST_IDS:\n        raise HTTPException(\n            status_code=400,\n            detail=\"브로드캐스트 명령은 막혀 있습니다(A7) — PC 를 하나씩 지정하십시오\")\n    body = await request.json()\n    command = body.get(\"command\")\n    if not command:\n        raise HTTPException(status_code=400, detail=\"command 필드 필요\")\n    cmd_id = await insert_updater_command",
     "    body = await request.json()\n    command = body.get(\"command\")\n    if not command:\n        raise HTTPException(status_code=400, detail=\"command 필드 필요\")\n    cmd_id = await insert_updater_command", "test_regressions.py"),
    ("_by 재배달 표식 제거", "main.py",
     "    args = {**args, \"_by\": \"human\"}\n", "", "test_stage1.py"),
    ("설정 허용목록 제거", "main.py",
     "    elif key in MACRO_READABLE_SETTINGS:\n        tenant = check_api_key(request) or check_session(request)\n    else:\n        tenant = check_session(request)          # ★그 밖은 세션만★ (2026-09-12)",
     "    else:\n        tenant = check_api_key(request) or check_session(request)", "test_stage1.py"),
    ("음소거 테넌트 분리 제거", "main.py",
     "    return max(0.0, _TG_MUTE.get(ns(tenant, base), 0.0) - now)",
     "    return max(0.0, _TG_MUTE.get(ns('main', base), 0.0) - now)", "test_stage1.py"),
    ("FV 완료 규칙 되돌리기", "main.py",
     "    done = sum(1 for d in dp if d.get(\"completed\") and d.get(\"today\") is not False)",
     "    done = sum(1 for d in dp if d.get(\"completed\"))", "test_stage1.py"),
]


def main_():
    print("%-24s %-5s %s" % ("일부러 되돌린 것", "종료", "판정"))
    allok = True
    for label, fname, new, old, testfile in CASES:
        tmp = tempfile.mkdtemp(prefix="prove_")
        dst = os.path.join(tmp, "server")
        shutil.copytree(HERE, dst, ignore=shutil.ignore_patterns("__pycache__", "static", "*.pdf"))
        p = os.path.join(dst, fname)
        t = io.open(p, encoding="utf-8", newline="").read()
        nl = "\r\n" if "\r\n" in t else "\n"
        o, w = new.replace("\n", nl), old.replace("\n", nl)
        if t.count(o) < 1:
            print("%-24s %-5s ★앵커 못 찾음★ — 시험 대상 코드가 바뀌었다, 이 항목을 갱신하라" % (label, "-"))
            allok = False
            continue
        io.open(p, "w", encoding="utf-8", newline="").write(t.replace(o, w))
        r = subprocess.run([PY, "-X", "utf8", os.path.join(dst, "tests", testfile)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           cwd=dst, timeout=900)
        caught = r.returncode == 1
        allok &= caught
        fails = [ln.strip() for ln in (r.stdout or "").splitlines() if "[FAIL]" in ln]
        print("%-24s %-5s %s  %s" % (label, r.returncode, "✔ 잡음" if caught else "★★못 잡음★★", (fails[0][:50] if fails else "")))
        shutil.rmtree(tmp, ignore_errors=True)
    print("-" * 60)
    print(("✔ 되돌린 %d건을 시험이 전부 잡는다" % len(CASES)) if allok else "★★가드레일에 구멍이 있다★★")
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main_())
