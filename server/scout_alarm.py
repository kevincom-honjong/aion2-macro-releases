# -*- coding: utf-8 -*-
"""🔭 스카우터 알람을 서버 안으로 (주인님 #288, 2026-09-27).

주인님: 「스카우터를 꺼라했지 알람 울렸던 것도 안 울리는 거는 뭐야」 — #270 에서 스카우터 ★프로세스★ 를 폐기하자
그 트립마다 나가던 텔레그램(🔭 스카우터: PC — 이유)도 같이 사라졌다. 끄라신 건 프로세스와 폴링이지 알람이 아니다.
서버는 /status 를 이미 메모리에서 만든다 → 폴링·나가는 바이트 없이 같은 조건을 여기서 본다.

옮긴 조건 (web/.claude/ops/scouter.py 머리 · 메인루프 · trip_once:3841 · _mass_outage_check:3925)
  1 status == error                     (사람이 exit 로 끈 흔적이면 아님) — 지속형, 한 번 + 6시간 재알림
  2 offline      3분 이상               (로그가 흐르면·사람이 껐으면 아님)
  3 reconnecting 4분 이상               (〃)
  4 hunting/moving 인데 WS 끊김 + 상태 보고 정체 6분 이상 (〃)
  5 hunting 인데 지금 슬롯이 이미 완료(남은 슬롯 있음) 2분 이상 — 서버 부팅 때 이미 그랬던 PC 는 한 번 풀릴 때까지 안 깨움
  6 errors 가 새로 생김                  — 사건형(같은 PC·같은 이유 EVENT_WINDOW 안 한 번)
  7 버그스샷 증가(물리 PC 단위): 캡차·계정 혼입·예외 = 바로 / 전환 실패 가족 = 바로 /
    나머지 깨울 종류(BUG_WAKE, 과제 음소거 제외) = 유예 10분 뒤 여전히 멈춰 있으면(자가복구 아님) — 첫 감지부터 20분 상한
  8 15분 안에 3대가 새로 죽으면 「회선/전원 의심」 한 통(30분에 한 번), 그 틱엔 한 대씩 알리지 않는다
조용히 두는 것: 매크로의 stopped_by=human 카드 · 로그의 「사람이 세움」(■정지·Home·PageDown·exit·어비스 자동 퇴장) ·
  오늘 할 일을 다 끝낸 PC(지속형 조건) · 다른 계정(other_account) · PC-TEST.
재알림: 지속형은 (PC, 종류) 마다 RENOTIFY(6시간) 에 한 번 «※계속 고장 중 — 재알림», 풀리면 재무장. 장부는 부른 쪽이 저장한다.

★이 파일은 순수하다★ — 시계(now)·로그 읽기·버그 이름 읽기는 부른 쪽이 넣는다(시험은 가상 시계). main.py 의 _scout_loop 가 배선한다.
"""
import re
from datetime import datetime

RENOTIFY = 6 * 3600.0
EVENT_WINDOW = 1800.0                 # 사건형(errors·버그) 같은 PC·같은 이유는 30분에 한 번
DWELL = {"offline": 180.0, "reconnecting": 240.0, "wsdead": 360.0, "done_slot": 120.0}
REPORT_FRESH_S = 210.0                # 상태 보고 30초 × 7 — 이보다 신선하면 살아 있다(scouter _report_fresh)
LOG_FRESH_S = 150.0                   # 마지막 로그 줄이 이보다 최근이면 살아 있다(scouter _logs_fresh)
MASS_WINDOW = 900.0
MASS_N = 3
MASS_COOLDOWN = 1800.0
BUG_GRACE_S = 600.0
BUG_TOTAL_CAP_S = 1200.0

BUG_HARD = ("captcha", "acct_mismatch", "exception")
BUG_CHAR_SWITCH_MARKS = ("switch_slot", "switch_server_select", "switch_webplay_gate", "no_popup")
BUG_SWITCH_MARKS = ("switch", "dropdown", "aion2", "hostacct", "launcher", "-connect-", "confirm", "plrow",
                    "rescue", "relogin", "login", "kill", "tour_", "-pick-", "-run-")
BUG_WAKE = ("unknown_screen", "-fail", "_fail", "acct_mismatch", "stuck", "no_stream", "exception", "no_popup")
BUG_KNOWN_MUTED = {"warehouse_enter_fail": "#81 서버창고 좌표 재실측"}
BUG_WORK_ST = frozenset(("hunting", "moving", "abyss", "dungeon", "corridor", "surface", "awakening", "nightmare"))
BUG_STOP_ST = frozenset(("idle", "error", "offline", "paused", "captcha", "stale", "unknown", "", "None"))

EXIT_MARKS = ("매크로 종료", "원격 종료", "exit 명령", "프로그램 종료", "이용 중지", "no_restart", "종료 명령")
BOOT_MARKS = ("핫키 대기", "서버 연결 시작", "[BOOT]", "백그라운드 스레드 시작됨")
PARK_MARKS = EXIT_MARKS + ("[원격명령] 매크로 정지", "stop 명령(사람)", "수동 일시정지", "수동 종료", "어비스에서 자동 퇴장")
PARK_CANCEL_MARKS = BOOT_MARKS + ("금지 해제", "일시정지 해제", "[원격명령] 수신: start", "[원격명령] 수신: restart")

PERSIST_KEYS = ("error", "offline", "reconnecting", "wsdead", "done_slot")


def base_pc(pc_id) -> str:
    return re.sub(r"[a-e]$", "", str(pc_id or ""))


def age_s(ts, now: float):
    """서버 UTC 문자열 → 경과 초(못 읽으면 None)."""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "")[:19])
        return now - (dt - datetime(1970, 1, 1)).total_seconds()
    except Exception:
        return None


def stopped_by_human(r: dict) -> str:
    if str(r.get("stopped_by") or "") != "human":
        return ""
    return str(r.get("stopped_why") or "사람이 껐다")[:60]


def park_reason(lines) -> str:
    """로그 줄(오래된→새) 에 「사람이 세움」 흔적이 부팅·재개 표식보다 뒤에 있으면 그 표식."""
    park_i, park_t, cancel_i = -1, "", -1
    for i, t in enumerate(lines or []):
        t = str(t)
        if any(m in t for m in PARK_CANCEL_MARKS):
            cancel_i = i
            continue
        hit = next((m for m in PARK_MARKS if m in t), None)
        if hit:
            park_i, park_t = i, hit
    return park_t if park_i > cancel_i else ""


def all_done(r) -> bool:
    dp = r.get("daily_progress") or []
    return bool(dp) and all(isinstance(c, dict) and c.get("completed") for c in dp)


def done_slot(r) -> bool:
    dp = [c for c in (r.get("daily_progress") or []) if isinstance(c, dict)]
    cd = next((c for c in dp if c.get("slot") == (r.get("slot") or 0)), None)
    return str(r.get("status")) == "hunting" and bool(cd and cd.get("completed")) and any(not c.get("completed") for c in dp)


def report_age(r, now):
    """마지막 상태 보고(30초 주기)부터 초 — 모르면 None."""
    for k in ("last_active", "_updated_at"):
        a = age_s(r.get(k), now) if r.get(k) else None
        if a is not None:
            return a
    return None


def report_fresh(r, now) -> bool:
    a = report_age(r, now)
    return a is not None and a < REPORT_FRESH_S


def _shot_key(name):
    """스샷 이름의 시각(YYYYMMDD_HHMMSS) 순 — 같은 물리 PC 의 PC-05_·PC-05b_ 가 섞여도 최신이 끝."""
    m = re.search(r"(\d{8}_\d{6})", str(name))
    return (m.group(1) if m else "", str(name))


def bug_kind(name) -> str:
    n = str(name or "").lower().rsplit("/", 1)[-1]
    n = re.sub(r"\.(png|jpe?g)$", "", n)
    m = re.search(r"\d{8}_\d{6}_(.+)$", n)
    return (m.group(1) if m else n) or "?"


def bug_family(name) -> str:
    k = bug_kind(name)
    if any(m in k for m in BUG_CHAR_SWITCH_MARKS):
        return "char_switch"
    if any(m in k for m in BUG_SWITCH_MARKS):
        return "acct_switch"
    return k


def bug_route(fresh):
    """새 버그스샷 이름 → (hard, switch, defer) — 셋 다 아니면 안 깨움."""
    fresh = [str(n) for n in (fresh or [])]
    hard = [n for n in fresh if any(h in n.lower() for h in BUG_HARD)]
    hit = [n for n in fresh if any(w in n for w in BUG_WAKE) and n not in hard
           and not any(k in n for k in BUG_KNOWN_MUTED)]
    sw = [n for n in hit if bug_family(n) in ("acct_switch", "char_switch")]
    return hard, sw, [n for n in hit if n not in sw]


def watchable(r) -> bool:
    pid = str(r.get("pc_id") or "")
    return (pid.startswith("PC-") and pid != "PC-TEST" and str(r.get("status")) != "other_account"
            and not stopped_by_human(r) and not r.get("banned"))


class Scout:
    """상태 한 벌. step() 이 알릴 것 [(pc, key, text)] 을 돌려준다. alerted 는 부른 쪽이 저장·복원한다."""

    def __init__(self, alerted=None):
        self.alerted = dict(alerted or {})    # "PC:key" -> 마지막 알린 시각 (지속형 · 사건형 둘 다)
        self.since = {}                       # "PC:key" -> 처음 본 시각
        self.base_bugs = {}                   # 물리 PC -> _bug_count
        self.base_errs = {}                   # 카드 -> errors 개수
        self.known_done = set()               # 부팅 때 이미 완료슬롯 사냥이던 카드
        self.bug_pending = {}                 # 물리 PC -> {"t0", "names", "card"}
        self.mass_died = {}
        self.mass_said = 0.0
        self.first = True
        self.dirty = False

    # ── 장부 ─────────────────────────────────────────────────────────
    def _once(self, pid, key, why, now, renotify=RENOTIFY):
        k = f"{pid}:{key}"
        last = self.alerted.get(k)
        if last is not None and now - last < renotify:
            return None
        self.alerted[k] = now
        self.dirty = True
        return (pid, key, why + ("" if last is None or key not in PERSIST_KEYS else "  ※계속 고장 중 — 재알림"))

    def _rearm(self, pid, key):
        if self.alerted.pop(f"{pid}:{key}", None) is not None:
            self.dirty = True

    def _mass(self, pid, dead, now):
        if dead:
            self.mass_died.setdefault(pid, now)
        else:
            self.mass_died.pop(pid, None)
        fresh = sorted(k for k, t in self.mass_died.items() if now - t <= MASS_WINDOW)
        if len(fresh) < MASS_N or now - self.mass_said < MASS_COOLDOWN:
            return None
        self.mass_said = now
        return (fresh[0], "mass", f"★회선/전원 의심★ {int(MASS_WINDOW / 60)}분 안에 {len(fresh)}대가 함께 죽었습니다 "
                                  f"({', '.join(fresh)}) — 개별 고장이 아닙니다. 인터넷 요금/공유기/정전을 먼저 확인해 주세요")

    # ── 한 틱 ────────────────────────────────────────────────────────
    async def step(self, rows, now, logs_fn, bugs_fn):
        """rows = /status 카드 · logs_fn(pid) → [(created_at, message)] 최근 80줄(오래된→새) · bugs_fn(pid) → 이름 목록."""
        out = []
        rows = [r for r in (rows or []) if isinstance(r, dict) and watchable(r)]
        first, self.first = self.first, False
        cache = {}

        async def lines(pid):
            if pid not in cache:
                try:
                    cache[pid] = list(await logs_fn(pid) or [])
                except Exception:
                    cache[pid] = None          # 못 읽음 = 증거 없음(살아 있다·사람이 세웠다로 치지 않는다)
            return cache[pid]

        async def parked(pid, r):
            if stopped_by_human(r):
                return True
            L = await lines(pid)
            return bool(L) and bool(park_reason([m for _t, m in L]))

        async def logs_flowing(pid):
            L = await lines(pid)
            if not L:
                return False
            a = age_s(L[-1][0], now)
            return a is not None and a < LOG_FRESH_S

        seen_bases = set()
        for r in rows:
            pid, st = str(r.get("pc_id")), str(r.get("status"))
            bp = base_pc(pid)
            # 7 버그스샷 — 물리 PC 한 번(카드마다 같은 _bug_count)
            if bp not in seen_bases:
                seen_bases.add(bp)
                nb = int(r.get("_bug_count") or 0)
                ob = self.base_bugs.get(bp)
                self.base_bugs[bp] = nb
                if ob is not None and nb > ob and not first:
                    try:
                        names = sorted((str(x) for x in (await bugs_fn(bp) or [])), key=_shot_key)[-(nb - ob):]
                    except Exception:
                        names = []
                    hard, sw, defer = bug_route(names)
                    if hard:
                        a = self._once(bp, "bug:" + bug_family(hard[-1]),
                                       f"버그스샷 {hard[-1][-48:]} (늘 깨우는 종류: 캡차·계정 혼입·예외)", now, EVENT_WINDOW)
                        out += [a] if a else []
                    elif sw:
                        a = self._once(bp, "bug:" + bug_family(sw[-1]),
                                       f"버그스샷 {sw[-1][-48:]} (전환 실패 = 유예 없이 바로)", now, EVENT_WINDOW)
                        out += [a] if a else []
                    elif defer:
                        p = self.bug_pending.setdefault(bp, {"t0": now, "names": []})
                        p["t"] = now
                        p["names"] = (p["names"] + defer)[-8:]
            # 6 errors 새로 생김
            ne = len(r.get("errors") or [])
            oe = self.base_errs.get(pid)
            self.base_errs[pid] = ne
            if oe is not None and ne > oe and not first:
                a = self._once(pid, "errors", f"errors 발생 {ne}건 — {str((r.get('errors') or [''])[-1])[:120]}",
                               now, EVENT_WINDOW)
                out += [a] if a else []
            # 1 error
            if st == "error":
                if f"{pid}:error" not in self.alerted or now - self.alerted[f"{pid}:error"] >= RENOTIFY:
                    L = await lines(pid)
                    if not (L and park_reason([m for _t, m in L]) in EXIT_MARKS):
                        a = self._once(pid, "error", "상태=error", now)
                        out += [a] if a else []
            elif st != "offline":
                self._rearm(pid, "error")
            if first and done_slot(r):
                self.known_done.add(pid)
            if all_done(r):
                for k in PERSIST_KEYS[1:]:
                    self.since.pop(f"{pid}:{k}", None)
                continue
            # 2·3 offline / reconnecting · 4 WS 끊김 + 보고 정체
            conds = (("offline", st == "offline"), ("reconnecting", st == "reconnecting"),
                     ("wsdead", st in ("hunting", "moving") and r.get("_ws_live") is False and not report_fresh(r, now)))
            if not any(c for _k, c in conds):
                self._mass(pid, False, now)
            for key, cond in conds:
                k = f"{pid}:{key}"
                if not cond:
                    self.since.pop(k, None)
                    self._rearm(pid, key)
                    continue
                self.since.setdefault(k, now)
                # 4 는 「보고가 멈춘 지」 로 잰다(보고 30초 주기 — 마지막 보고 시각이 곧 정체 시작)
                held = report_age(r, now) if key == "wsdead" else now - self.since[k]
                if held < DWELL[key]:
                    continue
                if k in self.alerted and now - self.alerted[k] < RENOTIFY:
                    m = self._mass(pid, True, now)      # 이미 알린 PC 도 집단 사망 셈에는 넣는다(scouter P4 반증)
                    out += [m] if m else []
                    continue
                if await logs_flowing(pid) or await parked(pid, r):
                    self.since.pop(k, None)             # 살아 있다 / 사람이 세웠다 — 처음부터 다시 잰다
                    continue
                m = self._mass(pid, True, now)
                if m:
                    out.append(m)
                    continue
                what = {"offline": "offline 상태가", "reconnecting": "reconnecting 상태가",
                        "wsdead": "사냥 중이라는데 WS 끊김·상태 보고 정체가"}[key]
                a = self._once(pid, key, f"{what} {held / 60:.1f}분째 지속 (로그도 정지)", now)
                out += [a] if a else []
            # 5 완료 슬롯에서 사냥
            k = f"{pid}:done_slot"
            if done_slot(r):
                if pid in self.known_done:
                    continue
                self.since.setdefault(k, now)
                held = now - self.since[k]
                if held >= DWELL["done_slot"]:
                    left = [c.get("slot") for c in (r.get("daily_progress") or []) if isinstance(c, dict) and not c.get("completed")]
                    a = self._once(pid, "done_slot", f"이미 완료된 슬롯 {r.get('slot')} 에서 {held / 60:.1f}분째 사냥 중 "
                                                     f"(남은 슬롯 {left}) — 전환이 실패한 채 방치된 상태", now)
                    out += [a] if a else []
            else:
                self.since.pop(k, None)
                self._rearm(pid, "done_slot")
                self.known_done.discard(pid)
        # 7 유예 중 버그스샷 판정 — 물리 PC 의 카드 중 하나라도 일하고 살아 있으면 자가복구
        by_base = {}
        for r in rows:
            by_base.setdefault(base_pc(r.get("pc_id")), []).append(r)
        for bp, p in list(self.bug_pending.items()):
            if now - p.get("t", p["t0"]) < BUG_GRACE_S:
                continue
            cards = by_base.get(bp) or []
            working = [r for r in cards if str(r.get("status")) in BUG_WORK_ST
                       and (r.get("_ws_live") is not False or report_fresh(r, now))]
            stopped = all(str(r.get("status")) in BUG_STOP_ST for r in cards) if cards else True
            if working:
                self.bug_pending.pop(bp)              # 자가복구 — 안 깨운다
                continue
            if not stopped and now - p["t0"] < BUG_TOTAL_CAP_S:
                continue                              # 전환·접속 중 — 상한까지 더 본다
            self.bug_pending.pop(bp)
            if cards and all([await parked(str(r.get("pc_id")), r) for r in cards]):
                continue                              # 사람이 세웠다
            a = self._once(bp, "bug:" + bug_family(p["names"][-1]),
                           f"버그스샷 {p['names'][-1][-48:]} → 첫 감지 {(now - p['t0']) / 60:.0f}분 뒤에도 멈춰 있음 "
                           f"(자가복구 안 됨)", now, EVENT_WINDOW)
            out += [a] if a else []
        # 사라진 카드의 시계는 버린다(다시 나타나면 처음부터)
        live = {str(r.get("pc_id")) for r in rows}
        for k in [k for k in self.since if k.split(":")[0] not in live]:
            self.since.pop(k, None)
        return out
