// 合言葉（Basic 認証）。サイトのすべての要求（画面もデータも）がここを通る。
// SITE_PASSWORD（Cloudflare Pages の秘密の環境変数）が無いときは、何も見せない。ユーザー名は何でもよい。
const enc = new TextEncoder();

async function digest(s) {
  return new Uint8Array(await crypto.subtle.digest('SHA-256', enc.encode(s)));
}

// 長さや先頭の一致が時間に出ないよう、ハッシュにしてから全部を比べる
async function same(a, b) {
  const [x, y] = await Promise.all([digest(a), digest(b)]);
  let d = 0;
  for (let i = 0; i < x.length; i++) d |= x[i] ^ y[i];
  return d === 0;
}

// 手元のサーバー（stocknews/server.py）と同じ守り。画面は自分の資源だけを読む。
const SECURITY_HEADERS = {
  'X-Robots-Tag': 'noindex, nofollow',
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'no-referrer',
  'X-Frame-Options': 'DENY',
  'Content-Security-Policy':
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; " +
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
};

export async function onRequest({ request, env, next }) {
  const pass = env.SITE_PASSWORD;
  if (!pass) {
    return new Response('Not configured', { status: 503, headers: { 'Cache-Control': 'no-store' } });
  }
  const h = request.headers.get('Authorization') || '';
  if (h.startsWith('Basic ')) {
    let dec = '';
    try {
      dec = new TextDecoder().decode(Uint8Array.from(atob(h.slice(6)), (c) => c.charCodeAt(0)));
    } catch (e) { /* 壊れた値は不一致として扱う */ }
    const i = dec.indexOf(':');
    if (i >= 0 && (await same(dec.slice(i + 1), pass))) {
      const res = await next();
      const out = new Response(res.body, res);
      for (const [k, v] of Object.entries(SECURITY_HEADERS)) out.headers.set(k, v);
      // ニュースは数十分ごとに変わる。共有のキャッシュには残さず、使うたびに更新の有無を確かめる
      out.headers.set('Cache-Control', 'private, no-cache');
      return out;
    }
  }
  return new Response('合言葉が必要です', {
    status: 401,
    headers: {
      'WWW-Authenticate': 'Basic realm="stock-news", charset="UTF-8"',
      'Content-Type': 'text/plain; charset=utf-8',
      'Cache-Control': 'no-store',
      'X-Robots-Tag': 'noindex, nofollow',
    },
  });
}
