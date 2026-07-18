/**
 * Gmail cleanup MVP.
 *
 * Paste this file into Google Apps Script at https://script.google.com,
 * set DRY_RUN to false after checking the log, then run startCleanup().
 *
 * Messages moved to Trash can still be recovered from Gmail Trash for a while.
 */

const DRY_RUN = true;
const PAGE_SIZE = 100;
const MAX_BATCHES_PER_RUN = 40;

const CLEANUP_JOBS = [
  {
    name: 'old promotions and social',
    enabled: true,
    query:
      '(category:promotions OR category:social) older_than:30d -is:starred -is:important -in:trash -in:spam',
  },
  {
    name: 'old updates',
    enabled: false,
    query:
      'category:updates older_than:180d -is:starred -is:important -in:trash -in:spam',
  },
];

function startCleanup() {
  resetCleanupState();
  scheduleNextRun_();
  runCleanup();
}

function runCleanup() {
  const props = PropertiesService.getScriptProperties();
  let jobIndex = Number(props.getProperty('jobIndex') || '0');
  let totalMoved = Number(props.getProperty('totalMoved') || '0');
  let batches = 0;

  while (jobIndex < CLEANUP_JOBS.length && batches < MAX_BATCHES_PER_RUN) {
    const job = CLEANUP_JOBS[jobIndex];

    if (!job.enabled) {
      console.log(`Skipping disabled job: ${job.name}`);
      jobIndex++;
      props.setProperty('jobIndex', String(jobIndex));
      continue;
    }

    const threads = GmailApp.search(job.query, 0, PAGE_SIZE);
    if (threads.length === 0) {
      console.log(`Done: ${job.name}`);
      jobIndex++;
      props.setProperty('jobIndex', String(jobIndex));
      continue;
    }

    if (DRY_RUN) {
      console.log(
        `[DRY RUN] ${job.name}: would move ${threads.length} threads. Query: ${job.query}`,
      );
      jobIndex++;
      props.setProperty('jobIndex', String(jobIndex));
      continue;
    }

    GmailApp.moveThreadsToTrash(threads);
    totalMoved += threads.length;
    batches++;
    props.setProperty('totalMoved', String(totalMoved));
    console.log(`${job.name}: moved ${threads.length} threads. Total moved: ${totalMoved}`);
  }

  props.setProperty('jobIndex', String(jobIndex));

  if (jobIndex >= CLEANUP_JOBS.length) {
    clearCleanupTriggers_();
    console.log(`Cleanup complete. Total moved to Trash: ${totalMoved}`);
  } else {
    scheduleNextRun_();
    console.log(`Paused for quota/runtime. Progress saved. Total moved: ${totalMoved}`);
  }
}

function resetCleanupState() {
  PropertiesService.getScriptProperties().deleteAllProperties();
  clearCleanupTriggers_();
  console.log('Cleanup state reset.');
}

function cleanupStatus() {
  const props = PropertiesService.getScriptProperties();
  console.log(`jobIndex: ${props.getProperty('jobIndex') || '0'}`);
  console.log(`totalMoved: ${props.getProperty('totalMoved') || '0'}`);
  CLEANUP_JOBS.forEach((job) => {
    if (!job.enabled) {
      console.log(`${job.name}: disabled`);
      return;
    }
    const count = GmailApp.search(job.query, 0, 500).length;
    console.log(`${job.name}: at least ${count} matching threads remain`);
  });
}

function scheduleNextRun_() {
  clearCleanupTriggers_();
  ScriptApp.newTrigger('runCleanup').timeBased().after(60 * 1000).create();
}

function clearCleanupTriggers_() {
  ScriptApp.getProjectTriggers().forEach((trigger) => {
    if (trigger.getHandlerFunction() === 'runCleanup') {
      ScriptApp.deleteTrigger(trigger);
    }
  });
}
