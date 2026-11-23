"""AI 업데이트 모음.

sources.json 의 출처를 모아 data/items.json 에 누적하고, docs/index.html 을 만들고,
디스코드에 "새 소식 N건 + 사이트 링크"를 보낸다. 표준 라이브러리만 사용.

    python digest.py            수집 + 사이트 + 디스코드
    python digest.py --dry      디스코드 전송 없이
    python digest.py --selftest

환경변수: DISCORD_WEBHOOK, SITE_URL, GITHUB_TOKEN(선택),
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
ARCHIVE = ROOT / 'data' / 'items.json'
TEMPLATE = ROOT / 'template.html'
SITE = ROOT / 'docs' / 'index.html'

KST = timezone(timedelta(hours=9))
UA = 'Mozilla/5.0 (compatible; ai-digest/1.0)'
PER_SOURCE = 30      # 출처당 최신 N개 (OpenAI 피드는 전체 기록이라 큼)
ARCHIVE_MAX = 4000
FILTERS = {'ai': r'\bAI\b|LLM|MCP|GPT|Claude|Gemini|Codex|에이전트|agent|인공지능|RAG|Copilot|OpenAI|Anthropic|모델'}


# ---------- 공통 ----------

def fetch(url, headers=None):
    req = urllib.request.Request(url, headers={'User-Agent': UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def fetch_json(url, headers=None):
    return json.loads(fetch(url, headers))


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


def make_item(src, title, url, date, summary='', image=''):
    return {'id': url, 'cat': src['cat'], 'src': src['name'], 'title': title.strip(), 'url': url,
            'date': date, 'summary': summary, 'image': image}


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


def github_item(src, full_name, desc, stars, date=None):
    return make_item(src, full_name, f'https://github.com/{full_name}', date or to_kst(None),
                     plain(f'★{stars} · {desc or ""}'), f'https://opengraph.githubassets.com/1/{full_name}')


def from_github_trending(src):
    page = fetch('https://github.com/trending?since=daily').decode('utf-8', 'replace')
    out = []
    for block in page.split('<article class="Box-row')[1:]:
        name = re.search(r'href="/([^/"]+/[^/"]+)" data-view-component="true" class="Link"', block)
        if not name:
            continue
        desc = re.search(r'<p class="col-9[^>]*>(.*?)</p>', block, re.S)
        today = re.search(r'([\d,]+) stars today', block)
        out.append(github_item(src, name.group(1), plain(desc.group(1)) if desc else '',
                               f'+{today.group(1)} 오늘' if today else ''))
    return out


def from_github_search(src):
    since = (now_kst() - timedelta(days=src.get('days', 7))).strftime('%Y-%m-%d')
    q = urllib.parse.quote(f"{src['query']} created:>{since} stars:>={src.get('min_stars', 0)}")
    headers = {'Accept': 'application/vnd.github+json'}
    if os.environ.get('GITHUB_TOKEN'):
        headers['Authorization'] = 'Bearer ' + os.environ['GITHUB_TOKEN']
    data = fetch_json(f'https://api.github.com/search/repositories?q={q}&sort=stars&order=desc&per_page=15', headers)
    return [github_item(src, r['full_name'], r['description'], r['stargazers_count'], to_kst(r['created_at']))
            for r in data['items']]


def from_hn(src):
    url = (f"https://hn.algolia.com/api/v1/search_by_date?tags={src['tags']}&hitsPerPage=40"
           f"&numericFilters=points%3E{src.get('min_points', 0)}")
    out = []
    for h in fetch_json(url)['hits']:
        link = h.get('url') or f"https://news.ycombinator.com/item?id={h['objectID']}"
        out.append(make_item(src, h['title'], link, to_kst(h['created_at']),
                             f"{h.get('points', 0)} points · 댓글 {h.get('num_comments', 0)}"))
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


def apply_rules(src, items):
    """sources.json 의 filter(제목 정규식 또는 FILTERS 이름)·project_prefix 적용."""
    pattern = FILTERS.get(src.get('filter'), src.get('filter'))
    if pattern:
        items = [i for i in items if re.search(pattern, i['title'], re.I)]
    prefix = src.get('project_prefix')
    for i in items:
        if prefix and i['title'].lstrip().lower().startswith(prefix.lower()):
            i['cat'] = '프로젝트'
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


# ---------- 누적·출력 ----------

def merge(archive, items, first_run):
    """새 글을 누적에 더하고, 알림 대상(첫 실행이면 최근 하루치만)을 돌려준다."""
    known = {a['id'] for a in archive}
    stamp = now_kst().isoformat(timespec='minutes')
    new = [dict(i, added=stamp) for i in items if i['id'] not in known]
    if first_run:
        cutoff = (now_kst() - timedelta(days=1)).isoformat()
        new_to_notify = [i for i in new if i['date'] >= cutoff]
    else:
        new_to_notify = new
    merged = sorted(archive + new, key=lambda i: i['date'], reverse=True)[:ARCHIVE_MAX]
    return merged, new_to_notify


def discord_text(new, site_url):
    counts = {}
    for i in new:
        counts[i['cat']] = counts.get(i['cat'], 0) + 1
    summary = ' · '.join(f'{c} {n}' for c, n in sorted(counts.items(), key=lambda x: -x[1]))
    lines = [f'**AI 업데이트 {now_kst():%m/%d}** 새 소식 {len(new)}건 ({summary})']
    lines += [f"• [{i['src']}] {i['title'][:80]}" for i in new if i['cat'] in ('도구', 'MCP')][:5]
    if site_url:
        lines.append(f'<{site_url}>')
    return '\n'.join(lines)[:1900]  # 디스코드 2000자 제한


def send_discord(webhook, text):
    req = urllib.request.Request(webhook, data=json.dumps({'content': text}).encode(),
                                 headers={'Content-Type': 'application/json', 'User-Agent': UA})
    urllib.request.urlopen(req, timeout=30).read()


def build_site(archive, errors):
    data = json.dumps(archive, ensure_ascii=False).replace('</', '<\\/')
    meta = json.dumps({'updated': f'{now_kst():%Y-%m-%d %H:%M}', 'errors': errors}, ensure_ascii=False)
    page = TEMPLATE.read_text(encoding='utf-8').replace('/*DATA*/[]', data).replace('/*META*/{}', meta)
    SITE.parent.mkdir(exist_ok=True)
    SITE.write_text(page, encoding='utf-8')


def main(dry):
    sources = json.loads(SOURCES.read_text(encoding='utf-8'))
    first_run = not ARCHIVE.exists()
    archive = [] if first_run else json.loads(ARCHIVE.read_text(encoding='utf-8'))

    items, errors = collect(sources)
    archive, new = merge(archive, items, first_run)

    ARCHIVE.parent.mkdir(exist_ok=True)
    ARCHIVE.write_text(json.dumps(archive, ensure_ascii=False, indent=0), encoding='utf-8')
    build_site(archive, errors)

    webhook = os.environ.get('DISCORD_WEBHOOK', '')
    sent = bool(new and webhook.startswith('https://') and not dry)
    if sent:
        send_discord(webhook, discord_text(new, os.environ.get('SITE_URL', '')))

    print(f"수집 {len(items)} · 새 소식 {len(new)} · 누적 {len(archive)} · 디스코드 {'전송' if sent else '생략'}")
    for e in errors:
        print('  건너뜀:', e)


# ---------- 자체 점검 ----------

def selftest():
    src = {'cat': '도구', 'name': 'T'}
    atom = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>v1.2</title>
      <link href="https://x/1"/><updated>2026-11-20T01:00:00Z</updated>
      <content>&lt;p&gt;Fix &lt;img src="https://i/a.png"&gt;bug&lt;/p&gt;</content></entry></feed>'''
    rss = b'''<rss><channel><item><title>Show GN: AI tool</title><link>https://y/2</link>
      <pubDate>Fri, 20 Nov 2026 10:00:00 GMT</pubDate><description>hello</description></item></channel></rss>'''
    global fetch
    real_fetch, fetch = fetch, lambda url, headers=None: atom if 'atom' in url else rss
    try:
        a = from_rss(dict(src, url='atom'))[0]
        r = from_rss(dict(src, url='rss'))[0]
    finally:
        fetch = real_fetch
    assert a['url'] == 'https://x/1' and a['summary'] == 'Fix bug' and a['image'] == 'https://i/a.png'
    assert a['date'].startswith('2026-11-20T10:00') and r['date'].startswith('2026-11-20T19:00')

    ruled = apply_rules({'filter': 'ai', 'project_prefix': 'Show GN'}, [r, dict(a, title='no match')])
    assert len(ruled) == 1 and ruled[0]['cat'] == '프로젝트'

    old = make_item(src, 'old', 'https://o', '2020-01-01T00:00+09:00')
    merged, new = merge([old], [old, a], first_run=False)
    assert len(merged) == 2 and [i['id'] for i in new] == ['https://x/1']
    assert merge([], [old], first_run=True)[1] == []  # 첫 실행엔 오래된 글 알림 안 함

    text = discord_text([a] * 3, 'https://site')
    assert '새 소식 3건' in text and text.endswith('<https://site>')

    sources = json.loads(SOURCES.read_text(encoding='utf-8'))
    assert all(s['type'] in COLLECTORS for s in sources), 'sources.json 에 모르는 type'
    print('selftest OK')


if __name__ == '__main__':
    selftest() if '--selftest' in sys.argv else main(dry='--dry' in sys.argv)
