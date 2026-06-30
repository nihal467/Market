const IST_OFFSET_MS = 5.5 * 60 * 60 * 1000;

function istNow(date = new Date()) {
  return new Date(date.getTime() + IST_OFFSET_MS);
}

function isMarketWatchWindow(date = new Date()) {
  const ist = istNow(date);
  const day = ist.getUTCDay();
  const mins = ist.getUTCHours() * 60 + ist.getUTCMinutes();
  return day >= 1 && day <= 5 && mins >= 9 * 60 + 5 && mins <= 15 * 60 + 35;
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body, null, 2), {
    status,
    headers: {"content-type": "application/json; charset=utf-8"},
  });
}

async function dispatchGithubWorkflow(env, reason) {
  if (!env.GITHUB_TOKEN) {
    return {ok: false, status: 500, body: "Missing GITHUB_TOKEN secret"};
  }

  const owner = env.GITHUB_OWNER || "nihal467";
  const repo = env.GITHUB_REPO || "Market";
  const workflow = env.WORKFLOW_FILE || "intraday_watchdog.yml";
  const ref = env.GITHUB_REF || "main";
  const url = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflow}/dispatches`;

  const response = await fetch(url, {
    method: "POST",
    headers: {
      "accept": "application/vnd.github+json",
      "authorization": `Bearer ${env.GITHUB_TOKEN}`,
      "content-type": "application/json",
      "user-agent": "market-intraday-watchdog-worker",
      "x-github-api-version": "2022-11-28",
    },
    body: JSON.stringify({ref}),
  });

  const body = response.status === 204 ? "" : await response.text();
  return {ok: response.ok, status: response.status, body};
}

async function run(env, reason) {
  const now = new Date();
  const ist = istNow(now).toISOString();
  if (!isMarketWatchWindow(now)) {
    return {dispatched: false, reason: "outside_market_window", ist};
  }

  const result = await dispatchGithubWorkflow(env, reason);
  return {
    dispatched: result.ok,
    github_status: result.status,
    github_body: result.body,
    reason,
    ist,
  };
}

export default {
  async scheduled(_controller, env, ctx) {
    ctx.waitUntil(run(env, "cloudflare-cron").then((result) => {
      console.log(JSON.stringify(result));
    }));
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/health") {
      return json({
        ok: true,
        worker: "market-intraday-watchdog",
        market_window: isMarketWatchWindow(),
        ist: istNow().toISOString(),
      });
    }

    if (url.pathname === "/trigger") {
      if (!env.TRIGGER_SECRET || request.headers.get("x-trigger-secret") !== env.TRIGGER_SECRET) {
        return json({ok: false, error: "unauthorized"}, 401);
      }
      return json(await run(env, "manual-worker-trigger"));
    }

    return json({
      ok: true,
      endpoints: ["/health", "/trigger"],
      note: "Cron trigger dispatches GitHub Actions automatically during market hours.",
    });
  },
};
