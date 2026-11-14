// 合言葉。サイトのすべての要求（画面もデータも）がここを通る。
// SITE_PASSWORD（Cloudflare Pages の秘密の環境変数）が無いときは、何も見せない。
//
// 入り方は 2 つ:
//   1. ログイン画面（/login）で合言葉を入れる → 長持ちする Cookie（180 日・使うたびに延びる）が付く。
//      スマホのホーム画面に入れたアプリは、Basic 認証の入力を覚えないので、毎回聞かれる。こちらなら一度で済む。
//   2. Authorization: Basic（ユーザー名は何でもよい）。確認用の道具（publish check）や curl 用。
// Cookie は「期限.HMAC-SHA256(期限, 合言葉)」。合言葉を変えると、前の Cookie は全部使えなくなる。
const enc = new TextEncoder();
const COOKIE = 'sn_auth';
const LIFETIME = 180 * 86400;        // 秒
const REFRESH_BELOW = 90 * 86400;    // 残りがこれより短くなったら、画面を開いたときに延ばす

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

async function sign(pass, exp) {
  const key = await crypto.subtle.importKey('raw', enc.encode(pass), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const mac = new Uint8Array(await crypto.subtle.sign('HMAC', key, enc.encode('sn1:' + exp)));
  return Array.from(mac, (b) => b.toString(16).padStart(2, '0')).join('');
}

async function makeCookie(pass, now) {
  const exp = now + LIFETIME;
  return `${COOKIE}=${exp}.${await sign(pass, exp)}; Path=/; Max-Age=${LIFETIME}; HttpOnly; Secure; SameSite=Lax`;
}

// 戻り値: 検証できなければ null、できれば期限までの残り秒
async function cookieRemaining(pass, request, now) {
  const jar = request.headers.get('Cookie') || '';
  for (const part of jar.split(';')) {
    const [k, ...rest] = part.trim().split('=');
    if (k !== COOKIE) continue;
    const m = /^(\d{1,12})\.([0-9a-f]{64})$/.exec(rest.join('='));
    if (!m) return null;
    const exp = Number(m[1]);
    if (exp <= now || exp > now + LIFETIME + 86400) return null;
    return (await same(m[2], await sign(pass, exp))) ? exp - now : null;
  }
  return null;
}

async function basicOk(pass, request) {
  const h = request.headers.get('Authorization') || '';
  if (!h.startsWith('Basic ')) return false;
  let dec = '';
  try {
    dec = new TextDecoder().decode(Uint8Array.from(atob(h.slice(6)), (c) => c.charCodeAt(0)));
  } catch (e) { /* 壊れた値は不一致として扱う */ }
  const i = dec.indexOf(':');
  return i >= 0 && same(dec.slice(i + 1), pass);
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

function loginPage(message, status) {
  const html = `<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark"><meta name="robots" content="noindex, nofollow"><title>株ニュース</title>
<style>
body{margin:0;min-height:100vh;display:grid;place-items:center;font:16px/1.6 -apple-system,BlinkMacSystemFont,"Hiragino Sans","Yu Gothic UI",Meiryo,sans-serif;background:Canvas;color:CanvasText}
form{width:min(340px,calc(100vw - 32px));display:grid;gap:12px}
h1{margin:0 0 4px;font-size:20px}
input,button{font:inherit;font-size:16px;padding:12px 14px;border-radius:10px;border:1px solid GrayText;box-sizing:border-box;width:100%}
button{background:CanvasText;color:Canvas;border-color:CanvasText;font-weight:700;cursor:pointer}
p{margin:0;color:#b42318;font-size:14px}
</style></head><body>
<form method="post" action="/login">
<h1>株ニュース</h1>
${message ? `<p role="alert">${message}</p>` : ''}
<input type="password" name="password" placeholder="合言葉" autocomplete="current-password" autofocus required>
<button type="submit">開く</button>
</form></body></html>`;
  return new Response(html, {
    status,
    headers: {
      'Content-Type': 'text/html; charset=utf-8',
      'Cache-Control': 'no-store',
      'X-Robots-Tag': 'noindex, nofollow',
      'X-Content-Type-Options': 'nosniff',
      'Referrer-Policy': 'no-referrer',
      'X-Frame-Options': 'DENY',
      // この画面だけ、入力欄の見た目のための style を許す（script は無し・送信先は自分だけ）
      'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    },
  });
}

function redirect(location, extra = {}) {
  return new Response(null, { status: 303, headers: { Location: location, 'Cache-Control': 'no-store', ...extra } });
}

export async function onRequest({ request, env, next }) {
  const pass = env.SITE_PASSWORD;
  if (!pass) {
    return new Response('Not configured', { status: 503, headers: { 'Cache-Control': 'no-store' } });
  }
  const now = Math.floor(Date.now() / 1000);
  const url = new URL(request.url);
  const isRead = request.method === 'GET' || request.method === 'HEAD';

  // ---- ログイン・ログアウト ----
  if (url.pathname === '/login') {
    if (request.method === 'POST') {
      let given = '';
      if (Number(request.headers.get('Content-Length') || 0) <= 2000) {
        try { given = String((await request.formData()).get('password') || ''); } catch (e) { /* 形式が違う送信は不一致 */ }
      }
      if (given && (await same(given, pass))) {
        return redirect('/', { 'Set-Cookie': await makeCookie(pass, now) });
      }
      await new Promise((r) => setTimeout(r, 600));      // 続けて試されるのを遅らせる
      return loginPage('合言葉が違います', 401);
    }
    if (isRead) {
      if ((await basicOk(pass, request)) || (await cookieRemaining(pass, request, now)) !== null) return redirect('/');
      return loginPage('', 200);
    }
    return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET, HEAD, POST' } });
  }
  if (url.pathname === '/logout') {
    return redirect('/login', { 'Set-Cookie': `${COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax` });
  }

  // ---- ふつうの要求 ----
  let ok = await basicOk(pass, request);
  let remaining = null;
  if (!ok) {
    remaining = await cookieRemaining(pass, request, now);
    ok = remaining !== null;
  }
  if (ok) {
    const res = await next();
    const out = new Response(res.body, res);
    for (const [k, v] of Object.entries(SECURITY_HEADERS)) out.headers.set(k, v);
    // ニュースは数十分ごとに変わる。共有のキャッシュには残さず、使うたびに更新の有無を確かめる
    out.headers.set('Cache-Control', 'private, no-cache');
    // 使い続けている間は期限を延ばす（画面を開いたときだけ）
    if (remaining !== null && remaining < REFRESH_BELOW && isRead && (request.headers.get('Accept') || '').includes('text/html')) {
      out.headers.append('Set-Cookie', await makeCookie(pass, now));
    }
    return out;
  }
  // ブラウザーで画面を開いたときはログイン画面へ。それ以外（データの取得・道具）は 401
  if (isRead && (request.headers.get('Accept') || '').includes('text/html')) {
    return redirect('/login');
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
