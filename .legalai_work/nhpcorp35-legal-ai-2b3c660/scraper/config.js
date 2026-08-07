// config.js — scraper configuration
'use strict';

module.exports = {
  // ScraperAPI
  scraperApiKey: process.env.SCRAPERAPI_KEY || '',

  // Storage
  baseStoragePath: '/home/node/.openclaw/workspace/Legal-AI/data/raw',

  // Rate limits
  maxFilesPerDay: 500,
  maxFilesPerHour: 50,
  minDelayMs: 3000,    // 3 seconds
  maxDelayMs: 8000,    // 8 seconds
  longPauseEvery: [10, 20],   // random between these two values
  longPauseMs: [30000, 90000], // 30–90 seconds
  backoffOnErrorMs: [600000, 1800000], // 10–30 minutes on repeated errors
  errorThreshold: 2,  // errors before backoff

  // Document priority
  priorityTypes: ['decision', 'motion_order'],
  secondaryTypes: ['brief', 'affirmation', 'affidavit'],
  skipKeywords: ['administrative', 'notice of filing', 'receipt'],

  // NY Appellate Division departments
  courts: {
    AD1: {
      name: 'NY_Appellate_AD1',
      indexUrl: 'https://www.nycourts.gov/courts/AD1/calendar/appsmots/AppMotIndex.shtml',
      appsmotBase: 'https://www.nycourts.gov/courts/AD1/calendar/AppsMots/',
    },
  },

  // Which court to start with
  activeCourt: 'AD1',

  // Test mode: limit total downloads (set to 0 for unlimited)
  testLimit: 100,
};
