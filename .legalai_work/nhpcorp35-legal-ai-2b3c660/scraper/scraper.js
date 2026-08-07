// scraper.js — NY AD1 Appellate scraper (3-level navigation)
'use strict';

require('dotenv').config({ path: '/home/node/.openclaw/.env' });

const path = require('path');
const axios = require('axios');
const config = require('./config');
const tracker = require('./tracker');
const { downloadFile } = require('./fetcher');
const { extractMonthLinks, extractSessionLinks, extractDocuments } = require('./parser');
const {
  randomDelay, longPause, backoffPause,
  randomInt, toYMD,
  buildFilename, buildStoragePath,
  fileExists, ensureDir,
} = require('./utils');

const court = config.courts[config.activeCourt];
const TEST_LIMIT = config.testLimit || 0;

// ─── ScraperAPI fetch (plain) ─────────────────────────────────────────────────

async function apiGet(url, render = false) {
  const params = { api_key: config.scraperApiKey, url };
  if (render) { params.render = 'true'; params.wait = '4000'; } // wait 4s for JS
  const apiUrl = 'https://api.scraperapi.com/?' + new URLSearchParams(params).toString();
  const resp = await axios.get(apiUrl, { timeout: 45000 });
  const body = resp.data;
  if (typeof body === 'string') {
    const lower = body.toLowerCase();
    if (lower.includes('captcha') || (lower.includes('access denied') && body.length < 2000)) {
      throw new Error('CAPTCHA_OR_BLOCKED');
    }
  }
  return body;
}

// ─── Main ─────────────────────────────────────────────────────────────────────

async function run() {
  console.log(`\n🏛  NY AD1 Appellate Scraper`);
  if (TEST_LIMIT) console.log(`🧪 TEST MODE: max ${TEST_LIMIT} files`);
  console.log(`📊 Tracker: ${JSON.stringify(tracker.status())}\n`);

  if (!config.scraperApiKey) {
    console.error('❌ SCRAPERAPI_KEY not set.'); process.exit(1);
  }

  // Level 1: fetch index page
  console.log('📥 Fetching index:', court.indexUrl);
  let indexHtml;
  try {
    indexHtml = await apiGet(court.indexUrl);
  } catch (e) {
    console.error('❌ Index fetch failed:', e.message); process.exit(1);
  }

  const allMonths = extractMonthLinks(indexHtml);
  // Filter out future months (no docs yet)
  const now = new Date();
  const months = allMonths.filter(m =>
    m.year < now.getFullYear() ||
    (m.year === now.getFullYear() && m.month <= now.getMonth() + 1)
  );
  console.log(`📅 Found ${months.length} usable month entries (newest first, ${allMonths.length - months.length} future skipped)\n`);

  let totalDownloaded = 0;
  let requestCount = 0;

  outer:
  for (const month of months) {
    console.log(`\n📆 ${month.monthName} ${month.year} — ${month.url}`);

    // Level 2: fetch month page
    let monthHtml;
    try {
      monthHtml = await apiGet(month.url);
      tracker.resetErrors();
    } catch (e) {
      console.error('  ❌ Month page error:', e.message);
      if (e.message.includes('CAPTCHA_OR_BLOCKED')) { console.log('🛑 Blocked. Stopping.'); break; }
      tracker.recordError(e.message);
      if (tracker.shouldBackoff()) await backoffPause();
      continue;
    }

    requestCount++;
    await checkLongPause(requestCount);

    const sessions = extractSessionLinks(monthHtml, month.url);
    console.log(`  🗓  ${sessions.length} session(s) found`);

    for (const session of sessions) {
      console.log(`\n  📄 Session [${session.sessionType}] ${session.dateStr} — ${session.url}`);

      // Level 3: fetch session page (JS rendered)
      let sessionHtml;
      try {
        sessionHtml = await apiGet(session.url, true); // render=true
        tracker.resetErrors();
      } catch (e) {
        console.error('  ❌ Session fetch error:', e.message);
        if (e.message.includes('CAPTCHA_OR_BLOCKED')) { console.log('🛑 Blocked. Stopping.'); break outer; }
        tracker.recordError(e.message);
        if (tracker.shouldBackoff()) await backoffPause();
        else await randomDelay();
        continue;
      }

      requestCount++;
      await randomDelay();

      const docs = extractDocuments(sessionHtml, session.url, session.sessionType, session.dateStr);
      console.log(`  🔗 ${docs.length} document(s) found`);

      for (const doc of docs) {
        // Check all limits
        if (!tracker.canDownload()) { console.log('🛑 Rate limit reached. Stopping.'); break outer; }
        if (TEST_LIMIT && totalDownloaded >= TEST_LIMIT) {
          console.log(`🧪 Test limit (${TEST_LIMIT}) reached. Stopping.`);
          break outer;
        }

        const result = await processDoc(doc);
        if (result === 'downloaded') {
          totalDownloaded++;
          requestCount++;
        }
        if (result === 'blocked') { break outer; }

        await checkLongPause(requestCount);
        if (result === 'downloaded' || result === 'new') await randomDelay();
      }
    }

    await randomDelay();
  }

  console.log(`\n✅ Done. Downloaded: ${totalDownloaded} files`);
  console.log(`📊 Final status: ${JSON.stringify(tracker.status())}`);
  printSummary();
}

// ─── Process one document ────────────────────────────────────────────────────

async function processDoc(doc) {
  const { url, caseNumber, docType, date, year } = doc;

  if (tracker.hasUrl(url)) {
    console.log(`    ⏭  Already have: ${url.split('/').pop()}`);
    return 'skip';
  }

  const storageDir = buildStoragePath(court.name, year, caseNumber);
  // For UNKNOWN IDs, derive a stable suffix from the URL so dedup works across restarts
  const unknownSuffix = caseNumber ? null : url.split('/').pop().replace('.pdf','').slice(0,8).replace(/[^a-zA-Z0-9]/g,'').toUpperCase() || Math.random().toString(36).slice(2,7).toUpperCase();
  const filename = buildFilename(caseNumber, docType, date, unknownSuffix);
  const filePath = path.join(storageDir, filename);

  if (fileExists(filePath)) {
    console.log(`    ⏭  File exists: ${filename}`);
    return 'skip';
  }

  ensureDir(storageDir);
  console.log(`    ⬇️  [${docType}] ${filename}`);

  try {
    await downloadFile(url, filePath);

    tracker.record({
      case_number: caseNumber || 'UNKNOWN',
      case_title: doc.caseTitle || '',
      index_number: null,
      document_type: docType,
      date: toYMD(date),
      source_url: url,
      filename,
      storage_path: filePath,
      court: court.name,
      scraped_at: new Date().toISOString(),
    });

    console.log(`    💾 Saved`);
    return 'downloaded';
  } catch (e) {
    const msg = e.message || String(e);
    console.error(`    ❌ Failed: ${msg}`);
    if (msg.includes('CAPTCHA_OR_BLOCKED')) return 'blocked';
    tracker.recordError(msg);
    if (tracker.shouldBackoff()) {
      console.log('    ⚠️  Backing off...');
      await backoffPause();
    }
    return 'error';
  }
}

// ─── Long pause logic ─────────────────────────────────────────────────────────

let _nextLongPauseAt = randomInt(config.longPauseEvery[0], config.longPauseEvery[1]);

async function checkLongPause(count) {
  if (count >= _nextLongPauseAt) {
    await longPause();
    _nextLongPauseAt = count + randomInt(config.longPauseEvery[0], config.longPauseEvery[1]);
  }
}

// ─── Summary ──────────────────────────────────────────────────────────────────

function printSummary() {
  const all = tracker.getMetadata();
  if (!all.length) { console.log('\nNo files downloaded.'); return; }
  console.log(`\n📋 Last ${Math.min(all.length, 20)} of ${all.length} downloaded files:`);
  all.slice(-20).forEach((m, i) => {
    console.log(`  ${i+1}. [${m.document_type}] ${m.filename} (${m.case_title || ''})`);
  });
}

// ─── Run ──────────────────────────────────────────────────────────────────────

run().catch(err => {
  console.error('Fatal:', err);
  process.exit(1);
});
