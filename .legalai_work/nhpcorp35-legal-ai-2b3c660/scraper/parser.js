// parser.js — extract document links from NY AD1 court pages
'use strict';

const cheerio = require('cheerio');

const INDEX_BASE = 'https://www.nycourts.gov/courts/AD1/calendar/appsmots/';
const APPSMOT_BASE = 'https://www.nycourts.gov/courts/AD1/calendar/AppsMots/';
const PDF_BASE = 'https://www.nycourts.gov/courts/AD1/calendar/AppsMots/';

/**
 * Level 1: Parse the main index page.
 * Returns array of { year, month, url } for each month link.
 * Ordered newest first.
 */
function extractMonthLinks(html) {
  const $ = cheerio.load(html);
  const months = [];

  $('a[href]').each((_, el) => {
    const href = $(el).attr('href');
    const text = $(el).text().trim();
    if (!href) return;

    // Pattern: 2026/January.shtml or 2024/01_January/January.shtml
    const m = href.match(/(\d{4})\//);
    if (!m) return;

    const monthNames = ['january','february','march','april','may','june',
                        'july','august','september','october','november','december'];
    const textLower = text.toLowerCase();
    if (!monthNames.includes(textLower)) return;

    const year = parseInt(m[1]);
    const monthIndex = monthNames.indexOf(textLower) + 1;

    let fullUrl;
    try {
      fullUrl = new URL(href, INDEX_BASE).toString();
    } catch { return; }

    months.push({ year, month: monthIndex, monthName: text, url: fullUrl });
  });

  // Sort newest first
  months.sort((a, b) => b.year - a.year || b.month - a.month);
  return months;
}

/**
 * Level 2: Parse a monthly page.
 * Returns array of session URLs (Appeals + Motions).
 */
function extractSessionLinks(html, monthPageUrl) {
  const $ = cheerio.load(html);
  const sessions = [];
  const seen = new Set();

  $('a[href]').each((_, el) => {
    const href = $(el).attr('href');
    if (!href) return;
    if (!href.includes('appsmotsv2')) return;

    let fullUrl;
    try {
      fullUrl = new URL(href, monthPageUrl).toString();
    } catch { return; }

    if (seen.has(fullUrl)) return;
    seen.add(fullUrl);

    // Determine type from ?x= param: a = appeals, m = motions
    const xParam = new URL(fullUrl).searchParams.get('x') || '';
    const sessionType = xParam.startsWith('a') ? 'decision' : 'motion_order';
    const dateStr = xParam.slice(1); // e.g. "20260106"

    sessions.push({ url: fullUrl, sessionType, dateStr });
  });

  return sessions;
}

/**
 * Level 3: Parse a session page (requires JS-rendered HTML).
 * Returns array of document objects.
 */
function extractDocuments(html, sessionUrl, sessionType, dateStr) {
  const $ = cheerio.load(html);
  const docs = [];
  const seen = new Set();

  // The table has rows: <td><a href="2026/apps/20260106/CASE.pdf">Case Name</a></td><td>CASE_NUM</td>
  $('table#ml tbody tr').each((_, row) => {
    const cells = $(row).find('td');
    if (cells.length < 1) return;

    const link = $(cells[0]).find('a');
    const href = link.attr('href');
    const caseTitle = link.text().trim();
    const caseNumRaw = cells.length >= 2 ? $(cells[1]).text().trim() : null;

    if (!href || !href.includes('.pdf')) return;
    if (seen.has(href)) return;
    seen.add(href);

    // Resolve PDF URL relative to APPSMOT_BASE
    let pdfUrl;
    try {
      pdfUrl = new URL(href, APPSMOT_BASE).toString();
    } catch { return; }

    // ID extraction — priority order:
    // 1. Standard case number: YYYY-XXXXX (e.g. 2025-03310)
    // 2. Motion number: M-XXXX (e.g. M-0031)
    // 3. null → caller will assign UNKNOWN + unique suffix
    const searchStr = (caseNumRaw || '') + ' ' + href;
    const caseMatch = searchStr.match(/\b(\d{4}-\d{4,6})\b/);
    const motionMatch = searchStr.match(/\b(M-\d{3,})\b/i);
    let caseNumber = null;
    if (caseMatch) {
      caseNumber = caseMatch[1];
    } else if (motionMatch) {
      caseNumber = motionMatch[1].toUpperCase();
    }

    // Date from dateStr YYYYMMDD
    const date = dateStr
      ? `${dateStr.slice(0,4)}-${dateStr.slice(4,6)}-${dateStr.slice(6,8)}`
      : null;

    const year = date ? parseInt(date.slice(0,4)) : new Date().getFullYear();

    docs.push({
      url: pdfUrl,
      caseNumber,
      caseTitle,
      docType: sessionType,
      date,
      year,
      rawHref: href,
    });
  });

  return docs;
}

module.exports = { extractMonthLinks, extractSessionLinks, extractDocuments };
