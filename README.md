# AI 업데이트 모음

매일 오전 8시(KST) GitHub Actions가 AI 도구 릴리스·MCP·회사 소식·재밌는 프로젝트를 모아
`docs/index.html`(GitHub Pages)을 갱신하고, 디스코드에 "새 소식 N건 + 사이트 링크"를 보냅니다.

## 사이트 탭
- **소식**: 분류·출처·기간 필터, 검색, 중요만(빨간색)·새 글만 보기. 영어 글은 한국어 제목·요약(원문 제목은 작게)
- **필수 도구**: 분야별로 고른 스킬·MCP + "이렇게 시켜 보세요" 예시(복사 버튼), 포크 많은 인기 스킬·MCP 자동 수집
- **바로가기**: 공식 문서·AI 서비스·찾아보기 링크

## 구조
- `sources.json` 소식 출처 목록 (여기만 고치면 출처 추가·삭제)
- `essentials.json` 필수 도구(picks)·자동 수집 검색어(auto)·바로가기(links)
- `digest.py` 수집·누적·사이트 생성·디스코드 알림 (표준 라이브러리만)
- `template.html` 사이트 화면 (분류·출처·기간 필터, 검색, 새 글 표시)
- `data/items.json` 누적 데이터 (최대 4000건)
- `.github/workflows/daily.yml` 매일 실행

## 처음 설정
1. 저장소 Settings → Pages → Source: `Deploy from a branch`, Branch: `main` / `/docs`
2. Settings → Secrets and variables → Actions
   - Secret `DISCORD_WEBHOOK`: 알림 받을 채널의 웹후크 URL
   - (선택) Secret `DISCORD_BOT_TOKEN`, Variable `DISCORD_INBOX_CHANNEL`: 링크를 붙여 넣을 채널 ID → '직접 모은 것'
3. Actions 탭 → daily-digest → Run workflow 로 첫 실행

## 출처 추가
`sources.json`에 한 줄 추가. RSS·Atom이면:
```json
{"cat": "회사 소식", "name": "이름", "type": "rss", "url": "피드 주소"}
```
- `type`: `rss` · `github_trending` · `github_search`(query, days, min_stars) · `hn`(tags, min_points) · `discord_inbox`
- `filter`: 제목 정규식, 또는 `"ai"`(AI 키워드 묶음)
- `exclude`: 제목·설명이 맞으면 제외, `"spam"`(크랙·불법 배포 저장소 거르기)
- `important_title` / `important_score`: 제목이 맞거나 점수(별·포인트)가 이 값 이상이면 빨간색 '중요'
- `project_prefix`: 이 글자로 시작하는 제목은 '프로젝트'로 분류 (예: `"Show GN"`)
- 새 분류를 만들면 `template.html`의 `CATS`에도 추가
- 새 종류의 출처는 `digest.py`에 `from_xxx(src)` 함수를 만들고 `COLLECTORS`에 등록

## 로컬 확인
```
python digest.py --selftest
python digest.py --dry      # 디스코드 전송 없이 data/, docs/ 생성
```
