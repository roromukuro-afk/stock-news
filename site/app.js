(() => {
  'use strict';

  // ---- 定数 ----------------------------------------------------------------
  const CATS = [
    ['all', 'すべて'], ['market', '株式・市況'], ['disclosure', '決算・開示'], ['macro', '経済・政策'],
    ['biz', '企業・産業'], ['world', '国際'], ['tech', 'テック'], ['general', '一般'],
  ];
  const CAT_LABEL = Object.fromEntries(CATS);
  const KIND_ORDER = ['earnings', 'guidance', 'dividend', 'buyback', 'ma', 'alliance', 'personnel', 'agm', 'legal', 'product', 'other'];
  const TIER_LABEL = { critical: '最重要', important: '重要' };
  const PAGE = 150;
  const READ_MAX = 6000;
  const SAVED_MAX = 500;
  const CSRF = { 'X-Requested-With': 'stocknews' };

  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));

  // ---- 保存（このブラウザーだけ。使えない環境でも動くように try で包む） ----
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* 保存できなくても表示は続ける */ } },
  };

  const prefs = Object.assign({ cat: 'all', lang: 'all', summary: true, unread: false, view: 'news', stock: null, tierOnly: false, uncertain: true },
    store.get('sn1.prefs', {}));
  const readSet = new Set(store.get('sn1.read', []));
  const saved = new Map(Object.entries(store.get('sn1.saved', {})));

  const S = {
    data: null,         // news.json
    items: [],          // 検索用の印を付けた記事
    live: false,        // サーバーにつながっているか（更新・編集ができるか）
    q: '',
    kind: 'all',        // 銘柄別の画面での種類
    onlyWatch: false,
    onlySaved: false,
    limit: PAGE,
    updating: false,
    pollTimer: null,
    editing: null,      // ウォッチの編集中の銘柄名
  };

  // ---- 小さな道具 ------------------------------------------------------------
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fold = (s) => String(s ?? '').normalize('NFKC').toLowerCase();
  const safeUrl = (u) => (/^https?:\/\//i.test(u) ? u : '#');
  const tz = 'Asia/Tokyo';
  const fmtTime = new Intl.DateTimeFormat('ja-JP', { timeZone: tz, hour: '2-digit', minute: '2-digit', hour12: false });
  const fmtDayKey = new Intl.DateTimeFormat('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit' });
  const fmtDayLabel = new Intl.DateTimeFormat('ja-JP', { timeZone: tz, month: 'numeric', day: 'numeric', weekday: 'short' });
  const fmtDateTime = new Intl.DateTimeFormat('ja-JP', { timeZone: tz, month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false });

  const timeOf = (t) => fmtTime.format(new Date(t * 1000));
  const dayKey = (t) => fmtDayKey.format(new Date(t * 1000));

  function dayLabel(key, t) {
    const todayKey = fmtDayKey.format(new Date());
    const yKey = fmtDayKey.format(new Date(Date.now() - 86400000));
    const base = fmtDayLabel.format(new Date(t * 1000));
    if (key === todayKey) return `今日 ${base}`;
    if (key === yKey) return `昨日 ${base}`;
    return base;
  }

  function ago(ts) {
    const m = Math.floor((Date.now() / 1000 - ts) / 60);
    if (m < 1) return 'たった今';
    if (m < 60) return `${m}分前`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}時間前`;
    return `${Math.floor(h / 24)}日前`;
  }

  const starSvg = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M12 3.5l2.6 5.4 5.9.8-4.3 4.1 1 5.9L12 16.9 6.8 19.7l1-5.9L3.5 9.7l5.9-.8z" stroke-width="1.8" stroke-linejoin="round"/></svg>';

  const kindLabel = (k) => ((S.data && S.data.kinds) || {})[k] || k;
  const watchlist = () => (S.data && S.data.watchlist) || [];
  const stocksOf = (it) => it.stocks || [];

  // ---- 通信 ------------------------------------------------------------------
  async function api(method, path, body) {
    const res = await fetch(path, {
      method,
      headers: Object.assign({}, method === 'POST' ? CSRF : {}, body ? { 'Content-Type': 'application/json' } : {}),
      body: body ? JSON.stringify(body) : undefined,
      cache: 'no-store',
    });
    const text = await res.text();
    let json = null;
    try { json = JSON.parse(text); } catch { /* JSON でない応答 */ }
    if (!res.ok) throw new Error((json && json.error) || `通信に失敗しました（${res.status}）`);
    if (json === null) throw new Error('サーバーの応答を読めません');
    return json;
  }

  async function loadNews() {
    try {
      const res = await fetch('data/news.json?_=' + Date.now(), { cache: 'no-store' });
      if (!res.ok) { S.data = null; S.items = []; return; }
      S.data = await res.json();
    } catch {
      S.data = null; S.items = []; return;
    }
    S.items = S.data.items.map((it) => {
      it._k = fold([it.title, it.summary, it.source, it.company, stocksOf(it).map((s) => s.n).join(' ')].join(' '));
      return it;
    });
  }

  // ---- 絞り込み ---------------------------------------------------------------
  function savedItems() {
    // 保存した記事は、一覧から消えた古いものも控えから出す
    const byId = new Map(S.items.map((i) => [i.id, i]));
    const out = [];
    for (const [id, snap] of saved) out.push(byId.get(id) || Object.assign({ id, more: [], more_count: 0, stocks: [], _k: fold(snap.title + ' ' + snap.source) }, snap));
    return out.sort((a, b) => b.time - a.time);
  }

  // 共通の条件（言語・検索語・未読）で絞る。pool を渡すとその中から選ぶ
  function commonFilter(pool) {
    const q = fold(S.q).split(/\s+/).filter(Boolean);
    return pool.filter((it) => {
      if (prefs.lang !== 'all' && it.lang !== prefs.lang) return false;
      if (prefs.unread && readSet.has(it.id)) return false;
      for (const w of q) if (!it._k.includes(w)) return false;
      return true;
    });
  }

  // ニュース画面: カテゴリ以外の条件で絞った記事（タブの件数もこれで数える）
  function baseFilter() {
    const pool = S.onlySaved ? savedItems() : S.items;
    return commonFilter(pool).filter((it) => !S.onlyWatch || stocksOf(it).length > 0);
  }

  function inCat(it, cat) {
    if (cat === 'all') {
      // 適時開示は件数が多いので、「決算・開示」タブにまとめる（登録銘柄のものだけ「すべて」にも出す）
      return it.cat !== 'disclosure' || stocksOf(it).length > 0 || S.onlySaved;
    }
    return it.cat === cat;
  }

  // 銘柄別画面: 選んだ銘柄の記事
  const stockPool = (name) => S.items.filter((it) => stocksOf(it).some((s) => s.n === name));
  const relOf = (it, name) => (stocksOf(it).find((s) => s.n === name) || {}).r;
  const isImportant = (it) => it.tier === 'critical' || it.tier === 'important';

  function stockBase(name) {
    return commonFilter(stockPool(name)).filter((it) => (prefs.uncertain || relOf(it, name) === 'certain') && (!prefs.tierOnly || isImportant(it)));
  }

  // ---- 描画: 記事 ----------------------------------------------------------------
  function renderItem(it, ctxStock) {
    const read = readSet.has(it.id);
    const isSaved = saved.has(it.id);
    const sts = stocksOf(it);
    const more = it.more || [];
    const moreHtml = it.more_count
      ? `<details class="more"><summary>同じ話題 ほか${it.more_count}件</summary><ul>${more.map((m) =>
          `<li><span class="s">${esc(m.source)}</span><a href="${esc(safeUrl(m.url))}" target="_blank" rel="noopener noreferrer" data-id="${esc(it.id)}">${esc(m.title)}</a></li>`).join('')}</ul></details>`
      : '';
    const company = it.company ? `<span class="co">${esc(it.company)}</span>` : '';
    const stockTags = sts.map((s) =>
      `<span class="wtag">★ ${esc(s.n)}</span>${s.r === 'uncertain' ? '<span class="rel-uncertain" title="社名が見出しに無い・コードだけ・検索で当たっただけの記事">関連不確実</span>' : ''}`).join('');
    const kind = it.kind ? `<span class="kind">${esc(kindLabel(it.kind))}</span>` : '';
    const tier = TIER_LABEL[it.tier] ? `<span class="tier tier-${esc(it.tier)}" title="${esc(it.tier_reason || '')}">${TIER_LABEL[it.tier]}</span>` : '';
    const langTag = it.lang === 'en' ? '<span class="lang-tag">EN</span>' : '';
    const summary = it.summary ? `<p class="sum">${esc(it.summary)}</p>` : '';
    const notNeeded = ctxStock && S.live
      ? `<button class="btn btn-mini" type="button" data-noneed="${esc(it.id)}" data-stock="${esc(ctxStock)}" title="この銘柄では不要な記事なら、除外語を足せます">不要</button>` : '';
    return `<article class="item${read ? ' is-read' : ''}${sts.length ? ' is-watch' : ''}" data-id="${esc(it.id)}">
  <time class="t" datetime="${new Date(it.time * 1000).toISOString()}">${timeOf(it.time)}</time>
  <div>
    <h3 class="title">${company}<a href="${esc(safeUrl(it.url))}" target="_blank" rel="noopener noreferrer" data-id="${esc(it.id)}">${esc(it.title)}</a></h3>
    <p class="meta"><span class="cat c-${esc(it.cat)}">${esc(CAT_LABEL[it.cat] || it.cat)}</span>${kind}${tier}<span>${esc(it.source)}</span>${langTag}${stockTags}${notNeeded}</p>
    ${summary}${moreHtml}
  </div>
  <button class="star" type="button" aria-pressed="${isSaved}" aria-label="${isSaved ? 'あとで読むから外す' : 'あとで読む'}" data-star="${esc(it.id)}">${starSvg}</button>
</article>`;
  }

  function listHtml(items, ctxStock) {
    const shown = items.slice(0, S.limit);
    let html = '';
    let lastDay = '';
    for (const it of shown) {
      const k = dayKey(it.time);
      if (k !== lastDay) { html += `<h2 class="day">${esc(dayLabel(k, it.time))}</h2>`; lastDay = k; }
      html += renderItem(it, ctxStock);
    }
    return html;
  }

  function showMore(total) {
    const more = $('#btn-more');
    more.hidden = total <= S.limit;
    more.textContent = `さらに表示（残り ${total - S.limit} 件）`;
  }

  // ---- 描画: ニュース ------------------------------------------------------------
  function renderTabs(base) {
    const counts = Object.fromEntries(CATS.map(([k]) => [k, 0]));
    for (const it of base) { for (const [k] of CATS) if (inCat(it, k)) counts[k]++; }
    $('#tabs').innerHTML = CATS.map(([k, label]) =>
      `<button class="tab" role="tab" type="button" data-cat="${k}" aria-selected="${prefs.cat === k}">${esc(label)}<span class="n">${counts[k]}</span></button>`
    ).join('');
  }

  function renderNews() {
    const base = baseFilter();
    renderTabs(base);
    const list = $('#list');
    const items = base.filter((it) => inCat(it, prefs.cat));
    const note = $('#note');
    if (!S.data) {
      note.hidden = false; note.classList.remove('is-error');
      note.textContent = S.live ? 'まだニュースがありません。取得しています…（初回は約 10 秒）' : 'ニュースのデータがありません。PC で取得して置き直してください。';
    } else if (prefs.cat === 'all' && !S.onlySaved) {
      const dis = base.filter((it) => it.cat === 'disclosure' && !stocksOf(it).length).length;
      note.hidden = dis === 0;
      note.classList.remove('is-error');
      note.textContent = dis ? `適時開示 ${dis} 件は「決算・開示」タブにまとめています（登録銘柄のものはここにも出ます）。` : '';
    } else {
      note.hidden = true;
    }
    $('#excluded-box').hidden = true;
    if (!items.length) {
      list.innerHTML = `<div class="empty">${S.data ? '条件に合うニュースはありません。' : ''}</div>`;
      $('#btn-more').hidden = true;
      return;
    }
    list.innerHTML = listHtml(items, null);
    showMore(items.length);
  }

  // ---- 描画: 銘柄別 --------------------------------------------------------------
  function stockStats(name) {
    const pool = stockPool(name);
    const certain = pool.filter((it) => relOf(it, name) === 'certain');
    return {
      total: pool.length,
      certain: certain.length,
      unread: pool.filter((it) => !readSet.has(it.id)).length,
      important: pool.filter(isImportant).length,
      latest: pool.length ? Math.max(...pool.map((it) => it.time)) : 0,
    };
  }

  function renderStockGrid() {
    const wl = watchlist();
    const rows = wl.map((w, i) => ({ w, i, st: stockStats(w.name) }))
      .sort((a, b) => (b.st.latest - a.st.latest) || (a.i - b.i));
    $('#stock-grid').innerHTML = rows.length ? rows.map(({ w, st }) => `<button class="stock-card${st.total ? '' : ' is-empty'}" type="button" data-pick="${esc(w.name)}">
  <span class="nm">${esc(w.name)}</span>
  <span class="code">${esc(w.code || '')}</span>
  <span class="nums"><span>${st.total}件</span>${st.unread ? `<span class="pill">未読 ${st.unread}</span>` : ''}${st.important ? `<span class="pill pill-warn">重要 ${st.important}</span>` : ''}</span>
  <span class="latest">${st.latest ? `最新 ${fmtDateTime.format(new Date(st.latest * 1000))}` : '直近 7 日に記事なし'}</span>
</button>`).join('')
      : `<div class="stock-empty">銘柄が登録されていません。「ウォッチ」から追加してください。</div>`;
  }

  function renderStockDetail(name) {
    const w = watchlist().find((x) => x.name === name);
    $('#stock-name').textContent = name;
    $('#stock-code').textContent = w && w.code ? w.code : '';
    const base = stockBase(name);
    // 種類のチップ（件数つき）
    const counts = {};
    for (const it of base) counts[it.kind || 'other'] = (counts[it.kind || 'other'] || 0) + 1;
    const chips = [['all', 'すべて', base.length]].concat(KIND_ORDER.filter((k) => counts[k]).map((k) => [k, kindLabel(k), counts[k]]));
    if (S.kind !== 'all' && !counts[S.kind]) S.kind = 'all';
    $('#kind-chips').innerHTML = chips.map(([k, label, n]) =>
      `<button class="tab" role="tab" type="button" data-kind="${esc(k)}" aria-selected="${S.kind === k}">${esc(label)}<span class="n">${n}</span></button>`).join('');
    const items = base.filter((it) => S.kind === 'all' || (it.kind || 'other') === S.kind);
    const list = $('#list');
    list.innerHTML = items.length ? listHtml(items, name) : `<div class="empty">この条件に合う記事はありません。</div>`;
    showMore(items.length);
    // 除外した記事（確認用）
    const ex = ((S.data && S.data.excluded) || {})[name] || [];
    const box = $('#excluded-box');
    box.hidden = ex.length === 0;
    $('#excluded-sum').textContent = `除外した記事 ${ex.length} 件（確認用）`;
    $('#excluded-list').innerHTML = ex.map((e) => `<li><a href="${esc(safeUrl(e.url))}" target="_blank" rel="noopener noreferrer">${esc(e.title)}</a>
      <span class="meta2">${esc(e.source)} ・ ${esc(fmtDateTime.format(new Date(e.time * 1000)))} ・ ${esc(e.reason)}</span></li>`).join('');
    $('#note').hidden = true;
  }

  function renderStocks() {
    const name = prefs.stock && watchlist().some((w) => w.name === prefs.stock) ? prefs.stock : null;
    if (prefs.stock && !name) { prefs.stock = null; persistPrefs(); }
    $('#stock-grid').hidden = !!name;
    $('#stock-detail').hidden = !name;
    $('#t-tier').setAttribute('aria-pressed', String(prefs.tierOnly));
    $('#t-uncertain').setAttribute('aria-pressed', String(prefs.uncertain));
    if (!name) {
      renderStockGrid();
      $('#list').innerHTML = '';
      $('#btn-more').hidden = true;
      $('#excluded-box').hidden = true;
      $('#note').hidden = true;
      return;
    }
    renderStockDetail(name);
  }

  // ---- 描画: 全体 ----------------------------------------------------------------
  function render() {
    const stocksView = prefs.view === 'stocks';
    $$('#view button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.view === prefs.view)));
    $('#tabs-bar').hidden = stocksView;
    $('#stock-view').hidden = !stocksView;
    $('#t-watch').hidden = stocksView;
    $$('#lang button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.lang === prefs.lang)));
    $('#t-watch').setAttribute('aria-pressed', String(S.onlyWatch));
    $('#t-saved').setAttribute('aria-pressed', String(S.onlySaved));
    $('#t-saved').hidden = stocksView;
    $('#t-unread').setAttribute('aria-pressed', String(prefs.unread));
    $('#t-summary').setAttribute('aria-pressed', String(prefs.summary));
    document.body.classList.toggle('no-summary', !prefs.summary);
    $('#t-saved .n').textContent = saved.size ? `(${saved.size})` : '';
    const wcount = S.items.filter((i) => stocksOf(i).length).length;
    $('#t-watch .n').textContent = wcount ? `(${wcount})` : '';
    if (stocksView) renderStocks(); else renderNews();
  }

  function renderUpdated() {
    const el = $('#updated');
    if (S.updating) return;
    if (!S.data || !S.data.last_refresh) { el.textContent = ''; return; }
    el.textContent = `${fmtDateTime.format(new Date(S.data.last_refresh * 1000))} 更新（${ago(S.data.last_refresh)}）`;
  }

  // ---- 取り直し（サーバーにつながっているとき） -----------------------------------
  function setBusy(busy, label) {
    S.updating = busy;
    const b = $('#btn-refresh');
    b.disabled = busy;
    b.classList.toggle('is-busy', busy);
    $('span', b).textContent = busy ? '取得中' : '更新';
    if (busy) $('#updated').textContent = label || '取得中…';
  }

  async function pollStatus() {
    try {
      const st = await api('GET', '/api/status');
      S.live = true;
      if (st.updating) {
        setBusy(true, st.total ? `取得中 ${st.done}/${st.total}` : '取得中…');
        S.pollTimer = setTimeout(pollStatus, 1200);
        return;
      }
      const wasBusy = S.updating;
      setBusy(false);
      if (st.error) showError('取り直しに失敗しました: ' + st.error);
      if (wasBusy || !S.data || (st.last_refresh && (!S.data.last_refresh || st.last_refresh > S.data.last_refresh))) {
        await loadNews();
        render();
      }
      renderUpdated();
    } catch {
      setBusy(false);
    }
  }

  function showError(msg) {
    const n = $('#note');
    n.hidden = false; n.classList.add('is-error'); n.textContent = msg;
  }

  async function refresh() {
    try {
      await api('POST', '/api/refresh');
      setBusy(true, '取得中…');
      clearTimeout(S.pollTimer);
      S.pollTimer = setTimeout(pollStatus, 600);
    } catch (e) {
      showError(e.message);
    }
  }

  // ---- ウォッチリスト（銘柄） ---------------------------------------------------------
  function resetWatchForm() {
    S.editing = null;
    const f = $('#watch-form');
    f.reset();
    $('#watch-submit').textContent = '追加';
    $('#watch-cancel').hidden = true;
    $('#watch-hint').hidden = true;
  }

  function renderWatch() {
    const wl = watchlist();
    $('#watch-list').innerHTML = wl.length
      ? wl.map((w, i) => `<li><span><span class="nm">${esc(w.name)}</span><span class="sub">${esc([w.code, ...(w.aliases || [])].filter(Boolean).join(' / '))}${(w.exclude || []).length ? ` ・ 除外語 ${w.exclude.length}` : ''}</span></span>
          <span class="acts"><button class="btn" type="button" data-edit="${i}" ${S.live ? '' : 'disabled'}>編集</button><button class="btn" type="button" data-del="${i}" ${S.live ? '' : 'disabled'}>削除</button></span></li>`).join('')
      : '<li class="watch-empty">まだ登録がありません。</li>';
    const form = $('#watch-form');
    $$('input,button', form).forEach((el) => { el.disabled = !S.live; });
    $('#watch-msg').textContent = S.live ? '' : '銘柄の編集は、PC で起動した画面（start.bat）でだけできます。この画面は取得済みのデータの写しです。';
    $('#watch-msg').classList.remove('is-error');
  }

  function openWatch(editName, hint) {
    renderWatch();
    resetWatchForm();
    if (editName) {
      const i = watchlist().findIndex((w) => w.name === editName);
      if (i >= 0) startEdit(i, hint);
    }
    $('#dlg-watch').showModal();
    if (editName && hint) $('#watch-form [name="exclude"]').focus();
  }

  function startEdit(i, hint) {
    const w = watchlist()[i];
    if (!w) return;
    S.editing = w.name;
    const f = $('#watch-form');
    f.name.value = w.name;
    f.code.value = w.code || '';
    f.aliases.value = (w.aliases || []).join(', ');
    f.exclude.value = (w.exclude || []).join(', ');
    $('#watch-submit').textContent = '更新';
    $('#watch-cancel').hidden = false;
    const h = $('#watch-hint');
    h.hidden = !hint;
    h.textContent = hint ? `不要にした記事: ${hint}　→ 同名の別の話題を表す語を「除外語」に足してください。` : '';
  }

  async function saveWatch(list, okMsg, added = false) {
    const msg = $('#watch-msg');
    try {
      const res = await api('POST', '/api/watchlist', { watchlist: list });
      S.data.watchlist = res.watchlist;
      await loadNews();
      render();
      renderWatch();
      msg.classList.remove('is-error');
      msg.textContent = okMsg + (added && res.started ? ' ニュースを集め直しています…' : '');
      if (res.started) { setBusy(true, '取得中…'); clearTimeout(S.pollTimer); S.pollTimer = setTimeout(pollStatus, 800); }
      return true;
    } catch (e) {
      msg.classList.add('is-error');
      msg.textContent = e.message;
      return false;
    }
  }

  // ---- 取得状況 ---------------------------------------------------------------
  function renderSources() {
    const src = (S.data && S.data.sources) || [];
    const bad = src.filter((s) => !s.ok);
    $('#sources-sum').textContent = src.length
      ? `${src.length} 件中 ${src.length - bad.length} 件が最後の取得で成功しました。${bad.length ? '失敗したものを上に出しています。' : ''}`
      : 'まだ取得していません。';
    const rows = src.slice().sort((a, b) => (a.ok - b.ok) || a.name.localeCompare(b.name, 'ja'));
    $('#sources-list').innerHTML = rows.map((s) => `<li>
      <span class="mark ${s.ok ? 'ok' : 'ng'}">${s.ok ? '✓' : '×'}</span><span>${esc(s.name)}</span>
      <span class="cnt">${s.ok ? `${s.count}件（新着${s.new}）` : (s.last_ok ? `最後の成功 ${fmtDateTime.format(new Date(s.last_ok * 1000))}` : '一度も成功していません')}</span>
      ${s.ok ? '' : `<span class="err">${esc(s.error)}</span>`}</li>`).join('');
  }

  // ---- 操作 ------------------------------------------------------------------
  function persistPrefs() { store.set('sn1.prefs', prefs); }

  function markRead(id) {
    if (readSet.has(id)) return;
    readSet.add(id);
    store.set('sn1.read', Array.from(readSet).slice(-READ_MAX));
    const el = $(`.item[data-id="${CSS.escape(id)}"]`);
    if (el) el.classList.add('is-read');
  }

  function toggleSaved(id) {
    if (saved.has(id)) {
      saved.delete(id);
    } else {
      const it = S.items.find((i) => i.id === id);
      if (!it) return;
      saved.set(id, { title: it.title, url: it.url, source: it.source, cat: it.cat, lang: it.lang, time: it.time, summary: it.summary, company: it.company, stocks: it.stocks || [] });
      if (saved.size > SAVED_MAX) saved.delete(saved.keys().next().value);
    }
    store.set('sn1.saved', Object.fromEntries(saved));
    render();
  }

  function bind() {
    $('#view').addEventListener('click', (e) => {
      const b = e.target.closest('button'); if (!b) return;
      prefs.view = b.dataset.view; S.limit = PAGE; persistPrefs(); render(); window.scrollTo(0, 0);
    });
    $('#tabs').addEventListener('click', (e) => {
      const b = e.target.closest('.tab'); if (!b) return;
      prefs.cat = b.dataset.cat; S.limit = PAGE; persistPrefs(); render();
    });
    $('#lang').addEventListener('click', (e) => {
      const b = e.target.closest('button'); if (!b) return;
      prefs.lang = b.dataset.lang; S.limit = PAGE; persistPrefs(); render();
    });
    $('#t-watch').addEventListener('click', () => { S.onlyWatch = !S.onlyWatch; S.limit = PAGE; render(); });
    $('#t-saved').addEventListener('click', () => { S.onlySaved = !S.onlySaved; S.limit = PAGE; render(); });
    $('#t-unread').addEventListener('click', () => { prefs.unread = !prefs.unread; S.limit = PAGE; persistPrefs(); render(); });
    $('#t-summary').addEventListener('click', () => { prefs.summary = !prefs.summary; persistPrefs(); render(); });
    $('#btn-more').addEventListener('click', () => { S.limit += PAGE; render(); });

    // 銘柄別
    $('#stock-grid').addEventListener('click', (e) => {
      const b = e.target.closest('[data-pick]'); if (!b) return;
      prefs.stock = b.dataset.pick; S.kind = 'all'; S.limit = PAGE; persistPrefs(); render(); window.scrollTo(0, 0);
    });
    $('#stock-back').addEventListener('click', () => { prefs.stock = null; S.kind = 'all'; persistPrefs(); render(); });
    $('#kind-chips').addEventListener('click', (e) => {
      const b = e.target.closest('[data-kind]'); if (!b) return;
      S.kind = b.dataset.kind; S.limit = PAGE; render();
    });
    $('#t-tier').addEventListener('click', () => { prefs.tierOnly = !prefs.tierOnly; S.limit = PAGE; persistPrefs(); render(); });
    $('#t-uncertain').addEventListener('click', () => { prefs.uncertain = !prefs.uncertain; S.limit = PAGE; persistPrefs(); render(); });
    $('#stock-edit').addEventListener('click', () => { if (prefs.stock) openWatch(prefs.stock); });

    let t;
    $('#q').addEventListener('input', (e) => {
      clearTimeout(t);
      t = setTimeout(() => { S.q = e.target.value; S.limit = PAGE; render(); }, 120);
    });

    const list = $('#list');
    list.addEventListener('click', (e) => {
      const star = e.target.closest('[data-star]');
      if (star) { toggleSaved(star.dataset.star); return; }
      const no = e.target.closest('[data-noneed]');
      if (no) {
        const it = S.items.find((i) => i.id === no.dataset.noneed);
        openWatch(no.dataset.stock, it ? it.title : '');
        return;
      }
      const a = e.target.closest('a[data-id]');
      if (a) markRead(a.dataset.id);
    });
    list.addEventListener('auxclick', (e) => {
      const a = e.target.closest('a[data-id]');
      if (a) markRead(a.dataset.id);
    });

    $('#btn-refresh').addEventListener('click', refresh);
    $('#btn-watch').addEventListener('click', () => openWatch());
    $('#btn-sources').addEventListener('click', () => { renderSources(); $('#dlg-sources').showModal(); });

    $('#watch-list').addEventListener('click', (e) => {
      const ed = e.target.closest('[data-edit]');
      if (ed) { startEdit(Number(ed.dataset.edit)); return; }
      const b = e.target.closest('[data-del]'); if (!b) return;
      const wl = watchlist().slice();
      const [gone] = wl.splice(Number(b.dataset.del), 1);
      if (prefs.stock === gone.name) { prefs.stock = null; persistPrefs(); }
      saveWatch(wl, `「${gone.name}」を削除しました。`);
    });
    $('#watch-cancel').addEventListener('click', resetWatchForm);
    $('#watch-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const f = e.target;
      const entry = { name: f.name.value.trim(), code: f.code.value.trim(), aliases: f.aliases.value, exclude: f.exclude.value };
      const wl = watchlist().slice();
      const editing = S.editing;
      const idx = wl.findIndex((w) => w.name.toLowerCase() === (editing || entry.name).toLowerCase());
      if (idx >= 0) wl[idx] = entry; else wl.push(entry);
      const ok = await saveWatch(wl, editing ? `「${entry.name}」を更新しました。` : `「${entry.name}」を追加しました。`, true);
      if (ok) { if (editing && editing !== entry.name && prefs.stock === editing) { prefs.stock = entry.name; persistPrefs(); } resetWatchForm(); }
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === '/' && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName) && !e.ctrlKey && !e.metaKey) {
        e.preventDefault(); $('#q').focus();
      }
    });

    // 開きっぱなしでも、新しい取得があれば自動で読み込み直す／経過時間の表示を更新する
    setInterval(() => { if (S.live && !S.updating && !document.hidden) pollStatus(); else renderUpdated(); }, 60000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden && S.live && !S.updating) pollStatus(); });
  }

  // ---- 起動 ------------------------------------------------------------------
  async function init() {
    bind();
    await loadNews();
    try {
      const st = await api('GET', '/api/status');
      S.live = true;
      $('#btn-refresh').hidden = false;
      if (st.updating) setBusy(true, st.total ? `取得中 ${st.done}/${st.total}` : '取得中…');
    } catch {
      S.live = false;   // 書き出しただけの表示（サーバーなし）
    }
    render();
    renderUpdated();
    if (S.live) pollStatus();
  }

  init();
})();
