const puppeteer = require('puppeteer-core');
const path = require('path');
const fs = require('fs');

async function capture() {
  const assetsDir = path.join(__dirname, '..', 'docs', 'screenshots');
  if (!fs.existsSync(assetsDir)) fs.mkdirSync(assetsDir, { recursive: true });

  const browser = await puppeteer.launch({
    executablePath: 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox'],
    defaultViewport: { width: 1440, height: 900, deviceScaleFactor: 2 }
  });

  const page = await browser.newPage();

  const pagesToCapture = [
    { url: 'http://127.0.0.1:5000/', file: '01_main_dashboard.png' },
    { url: 'http://127.0.0.1:5000/tracking', file: '02_shipment_tracking.png' },
    { url: 'http://127.0.0.1:5000/inventory', file: '03_inventory_management.png' },
    { url: 'http://127.0.0.1:5000/network', file: '04_network_graph_routing.png' },
  ];

  for (const item of pagesToCapture) {
    try {
      console.log(`Navigating to ${item.url}...`);
      await page.goto(item.url, { waitUntil: 'networkidle0', timeout: 15000 });
      await new Promise(r => setTimeout(r, 1500));
      await page.screenshot({ path: path.join(assetsDir, item.file) });
      console.log(`Captured ${item.file}`);
    } catch (e) {
      console.error(`Failed ${item.url}:`, e.message);
    }
  }

  await browser.close();
}

capture().catch(err => {
  console.error('Screenshot capture failed:', err);
  process.exit(1);
});
