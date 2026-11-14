// 毎時、GitHub の「Update and publish」ワークフローを起動する（GitHub 側の定時実行が動かないための代わり）。
// 外から呼べる入口（fetch）は作らない。Cloudflare の定期実行（Cron Trigger）だけで動く。
// GITHUB_TOKEN は秘密の変数（Cloudflare 側にだけ置く）。権限は、この 1 つのリポジトリの Actions の読み書きだけ。
export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatch(env));
  },
};

async function dispatch(env) {
  if (!env.GITHUB_TOKEN) {
    console.error('GITHUB_TOKEN が設定されていません');
    return;
  }
  const url = `https://api.github.com/repos/${env.REPO}/actions/workflows/${env.WORKFLOW}/dispatches`;
  const res = await fetch(url, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'User-Agent': 'stock-news-cron',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ ref: env.REF || 'main' }),
  });
  // 成功は 204。失敗の理由（401 は鍵の期限切れ・取り消し、404 は権限不足）だけ記録する
  if (res.status === 204) console.log('workflow_dispatch: 204');
  else console.error('workflow_dispatch 失敗:', res.status, (await res.text()).slice(0, 200));
}
