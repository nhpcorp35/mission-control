// utils.js — shared helpers
'use strict';

const fs = require('fs');
const path = require('path');
const config = require('./config');

// ─── Delay helpers ──────────────────────────────────────────────────────────

function randomInt(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function randomDelay() {
  const ms = randomInt(config.minDelayMs, config.maxDelayMs);
  console.log(`  ⏱  Waiting ${(ms/1000).toFixed(1)}s...`);
  await sleep(ms);
}

async function longPause() {
  const ms = randomInt(config.longPauseMs[0], config.longPauseMs[1]);
  console.log(`  🛑 Long pause: ${(ms/1000).toFixed(0)}s...`);
  await sleep(ms);
}

async function backoffPause() {
  const ms = randomInt(config.backoffOnErrorMs[0], config.backoffOnErrorMs[1]);
  console.log(`  ⚠️  Backoff pause: ${(ms/60000).toFixed(1)} minutes...`);
  await sleep(ms);
}

// ─── Date helpers ────────────────────────────────────────────────────────────

function toYMD(dateStr) {
  if (!dateStr) return 'unknown-date';
  // Try to parse various formats
  const d = new Date(dateStr);
  if (isNaN(d.getTime())) return 'unknown-date';
  return d.toISOString().slice(0, 10);
}

function todayYMD() {
  return new Date().toISOString().slice(0, 10);
}

// ─── File naming ─────────────────────────────────────────────────────────────

function sanitize(str) {
  if (!str) return 'UNKNOWN';
  return str.replace(/[^a-zA-Z0-9\-_]/g, '_');
}

function buildFilename(caseNumber, docType, date, uniqueSuffix) {
  let cn;
  if (caseNumber) {
    cn = sanitize(caseNumber);
  } else {
    // No ID found — use UNKNOWN + short unique suffix to prevent collisions
    const suffix = uniqueSuffix || Math.random().toString(36).slice(2, 7).toUpperCase();
    cn = `UNKNOWN_${suffix}`;
  }
  const dt = docType ? docType.toLowerCase().replace(/\s+/g, '_') : 'document';
  const d = toYMD(date);
  return `${cn}__${dt}__${d}.pdf`;
}

function buildStoragePath(courtName, year, caseNumber) {
  const cn = caseNumber || 'unknown';
  return path.join(config.baseStoragePath, courtName, String(year), cn);
}

// ─── Deduplication ───────────────────────────────────────────────────────────

function fileExists(filePath) {
  return fs.existsSync(filePath);
}

function ensureDir(dirPath) {
  fs.mkdirSync(dirPath, { recursive: true });
}

// ─── Doc type detection ───────────────────────────────────────────────────────

function detectDocType(linkText, url) {
  const text = (linkText + ' ' + url).toLowerCase();
  if (text.includes('decision')) return 'decision';
  if (text.includes('motion') || text.includes('order')) return 'motion_order';
  if (text.includes('brief')) return 'brief';
  if (text.includes('affirmation')) return 'affirmation';
  if (text.includes('affidavit')) return 'affidavit';
  return 'document';
}

function isPriority(docType) {
  return config.priorityTypes.includes(docType);
}

function isSecondary(docType) {
  return config.secondaryTypes.includes(docType);
}

function shouldSkip(linkText, url) {
  const text = (linkText + ' ' + url).toLowerCase();
  return config.skipKeywords.some(kw => text.includes(kw));
}

// ─── ScraperAPI URL builder ───────────────────────────────────────────────────

function scraperUrl(targetUrl, opts = {}) {
  const params = new URLSearchParams({
    api_key: config.scraperApiKey,
    url: targetUrl,
    ...opts,
  });
  return `https://api.scraperapi.com/?${params.toString()}`;
}

module.exports = {
  randomInt, sleep, randomDelay, longPause, backoffPause,
  toYMD, todayYMD,
  sanitize, buildFilename, buildStoragePath,
  fileExists, ensureDir,
  detectDocType, isPriority, isSecondary, shouldSkip,
  scraperUrl,
};
