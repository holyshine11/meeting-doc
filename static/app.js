const POLL_MS = 1500;
const RATE = 16000;  // 서버가 받는 샘플레이트. 다르면 서버가 연결을 거절한다.
const $ = (id) => document.getElementById(id);
let selected = null;
// 회의 id는 폴더 이름이라 한글과 공백이 들어간다. 주소에 넣을 때는 항상 이 함수를 거친다.
const meetingUrl = (id) => `/api/meetings/${encodeURIComponent(id)}`;

async function api(path, options) {
  const res = await fetch(path, options);
  const body = await res.json().catch(() => ({}));
  // 입력 검사에 걸리면(422) detail이 오류 목록으로 온다. 사람이 읽을 문구로 바꾼다.
  const detail = Array.isArray(body.detail) ? '입력한 내용이 너무 길거나 형식이 맞지 않습니다. 고쳐서 다시 저장해 주세요.' : body.detail;
  if (!res.ok) throw Object.assign(new Error(detail || `요청 실패 (${res.status})`), { status: res.status });
  return body;
}

function notice(text) { $('notice').textContent = text; }

function clock(sec) {
  const s = Math.floor(sec);
  return [Math.floor(s / 3600), Math.floor(s / 60) % 60, s % 60].map((n) => String(n).padStart(2, '0')).join(':');
}

// 카드에 쓰는 사람 말투의 날짜와 길이: '2026년 11월 11일 (수)', '58분'
const DAY = new Intl.DateTimeFormat('ko', { year: 'numeric', month: 'long', day: 'numeric', weekday: 'short' });
function span(sec) {
  const m = Math.floor(sec / 60);
  return m >= 60 ? `${Math.floor(m / 60)}시간 ${m % 60}분` : m ? `${m}분` : `${Math.floor(sec)}초`;
}

function bytes(n) {
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return `${i ? n.toFixed(1) : n} ${units[i]}`;
}

function el(tag, text, props) {
  const node = Object.assign(document.createElement(tag), props);
  if (text !== undefined) node.textContent = text;  // 전사문, 제목, 이름은 신뢰하지 않는 입력이다. textContent로만 넣는다.
  return node;
}

function seek(sec) {
  const player = $('player');
  if (player.hidden) return;
  player.currentTime = sec;
  player.play();
}

// 글 속의 (시:분)은 화면에서 떼어 내고, 그 글을 누르면 그 시각부터 들리게 한다. 회의록 항목의 근거를 바로 들어 볼 수 있다.
const TIMES = /\s*\((\d{1,2}):(\d\d)(?:[,\s]+\d{1,2}:\d\d)*\)/;
function playAt(node, hour, minute) {
  node.classList.add('playable');
  node.onclick = (e) => { if (!e.target.closest('.edit, input, summary')) seek((Number(hour) * 60 + Number(minute)) * 60); };
}

// 회의록에 쓰는 Markdown만 그린다: 제목(#), 항목(-, *, 1.), 표(|), 문단. 모델이 쓴 글이라 textContent로만 넣는다.
// 큰 장(##)은 접고 펼 수 있게 묶는다. base를 주면 항목, 안건 제목, 표의 행에 '수정' 버튼이 붙는다.
// 굵은 글씨 표시는 떼어 내고 링크는 글자 그대로 보인다. 필요해지면 그때 늘린다.
const folded = new Set();  // 접어 둔 장의 제목. 회의록을 다시 그려도 접힌 채로 둔다
function markdown(text, base) {
  const nodes = [];
  let box = nodes, body = false;  // box: 지금 글을 담는 곳(맨 위 또는 장), body: 첫 장이 시작됐는가
  const last = () => (box === nodes ? nodes.at(-1) : box.lastElementChild);
  const add = (node) => (box === nodes ? nodes.push(node) : box.append(node));
  text.split('\n').forEach((raw, n) => {
    const line = raw.replaceAll('**', '').trimEnd();
    const head = line.match(/^(#{1,4}) +(.*)/);
    const item = line.match(/^(\s*(?:[-*]|\d+\.) +)(.*)/);
    // 고친 글 앞뒤에 줄 머리(- , ### , |)와 숨겨 둔 시각을 다시 붙여 그 줄만 바꾼다.
    const fix = (node, compose) => base && body && el('button', '수정', { type: 'button', className: 'edit', onclick: () => editRow(node, 'minutes', (next) => send('PATCH', `${base}/minutes/${n}`, { old: raw, text: compose(next) })) });
    const timed = (node, lead, rest) => {
      const times = rest.match(new RegExp(TIMES, 'g'))?.join('') || '';
      const t = rest.match(TIMES);
      node.append(el('span', rest.replace(new RegExp(TIMES, 'g'), '')), fix(node, ([next]) => lead + next + times) || '');
      if (t) playAt(node, t[1], t[2]);
      return node;
    };
    if (line.trimStart().startsWith('|')) {
      const cells = line.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
      if (cells.every((c) => /^[-: ]*$/.test(c))) return;  // |---| 구분 줄
      const first = last()?.tagName !== 'TABLE';
      if (first) add(el('table'));
      const table = last(), tr = el('tr');
      // '시각' 칸은 보이지 않게 빼고, 그 줄을 누르면 그 시각부터 들리게 한다.
      if (first) table.timeCol = cells.indexOf('시각');
      const time = table.timeCol >= 0 ? cells.splice(table.timeCol, 1)[0] : undefined;
      const t = time?.match(/^(\d{1,2}):(\d\d)$/);
      if (t) playAt(tr, t[1], t[2]);
      tr.append(el(first ? 'th' : 'td', first ? 'No' : String(table.rows.length)));  // 맨 앞에 번호 칸을 붙인다
      tr.append(...cells.map((c) => { const cell = el(first ? 'th' : 'td'); cell.append(el('span', c)); return cell; }));
      // 고친 칸의 '|'는 칸을 나누는 글자라 빗금으로 바꾸고, 빼 둔 시각 칸을 제자리에 다시 끼운다.
      const edit = !first && fix(tr, (next) => {
        const out = next.map((c) => c.replaceAll('|', '/'));
        if (time !== undefined) out.splice(table.timeCol, 0, time);
        return `| ${out.join(' | ')} |`;
      });
      if (base) { const cell = el(first ? 'th' : 'td'); cell.append(edit || ''); tr.append(cell); }
      table.append(tr);
    } else if (head && head[1].length === 2) {
      const section = el('details', undefined, { open: !folded.has(head[2]) });
      section.ontoggle = () => folded[section.open ? 'delete' : 'add'](head[2]);
      const summary = el('summary');
      summary.append(el('h4', head[2]));
      section.append(summary);
      nodes.push(section);
      box = section;
      body = true;
    } else if (head) add(head[1].length > 2 ? timed(el(`h${head[1].length + 2}`), `${head[1]} `, head[2]) : el('h3', head[2]));
    else if (item) {
      if (last()?.tagName !== 'UL') add(el('ul'));
      const li = timed(el('li'), item[1], item[2]);
      if (item[1].length - item[1].trimStart().length >= 2) li.classList.add('sub');
      last().append(li);
    } else if (line.trim()) add(el('p', line));
  });
  return nodes;
}

async function send(method, path, body) {
  try {
    await api(path, { method, headers: { 'Content-Type': 'application/json' }, body: body && JSON.stringify(body) });
    notice('');
  } catch (e) { notice(e.message); }
  refresh();
}

function stateText(m) {
  if (m.status === 'recording') return '녹음 중';
  if (m.status === 'processing') return `${m.stage || '준비 중'} ${Math.round(m.progress * 100)}%`;
  if (m.status === 'error') return `오류 : ${m.error}`;
  if (!m.duration && !m.audio) return '녹음 전';
  return `완료 (${clock(m.duration || 0)})${m.denoise_error ? `. ${m.denoise_error}` : ''}`;
}

// 카드의 상태 이름표. 첫 값은 색을 정하는 이름(style.css의 data-state), 둘째는 보이는 글자다.
function badge(m) {
  if (m.status === 'recording') return ['recording', '녹음 중'];
  if (m.status === 'processing') return ['processing', `처리 중 ${Math.round(m.progress * 100)}%`];
  if (m.status === 'error') return ['error', '오류'];
  if (!m.duration && !m.audio) return ['empty', '녹음 전'];
  return ['done', '완료'];
}

// 목록 조건. 기간이 비어 있으면 서버가 기본 기간(오늘부터 석 달 전까지)을 채워 돌려준다.
const filter = { q: '', start: '', end: '', pages: 1 };

async function refreshList() {
  if (selected) return;  // 상세 화면에서는 목록이 보이지 않는다
  const asked = new URLSearchParams(Object.entries(filter).filter(([, v]) => v)).toString();
  const page = await api(`/api/meetings?${asked}`);
  if (filter.q !== (new URLSearchParams(asked).get('q') || '')) return;  // 기다리는 사이 검색어가 바뀌었다
  filter.start = page.start;
  filter.end = page.end;
  $('period').textContent = `${page.start} ~ ${page.end}`;
  $('total').textContent = `${page.total}건`;  // 제목 '회의' 옆에 붙는다
  $('empty').hidden = page.total > 0;
  $('more').hidden = page.items.length >= page.total;
  if (!changed('list', page.items)) return;
  $('list').replaceChildren(...page.items.map((m) => {
    const li = document.createElement('li');
    const button = document.createElement('button');
    const [state, label] = badge(m);
    button.type = 'button';
    const detail = m.status === 'error' ? m.error : m.status === 'processing' ? m.stage : m.duration ? span(m.duration) : '';
    const foot = el('small');
    foot.append(el('span', DAY.format(new Date(`${m.created.slice(0, 10)}T00:00`)).replace(/ (\S+)$/, ' ($1)')), el('span', detail || ''));
    button.append(el('span', label, { className: 'badge' }), el('strong', m.title), foot);
    button.querySelector('.badge').dataset.state = state;
    button.onclick = () => { selected = m.id; notice(''); refresh(); };
    li.append(button);
    return li;
  }));
}

function search() { filter.pages = 1; refresh(); }
$('filter').onsubmit = (e) => e.preventDefault();
$('q').oninput = () => { filter.q = $('q').value.trim(); search(); };
$('more').onclick = () => { filter.pages += 1; refresh(); };

// 기간 달력. 날짜는 'YYYY-MM-DD' 글자로 다뤄 그대로 크기를 견준다. 첫 번째 누른 날과 두 번째 누른 날 사이가 기간이 된다.
const cal = { month: null, first: null };
const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
function drawCal() {
  const y = cal.month.getFullYear(), mo = cal.month.getMonth(), today = iso(new Date());
  const [from, to] = cal.first ? [cal.first, cal.first] : [filter.start, filter.end];
  $('cal-title').textContent = `${y}년 ${mo + 1}월`;
  $('cal-hint').textContent = cal.first ? '끝나는 날을 눌러 주세요.' : '시작하는 날을 눌러 주세요.';
  const cells = Array.from({ length: new Date(y, mo, 1).getDay() }, () => el('span'));
  for (let d = 1; d <= new Date(y, mo + 1, 0).getDate(); d += 1) {
    const day = iso(new Date(y, mo, d));
    const b = el('button', d, { type: 'button' });
    if (day >= from && day <= to) b.classList.add('in');
    if (day === from) b.classList.add('from');
    if (day === to) b.classList.add('to');
    if (day === today) b.setAttribute('aria-current', 'date');
    b.onclick = () => {
      if (!cal.first) { cal.first = day; drawCal(); return; }
      [filter.start, filter.end] = [cal.first, day].sort();
      openCal(false);
      search();
    };
    cells.push(b);
  }
  $('cal-days').replaceChildren(...cells);
}
function openCal(open) {
  $('cal').hidden = !open;
  $('period').setAttribute('aria-expanded', open);
  cal.first = null;
  if (!open) return;
  cal.month = new Date(`${filter.end || iso(new Date())}T00:00`);
  cal.month.setDate(1);
  drawCal();
}
function turnCal(step) { cal.month.setMonth(cal.month.getMonth() + step); drawCal(); }
$('period').onclick = () => openCal($('cal').hidden);
$('cal-prev').onclick = () => turnCal(-1);
$('cal-next').onclick = () => turnCal(1);
// 달력 밖을 누르거나 Esc를 누르면 닫는다. 날짜 칸은 누르는 순간 다시 그려져 문서에서 빠지므로 composedPath로 본다.
document.addEventListener('click', (e) => { if (!$('cal').hidden && !e.composedPath().includes($('period-box'))) openCal(false); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('cal').hidden) { openCal(false); $('period').focus(); } });

// 부분마다 마지막으로 그린 내용을 기억한다. 같으면 다시 그리지 않아 재생, 글자 선택, 스크롤이 끊기지 않는다.
let drawn = {};
function changed(part, value) {
  const key = JSON.stringify(value);
  if (drawn[part] === key) return false;
  drawn[part] = key;
  return true;
}

// 탭 묶음 하나를 만든다. 이름마다 tab-이름 버튼과 panel-이름 칸이 있다. 고르는 함수를 돌려준다.
function tabGroup(names) {
  const show = (name) => {
    for (const t of names) {
      $(`tab-${t}`).setAttribute('aria-selected', t === name);
      $(`tab-${t}`).tabIndex = t === name ? 0 : -1;
      $(`panel-${t}`).hidden = t !== name;
    }
  };
  names.forEach((t, i) => {
    $(`tab-${t}`).onclick = () => show(t);
    $(`tab-${t}`).onkeydown = (e) => {
      const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
      if (!step) return;
      const next = names[(i + step + names.length) % names.length];
      show(next);
      $(`tab-${next}`).focus();
    };
  });
  return show;
}
const showTab = tabGroup(['minutes', 'transcript', 'speakers', 'files']);
const showView = tabGroup(['original', 'summary']);  // 회의 내용 탭 안의 원본과 요약본

function renameSpeaker(base, number, name) {
  const next = prompt(`"${name}"의 이름`, name);
  if (next && next.trim()) send('PATCH', base, { speakers: { [number]: next.trim() } });
}

async function refreshDetail() {
  $('detail').hidden = !selected;
  document.querySelector('nav').hidden = Boolean(selected);  // 목록 화면과 상세 화면을 번갈아 보여 준다
  if (!selected) { drawn = {}; $('player').pause(); return; }  // 목록으로 나가면 듣던 소리도 멈춘다
  // 보던 회의가 지워졌으면 목록으로 돌아간다.
  const m = await api(meetingUrl(selected)).catch((e) => { if (e.status === 404) selected = null; throw e; });
  if (m.id !== selected) return;  // 기다리는 사이 다른 회의를 골랐다. 늦게 온 답으로 화면을 덮지 않는다.
  const base = meetingUrl(m.id);
  const busy = m.status === 'processing' || m.status === 'recording';
  const names = m.speakers || {};
  const nameOf = (n) => names[n] || `참석자 ${n}`;
  // 회의를 새로 골랐을 때만 탭을 정한다. 회의록이 있으면 회의록부터 보여 준다.
  if (drawn.meeting !== JSON.stringify(m.id)) {
    // 버튼마다 회의 주소를 쥐고 있으므로, 회의가 바뀌면 내용이 같아 보여도 전부 다시 그린다.
    drawn = { list: drawn.list, meeting: JSON.stringify(m.id) };
    folded.clear();
    showTab(m.minutes ? 'minutes' : 'transcript');
    showView('original');
    $('info-box').open = !m.transcript.length;  // 갓 만든 회의는 정보부터 적게 펼쳐 둔다
  }
  if (changed('count', m.count)) $('count').value = m.count || '';

  if (changed('head', [m.title, m.status, m.stage, m.progress, m.error, m.denoise_error, m.audio, m.duration, m.transcript.length > 0, !!recording])) {
    $('title').textContent = m.title;
    // 녹음 버튼: 이 회의를 녹음하는 중이면 끝내기, 아니면 시작. 다른 회의를 녹음하는 중에는 누를 수 없다.
    const mine = recording?.id === m.id;
    $('rec').hidden = busy && !mine;
    $('rec').disabled = !!recording && !mine;
    $('rec').classList.toggle('live', mine);
    if (!mine) $('rec').textContent = m.audio ? '이어서 녹음' : '녹음 시작';
    $('state').textContent = stateText(m);
    $('state').className = m.status === 'error' ? 'warn' : '';
    $('working').hidden = m.status !== 'processing';
    if (m.status !== 'processing') { $('stop').disabled = false; $('stop').textContent = '중지'; }
    $('progress').value = m.progress;
    for (const id of ['save', 'delete']) $(id).disabled = busy;
    $('minutes').disabled = $('summarize').disabled = m.status !== 'done' || !m.transcript.length;
    $('redo').disabled = busy || !m.audio;
    $('reprocess').disabled = busy || !m.audio;
  }

  const player = $('player');
  $('clean-box').hidden = !(m.audio && m.clean);
  const src = m.audio ? `${base}/files/${m.clean && $('clean').checked ? 'audio-clean.ogg' : encodeURIComponent(m.audio)}` : '';
  player.hidden = !src;
  if (player.dataset.src !== src) {
    // 원래 소리와 소음 줄인 소리를 바꿔 들어도 듣던 자리에서 이어진다.
    const at = player.currentTime, playing = !player.paused;
    player.dataset.src = src;
    if (src) { player.src = src; if (at) player.currentTime = at; if (playing) player.play(); } else player.removeAttribute('src');
  }

  // 회의 정보 칸은 서버 값이 바뀌었을 때만 채운다. 쓰는 중인 글자를 새로 고침이 덮지 않는다.
  if (changed('info', [m.title, m.created, m.place, m.attendees, names])) {
    const groups = groupsOf(m.attendees);
    info = { title: m.title, created: m.created.slice(0, 16), place: m.place || '', attendees: JSON.stringify(groups) };
    for (const k of INFO_FIELDS) $(`f-${k}`).value = info[k];
    // 비어 있으면 빈 줄 하나를 두고, 참석자 탭에서 붙인 이름을 보기로 보여 준다.
    const hint = Object.values(names).sort().join(', ') || '예 : 김하나, 이두리';
    $('companies').replaceChildren(...(groups.length ? groups : [{ name: '', people: [] }]).map((g) => companyRow(g, hint)));
  }

  if (changed('minutes', [m.minutes, m.minutes_error, m.minutes_stale, src, busy])) {
    $('minutes').textContent = m.minutes ? '회의록 다시 작성' : '회의록 작성';
    $('minutes').className = m.minutes && !m.minutes_stale ? '' : 'primary';
    $('docx').hidden = !m.minutes;
    $('docx').href = `${base}/minutes.docx`;
    $('docx').className = m.minutes && !m.minutes_stale ? 'button primary' : 'button';
    $('minutes-stale').hidden = !m.minutes_stale;
    $('minutes-error').textContent = m.minutes_error || '';
    $('minutes-empty').hidden = !!m.minutes;
    $('minutes-text').replaceChildren(...markdown(m.minutes || '', !busy && base));
  }

  if (changed('summary', [m.summary, m.summary_error, m.summary_stale, names, src])) {
    $('summarize').textContent = m.summary ? '다시 요약' : '회의 내용 요약';
    $('summarize').className = m.summary && !m.summary_stale ? '' : 'primary';
    $('summary-stale').hidden = !m.summary_stale;
    $('summary-error').textContent = m.summary_error || '';
    $('summary-empty').hidden = !!m.summary;
    $('summary-text').replaceChildren(...summaryRows(m.summary || '', names));
  }

  if (changed('files', [m.folder, m.files, m.audio, busy])) {
    $('path').textContent = m.folder;
    $('files').replaceChildren(...m.files.map((f) => {
      const li = el('li');
      li.append(el('a', f.name, { href: `${base}/files/${encodeURIComponent(f.name)}`, download: f.name }));
      if (f.name === m.audio && !busy) {
        li.append(el('button', '녹음만 삭제', { type: 'button', className: 'small', onclick: () => {
          if (confirm('녹음 파일만 지울까요? 회의 내용은 남지만 다시 듣기와 화자 다시 구분은 할 수 없게 됩니다.')) send('DELETE', `${base}/files/${encodeURIComponent(f.name)}`);
        } }));
      }
      li.append(el('small', bytes(f.size)));  // 크기는 줄 끝에 맞춘다
      return li;
    }));
  }

  if (!changed('transcript', [m.transcript, names, busy, src])) return;

  // 참석자 탭: 사람마다 말한 시간, 들어 보기, 이름, 합치기
  const stats = new Map();
  for (const s of m.transcript) {
    if (!s.speaker) continue;
    const st = stats.get(s.speaker) || stats.set(s.speaker, { sec: 0, longest: s }).get(s.speaker);
    st.sec += s.end - s.start;
    if (s.end - s.start > st.longest.end - st.longest.start) st.longest = s;
  }
  const people = [...stats].sort((a, b) => b[1].sec - a[1].sec);
  $('tab-speakers').textContent = `참석자 ${people.length || ''}`.trim();
  $('speakers').replaceChildren(...people.map(([n, st]) => {
    const li = el('li');
    const who = el('button', nameOf(n), { type: 'button', className: 'speaker', disabled: busy, title: '이름 바꾸기', onclick: () => renameSpeaker(base, n, nameOf(n)) });
    who.style.setProperty('--n', n);
    const merge = el('select', undefined, { disabled: busy || people.length < 2, ariaLabel: `${nameOf(n)} 합치기` });
    merge.append(el('option', '같은 사람 합치기', { value: '' }), ...people.filter(([o]) => o !== n).map(([o]) => el('option', `${nameOf(o)}에게 합치기`, { value: o })));
    merge.onchange = () => {
      const to = Number(merge.value);
      if (to && confirm(`"${nameOf(n)}"의 발언을 모두 "${nameOf(to)}"의 발언으로 합칠까요? 되돌리려면 화자를 다시 구분해야 합니다.`)) send('PATCH', base, { merge: { source: n, target: to } });
      else merge.value = '';
    };
    li.append(who, el('small', `${clock(st.sec)} 발언`), el('button', '들어 보기', { type: 'button', className: 'small', disabled: !src, onclick: () => seek(st.longest.start) }), merge);
    return li;
  }));

  // 회의 내용 탭: 같은 사람이 이어서 말한 줄은 한 묶음으로 보여 준다.
  const groups = [];
  m.transcript.forEach((s, index) => {
    if (!groups.length || groups.at(-1).speaker !== s.speaker) groups.push({ speaker: s.speaker, rows: [] });
    groups.at(-1).rows.push({ ...s, index });
  });
  $('transcript').replaceChildren(...groups.map((g) => {
    const li = el('li');
    const head = el('div', undefined, { className: 'who' });
    if (g.speaker) {
      const who = el('button', nameOf(g.speaker), { type: 'button', className: 'speaker', disabled: busy, title: '이름 바꾸기', onclick: () => renameSpeaker(base, g.speaker, nameOf(g.speaker)) });
      who.style.setProperty('--n', g.speaker);
      head.append(who);
    } else head.append(el('span', '화자 미확인', { className: 'hint' }));
    head.append(el('span', clock(g.rows[0].start), { className: 'time' }));
    const body = el('div');
    li.append(head, body);
    body.append(...g.rows.map((s) => {
      // 수정은 묶음 번호가 아니라 원래 줄 번호(s.index)로 보낸다.
      const row = el('div', undefined, { className: src ? 'row playable' : 'row', title: clock(s.start) });
      row.append(el('span', s.text));
      if (!busy) row.append(el('button', '수정', { type: 'button', className: 'edit', onclick: () => editRow(row, 'transcript', ([text]) => send('PATCH', `${base}/rows/${s.index}`, { text })) }));
      if (src) row.onclick = (e) => { if (!e.target.closest('.edit, input')) seek(s.start); };
      return row;
    }));
    return li;
  }));
}

// 회의 내용 요약을 원본과 같은 꼴(왼쪽 말한 사람과 시각, 오른쪽 내용)로 그린다. 항목은 '- 이름 : 내용 (시:분)'이다.
const SUMMARY_LINE = /^- (?:(.+?) : )?(.*?)\s*\((\d{1,2}):(\d\d)\)\s*$/;
function summaryRows(text, names) {
  return text.split('\n').filter((line) => line.startsWith('- ')).map((line) => {
    const [, who, said, hour, minute] = line.match(SUMMARY_LINE) || [null, '', line.slice(2)];
    const li = el('li');
    const head = el('div', undefined, { className: 'who' });
    // 이름표 점 색은 원본과 같게 화자 번호로 정한다. 이름을 붙이지 않은 화자는 '참석자 N'의 N이 번호다.
    const n = who && (Object.keys(names).find((k) => names[k] === who) || who.match(/^참석자 (\d+)$/)?.[1]);
    if (n) { const tag = el('span', who, { className: 'speaker' }); tag.style.setProperty('--n', n); head.append(tag); } else head.append(el('span', who || '화자 미확인', { className: 'hint' }));
    if (hour) head.append(el('span', clock((Number(hour) * 60 + Number(minute)) * 60), { className: 'time' }));
    const row = el('div', said, { className: 'row' });
    if (hour) playAt(row, hour, minute);
    li.append(head, row);
    return li;
  });
}

async function refresh() {
  try { await refreshList(); await refreshDetail(); } catch (e) { notice(e.message); }
}

$('back').onclick = $('home').onclick = () => { selected = null; notice(''); refresh(); };

$('upload').onclick = () => $('file').click();
$('file').onchange = async (e) => {
  const file = e.target.files[0];
  e.target.value = '';
  if (!file) return;
  const form = new FormData();
  form.append('file', file);
  notice('올리는 중...');
  try {
    selected = (await api('/api/meetings', { method: 'POST', body: form })).id;
    notice('');
  } catch (err) { notice(err.message); }
  refresh();
};

// 줄 안의 글(span)을 입력칸으로 바꿔 고치게 한다. Enter나 저장으로 저장하고 Esc나 취소로 되돌린다.
// part는 다시 그릴 부분(transcript, minutes), save는 고친 글들을 받아 서버로 보낸다.
function editRow(row, part, save) {
  const spans = [...row.querySelectorAll('span')];
  const texts = spans.map((s) => s.textContent);
  const inputs = texts.map((text) => el('input', undefined, { type: 'text', value: text, maxLength: 2000, ariaLabel: '고칠 내용' }));
  const close = () => { delete drawn[part]; refresh(); };
  const done = () => {
    const next = inputs.map((i) => i.value.trim());
    if (next.every(Boolean) && next.join('\n') !== texts.join('\n')) save(next);
    close();
  };
  for (const input of inputs) input.onkeydown = (e) => {
    if (e.isComposing) return;  // 한글을 조합하는 중의 Enter는 저장으로 치지 않는다
    if (e.key === 'Enter') done();
    if (e.key === 'Escape') close();
  };
  spans.forEach((s, i) => s.replaceWith(inputs[i]));
  row.classList.add('editing');
  row.querySelector('.edit').replaceWith(
    el('button', '저장', { type: 'button', className: 'edit', onclick: done }),
    el('button', '취소', { type: 'button', className: 'edit', onclick: close }),
  );
  inputs[0].focus();
}

let recording = null;
const SILENT_PEAK = 0.002;  // 이보다 작은 소리만 들어오면 마이크가 막힌 것으로 본다
const SILENT_MS = 5000;
const MIC_SILENT = '마이크 소리가 들어오지 않습니다. Windows는 설정 > 개인 정보 > 마이크에서 데스크톱 앱의 마이크 사용을 켜 주세요. 주소창 왼쪽 사이트 설정에서 쓰는 마이크가 맞는지도 확인하세요.';

// meeting 회의에 녹음한다. 녹음이 이미 있으면 뒤에 이어 붙는다.
async function startRecording(meeting, resumed) {
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  } catch (e) {
    notice('마이크를 쓸 수 없습니다. 주소창 왼쪽의 사이트 설정에서 마이크를 허용해 주세요.');
    return;
  }
  const stop = () => {
    stream.getTracks().forEach((t) => t.stop());
    if (recording) { clearInterval(recording.timer); recording.ws.close(); recording.context.close(); recording = null; }
    delete drawn.head;
    refresh();
  };
  try {
    // 브라우저가 16kHz로 바로 녹음해 준다고 가정한다. 안 되는 브라우저가 나오면 서버에서 리샘플한다.
    const context = new AudioContext({ sampleRate: RATE });
    await context.audioWorklet.addModule('recorder.js');
    const ws = new WebSocket(`ws://${location.host}/api/record?rate=${context.sampleRate}&meeting=${encodeURIComponent(meeting)}`);
    ws.binaryType = 'arraybuffer';
    const rec = { ws, context, started: Date.now(), id: meeting, peak: 0, silent: false };
    recording = rec;
    ws.onmessage = refresh;
    ws.onclose = () => { if (recording && !recording.stopping) notice('녹음 연결이 끊겼습니다. 그때까지의 녹음은 저장됩니다.'); stop(); };
    await new Promise((ok, fail) => { ws.onopen = ok; ws.onerror = () => fail(new Error('녹음을 시작하지 못했습니다. 이 브라우저가 맞지 않으면 Chrome이나 Edge로 열어 주세요.')); });
    const node = new AudioWorkletNode(context, 'recorder');
    node.port.onmessage = (e) => {
      if (rec.peak < SILENT_PEAK) rec.peak = new Float32Array(e.data).reduce((a, v) => Math.max(a, Math.abs(v)), rec.peak);
      if (ws.readyState === WebSocket.OPEN) ws.send(e.data);
    };
    context.createMediaStreamSource(stream).connect(node);
    const started = resumed ? '이어서 녹음 중입니다. 끝내면 앞 녹음에 붙여 처음부터 다시 받아씁니다.' : '녹음 중입니다. 끝내면 회의 내용을 글로 옮기고 화자를 구분합니다.';
    // 마이크가 막혀 있어도 브라우저는 오류 없이 무음을 넘긴다(Windows 개인 정보 설정이 대표적). 소리가 안 들어오면 바로 알린다.
    const tick = () => {
      if (selected === rec.id) $('rec').textContent = `녹음 끝내기 ${clock((Date.now() - rec.started) / 1000)}`;
      const silent = rec.peak < SILENT_PEAK && Date.now() - rec.started > SILENT_MS;
      if (silent !== rec.silent) { rec.silent = silent; notice(silent ? MIC_SILENT : started); }
    };
    delete drawn.head;
    tick();
    recording.timer = setInterval(tick, 1000);
    notice(started);
  } catch (e) {
    notice(e.message);
    stop();
  }
}

$('reprocess').onclick = () => {
  if (confirm('남아 있는 녹음으로 처음부터 다시 받아씁니다. 손으로 고친 문장과 참석자 이름이 지워집니다. 계속할까요?')) send('POST', `${meetingUrl(selected)}/process`);
};

$('new').onclick = async () => {
  // 제목은 임시로 붙이고, 상세 화면의 회의 정보에서 고친다. 녹음은 거기서 빨간 버튼으로 시작한다.
  const title = '회의 제목을 입력하세요.';
  try {
    selected = (await api('/api/new', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title }) })).id;
    notice('');
  } catch (e) { notice(e.message); }
  refresh();
};

$('rec').onclick = () => {
  if (!recording) { startRecording(selected, $('rec').textContent === '이어서 녹음'); return; }
  notice('');
  recording.stopping = true;
  recording.ws.send('stop');  // 서버가 저장을 마치고 연결을 닫으면 onclose에서 정리한다
};

// 녹음 중에 탭을 닫으려 하면 한 번 묻는다. 닫아도 그때까지의 소리는 서버에 남는다.
window.addEventListener('beforeunload', (e) => { if (recording) e.preventDefault(); });

// 회의록을 쓸 로컬 모델. 고른 값은 이 브라우저에 기억한다.
const loadModels = () => api('/api/models').then((list) => {
  $('engine').replaceChildren(...list.map((name) => el('option', name)));
  try { const last = localStorage.getItem('model'); if (list.includes(last)) $('engine').value = last; } catch { /* 기억만 못 한다 */ }
}, (e) => notice(e.message));
loadModels();

// 첫 화면 준비 안내: Ollama, 회의록 모델, 음성 인식 모델. 다 갖춰질 때까지 몇 초마다 다시 본다.
const SETUP_MS = 3000;
let setupTimer = null;
function step(id, state, say, job) {  // state: 'ok' 끝남, 'wait' 기다림, '' 지금 할 일
  const li = $(id);
  li.className = state;
  li.querySelector('.say').textContent = say;
  const bar = li.querySelector('progress');
  if (!bar) return;
  bar.hidden = !job || Boolean(job.error);
  if (job?.total) { bar.max = job.total; bar.value = job.done; } else bar.removeAttribute('value');  // 크기를 모르면 흐르는 막대
}
async function checkSetup() {
  clearTimeout(setupTimer);
  let s;
  try { s = await api('/api/setup'); } catch (e) { notice(e.message); return; }
  const wasOpen = !$('setup').hidden;
  const keep = '터미널 창은 닫지 마세요. 브라우저는 닫아도 계속 받습니다.';
  $('ollama-link').href = s.download_page;
  step('step-ollama', s.ollama ? 'ok' : '', s.ollama ? '' : '열리는 페이지에서 내 PC(macOS 또는 Windows)에 맞는 것을 받아 설치하고 Ollama를 실행하세요. 켜지면 이 화면이 저절로 다음 단계로 넘어갑니다.');

  const llm = s.jobs[s.model] || s.jobs[s.light_model];
  $('pull-main').textContent = `권장 모델 받기 (${s.model}, 메모리 16GB 이상 PC)`;
  $('pull-light').textContent = `가벼운 모델 받기 (${s.light_model}, 메모리 8GB PC)`;
  $('pull-main').dataset.name = s.model;
  $('pull-light').dataset.name = s.light_model;
  $('pull-whisper').dataset.name = 'whisper';
  if (s.llm) step('step-llm', 'ok', '');
  else if (!s.ollama) step('step-llm', 'wait', '1단계를 마치면 받을 수 있습니다.');
  else if (llm?.error) step('step-llm', '', `${llm.error} 다시 누르면 받던 곳부터 이어 받습니다.`, llm);
  else if (llm) step('step-llm', 'wait', `${llm.total ? `받는 중 ${bytes(llm.done)} / ${bytes(llm.total)}.` : '받기 시작하는 중입니다.'} ${keep}`, llm);
  else step('step-llm', '', '둘 중 PC 메모리에 맞는 것 하나만 받으면 됩니다. 몇 GB라 인터넷 속도에 따라 몇 분에서 수십 분 걸립니다.');

  const w = s.jobs.whisper;
  if (s.whisper) step('step-whisper', 'ok', '');
  else if (w?.error) step('step-whisper', '', `${w.error} 다시 눌러 주세요.`, w);
  else if (w) step('step-whisper', 'wait', `받는 중입니다. 1~2GB라 몇 분 걸립니다. ${keep}`, w);
  else step('step-whisper', '', '녹음을 글로 받아쓰는 모델입니다. 2단계와 함께 받아도 됩니다.');

  const ready = s.ollama && s.llm && s.whisper;
  $('setup').hidden = ready;
  if (ready && wasOpen) { loadModels(); notice('준비가 끝났습니다. 새 회의를 눌러 녹음하거나 녹음 파일을 올리세요.'); }
  if (!ready) setupTimer = setTimeout(checkSetup, SETUP_MS);
}
async function pull(name) {
  try { await api('/api/setup/pull', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) }); notice(''); } catch (e) { notice(e.message); }
  checkSetup();
}
$('setup-check').onclick = checkSetup;
for (const id of ['pull-main', 'pull-light', 'pull-whisper']) $(id).onclick = () => pull($(id).dataset.name);
checkSetup();
$('engine').onchange = () => { try { localStorage.setItem('model', $('engine').value); } catch { /* 기억만 못 한다 */ } };

$('summarize').onclick = () => {
  showView('summary');
  if ($('summarize').textContent === '회의 내용 요약' || confirm('회의 내용을 새로 요약할까요? 몇 분 걸립니다.')) send('POST', `${meetingUrl(selected)}/summary`, { model: $('engine').value || null });
};

$('minutes').onclick = () => {
  if ($('minutes').textContent === '회의록 작성' || confirm('지금 회의록을 새로 쓸까요? 몇 분 걸립니다.')) send('POST', `${meetingUrl(selected)}/minutes`, { model: $('engine').value || null });
};

// 하던 단계(문장 하나, 모델 답 하나)가 끝나야 멈춘다. 그동안은 버튼에 표시해 둔다.
$('stop').onclick = async () => {
  if (!confirm('지금 하던 처리를 중지할까요? 전에 받아쓴 회의 내용과 회의록은 그대로 남습니다.')) return;
  try { await api(`${meetingUrl(selected)}/stop`, { method: 'POST' }); $('stop').disabled = true; $('stop').textContent = '중지하는 중'; } catch (e) { notice(e.message); }
};

$('clean').onchange = refresh;

$('open').onclick =() => send('POST', `${meetingUrl(selected)}/open`);

let info = {};  // 서버에 저장돼 있는 회의 정보. 참석자는 JSON 글자로 둬서 그대로 견준다
const INFO_FIELDS = ['title', 'created', 'place'];

// 참석자 업체 한 줄: 업체명, 쉼표로 나눈 참석자 이름, 삭제
function companyRow(g = { name: '', people: [] }, hint = '예 : 김하나, 이두리') {
  const row = el('div', undefined, { className: 'company' });
  row.append(
    el('input', undefined, { type: 'text', value: g.name, maxLength: 100, placeholder: '업체명', ariaLabel: '업체명' }),
    el('input', undefined, { type: 'text', value: g.people.join(', '), maxLength: 2000, placeholder: hint, ariaLabel: '참석자 이름, 쉼표로 나눔' }),
    el('button', '삭제', { type: 'button', className: 'small', ariaLabel: '이 업체 삭제', onclick: () => row.remove() }),
  );
  return row;
}
// 업체별로 나누기 전에는 참석자를 글 한 줄로 저장했다. 그런 회의는 업체 이름 없는 한 줄로 보여 준다.
const splitNames = (text) => text.split(',').map((s) => s.trim()).filter(Boolean);
const groupsOf = (a) => (typeof a === 'string' ? (a.trim() ? [{ name: '', people: splitNames(a) }] : []) : a || []);
const readCompanies = () => [...$('companies').children]
  .map((row) => { const [name, people] = row.querySelectorAll('input'); return { name: name.value.trim(), people: splitNames(people.value) }; })
  .filter((g) => g.name || g.people.length);
$('add-company').onclick = () => {
  const row = companyRow();
  $('companies').append(row);
  row.querySelector('input').focus();
};

$('info').onsubmit = (e) => {
  e.preventDefault();
  // 바뀐 칸만 보낸다. 제목을 그대로 다시 보내면 회의 내용 파일이 새로 쓰여 회의록이 오래된 것으로 표시된다.
  const body = {};
  for (const k of INFO_FIELDS) { const v = $(`f-${k}`).value.trim(); if (v !== info[k]) body[k] = v; }
  const groups = readCompanies();
  if (JSON.stringify(groups) !== info.attendees) body.attendees = groups;
  if (!Object.keys(body).length) return;
  // 제목을 바꾸면 폴더 이름(id)도 바뀐다. 새 id로 갈아탄다.
  api(meetingUrl(selected), { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    .then((m) => { selected = m.id; notice('저장했습니다.'); }, (err) => notice(err.message))
    .finally(refresh);
};

$('redo').onclick = () => {
  const count = $('count').valueAsNumber;
  send('POST', `${meetingUrl(selected)}/speakers`, { count: Number.isNaN(count) ? null : count });
};

$('delete').onclick = async () => {
  if (!confirm('이 회의의 폴더를 통째로 지웁니다(녹음, 회의 내용, 모든 파일). 되돌릴 수 없습니다. 지울까요?')) return;
  try { await api(meetingUrl(selected), { method: 'DELETE' }); selected = null; } catch (e) { notice(e.message); }
  refresh();
};

refresh();
setInterval(refresh, POLL_MS);

// 스페이스바로 재생/일시정지. 글자를 넣는 칸과 재생바 자체(브라우저가 직접 처리)는 건드리지 않는다.
// 버튼에 초점이 있어도 재생을 우선한다. 버튼은 Enter로 누를 수 있다.
document.addEventListener('keydown', (e) => {
  if (e.code !== 'Space' || e.repeat || e.isComposing || e.ctrlKey || e.metaKey || e.altKey) return;
  const t = e.target, player = $('player');
  if (player.hidden || t.isContentEditable || t.matches('input:not([type=checkbox]), textarea, select, audio')) return;
  e.preventDefault();
  if (player.paused) player.play(); else player.pause();
});

// 다크 모드 스위치. 고른 값은 이 브라우저에만 기억한다.
// 저장해 둔 값이 없으면 시스템 설정을 따른다. 스위치를 한 번 누르면 그 뒤로는 고른 쪽으로 고정된다.
const theme = $('theme');
let saved = null;
try { saved = localStorage.getItem('theme'); } catch { /* 저장소를 못 쓰면 시스템 설정을 따른다 */ }
theme.checked = saved === 'dark' || (saved !== 'light' && matchMedia('(prefers-color-scheme: dark)').matches);
if (saved === 'dark' || saved === 'light') document.documentElement.style.colorScheme = saved;
theme.onchange = () => {
  const mode = theme.checked ? 'dark' : 'light';
  // 바꾸는 순간에는 색 전환 효과를 끈다. 켜 두면 모든 색이 한꺼번에 번지듯 바뀐다.
  document.documentElement.classList.add('no-anim');
  document.documentElement.style.colorScheme = mode;
  requestAnimationFrame(() => requestAnimationFrame(() => document.documentElement.classList.remove('no-anim')));
  try { localStorage.setItem('theme', mode); } catch { /* 기억만 못 한다 */ }
};
