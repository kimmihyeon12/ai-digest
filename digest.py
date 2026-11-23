"""AI 업데이트 모음.

매일 실행해서
  1) sources.json 의 출처를 모아 data/items.json 에 누적하고
  2) 영어 글은 한국어로 옮기고 (GitHub Models, Actions 에서만)
  3) essentials.json 의 필수 도구 별·포크 수를 갱신하고 인기 스킬을 자동 수집한 뒤
  4) docs/index.html 을 만들고 디스코드에 알린다.
표준 라이브러리만 사용.

    python digest.py            전체 실행
    python digest.py --dry      디스코드 전송 없이
    python digest.py --selftest

환경변수: DISCORD_WEBHOOK, SITE_URL, GITHUB_TOKEN(번역·GitHub API),
          DISCORD_BOT_TOKEN + DISCORD_INBOX_CHANNEL(선택, '직접 모은 것')
"""
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).parent
SOURCES = ROOT / 'sources.json'
ESSENTIALS = ROOT / 'essentials.json'
ARCHIVE = ROOT / 'data' / 'items.json'
KO_CACHE = ROOT / 'data' / 'ko_cache.json'      # 필수 도구 자동 목록 번역 보관
TEMPLATE = ROOT / 'template.html'
SITE = ROOT / 'docs' / 'index.html'

KST = timezone(timedelta(hours=9))
UA = 'Mozilla/5.0 (compatible; ai-digest/1.0)'
PER_SOURCE = 30      # 출처당 최신 N개 (OpenAI 피드는 전체 기록이라 큼)
ARCHIVE_MAX = 4000
FILTERS = {
    'ai': r'\bAI\b|LLM|MCP|GPT|Claude|Gemini|Codex|에이전트|agent|인공지능|RAG|Copilot|OpenAI|Anthropic|모델',
    # 별을 사서 띄우는 크랙·불법 배포 저장소 거르기
    'spam': r'crack|keygen|activat|license.?key|pro-win|free.?download|cheat|hack.?tool|unlock|premium.?free',
}

# 한국어화: GitHub Models (Actions 의 GITHUB_TOKEN + models: read 권한)
MODELS_URL = 'https://models.github.ai/inference/chat/completions'
MODELS_NAME = 'openai/gpt-4.1-mini'
KO_BATCH = 15          # 한 요청에 묶을 글 수
KO_MAX_PER_RUN = 300   # 한 번 실행에 번역할 최대 글 수 (무료 한도 보호)
TOOL_CATS = {'MCP', '스킬'}   # 요약 대신 '기능 + 활용 예' 한 줄
KO_PROMPT = (
    '아래 JSON 배열의 글을 한국어로 옮겨 주세요.\n'
    '- title: 자연스러운 한국어 제목. 제품·저장소·회사 이름과 버전 번호는 원문 그대로.\n'
    '- summary: 50자 안팎 한 문장. 번역투와 과장 없이 사람이 쓴 것처럼.\n'
    '- tool 이 true 면 summary 는 "무슨 기능인지 + 어떻게 쓰면 좋은지"를 한 문장으로.\n'
    '- 릴리스(버전) 글이면 summary 에 바뀐 점 핵심만.\n'
    '출력은 {"items":[{"id":..., "title":..., "summary":...}]} JSON 만.'
)


# ---------- 공통 ----------

def fetch(url, headers=None, data=None):
    req = urllib.request.Request(url, data=data, headers={'User-Agent': UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def fetch_json(url, headers=None, data=None):
    return json.loads(fetch(url, headers, data))


def github_headers():
    headers = {'Accept': 'application/vnd.github+json'}
    if os.environ.get('GITHUB_TOKEN'):
        headers['Authorization'] = 'Bearer ' + os.environ['GITHUB_TOKEN']
    return headers


def now_kst():
    return datetime.now(KST)


def to_kst(text):
    """ISO·RFC822 날짜 문자열 → 'YYYY-MM-DDTHH:MM+09:00'. 못 읽으면 지금."""
    dt = None
    if text:
        try:
            dt = datetime.fromisoformat(text.replace('Z', '+00:00'))
        except ValueError:
            try:
                dt = parsedate_to_datetime(text)
            except (TypeError, ValueError):
                pass
    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KST).isoformat(timespec='minutes')


def plain(text, limit=180):
    text = re.sub(r'<[^>]+>', ' ', html.unescape(text or ''))
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:limit] + ('…' if len(text) > limit else '')


def has_korean(text):
    return bool(re.search(r'[가-힣]', text or ''))


def load_json(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def save_json(path, data):
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding='utf-8')


def make_item(src, title, url, date, summary='', image='', score=0):
    """score: 중요도 판정용 숫자(별 수·점수 등). important 는 apply_rules 에서 정한다."""
    return {'id': url, 'cat': src['cat'], 'src': src['name'], 'title': title.strip(), 'url': url,
            'date': date, 'summary': summary, 'image': image, 'score': score, 'important': False}


# ---------- 출처 종류별 수집 (sources.json 의 type) ----------

def from_rss(src):
    out = []
    for node in ET.fromstring(fetch(src['url'])).iter():
        if node.tag.split('}')[-1] not in ('entry', 'item'):
            continue
        f = {}
        for child in node:
            f.setdefault(child.tag.split('}')[-1], child)
        link = f.get('link')
        url = (link.get('href') or link.text or '').strip() if link is not None else ''
        title = (f['title'].text or '') if 'title' in f else ''
        date = next((f[k].text for k in ('updated', 'published', 'pubDate', 'date') if k in f), None)
        body = next((f[k].text for k in ('content', 'summary', 'description') if k in f and f[k].text), '')
        img = re.search(r'<img[^>]+src="([^"]+)"', html.unescape(body))
        if url and title.strip():
            out.append(make_item(src, title, url, to_kst(date), plain(body), img.group(1) if img else ''))
    return out


def github_item(src, full_name, desc, stars_label, score, date=None):
    return make_item(src, full_name, f'https://github.com/{full_name}', date or to_kst(None),
                     plain(f'★{stars_label} · {desc or ""}'), f'https://opengraph.githubassets.com/1/{full_name}',
                     score)


def from_github_trending(src):
    page = fetch('https://github.com/trending?since=daily').decode('utf-8', 'replace')
    out = []
    for block in page.split('<article class="Box-row')[1:]:
        name = re.search(r'href="/([^/"]+/[^/"]+)" data-view-component="true" class="Link"', block)
        if not name:
            continue
        desc = re.search(r'<p class="col-9[^>]*>(.*?)</p>', block, re.S)
        today = re.search(r'([\d,]+) stars today', block)
        gained = int(today.group(1).replace(',', '')) if today else 0
        out.append(github_item(src, name.group(1), plain(desc.group(1)) if desc else '',
                               f'+{gained:,} 오늘', gained))
    return out


def search_repos(query, sort='stars', limit=15):
    q = urllib.parse.quote(query)
    url = f'https://api.github.com/search/repositories?q={q}&sort={sort}&order=desc&per_page={limit}'
    return fetch_json(url, github_headers())['items']


def from_github_search(src):
    since = (now_kst() - timedelta(days=src.get('days', 7))).strftime('%Y-%m-%d')
    repos = search_repos(f"{src['query']} created:>{since} stars:>={src.get('min_stars', 0)}")
    return [github_item(src, r['full_name'], r['description'], f"{r['stargazers_count']:,}",
                        r['stargazers_count'], to_kst(r['created_at']))
            for r in repos]


def from_hn(src):
    url = (f"https://hn.algolia.com/api/v1/search_by_date?tags={src['tags']}&hitsPerPage=40"
           f"&numericFilters=points%3E{src.get('min_points', 0)}")
    out = []
    for h in fetch_json(url)['hits']:
        link = h.get('url') or f"https://news.ycombinator.com/item?id={h['objectID']}"
        points = h.get('points') or 0
        out.append(make_item(src, h['title'], link, to_kst(h['created_at']),
                             f"{points} points · 댓글 {h.get('num_comments') or 0}", score=points))
    return out


def from_discord_inbox(src):
    """지정 채널에 붙여 넣은 링크(인스타 등). 봇 설정이 없으면 빈 목록."""
    token, channel = os.environ.get('DISCORD_BOT_TOKEN'), os.environ.get('DISCORD_INBOX_CHANNEL')
    if not (token and channel):
        return []
    messages = fetch_json(f'https://discord.com/api/v10/channels/{channel}/messages?limit=100',
                          {'Authorization': f'Bot {token}'})
    out = []
    for m in messages:
        note = plain(re.sub(r'https?://\S+', '', m.get('content', '')), 120)
        who = m['author'].get('global_name') or m['author']['username']
        for url in re.findall(r'https?://[^\s>)]+', m.get('content', '')):
            host = urllib.parse.urlparse(url).netloc.removeprefix('www.')
            site = '인스타그램' if 'instagram' in host else '스레드' if 'threads' in host else host
            out.append(make_item(dict(src, name=site), note or f'{site} 게시물', url,
                                 to_kst(m['timestamp']), f'{who} 님이 공유'))
    return out


COLLECTORS = {
    'rss': from_rss,
    'github_trending': from_github_trending,
    'github_search': from_github_search,
    'hn': from_hn,
    'discord_inbox': from_discord_inbox,
}


def rule_pattern(src, key):
    """sources.json 값이 FILTERS 이름이면 그 정규식, 아니면 값 자체."""
    value = src.get(key)
    return FILTERS.get(value, value)


def apply_rules(src, items):
    """sources.json 규칙 적용.
    filter: 제목이 맞는 것만 / exclude: 제목·설명이 맞으면 제외 / project_prefix: '프로젝트'로 분류
    important_title: 제목이 맞으면 중요 / important_score: score 가 이 값 이상이면 중요
    """
    keep, drop = rule_pattern(src, 'filter'), rule_pattern(src, 'exclude')
    if keep:
        items = [i for i in items if re.search(keep, i['title'], re.I)]
    if drop:
        items = [i for i in items if not re.search(drop, f"{i['title']} {i['summary']}", re.I)]
    prefix, imp_title, imp_score = src.get('project_prefix'), src.get('important_title'), src.get('important_score')
    for i in items:
        if prefix and i['title'].lstrip().lower().startswith(prefix.lower()):
            i['cat'] = '프로젝트'
        i['important'] = bool((imp_title and re.search(imp_title, i['title'], re.I))
                              or (imp_score and i['score'] >= imp_score))
    return items[:PER_SOURCE]


def collect(sources):
    items, errors = {}, []
    for src in sources:
        try:
            for i in apply_rules(src, COLLECTORS[src['type']](src)):
                items.setdefault(i['id'], i)
        except Exception as ex:  # 한 출처가 막혀도 나머지는 계속
            errors.append(f"{src['name']}: {type(ex).__name__} {ex}"[:200])
    return list(items.values()), errors


# ---------- 누적 ----------

def merge(archive, items, first_run):
    """새 글을 누적에 더하고, 알림 대상(첫 실행이면 최근 하루치만)을 돌려준다.
    이미 있는 글은 중요도·점수만 최신으로 갱신(번역은 유지)."""
    by_id = {a['id']: a for a in archive}
    stamp = now_kst().isoformat(timespec='minutes')
    new = []
    for i in items:
        if i['id'] in by_id:
            by_id[i['id']].update(score=i['score'], important=i['important'] or by_id[i['id']].get('important', False))
        else:
            new.append(dict(i, added=stamp))
    if first_run:
        cutoff = (now_kst() - timedelta(days=1)).isoformat()
        to_notify = [i for i in new if i['date'] >= cutoff]
    else:
        to_notify = new
    merged = sorted(list(by_id.values()) + new, key=lambda i: i['date'], reverse=True)[:ARCHIVE_MAX]
    return merged, to_notify


# ---------- 한국어화 ----------

def ask_models(rows):
    """[{id,title,summary,tool}] → {id: {title, summary}}. 실패하면 예외."""
    body = json.dumps({
        'model': MODELS_NAME,
        'temperature': 0.2,
        'response_format': {'type': 'json_object'},
        'messages': [{'role': 'user', 'content': KO_PROMPT + '\n\n' + json.dumps(rows, ensure_ascii=False)}],
    }).encode()
    headers = {'Authorization': 'Bearer ' + os.environ['GITHUB_TOKEN'], 'Content-Type': 'application/json'}
    reply = fetch_json(MODELS_URL, headers, body)['choices'][0]['message']['content']
    return {r['id']: r for r in json.loads(reply)['items'] if r.get('id')}


def localize(entries, key_of, text_of, is_tool, apply):
    """번역이 필요한 항목만 묶어서 번역하고 apply(entry, ko) 로 반영. 번역한 개수와 오류 반환."""
    if not os.environ.get('GITHUB_TOKEN'):
        return 0, []
    todo = [e for e in entries if text_of(e) and not has_korean(text_of(e)[0])][:KO_MAX_PER_RUN]
    done, errors = 0, []
    for start in range(0, len(todo), KO_BATCH):
        batch = todo[start:start + KO_BATCH]
        rows = [{'id': key_of(e), 'title': text_of(e)[0], 'summary': text_of(e)[1], 'tool': is_tool(e)}
                for e in batch]
        try:
            result = ask_models(rows)
        except Exception as ex:  # 한도 초과 등: 남은 건 다음 실행에
            errors.append(f'번역: {type(ex).__name__} {ex}'[:200])
            break
        for e in batch:
            ko = result.get(key_of(e))
            if ko and ko.get('title'):
                apply(e, ko)
                done += 1
    return done, errors


def localize_items(archive):
    def apply(item, ko):
        item['ko_title'], item['ko_summary'] = ko['title'], ko.get('summary', '')
    pending = [i for i in archive if 'ko_title' not in i]
    return localize(pending, key_of=lambda i: i['id'], text_of=lambda i: (i['title'], i['summary']),
                    is_tool=lambda i: i['cat'] in TOOL_CATS, apply=apply)


# ---------- 필수 도구 ----------

def refresh_essentials(cfg):
    """picks 의 별·포크·업데이트일 갱신, auto 검색으로 인기 저장소 수집. 결과 dict 와 오류 반환."""
    errors, cache = [], load_json(KO_CACHE, {})
    picks = []
    for p in cfg['picks']:
        entry = dict(p)
        if p.get('repo'):
            entry['url'] = f"https://github.com/{p['repo']}"
            try:
                r = fetch_json(f"https://api.github.com/repos/{p['repo']}", github_headers())
                entry.update(stars=r['stargazers_count'], forks=r['forks_count'], pushed=r['pushed_at'][:10])
            except Exception as ex:
                errors.append(f"{p['name']}: {type(ex).__name__}"[:200])
        picks.append(entry)

    auto, seen = [], {p.get('repo') for p in cfg['picks']}
    for a in cfg.get('auto', []):
        try:
            repos = search_repos(a['query'], a.get('sort', 'forks'), a.get('limit', 10))
        except Exception as ex:
            errors.append(f"{a['query']}: {type(ex).__name__} {ex}"[:200])
            continue
        drop = FILTERS.get(a.get('exclude'), a.get('exclude'))
        for r in repos:
            if r['full_name'] in seen or (drop and re.search(drop, f"{r['full_name']} {r['description']}", re.I)):
                continue
            seen.add(r['full_name'])
            ko = cache.get(r['full_name'], {})
            auto.append({'group': a['group'], 'name': r['full_name'], 'kind': '자동', 'repo': r['full_name'],
                         'url': r['html_url'], 'desc': plain(r['description'] or '', 200),
                         'what': ko.get('summary', ''), 'stars': r['stargazers_count'],
                         'forks': r['forks_count'], 'pushed': r['pushed_at'][:10]})

    def apply(entry, ko):
        entry['what'] = ko.get('summary', '')
        cache[entry['repo']] = {'summary': entry['what']}
    _, ko_errors = localize([e for e in auto if not e['what']], key_of=lambda e: e['repo'],
                            text_of=lambda e: (e['name'], e['desc']), is_tool=lambda e: True, apply=apply)
    save_json(KO_CACHE, cache)
    return {'picks': picks, 'auto': auto, 'links': cfg.get('links', [])}, errors + ko_errors


# ---------- 출력 ----------

def discord_text(new, site_url):
    counts = {}
    for i in new:
        counts[i['cat']] = counts.get(i['cat'], 0) + 1
    summary = ' · '.join(f'{c} {n}' for c, n in sorted(counts.items(), key=lambda x: -x[1]))
    lines = [f'**AI 업데이트 {now_kst():%m/%d}** 새 소식 {len(new)}건 ({summary})']
    picked = sorted(new, key=lambda i: (not i.get('important'), i['cat'] not in ('도구', 'MCP', '스킬')))[:6]
    for i in picked:
        mark = '🔴 ' if i.get('important') else '• '
        lines.append(f"{mark}[{i['src']}] {(i.get('ko_title') or i['title'])[:80]}")
    if site_url:
        lines.append(f'<{site_url}>')
    return '\n'.join(lines)[:1900]  # 디스코드 2000자 제한


def send_discord(webhook, text):
    fetch(webhook, {'Content-Type': 'application/json'}, json.dumps({'content': text}).encode())


def build_site(archive, essentials, errors):
    def embed(value):
        return json.dumps(value, ensure_ascii=False).replace('</', '<\\/')
    meta = {'updated': f'{now_kst():%Y-%m-%d %H:%M}', 'errors': errors}
    page = (TEMPLATE.read_text(encoding='utf-8')
            .replace('/*DATA*/[]', embed(archive))
            .replace('/*ESSENTIALS*/{}', embed(essentials))
            .replace('/*META*/{}', embed(meta)))
    SITE.parent.mkdir(exist_ok=True)
    SITE.write_text(page, encoding='utf-8')


def main(dry):
    sources = load_json(SOURCES, [])
    first_run = not ARCHIVE.exists()
    archive = load_json(ARCHIVE, [])

    items, errors = collect(sources)
    archive, new = merge(archive, items, first_run)
    translated, ko_errors = localize_items(archive)
    save_json(ARCHIVE, archive)

    essentials, ess_errors = refresh_essentials(load_json(ESSENTIALS, {'picks': []}))
    all_errors = errors + ko_errors + ess_errors
    build_site(archive, essentials, all_errors)

    webhook = os.environ.get('DISCORD_WEBHOOK', '')
    sent = bool(new and webhook.startswith('https://') and not dry)
    if sent:
        send_discord(webhook, discord_text(new, os.environ.get('SITE_URL', '')))

    print(f"수집 {len(items)} · 새 소식 {len(new)} · 번역 {translated} · 누적 {len(archive)} · "
          f"필수 도구 {len(essentials['picks'])}+자동 {len(essentials['auto'])} · 디스코드 {'전송' if sent else '생략'}")
    for e in all_errors:
        print('  건너뜀:', e)


# ---------- 자체 점검 ----------

def selftest():
    src = {'cat': '도구', 'name': 'T'}
    atom = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>v1.2.0</title>
      <link href="https://x/1"/><updated>2026-11-20T01:00:00Z</updated>
      <content>&lt;p&gt;Fix &lt;img src="https://i/a.png"&gt;bug&lt;/p&gt;</content></entry></feed>'''
    rss = b'''<rss><channel><item><title>Show GN: AI tool</title><link>https://y/2</link>
      <pubDate>Fri, 20 Nov 2026 10:00:00 GMT</pubDate><description>hello</description></item></channel></rss>'''
    global fetch
    real_fetch, fetch = fetch, lambda url, headers=None, data=None: atom if 'atom' in url else rss
    try:
        a = from_rss(dict(src, url='atom'))[0]
        r = from_rss(dict(src, url='rss'))[0]
    finally:
        fetch = real_fetch
    assert a['url'] == 'https://x/1' and a['summary'] == 'Fix bug' and a['image'] == 'https://i/a.png'
    assert a['date'].startswith('2026-11-20T10:00') and r['date'].startswith('2026-11-20T19:00')

    ruled = apply_rules({'filter': 'ai', 'project_prefix': 'Show GN'}, [r, dict(a, title='no match')])
    assert len(ruled) == 1 and ruled[0]['cat'] == '프로젝트'
    assert apply_rules({'important_title': r'^v?\d+\.\d+\.0\b'}, [dict(a)])[0]['important']
    assert not apply_rules({'important_score': 100}, [dict(a, score=50)])[0]['important']
    assert apply_rules({'exclude': 'spam'}, [dict(a, title='acrobat-pro-win crack')]) == []

    old = make_item(src, 'old', 'https://o', '2020-01-01T00:00+09:00')
    merged, new = merge([dict(old, ko_title='옛')], [dict(old, score=9), a], first_run=False)
    assert len(merged) == 2 and [i['id'] for i in new] == ['https://x/1']
    kept = next(i for i in merged if i['id'] == 'https://o')
    assert kept['ko_title'] == '옛' and kept['score'] == 9      # 번역 유지, 점수 갱신
    assert merge([], [old], first_run=True)[1] == []           # 첫 실행엔 오래된 글 알림 안 함

    text = discord_text([dict(a, important=True, ko_title='버전 1.2.0')], 'https://site')
    assert '🔴' in text and '버전 1.2.0' in text and text.endswith('<https://site>')
    assert has_korean('한글 제목') and not has_korean('English')

    assert all(s['type'] in COLLECTORS for s in load_json(SOURCES, [])), 'sources.json 에 모르는 type'
    ess = load_json(ESSENTIALS, {})
    assert all(p.get('repo') or p.get('url') for p in ess['picks']), 'essentials picks 에 repo/url 없음'
    print('selftest OK')


if __name__ == '__main__':
    selftest() if '--selftest' in sys.argv else main(dry='--dry' in sys.argv)
