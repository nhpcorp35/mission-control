// tracker.js — download tracking, rate limiting, deduplication
'use strict';

const fs = require('fs');
const path = require('path');
const config = require('./config');
const { todayYMD } = require('./utils');

const TRACKER_PATH = path.join(
  '/home/node/.openclaw/workspace/Legal-AI',
  'download_tracker.json'
);

class Tracker {
  constructor() {
    this.data = this._load();
    this._resetIfNewDay();
  }

  _load() {
    if (fs.existsSync(TRACKER_PATH)) {
      try {
        return JSON.parse(fs.readFileSync(TRACKER_PATH, 'utf8'));
      } catch (e) {
        console.warn('Tracker file corrupt, starting fresh.');
      }
    }
    return this._fresh();
  }

  _fresh() {
    return {
      date: todayYMD(),
      dailyCount: 0,
      hourlyBuckets: {}, // key: YYYY-MM-DD-HH, value: count
      downloadedUrls: [],  // dedup list
      downloadedFiles: [], // dedup by path
      consecutiveErrors: 0,
      lastErrorTime: null,
      metadata: [],        // full record of each download
    };
  }

  _resetIfNewDay() {
    const today = todayYMD();
    if (this.data.date !== today) {
      console.log(`📅 New day (${today}) — resetting daily counters.`);
      const preserved = {
        downloadedUrls: this.data.downloadedUrls,
        downloadedFiles: this.data.downloadedFiles,
        metadata: this.data.metadata,
      };
      this.data = { ...this._fresh(), ...preserved };
      this.save();
    }
  }

  save() {
    fs.writeFileSync(TRACKER_PATH, JSON.stringify(this.data, null, 2));
  }

  // ─── Limits ────────────────────────────────────────────────────────────────

  _hourKey() {
    const now = new Date();
    return `${now.toISOString().slice(0, 13)}`; // YYYY-MM-DDTHH
  }

  hourlyCount() {
    return this.data.hourlyBuckets[this._hourKey()] || 0;
  }

  canDownload() {
    this._resetIfNewDay();
    if (this.data.dailyCount >= config.maxFilesPerDay) {
      console.log(`🛑 Daily limit (${config.maxFilesPerDay}) reached. Stop for today.`);
      return false;
    }
    if (this.hourlyCount() >= config.maxFilesPerHour) {
      console.log(`🛑 Hourly limit (${config.maxFilesPerHour}) reached. Pause.`);
      return false;
    }
    return true;
  }

  // ─── Dedup ─────────────────────────────────────────────────────────────────

  hasUrl(url) {
    return this.data.downloadedUrls.includes(url);
  }

  hasFile(filePath) {
    return this.data.downloadedFiles.includes(filePath);
  }

  // ─── Record ────────────────────────────────────────────────────────────────

  record(meta) {
    const hourKey = this._hourKey();
    this.data.dailyCount++;
    this.data.hourlyBuckets[hourKey] = (this.data.hourlyBuckets[hourKey] || 0) + 1;
    this.data.downloadedUrls.push(meta.source_url);
    this.data.downloadedFiles.push(meta.storage_path);
    this.data.metadata.push(meta);
    this.data.consecutiveErrors = 0;
    this.save();
    console.log(`  ✅ Recorded [daily: ${this.data.dailyCount}/${config.maxFilesPerDay}, hourly: ${this.hourlyCount()}/${config.maxFilesPerHour}]`);
  }

  recordError(err) {
    this.data.consecutiveErrors++;
    this.data.lastErrorTime = new Date().toISOString();
    this.save();
    console.log(`  ❌ Error recorded (consecutive: ${this.data.consecutiveErrors})`);
    if (err) console.log(`     ${err}`);
  }

  shouldBackoff() {
    return this.data.consecutiveErrors >= config.errorThreshold;
  }

  resetErrors() {
    this.data.consecutiveErrors = 0;
    this.save();
  }

  // ─── Status ────────────────────────────────────────────────────────────────

  status() {
    return {
      date: this.data.date,
      dailyCount: this.data.dailyCount,
      hourlyCount: this.hourlyCount(),
      totalDownloaded: this.data.downloadedUrls.length,
      consecutiveErrors: this.data.consecutiveErrors,
    };
  }

  getMetadata() {
    return this.data.metadata;
  }
}

module.exports = new Tracker(); // singleton
