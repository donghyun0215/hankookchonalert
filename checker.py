#!/usr/bin/env python3
"""
hankookchon.com/job_b 새 글 감지 → 텔레그램 알림.
Cloudflare 차단 때문에 Camoufox(스텔스 Firefox)로 페이지를 연다.

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
    # seen 무한 증식 방지 (최근 1000개만 유지)
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


def fetch_board_html() -> str | None:
    """Camoufox로 페이지를 열고 CF 챌린지가 풀릴 때까지 대기."""
    from camoufox.sync_api import Camoufox

    with Camoufox(headless=True, humanize=True, os="windows", geoip=True) as browser:
        page = browser.new_page()
        page.goto(BOARD_URL, wait_until="domcontentloaded", timeout=90_000)
        deadline = time.time() + 120
        while time.time() < deadline:
            html = page.content()
            title = page.title()
            if not is_challenge(html, title):
                print(f"[ok] challenge passed, title={title!r}, len={len(html)}")
                return html
            time.sleep(4)
        # 실패: 디버그용으로 저장
        DEBUG_HTML.write_text(page.content(), encoding="utf-8")
        print("[fail] cloudflare challenge not solved within 120s")
        return None


def parse_posts(html: str) -> list[dict]:
    """게시판 HTML에서 (id, title, url) 추출. 보드 구조를 모를 수 있어 휴리스틱 파서."""
    soup = BeautifulSoup(html, "html.parser")
    posts, seen_urls = [], set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if "job_b" not in href:
            continue
        url = urljoin(BOARD_URL, href)
        # 개별 글로 보이는 패턴: view/uid/idx/no/wr_id/document_srl 또는 /job_b/숫자
        if not re.search(r"(view|uid=|idx=|no=|wr_id=|document_srl|bmode=|/job_b/\d+)", url, re.I):
            continue
        title = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        if len(title) < 4 or title in NAV_TITLES or re.fullmatch(r"[\d\s./-]+", title):
            continue  # 페이지 번호, 날짜, 네비게이션 제외
        if url in seen_urls:
            continue
        seen_urls.add(url)
        # id: URL의 숫자 파라미터 우선, 없으면 URL 자체
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
        return 0  # 워크플로 자체는 성공 처리 (state 커밋 위해)

    posts = parse_posts(html)
    print(f"[info] parsed {len(posts)} posts")
    for p in posts[:10]:
        print("   -", p["id"], "|", p["title"][:50])

    if len(posts) < 3:
        # 파싱 실패로 간주 (구조 변경 or 챌린지 오탐)
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
        # 첫 실행: 현재 글들을 기준선으로만 저장, 알림 폭탄 방지
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
        # 오래된 것부터 알림 (목록은 보통 최신순이므로 역순)
        for p in reversed(new_posts):
            hot = bool(HOT_KEYWORDS.search(p["title"]))
            prefix = "🎙️ <b>통역 공고!</b>\n" if hot else "📌 새 구인 공고\n"
            tg_send(
                f"{prefix}<b>{p['title']}</b>\n{p['url']}",
                silent=not hot,  # 통역 공고만 소리 알림, 나머지는 무음 알림
            )
            time.sleep(1)
        state["seen"].extend(p["id"] for p in new_posts)
    else:
        print("[info] no new posts")

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
