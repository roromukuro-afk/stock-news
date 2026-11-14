// 合言葉の検査（functions/_middleware.js）の試験。実行: node --test tests/middleware.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import { onRequest } from '../functions/_middleware.js';

const basic = (user, pass) => 'Basic ' + Buffer.from(`${user}:${pass}`).toString('base64');
const run = (headers, env = { SITE_PASSWORD: 'correct-horse' }, { method = 'GET', path = '/data/news.json', body } = {}) => {
  let nextCalled = 0;
  const p = onRequest({
    request: new Request('https://example.pages.dev' + path, { method, headers, body }),
    env,
    next: async () => { nextCalled++; return new Response('{"ok":true}', { headers: { 'Content-Type': 'application/json' } }); },
  });
  return p.then((res) => ({ res, nextCalled }));
};

test('合言葉なしは 401 で、中身を取りに行かない', async () => {
  const { res, nextCalled } = await run({});
  assert.equal(res.status, 401);
  assert.match(res.headers.get('WWW-Authenticate'), /^Basic /);
  assert.equal(nextCalled, 0);
});

test('違う合言葉は 401', async () => {
  const { res, nextCalled } = await run({ Authorization: basic('me', 'wrong') });
  assert.equal(res.status, 401);
  assert.equal(nextCalled, 0);
});

test('正しい合言葉は 200・ユーザー名は何でもよい・保護用ヘッダー付き', async () => {
  for (const user of ['me', '', 'ユーザー']) {   // ユーザー名に「:」は使えない（仕様）
    const { res, nextCalled } = await run({ Authorization: basic(user, 'correct-horse') });
    assert.equal(res.status, 200, `user=${user}`);
    assert.equal(nextCalled, 1);
    assert.equal(res.headers.get('X-Robots-Tag'), 'noindex, nofollow');
    assert.match(res.headers.get('Content-Security-Policy'), /default-src 'self'/);
    assert.equal(res.headers.get('Cache-Control'), 'private, no-cache');
  }
});

test('壊れた Authorization・別の方式は 401', async () => {
  for (const h of ['Basic !!!not-base64!!!', 'Bearer correct-horse', 'Basic ', basic('me', 'correct-horse').toLowerCase()]) {
    const { res } = await run({ Authorization: h });
    assert.equal(res.status, 401, h);
  }
});

test('合言葉の登録が無いときは、正しそうな値でも何も見せない（503）', async () => {
  const { res, nextCalled } = await run({ Authorization: basic('me', 'anything') }, {});
  assert.equal(res.status, 503);
  assert.equal(nextCalled, 0);
});

test('前方一致・部分一致では通らない', async () => {
  for (const p of ['correct', 'correct-horse2', 'correct-hors', '']) {
    const { res } = await run({ Authorization: basic('me', p) });
    assert.equal(res.status, 401, p);
  }
});

// ---- ログイン画面と Cookie（スマホのホーム画面のアプリで毎回聞かれないようにする） ----
const PASS = 'correct-horse';
const NOW = () => Math.floor(Date.now() / 1000);
const sig = (exp, pass = PASS) => createHmac('sha256', pass).update('sn1:' + exp).digest('hex');
const cookieFor = (exp, pass = PASS) => `sn_auth=${exp}.${sig(exp, pass)}`;
const form = (password) => ({ method: 'POST', path: '/login', body: new URLSearchParams({ password }), headers: { 'Content-Type': 'application/x-www-form-urlencoded' } });
const login = (password, env) => { const { headers, ...opts } = form(password); return run(headers, env, opts); };
const HTML = { Accept: 'text/html,application/xhtml+xml' };

test('ブラウザーで画面を開いたとき、合言葉なしはログイン画面へ（データの取得は 401 のまま）', async () => {
  const { res, nextCalled } = await run(HTML, undefined, { path: '/' });
  assert.equal(res.status, 303);
  assert.equal(res.headers.get('Location'), '/login');
  assert.equal(nextCalled, 0);
  assert.equal((await run({})).res.status, 401);                          // Accept なし（道具・fetch）は 401
  assert.equal((await run({ Accept: 'application/json' })).res.status, 401);
});

test('ログイン画面: 入力欄だけ・中身は取りに行かない・script なし', async () => {
  const { res, nextCalled } = await run(HTML, undefined, { path: '/login' });
  assert.equal(res.status, 200);
  assert.equal(nextCalled, 0);
  const html = await res.text();
  assert.match(html, /type="password"/);
  assert.match(html, /autocomplete="current-password"/);
  assert.doesNotMatch(html, /<script/i);
  assert.match(res.headers.get('Content-Security-Policy'), /default-src 'none'/);
  assert.equal(res.headers.get('Cache-Control'), 'no-store');
});

test('正しい合言葉でログイン → 長持ちする Cookie が付き、その Cookie だけで入れる', async () => {
  const { res } = await login(PASS);
  assert.equal(res.status, 303);
  assert.equal(res.headers.get('Location'), '/');
  const set = res.headers.get('Set-Cookie');
  assert.match(set, /^sn_auth=\d+\.[0-9a-f]{64};/);
  for (const a of ['HttpOnly', 'Secure', 'SameSite=Lax', 'Path=/', `Max-Age=${180 * 86400}`]) assert.ok(set.includes(a), a);
  const ck = set.split(';')[0];
  const { res: r2, nextCalled } = await run({ Cookie: ck });            // Authorization なし
  assert.equal(r2.status, 200);
  assert.equal(nextCalled, 1);
  assert.equal(r2.headers.get('X-Robots-Tag'), 'noindex, nofollow');
});

test('違う合言葉・空・形式の違う送信ではログインできない（Cookie は付かない）', async () => {
  for (const p of ['wrong', '', 'correct', 'correct-horse2']) {
    const { res } = await login(p);
    assert.equal(res.status, 401, p);
    assert.equal(res.headers.get('Set-Cookie'), null);
    assert.match(await res.text(), /合言葉が違います/);
  }
  const { res } = await run({ 'Content-Type': 'application/json' }, undefined, { method: 'POST', path: '/login', body: '{"password":"correct-horse"}' });
  assert.equal(res.status, 401);
});

test('Cookie の改ざん・期限切れ・別の合言葉で作ったもの・壊れたものは通らない', async () => {
  const exp = NOW() + 100 * 86400;
  const good = cookieFor(exp);
  assert.equal((await run({ Cookie: good })).res.status, 200);
  const bad = [
    good.slice(0, -1) + (good.endsWith('0') ? '1' : '0'),                 // 署名の 1 文字を変えた
    `sn_auth=${exp + 86400}.${sig(exp)}`,                                // 期限だけ延ばした
    cookieFor(NOW() - 10),                                                // 期限切れ（署名は正しい）
    cookieFor(exp, 'other-password'),                                     // 別の合言葉で署名
    'sn_auth=abc', 'sn_auth=', `sn_auth=${exp}`, `sn_auth=${exp}.zz`,
    `other=${good.split('=')[1]}`,                                        // 名前が違う
    `sn_auth=${NOW() + 999 * 86400}.${sig(NOW() + 999 * 86400)}`,         // 期限が遠すぎる（署名が正しくても通さない）
  ];
  for (const c of bad) assert.equal((await run({ Cookie: c })).res.status, 401, c);
});

test('合言葉を変えると、前の Cookie は全部使えなくなる', async () => {
  const ck = cookieFor(NOW() + 100 * 86400);
  assert.equal((await run({ Cookie: ck }, { SITE_PASSWORD: 'new-password' })).res.status, 401);
});

test('使い続けている間は期限を延ばす（残りが短く、画面を開いたときだけ）', async () => {
  const old = cookieFor(NOW() + 30 * 86400);
  const { res } = await run({ Cookie: old, ...HTML }, undefined, { path: '/' });
  assert.equal(res.status, 200);
  assert.match(res.headers.get('Set-Cookie'), /^sn_auth=\d+\.[0-9a-f]{64}; .*Max-Age=15552000/);
  assert.equal((await run({ Cookie: old })).res.headers.get('Set-Cookie'), null);              // データの取得では延ばさない
  const fresh = cookieFor(NOW() + 170 * 86400);
  assert.equal((await run({ Cookie: fresh, ...HTML }, undefined, { path: '/' })).res.headers.get('Set-Cookie'), null);
});

test('入っているときの /login は画面へ・/logout で Cookie が消える・Basic も使える', async () => {
  const ck = cookieFor(NOW() + 100 * 86400);
  const r1 = (await run({ Cookie: ck }, undefined, { path: '/login' })).res;
  assert.equal(r1.status, 303);
  assert.equal(r1.headers.get('Location'), '/');
  const r2 = (await run({}, undefined, { path: '/logout' })).res;
  assert.equal(r2.headers.get('Location'), '/login');
  assert.match(r2.headers.get('Set-Cookie'), /^sn_auth=; .*Max-Age=0/);
  assert.equal((await run({ Authorization: basic('me', PASS) })).res.status, 200);
});

test('合言葉の登録が無いときは、ログインも通らない（503）・GET/POST 以外の /login は 405', async () => {
  assert.equal((await login(PASS, {})).res.status, 503);
  assert.equal((await run({}, undefined, { method: 'DELETE', path: '/login' })).res.status, 405);
});
