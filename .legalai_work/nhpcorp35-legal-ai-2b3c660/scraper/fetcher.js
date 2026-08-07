// fetcher.js — HTTP fetching via ScraperAPI
'use strict';

const axios = require('axios');
const fs = require('fs');
const { scraperUrl } = require('./utils');

const DEFAULT_TIMEOUT = 30000; // 30s

/**
 * Fetch HTML from a URL via ScraperAPI.
 * Returns { html, status } or throws.
 */
async function fetchHtml(url) {
  const apiUrl = scraperUrl(url, { render: 'false' });
  const resp = await axios.get(apiUrl, {
    timeout: DEFAULT_TIMEOUT,
    headers: { 'Accept': 'text/html,application/xhtml+xml' },
  });

  const status = resp.status;

  // Detect captcha / block pages
  const body = resp.data || '';
  if (typeof body === 'string') {
    if (body.toLowerCase().includes('captcha') || body.toLowerCase().includes('access denied')) {
      throw new Error('CAPTCHA_OR_BLOCKED');
    }
  }

  return { html: body, status };
}

/**
 * Download a PDF/binary file via ScraperAPI and save it to destPath.
 * Returns true on success, throws on error.
 */
async function downloadFile(url, destPath) {
  const apiUrl = scraperUrl(url);
  const resp = await axios.get(apiUrl, {
    responseType: 'arraybuffer',
    timeout: 60000,
    headers: { 'Accept': 'application/pdf,*/*' },
  });

  const status = resp.status;
  if (status !== 200) {
    throw new Error(`HTTP ${status} for ${url}`);
  }

  const data = Buffer.from(resp.data);

  // Sanity check: PDFs start with %PDF
  if (data.length < 5 || data.slice(0, 4).toString() !== '%PDF') {
    const snippet = data.slice(0, 200).toString('utf8');
    if (snippet.toLowerCase().includes('captcha') || snippet.toLowerCase().includes('access denied')) {
      throw new Error('CAPTCHA_OR_BLOCKED');
    }
    // Could be a non-PDF document — still save it (some courts serve .doc etc.)
    console.log(`  ⚠️  File may not be a PDF (starts with: ${data.slice(0,4).toString()})`);
  }

  fs.writeFileSync(destPath, data);
  return true;
}

module.exports = { fetchHtml, downloadFile };
