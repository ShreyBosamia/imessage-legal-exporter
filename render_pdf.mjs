import { pathToFileURL } from "node:url";
import path from "node:path";
import { chromium } from "playwright";

const [htmlInput, pdfOutput] = process.argv.slice(2);

if (!htmlInput || !pdfOutput) {
  console.error("usage: node render_pdf.mjs <thread.html> <thread.pdf>");
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
  const diagnostics = [];
  page.on("console", (message) => diagnostics.push(`console ${message.type()}: ${message.text()}`));
  page.on("pageerror", (error) => diagnostics.push(`pageerror: ${error.message}`));
  page.setDefaultTimeout(180000);
  await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "networkidle" });
  try {
    await page.waitForFunction(
      () => document.getElementById("transcript")?.dataset.paginated === "true",
      null,
      { timeout: 180000 }
    );
  } catch (error) {
    const state = await page.evaluate(() => {
      const transcript = document.getElementById("transcript");
      return {
        paginated: transcript?.dataset.paginated ?? null,
        pages: document.querySelectorAll(".page").length,
        blocksRemaining: document.querySelectorAll(
          "#message-source .message"
        ).length,
        renderedBlocks: document.querySelectorAll(
          ".page .message"
        ).length,
      };
    });
    console.error(JSON.stringify({ error: error.message, state, diagnostics }, null, 2));
    throw error;
  }
  const stats = await page.evaluate(() => {
    const blockSelector = ".message";
    const pageBlockCounts = Array.from(document.querySelectorAll(".page")).map(
      (node) => node.querySelectorAll(blockSelector).length
    );
    const sequences = Array.from(document.querySelectorAll(".page .message")).map(
      (node) => node.dataset.sequence
    );
    return {
      renderedBlocks: document.querySelectorAll(".page .message").length,
      renderedMessages: document.querySelectorAll(".page .message").length,
      renderedExhibits: document.querySelectorAll(".attachment-exhibit").length,
      uniqueSequences: new Set(sequences).size,
      pages: pageBlockCounts.length,
      maxBlocksOnPage: Math.max(0, ...pageBlockCounts),
      emptyPages: pageBlockCounts.filter((count) => count === 0).length,
      singleBlockPages: pageBlockCounts.filter((count) => count === 1).length,
      continuationBlocks: document.querySelectorAll(".message[data-continuation-part]").length,
      oversizedBlocks: document.querySelectorAll("[data-oversized='true']").length,
    };
  });
  console.log(`Pagination stats: ${JSON.stringify(stats)}`);
  if (stats.emptyPages > 0 || stats.oversizedBlocks > 0) {
    throw new Error(`pagination audit failed: ${JSON.stringify(stats)}`);
  }
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
