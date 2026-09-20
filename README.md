# 한국촌 구인게시판 텔레그램 알리미

`hankookchon.com/job_b`에 새 글이 올라오면 텔레그램으로 알려준다.
통역/번역 공고(🎙️)는 소리 알림, 나머지는 무음 알림.

한국촌이 Cloudflare 봇 차단을 쓰기 때문에 일반 스크레이퍼는 403이 뜬다.
그래서 Camoufox(스텔스 브라우저)로 페이지를 열고, GitHub Actions가 30분마다 실행한다.
서버 불필요, 비용 0원 (public repo 기준 Actions 무제한).

## 셋업 (10분)

1. **텔레그램 봇 만들기**
   - 텔레그램에서 [@BotFather](https://t.me/BotFather) → `/newbot` → 이름 정하기
   - 받은 **bot token** 복사 (예: `1234567:AA...`)
   - 만든 봇에게 아무 메시지 하나 보내기 (봇이 나한테 먼저 말 못 걺)
   - [@userinfobot](https://t.me/userinfobot) 에게 말 걸면 내 **chat id**(숫자) 알려줌

2. **GitHub 레포 만들기**
   - 이 폴더를 새 **public** 레포로 push (public이어야 Actions 분량 무제한)
   - 게시판은 어차피 공개 데이터라 seen.json이 공개돼도 문제 없음. 토큰은 Secrets에만 들어감.

3. **Secrets 등록**
   - 레포 → Settings → Secrets and variables → Actions → New repository secret
   - `TELEGRAM_BOT_TOKEN` = 봇 토큰
   - `TELEGRAM_CHAT_ID` = 내 chat id

4. **첫 실행**
   - Actions 탭 → "hankookchon job monitor" → Run workflow
   - 성공하면 텔레그램으로 "✅ 모니터링 시작" 메시지가 옴
   - 첫 실행은 기준선만 잡고, 이후 새 글부터 알림

## 실패할 때

- Cloudflare가 GitHub의 데이터센터 IP를 막으면 챌린지를 못 풀 수 있다.
  3회 연속 실패하면 ⚠️ 텔레그램 경고가 온다.
- "파싱이 안 돼요" 경고가 오면: Actions 실행 페이지에서 `page-debug` 아티팩트를
  받아서 Claude한테 던져주면 파서를 게시판 구조에 맞게 고칠 수 있다.
- 계속 막히면 플랜 B: 같은 스크립트를 집 컴퓨터(주거용 IP)에서 cron으로 돌리면 거의 100% 통과.
