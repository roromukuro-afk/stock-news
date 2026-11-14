// 合言葉の検査（functions/_middleware.js）の試験。実行: node --test tests/middleware.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { onRequest } from '../functions/_middleware.js';

const basic = (user, pass) => 'Basic ' + Buffer.from(`${user}:${pass}`).toString('base64');
const run = (headers, env = { SITE_PASSWORD: 'correct-horse' }) => {
  let nextCalled = 0;
  const p = onRequest({
    request: new Request('https://example.pages.dev/data/news.json', { headers }),
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
