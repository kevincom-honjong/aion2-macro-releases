# -*- coding: utf-8 -*-
"""🔭 스카우터 알람을 서버 안으로 (주인님 #288, 2026-09-27).

주인님: 「스카우터를 꺼라했지 알람 울렸던 것도 안 울리는 거는 뭐야」 — #270 에서 스카우터 ★프로세스★ 를 폐기하자
그 트립마다 나가던 텔레그램(🔭 스카우터: PC — 이유)도 같이 사라졌다. 끄라신 건 프로세스와 폴링이지 알람이 아니다.
서버는 /status 를 이미 메모리에서 만든다 → 폴링·나가는 바이트 없이 같은 조건을 여기서 본다.

옮긴 조건 (web/.claude/ops/scouter.py 머리 · 메인루프 · trip_once:3841 · _mass_outage_check:3925)
  1 status == error                 — 사람이 exit 로 끈 흔적이면 아님 · 매크로가 최근 10분 안에 스스로 알람을 냈으면 그걸로 친다
  2 offline      3분 이상            ┐ 2·4 는 ★한 사고★(키 "dead") — WS 끊김이 900초 뒤 offline 으로 뒤집혀도 두 번 안 운다
  4 일하는 상태(사냥·이동·어비스·던전·회랑…)인데 WS 끊김 + ★서버 수신★ 정체 6분 이상 ┘
  3 reconnecting 4분 이상
  5 hunting 인데 지금 슬롯이 오늘 이미 완료(남은 슬롯 있음) 2분 이상 — 서버 부팅 때 이미 그랬던 PC 는 한 번 풀릴 때까지 안 깨움
  6 errors 가 새로 생김(※ 지금 매크로는 errors 를 안 채운다 — add_error 호출처 0, 반증 LOW. 채우면 바로 산다)
  7 ★새★ 버그스샷 이름(개수가 아니라 이름 — 40장 상한에서 교체되면 개수가 안 는다): 캡차·계정 혼입·예외·전환 실패 가족 = 바로 /
    나머지 깨울 종류(과제 음소거 제외) = 유예 10분 뒤 여전히 멈춰 있으면, 첫 감지부터 20분 상한. 같은 PC·같은 종류는 6시간에 한 번
  8 15분 안에 3대가 ★새로★ 죽으면(부팅 때 이미 죽어 있던·이미 알린·음소거 PC 는 안 셈) 「회선/전원 의심」 한 통(함대 이름, 30분에 한 번)
조용히 두는 것: stopped_by=human 카드 · 사망 조건(1·2·3·4)은 로그의 exit 흔적(scouter _exited_on_purpose 표식)만 ·
  버그 유예는 넓은 「사람이 세움」 표식 · 서버가 쓴 줄([순환]·[효율]·[스카우터]·[팜뷰]·[알람])은 「로그가 흐른다」 로 안 친다 ·
  음소거·은퇴 PC 는 후보도 안 만든다(풀린 뒤에도 고장이면 그때 운다). ★「오늘 할 일 끝」 으로 사망을 면제하지 않는다★
  (어비스 무한사냥 PC 는 슬롯이 다 끝나도 돈다 — 반증 MED-3).
재알림: 지속형은 (PC, 종류) 마다 RENOTIFY(6시간) 에 한 번 «※계속 고장 중 — 재알림», 풀리면 재무장.
★장부는 실제로 보냈을 때만 적는다★(mark) — 음소거·전송 실패는 다음 틱에 다시 본다. 부른 쪽이 장부를 저장한다.

★이 파일은 순수하다★ — 시계·로그·버그 이름·음소거는 부른 쪽이 넣는다(시험은 가상 시계). main.py 의 _scout_tick 이 배선한다.
"""
import re
from datetime import datetime

RENOTIFY = 6 * 3600.0
EVENT_WINDOW = 1800.0                 # errors·늘 깨우는 버그는 같은 PC·같은 종류 30분에 한 번
BUG_DEFER_WINDOW = RENOTIFY           # 유예 버그(회랑 입구 실패 등 반복 많은 종류)는 같은 PC·같은 종류 6시간에 한 번
DWELL = {"offline": 180.0, "reconnecting": 240.0, "wsdead": 360.0, "done_slot": 120.0}
LOG_FRESH_S = 150.0
MACRO_ALARM_WINDOW = 600.0
MASS_WINDOW = 900.0
MASS_N = 3
MASS_COOLDOWN = 1800.0
FLEET_ID = "함대"
BUG_GRACE_S = 600.0
BUG_TOTAL_CAP_S = 1200.0

BUG_HARD = ("captcha", "acct_mismatch", "exception")
BUG_CHAR_SWITCH_MARKS = ("switch_slot", "switch_server_select", "switch_webplay_gate", "no_popup")
BUG_SWITCH_MARKS = ("switch", "dropdown", "aion2", "hostacct", "launcher", "-connect-", "confirm", "plrow",
                    "rescue", "relogin", "login", "kill", "tour_", "-pick-", "-run-")
BUG_WAKE = ("unknown_screen", "-fail", "_fail", "acct_mismatch", "stuck", "no_stream", "exception", "no_popup")
BUG_KNOWN_MUTED = {"warehouse_enter_fail": "#81 서버창고 좌표 재실측"}
WORK_ST = frozenset(("hunting", "moving", "abyss", "dungeon", "corridor", "surface", "awakening", "nightmare"))
STOP_ST = frozenset(("idle", "error", "offline", "paused", "captcha", "stale", "unknown", "", "None"))

EXIT_MARKS = ("매크로 종료", "원격 종료", "exit 명령", "프로그램 종료", "이용 중지", "no_restart", "종료 명령")
BOOT_MARKS = ("핫키 대기", "서버 연결 시작", "[BOOT]", "백그라운드 스레드 시작됨")
PARK_MARKS = EXIT_MARKS + ("[원격명령] 매크로 정지", "stop 명령(사람)", "수동 일시정지", "수동 종료", "어비스에서 자동 퇴장")
PARK_CANCEL_MARKS = BOOT_MARKS + ("금지 해제", "일시정지 해제", "[원격명령] 수신: start", "[원격명령] 수신: restart")
SERVER_LINE_HEADS = ("[순환]", "[효율]", "[스카우터]", "[팜뷰]", "[알람]", "[정지]")
ALARM_HEAD = "[알람]"
# 서버가 쓰는 줄은 «[YYYY-MM-DD HH:MM:SS] [텔레그램] 중계 …: PC | [순환] …» 처럼 시각·중계 머리에 싸여 온다(main._rot_say·_eff_say)
_SERVER_WRAP = re.compile(r"^\s*(?:\[\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\]\s*)?(?:\[텔레그램\][^|]*\|\s*)?")
# 매크로가 스스로 운 「전환 실패」 알람 — 이것만 error 알람과 겹친다고 본다(반증 2차 LOW3: 아무 [알람] 이나 삼키지 않게)
SWITCH_FAIL_RE = re.compile(r"전환 실패|뒷단계 실패")


def is_server_line(m) -> bool:
    return _SERVER_WRAP.sub("", str(m), count=1).startswith(SERVER_LINE_HEADS)


def base_pc(pc_id) -> str:
    return re.sub(r"[a-e]$", "", str(pc_id or ""))


def age_s(ts, now: float):
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "")[:19])
        return now - (dt - datetime(1970, 1, 1)).total_seconds()
    except Exception:
        return None


def stopped_by_human(r: dict) -> str:
    if str(r.get("stopped_by") or "") != "human":
        return ""
    return str(r.get("stopped_why") or "사람이 껐다")[:60]


def mark_after_cancel(lines, marks) -> str:
    """로그 줄(오래된→새) 에 marks 가 부팅·재개 표식보다 뒤에 있으면 그 표식."""
    park_i, park_t, cancel_i = -1, "", -1
    for i, t in enumerate(lines or []):
        t = str(t)
        if any(m in t for m in PARK_CANCEL_MARKS):
            cancel_i = i
            continue
        hit = next((m for m in marks if m in t), None)
        if hit:
            park_i, park_t = i, hit
    return park_t if park_i > cancel_i else ""


def park_reason(lines) -> str:
    return mark_after_cancel(lines, PARK_MARKS)


def exit_reason(lines) -> str:
    return mark_after_cancel(lines, EXIT_MARKS)


def _dp(r):
    return [c for c in (r.get("daily_progress") or []) if isinstance(c, dict)]


def _done_today(c) -> bool:
    """서버가 얹는 today(05:00 경계) — 없으면 옛 응답(completed 만)."""
    return bool(c.get("completed")) and c.get("today") is not False


def done_slot(r) -> bool:
    dp = _dp(r)
    cd = next((c for c in dp if c.get("slot") == (r.get("slot") or 0)), None)
    return str(r.get("status")) == "hunting" and bool(cd and _done_today(cd)) and any(not _done_today(c) for c in dp)


def silent_s(r, now):
    """★서버가 마지막으로 받은 시각★(_updated_at)부터 초 — 매크로 시계(last_active)는 최대 300초 어긋난다. 모르면 None."""
    return age_s(r.get("_updated_at"), now) if r.get("_updated_at") else None


def _shot_key(name):
    m = re.findall(r"(\d{8}_\d{6})", str(name))
    return (m[-1] if m else "", str(name))


def shot_base(name) -> str:
    m = re.match(r"^(PC-\d+)[a-e]?_", str(name))
    return m.group(1) if m else ""


def bug_kind(name) -> str:
    """이름 → 종류. ★마지막★ 시각 뒤를 쓴다(실물 이름은 시각이 둘: PC-05_…_PC-05_20260927_102408_cube_trash_open_fail)."""
    n = str(name or "").lower().rsplit("/", 1)[-1]
    n = re.sub(r"\.(png|jpe?g)$", "", n)
    m = re.search(r".*\d{8}_\d{6}_(.+)$", n)
    n = m.group(1) if m else n
    n = re.sub(r"lc\d+", "lcN", n)
    n = re.sub(r"slot\d+", "slotN", n)
    n = re.sub(r"acct\d+", "acctN", n)
    n = re.sub(r"\d{3,}", "N", n)
    return n or "?"


def bug_family(name) -> str:
    k = bug_kind(name)
    if any(m in k for m in BUG_CHAR_SWITCH_MARKS):
        return "char_switch"
    if any(m in k for m in BUG_SWITCH_MARKS):
        return "acct_switch"
    return k


def bug_route(fresh):
    """새 버그스샷 이름 → (hard, switch, defer)."""
    fresh = [str(n) for n in (fresh or [])]
    hard = [n for n in fresh if any(h in bug_kind(n) for h in BUG_HARD)]
    hit = [n for n in fresh if any(w in bug_kind(n) for w in BUG_WAKE) and n not in hard
           and not any(k in n for k in BUG_KNOWN_MUTED)]
    sw = [n for n in hit if bug_family(n) in ("acct_switch", "char_switch")]
    return hard, sw, [n for n in hit if n not in sw]


def watchable(r) -> bool:
    pid = str(r.get("pc_id") or "")
    return (pid.startswith("PC-") and pid != "PC-TEST" and str(r.get("status")) != "other_account"
            and not stopped_by_human(r) and not r.get("banned"))


def _safe_watchable(r) -> bool:
    """한 카드의 모양이 틀려도(str() 이 터지는 값 등) 틱 전체가 죽지 않게 — 그 카드만 뺀다(반증 LOW)."""
    try:
        return isinstance(r, dict) and watchable(r) and isinstance(str(r.get("status")), str)
    except Exception:
        return False


def _fmt_min(s) -> str:
    s = max(0.0, float(s or 0))
    return f"{s / 60:.0f}분" if s < 5400 else f"{s / 3600:.1f}시간"


class Scout:
    """상태 한 벌. step() 이 알릴 후보 [(pc, key, text)] 를 돌려주고, 부른 쪽이 ★실제로 보낸 것만★ mark() 한다."""

    def __init__(self, alerted=None):
        self.alerted = dict(alerted or {})    # "PC:key" -> 마지막으로 ★보낸★ 시각
        self.since = {}
        self.base_errs = {}
        self.known_done = set()
        self.bug_seen = None                  # 지금까지 본 버그스샷 이름(첫 틱이 기준선)
        self.bug_pending = {}                 # 물리 PC -> {"t0","t","names"}
        self.boot_dead = set()                # 첫 틱에 이미 죽어 있던 카드 — 집단 사망 셈에서 뺀다(풀리면 해제)
        self.mass_died = {}
        self.mass_said = 0.0
        self.mass_last = []
        self.bug_hold = {}                    # (물리 PC, key) -> 이번 틱 후보를 낸 새 이름들 — ★보낸 뒤에만★ 본 것으로
        self.first = True
        self.dirty = False
        self.judged = 0                       # 이번 틱에 판정한 카드 수(/health 심장박동)
        self.last_row_err = ""                # 마지막 카드별 판정 예외(틱은 계속)

    # ── 장부 ─────────────────────────────────────────────────────────
    def due(self, pid, key, now, window=RENOTIFY) -> bool:
        last = self.alerted.get(f"{pid}:{key}")
        return last is None or now - last >= window

    def mark(self, pid, key, now):
        if key == "mass":
            self.mass_said = now
            for p in self.mass_last:           # 회선 알람이 이름을 불렀다 — 그 PC 들을 한 대씩 또 부르지 않는다
                self.alerted[f"{p}:dead"] = now
            self.dirty = True
            return
        self.alerted[f"{pid}:{key}"] = now
        self.dirty = True
        if key.startswith("bug:"):                # 버그 알람은 보낸 뒤에만 「봤다」·유예 끝 (반증 2차 LOW2)
            if self.bug_seen is not None:
                self.bug_seen |= set(self.bug_hold.pop((pid, key), []))
            p = self.bug_pending.get(pid)
            if p and p.get("key") == key:
                self.bug_pending.pop(pid, None)

    def _cand(self, pid, key, why, now, window=RENOTIFY, again=False):
        if not self.due(pid, key, now, window):
            return None
        return (pid, key, why + ("  ※계속 고장 중 — 재알림" if again and f"{pid}:{key}" in self.alerted else ""))

    def _rearm(self, pid, key):
        if self.alerted.pop(f"{pid}:{key}", None) is not None:
            self.dirty = True

    def _mass(self, pid, dead, now, countable=True):
        """기록만 한다 — 회선 알람은 틱 끝에 ★한 번★ 판정(_mass_cand). 반증 2차 HIGH: 카드마다 판정하면
        3대째 뒤로 죽는 PC 마다 같은 알람이 한 틱에 또 나왔다(10대→8통)."""
        if dead and countable:
            self.mass_died.setdefault(pid, now)
        elif not dead:
            self.mass_died.pop(pid, None)

    def _mass_cand(self, now):
        fresh = sorted(k for k, t in self.mass_died.items() if now - t <= MASS_WINDOW)
        if len(fresh) < MASS_N or now - self.mass_said < MASS_COOLDOWN:
            return None
        self.mass_last = fresh
        return (FLEET_ID, "mass", f"★회선/전원 의심★ {int(MASS_WINDOW / 60)}분 안에 {len(fresh)}대가 함께 죽었습니다 "
                                  f"({', '.join(fresh)}) — 개별 고장이 아닙니다. 인터넷 요금/공유기/정전을 먼저 확인해 주세요")

    # ── 한 틱 ────────────────────────────────────────────────────────
    async def step(self, rows, now, logs_fn, bug_names, muted_fn=lambda pid: False, bug_muted_fn=None):
        """rows = /status 카드 · logs_fn(pid) → [(created_at, message)] 최근 줄(오래된→새) ·
        bug_names = 지금 있는 버그스샷 이름 전부 · muted_fn(pid) → 음소거·은퇴면 True ·
        bug_muted_fn(물리 PC) → 버그스샷 알람만 뺀다(사고 667 본인 확인 보류 — 죽음·error 는 그대로 운다)."""
        bug_muted = bug_muted_fn or (lambda pid: False)
        out = []
        rows = [r for r in (rows or []) if _safe_watchable(r)]
        self.judged = len(rows)
        first, self.first = self.first, False
        cache = {}

        async def lines(pid):
            if pid not in cache:
                try:
                    cache[pid] = list(await logs_fn(pid) or [])
                except Exception:
                    cache[pid] = None
            return cache[pid]

        async def msgs(pid):
            return [str(m) for _t, m in (await lines(pid) or [])]

        async def logs_flowing(pid):
            for t, m in reversed(await lines(pid) or []):
                if is_server_line(m):
                    continue                  # 서버가 쓴 줄은 매크로가 살아 있다는 증거가 아니다
                a = age_s(t, now)
                return a is not None and a < LOG_FRESH_S
            return False

        async def macro_alarmed(pid):
            for t, m in reversed(await lines(pid) or []):
                if str(m).startswith(ALARM_HEAD) and "🔭" not in str(m) and SWITCH_FAIL_RE.search(str(m)):
                    a = age_s(t, now)
                    return a is not None and a < MACRO_ALARM_WINDOW
            return False

        # 7 새 버그스샷 이름 — 기준선은 첫 틱
        names = {str(n) for n in (bug_names or []) if n}
        new = [] if self.bug_seen is None else sorted(names - self.bug_seen, key=_shot_key)
        self.bug_seen = set(names)
        self.bug_hold = {}
        bases = {base_pc(r.get("pc_id")) for r in rows}
        by_base = {}
        for n in new:
            by_base.setdefault(shot_base(n), []).append(n)
        for bp, ns_ in by_base.items():
            if bp not in bases or muted_fn(bp) or bug_muted(bp):
                continue
            try:
                hard, sw, defer = bug_route(ns_)
                for n, why in ([(hard[-1], "늘 깨우는 종류: 캡차·계정 혼입·예외")] if hard else
                               [(sw[-1], "전환 실패 = 유예 없이 바로")] if sw else []):
                    a = self._cand(bp, "bug:" + bug_kind(n), f"버그스샷 {bug_kind(n)} ({why})", now, EVENT_WINDOW)
                    if a:
                        out.append(a)
                        self.bug_hold[(bp, a[1])] = list(ns_)
                        self.bug_seen -= set(ns_)     # 못 보내면 다음 틱에 다시 「새 이름」
                if defer and not hard and not sw:
                    p = self.bug_pending.setdefault(bp, {"t0": now, "names": []})
                    p["t"] = now
                    p["names"] = (p["names"] + defer)[-8:]
            except Exception as e:
                print(f"[스카우터] {bp} 버그스샷 판정 실패(건너뜀): {type(e).__name__}: {e}", flush=True)

        for r in rows:
            try:
                out += await self._one(r, now, first, lines, msgs, logs_flowing, macro_alarmed, muted_fn)
            except Exception as e:
                self.last_row_err = f"{r.get('pc_id')}: {type(e).__name__}: {e}"[:200]
                print(f"[스카우터] {r.get('pc_id')} 판정 실패(건너뜀): {type(e).__name__}: {e}", flush=True)

        # 7 유예 판정 — 물리 PC 의 카드 중 하나라도 일하며 살아 있으면 자가복구
        grp = {}
        for r in rows:
            grp.setdefault(base_pc(r.get("pc_id")), []).append(r)
        for bp, p in list(self.bug_pending.items()):
            try:
                if now - p.get("t", p["t0"]) < BUG_GRACE_S:
                    continue
                cards = grp.get(bp) or []
                working = [r for r in cards if str(r.get("status")) in WORK_ST
                           and (r.get("_ws_live") is not False or (silent_s(r, now) or 1e9) < 210)]
                stopped = all(str(r.get("status")) in STOP_ST for r in cards) if cards else True
                if working:
                    self.bug_pending.pop(bp)
                    continue
                if not stopped and now - p["t0"] < BUG_TOTAL_CAP_S:
                    continue
                if muted_fn(bp) or bug_muted(bp) or (cards and all([bool(park_reason(await msgs(str(r.get("pc_id"))))) for r in cards])):
                    self.bug_pending.pop(bp)
                    continue
                k = bug_kind(p["names"][-1])
                a = self._cand(bp, "bug:" + k, f"버그스샷 {k} → 첫 감지 {_fmt_min(now - p['t0'])} 뒤에도 멈춰 있음 "
                                               f"(자가복구 안 됨)", now, BUG_DEFER_WINDOW)
                if a:
                    out.append(a)
                    p["key"] = a[1]           # 보낸 뒤 mark() 가 뺀다 — 실패하면 다음 틱에 다시
                else:
                    self.bug_pending.pop(bp)
            except Exception as e:
                self.bug_pending.pop(bp, None)
                print(f"[스카우터] {bp} 버그 유예 판정 실패(뺌): {type(e).__name__}: {e}", flush=True)
        m = self._mass_cand(now)
        if m:
            # 이 틱에 회선 알람이 났으면 그 이름들의 한 대씩 알람은 뺀다(scouter: 집단이면 한 대 얘기를 안 한다)
            out = [c for c in out if not (c[1] == "dead" and c[0] in self.mass_last)] + [m]
        live = {str(r.get("pc_id")) for r in rows}
        for k in [k for k in self.since if k.split(":")[0] not in live]:
            self.since.pop(k, None)
        return out

    async def _one(self, r, now, first, lines, msgs, logs_flowing, macro_alarmed, muted_fn):
        out = []
        pid, st = str(r.get("pc_id")), str(r.get("status"))
        muted = bool(muted_fn(pid))
        # 6 errors
        errs = r.get("errors")
        errs = errs if isinstance(errs, list) else []
        ne, oe = len(errs), self.base_errs.get(pid)
        self.base_errs[pid] = ne
        if oe is not None and ne > oe and not muted:
            a = self._cand(pid, "errors", f"errors 발생 {ne}건 — {str(errs[-1])[:120]}", now, EVENT_WINDOW)
            out += [a] if a else []
        # 1 error
        if st == "error":
            if not muted and self.due(pid, "error", now):
                if await macro_alarmed(pid):
                    self.mark(pid, "error", now)          # 매크로가 방금 스스로 알렸다 — 같은 일로 두 번 안 운다
                elif not exit_reason(await msgs(pid)):
                    a = self._cand(pid, "error", "상태=error", now, again=True)
                    out += [a] if a else []
        elif st != "offline":
            self._rearm(pid, "error")
        # 2·4 죽음(한 사고) · 3 reconnecting
        sil = silent_s(r, now)
        off = st == "offline"
        wsd = st in WORK_ST and r.get("_ws_live") is False and sil is not None and sil >= DWELL["wsdead"]
        dead = off or wsd
        if first and dead:
            self.boot_dead.add(pid)
        if not dead:
            self.boot_dead.discard(pid)
        conds = (("dead", dead, "offline" if off else "wsdead"), ("reconnecting", st == "reconnecting", "reconnecting"))
        for key, cond, kind in conds:
            k = f"{pid}:{key}"
            if not cond:
                self.since.pop(k, None)
                self._rearm(pid, key)
                if key == "dead":
                    self._mass(pid, False, now)
                continue
            self.since.setdefault(k, now)
            held = sil if kind == "wsdead" else now - self.since[k]
            if held is None or held < DWELL[kind]:
                continue
            countable = key == "dead" and not muted and pid not in self.boot_dead
            if not self.due(pid, key, now):
                if key == "dead":
                    self._mass(pid, True, now, countable=False)     # 이미 알린 PC 는 「새로 죽음」 이 아니다
                continue
            if await logs_flowing(pid) or exit_reason(await msgs(pid)):
                self.since.pop(k, None)
                continue
            if key == "dead":
                self._mass(pid, True, now, countable)
            if muted:
                continue
            dur = max(held, sil or 0) if kind == "offline" else held
            what = {"offline": "offline", "reconnecting": "reconnecting",
                    "wsdead": f"{st} 중이라는데 WS 끊김·서버 수신 정체가"}[kind]
            a = self._cand(pid, key, f"{what} {_fmt_min(dur)}째 (로그도 정지)", now, again=True)
            out += [a] if a else []
        # 5 오늘 완료한 슬롯에서 사냥
        k = f"{pid}:done_slot"
        if first and done_slot(r):
            self.known_done.add(pid)
        if done_slot(r):
            if pid in self.known_done or muted:
                return out
            self.since.setdefault(k, now)
            held = now - self.since[k]
            if held >= DWELL["done_slot"]:
                left = [c.get("slot") for c in _dp(r) if not _done_today(c)]
                a = self._cand(pid, "done_slot", f"오늘 이미 완료한 슬롯 {r.get('slot')} 에서 {_fmt_min(held)}째 사냥 중 "
                                                 f"(남은 슬롯 {left}) — 전환이 실패한 채 방치된 상태", now, again=True)
                out += [a] if a else []
        else:
            self.since.pop(k, None)
            self._rearm(pid, "done_slot")
            self.known_done.discard(pid)
        return out
