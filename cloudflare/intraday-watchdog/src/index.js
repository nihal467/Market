const IST_OFFSET_MS = 5.5 * 60 * 60 * 1000;
const WINDOW_MINUTES = 5;

const TASKS = [
  {
    id: "intraday-watchdog",
    workflow: "intraday_watchdog.yml",
    mode: "window",
    days: [1, 2, 3, 4, 5],
    start: "09:05",
    end: "15:35",
    dedupe: false,
  },
  {
    id: "daily-dashboard",
    workflow: "daily.yml",
    mode: "at",
    days: [1, 2, 3, 4, 5],
    at: "16:00",
    dedupe: true,
  },
  {
    id: "daily-analysis-close",
    workflow: "daily_analysis.yml",
    mode: "at",
    days: [1, 2, 3, 4, 5],
    at: "16:30",
    dedupe: true,
  },
  {
    id: "daily-refinement-task",
    workflow: "daily_refinement.yml",
    mode: "at",
    days: [1, 2, 3, 4, 5],
    at: "17:15",
    dedupe: true,
  },
  {
    id: "weekly-watchlist",
    workflow: "weekly_watchlist.yml",
    mode: "at",
    days: [0],
    at: "17:30",
    dedupe: true,
  },
];

function istNow(date = new Date()) {
  return new Date(date.getTime() + IST_OFFSET_MS);
}

function minutes(time) {
  const [hour, minute] = time.split(":").map(Number);
  return hour * 60 + minute;
}

function istInfo(date = new Date()) {
  const ist = istNow(date);
  return {
    iso: ist.toISOString(),
    day: ist.getUTCDay(),
    minutes: ist.getUTCHours() * 60 + ist.getUTCMinutes(),
  };
}

function windowStartUtc(date, targetMinutes) {
  const ist = istNow(date);
  const midnightUtc = Date.UTC(
    ist.getUTCFullYear(),
    ist.getUTCMonth(),
    ist.getUTCDate(),
    0,
    0,
    0,
  ) - IST_OFFSET_MS;
  return new Date(midnightUtc + targetMinutes * 60 * 1000);
}

function taskDue(task, date = new Date()) {
  const info = istInfo(date);
  if (!task.days.includes(info.day)) {
    return null;
  }

  if (task.mode === "window") {
    const start = minutes(task.start);
    const end = minutes(task.end);
    if (info.minutes >= start && info.minutes <= end) {
      return {task, since: new Date(date.getTime() - WINDOW_MINUTES * 60 * 1000)};
    }
    return null;
  }

  const target = minutes(task.at);
  if (info.minutes >= target && info.minutes < target + WINDOW_MINUTES) {
    return {task, since: windowStartUtc(date, target)};
  }
  return null;
}

function dueTasks(date = new Date()) {
  return TASKS.map((task) => taskDue(task, date)).filter(Boolean);
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body, null, 2), {
    status,
    headers: {"content-type": "application/json; charset=utf-8"},
  });
}

function githubHeaders(env) {
  return {
    "accept": "application/vnd.github+json",
    "authorization": `Bearer ${env.GITHUB_TOKEN}`,
    "content-type": "application/json",
    "user-agent": "market-cloudflare-scheduler",
    "x-github-api-version": "2022-11-28",
  };
}

function githubWorkflowUrl(env, workflow, suffix = "") {
  const owner = env.GITHUB_OWNER || "nihal467";
  const repo = env.GITHUB_REPO || "Market";
  return `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflow}${suffix}`;
}

async function hasRunSince(env, workflow, since) {
  const url = new URL(githubWorkflowUrl(env, workflow, "/runs"));
  url.searchParams.set("per_page", "20");
  const response = await fetch(url, {headers: githubHeaders(env)});
  if (!response.ok) {
    return false;
  }
  const data = await response.json();
  const sinceMs = since.getTime();
  return (data.workflow_runs || []).some((run) => new Date(run.created_at).getTime() >= sinceMs);
}

async function dispatchGithubWorkflow(env, task) {
  const ref = env.GITHUB_REF || "main";
  const url = githubWorkflowUrl(env, task.workflow, "/dispatches");
  const response = await fetch(url, {
    method: "POST",
    headers: githubHeaders(env),
    body: JSON.stringify({ref}),
  });
  const body = response.status === 204 ? "" : await response.text();
  return {ok: response.ok, status: response.status, body};
}

async function run(env, reason, date = new Date()) {
  if (!env.GITHUB_TOKEN) {
    return [{ok: false, status: 500, task: "scheduler", body: "Missing GITHUB_TOKEN secret"}];
  }

  const due = dueTasks(date);
  if (!due.length) {
    return [{
      dispatched: false,
      reason: "nothing_due",
      ist: istInfo(date).iso,
      trigger: reason,
    }];
  }

  const results = [];
  for (const item of due) {
    const task = item.task;
    if (task.dedupe && await hasRunSince(env, task.workflow, item.since)) {
      results.push({
        dispatched: false,
        skipped: "already_ran",
        task: task.id,
        workflow: task.workflow,
        since: item.since.toISOString(),
      });
      continue;
    }

    const result = await dispatchGithubWorkflow(env, task);
    results.push({
      dispatched: result.ok,
      task: task.id,
      workflow: task.workflow,
      github_status: result.status,
      github_body: result.body,
      trigger: reason,
      ist: istInfo(date).iso,
    });
  }
  return results;
}

export default {
  async scheduled(_controller, env, ctx) {
    ctx.waitUntil(run(env, "cloudflare-cron").then((results) => {
      console.log(JSON.stringify(results));
    }));
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/health") {
      return json({
        ok: true,
        worker: "market-cloudflare-scheduler",
        ist: istInfo().iso,
        due: dueTasks().map((item) => ({
          task: item.task.id,
          workflow: item.task.workflow,
          since: item.since.toISOString(),
        })),
        tasks: TASKS.map((task) => ({
          id: task.id,
          workflow: task.workflow,
          mode: task.mode,
          at: task.at,
          start: task.start,
          end: task.end,
          days: task.days,
        })),
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
      note: "Cloudflare Cron dispatches all GitHub Actions schedules.",
    });
  },
};
