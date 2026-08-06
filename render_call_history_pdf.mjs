import { pathToFileURL } from "node:url";
import path from "node:path";
import { chromium } from "playwright";

const [htmlInput, pdfOutput] = process.argv.slice(2);

if (!htmlInput || !pdfOutput) {
  console.error("usage: node render_call_history_pdf.mjs <call_history.html> <call_history.pdf>");
  process.exit(2);
}

const htmlPath = path.resolve(htmlInput);
const pdfPath = path.resolve(pdfOutput);

const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({
    viewport: { width: 816, height: 1056 },
    deviceScaleFactor: 1,
  });
  page.setDefaultTimeout(60000);
  await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "networkidle" });
  await page.emulateMedia({ media: "print" });
  await page.pdf({
    path: pdfPath,
    width: "8.5in",
    height: "11in",
    printBackground: true,
    preferCSSPageSize: true,
    margin: { top: "0", right: "0", bottom: "0", left: "0" },
  });
  console.log(`Rendered PDF: ${pdfPath}`);
} finally {
  await browser.close();
}
