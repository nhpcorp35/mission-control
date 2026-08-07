// Quick sanity check — doesn't fetch anything, just validates setup
'use strict';
require('dotenv').config({ path: '/home/node/.openclaw/.env' });

const config = require('./config');
const tracker = require('./tracker');
const { buildFilename, buildStoragePath, scraperUrl } = require('./utils');

console.log('✅ Config loaded');
console.log('  API key present:', !!config.scraperApiKey);
console.log('  Active court:', config.activeCourt);
console.log('  Base path:', config.baseStoragePath);
console.log('');
console.log('✅ Tracker status:', tracker.status());
console.log('');

const filename = buildFilename('2024-00171', 'motion_order', '2025-01-07');
const dir = buildStoragePath('NY_Appellate_AD1', 2025, '2024-00171');
console.log('✅ Sample filename:', filename);
console.log('  Storage dir:', dir);
console.log('');

const sampleUrl = scraperUrl('https://www.nycourts.gov/courts/ad1/example.pdf');
console.log('✅ Sample ScraperAPI URL (truncated):', sampleUrl.slice(0, 80) + '...');
console.log('');
console.log('All good. Ready to run: node scraper.js [AD1|AD2|AD3|AD4]');
