#!/usr/bin/env python3
"""
hankookchon.com/job_b 새 글 감지 → 텔레그램 알림. (v2)
v2 변경점: headless="virtual" (Xvfb 가상 디스플레이) + 브라우저 2회 재시도
          + Turnstile 체크박스 클릭 시도 → Cloudflare 통과율 개선

env:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  (GitHub Secrets)
"""
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BOARD_URL = "https://www.hankookchon.com/job_b"
STATE_FILE = Path("state/seen.json")
DEBUG_HTML = Path("page_debug.html")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# 이 키워드가 제목에 있으면 우선순위 알림 (통역 관련)
HOT_KEYWORDS = re.compile(r"통역|번역|interpret|translat|통번역", re.I)

# 알림 제외할 네비게이션성 링크 텍스트
NAV_TITLES = {"구인", "구직", "구인구직", "목록", "다음", "이전", "글쓰기", "검색", "처음", "끝"}

FAIL_ALERT_AT = 3          # 연속 실패 n회째에 경고 1번
FAIL_ALERT_EVERY = 48      # 이후 n회마다 다시 경고 (30분 주기면 ~하루)

BROWSER_ATTEMPTS = 2       # 실행당 브라우저 재시도 횟수
CHALLENGE_WAIT_SEC = 100   # 시도당 챌린지 해결 대기 시간


def tg_send(text: str, silent: bool = False) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        print("[warn] telegram secrets missing; would send:\n", text)
        return False
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
            "disable_notification": silent,
        },
        timeout=30,
    )
    if r.status_code != 200:
        print("[error] telegram:", r.status_code, r.text[:300])
    return r.status_code == 200


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"seen": [], "fail_count": 0, "seeded": False}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["seen"] = state["seen"][-1000:]
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def is_challenge(html: str, title: str) -> bool:
    t = (title or "").lower()
    return (
        "just a moment" in t
        or "잠시만" in (title or "")
        or ("challenges.cloudflare.com" in html and "cf-turnstile" in html)
        or "Performing security verification" in html
    )


def try_click_turnstile(page) -> None:
    """CF 체크박스형 챌린지일 경우 위젯 클릭 시도 (best-effort)."""
    try:
        frames = page.frames
        for f in frames:
            if "challenges.cloudflare.com" in (f.url or ""):
                # iframe 요소 자체의 위치를 찾아 좌측 체크박스 부근 클릭
                el = page.query_selector("iframe[src*='challenges.cloudflare.com']")
                if el:
                    box = el.bounding_box()
                    if box:
                        page.mouse.click(box["x"] + 30, box["y"] + box["height"] / 2)
                        print("[info] clicked turnstile area")
                        return
    except Exception as e:
        print("[info] turnstile click attempt failed:", e)


def fetch_board_html() -> str | None:
    """Camoufox(가상 디스플레이)로 페이지를 열고 CF 챌린지가 풀릴 때까지 대기."""
    from camoufox.sync_api import Camoufox

    last_html = ""
    for attempt in range(1, BROWSER_ATTEMPTS + 1):
        print(f"[info] browser attempt {attempt}/{BROWSER_ATTEMPTS}")
        try:
            with Camoufox(
                headless="virtual",  # Xvfb 가상 디스플레이 — CF headless 탐지 회피
                humanize=True,
                os="windows",
                geoip=True,
            ) as browser:
                page = browser.new_page()
                page.goto(BOARD_URL, wait_until="domcontentloaded", timeout=90_000)
                deadline = time.time() + CHALLENGE_WAIT_SEC
                clicked = False
                while time.time() < deadline:
                    last_html = page.content()
                    title = page.title()
                    if not is_challenge(last_html, title):
                        print(f"[ok] challenge passed, title={title!r}, len={len(last_html)}")
                        return last_html
                    # 15초쯤 기다려도 안 풀리면 체크박스 클릭 1회 시도
                    if not clicked and time.time() > deadline - CHALLENGE_WAIT_SEC + 15:
                        try_click_turnstile(page)
                        clicked = True
                    time.sleep(4)
                print(f"[fail] attempt {attempt}: challenge not solved in {CHALLENGE_WAIT_SEC}s")
        except Exception as e:
            print(f"[error] attempt {attempt} exception:", e)
        time.sleep(3)

    if last_html:
        DEBUG_HTML.write_text(last_html, encoding="utf-8")
    return None


def parse_posts(html: str) -> list[dict]:
    """게시판 HTML에서 (id, title, url) 추출. 휴리스틱 파서."""
    soup = BeautifulSoup(html, "html.parser")
    posts, seen_urls = [], set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if "job_b" not in href:
            continue
        url = urljoin(BOARD_URL, href)
        if not re.search(r"(view|uid=|idx=|no=|wr_id=|document_srl|bmode=|/job_b/\d+)", url, re.I):
            continue
        title = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        if len(title) < 4 or title in NAV_TITLES or re.fullmatch(r"[\d\s./-]+", title):
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        m = re.search(r"(?:uid|idx|no|wr_id|document_srl)=(\d+)", url) or re.search(
            r"/job_b/(\d+)", url
        )
        posts.append({"id": m.group(1) if m else url, "title": title, "url": url})

    return posts


def main() -> int:
    state = load_state()

    try:
        html = fetch_board_html()
    except Exception as e:
        print("[error] fetch exception:", e)
        html = None

    if html is None:
        state["fail_count"] += 1
        fc = state["fail_count"]
        if fc == FAIL_ALERT_AT or (fc > FAIL_ALERT_AT and fc % FAIL_ALERT_EVERY == 0):
            tg_send(
                f"⚠️ 한국촌 구인 모니터링 실패 {fc}회 연속.\n"
                f"Cloudflare에 막혔을 가능성이 커요. GitHub Actions 로그 확인 필요.",
                silent=True,
            )
        save_state(state)
        return 0

    posts = parse_posts(html)
    print(f"[info] parsed {len(posts)} posts")
    for p in posts[:10]:
        print("   -", p["id"], "|", p["title"][:50])

    if len(posts) < 3:
        DEBUG_HTML.write_text(html, encoding="utf-8")
        state["fail_count"] += 1
        if state["fail_count"] == FAIL_ALERT_AT:
            tg_send(
                "⚠️ 한국촌 페이지는 열렸는데 글 파싱이 안 돼요. "
                "게시판 구조가 예상과 다른 듯 — page_debug.html 아티팩트를 확인해주세요.",
                silent=True,
            )
        save_state(state)
        return 0

    state["fail_count"] = 0
    seen = set(state["seen"])
    new_posts = [p for p in posts if p["id"] not in seen]

    if not state.get("seeded"):
        state["seen"].extend(p["id"] for p in posts)
        state["seeded"] = True
        save_state(state)
        tg_send(
            f"✅ 한국촌 구인게시판 모니터링 시작!\n"
            f"현재 {len(posts)}개 글을 기준으로, 이후 올라오는 새 글부터 알려줄게요.\n"
            f"🎙️ 통역/번역 공고는 따로 강조 표시됩니다."
        )
        return 0

    if new_posts:
        for p in reversed(new_posts):
            hot = bool(HOT_KEYWORDS.search(p["title"]))
            prefix = "🎙️ <b>통역 공고!</b>\n" if hot else "📌 새 구인 공고\n"
            tg_send(
                f"{prefix}<b>{p['title']}</b>\n{p['url']}",
                silent=not hot,
            )
            time.sleep(1)
        state["seen"].extend(p["id"] for p in new_posts)
    else:
        print("[info] no new posts")

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
