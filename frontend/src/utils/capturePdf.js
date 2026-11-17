// Saves part of the page as a PDF that looks like the page itself, without the print dialog.
// The element is drawn with html2canvas (already installed as jsPDF's optional dependency), cut into A4 pages
// without splitting a card in two where it can be avoided, and downloaded. Both libraries load on first use.
// The text in the PDF is part of a picture, so it cannot be selected or searched.

const PAGE_W = 210; // A4 portrait, mm
const PAGE_H = 297;
const MARGIN_X = 8;
const MARGIN_TOP = 8;
const MARGIN_BOTTOM = 12; // room for the page number
const MAX_CANVAS_PX = 14000; // browsers refuse canvases much taller than this

// Elements that sit inside another matching element are dropped: only the outer card has to stay in one piece.
function outerRanges(elements, originTop, scale) {
  const rects = elements.map((el) => {
    const r = el.getBoundingClientRect();
    return { el, top: (r.top - originTop) * scale, bottom: (r.bottom - originTop) * scale };
  });
  return rects
    .filter((a) => !rects.some((b) => b !== a && b.el.contains(a.el)))
    .sort((a, b) => a.top - b.top);
}

/**
 * Pages as [startPx, endPx] slices of a canvas `height` px tall. A slice moves its end up to the top of the block it
 * would cut through, when that block fits on one page and the move still leaves the slice at least a fifth full.
 */
export function pageSlices(height, pageHeight, ranges) {
  const slices = [];
  let start = 0;
  while (start < height) {
    let end = Math.min(start + pageHeight, height);
    if (end < height) {
      const cut = ranges.find((r) => r.top < end && r.bottom > end);
      if (cut && cut.bottom - cut.top <= pageHeight && cut.top - start >= pageHeight * 0.2) end = Math.floor(cut.top);
    }
    slices.push([start, end]);
    start = end;
  }
  return slices;
}

/**
 * Draws `element` and returns the jsPDF document.
 * options: hide, unclip, keepTogether (CSS selectors); windowWidth (px the page is laid out at);
 *          footer(pageNumber, pageCount) -> text for the bottom of each page.
 */
export async function buildPdfFromElement(element, { hide = [], unclip = [], keepTogether = [], windowWidth = 1100, footer } = {}) {
  const [{ default: html2canvas }, { jsPDF }] = await Promise.all([import('html2canvas'), import('jspdf')]);
  const scale = Math.max(1, Math.min(2, MAX_CANVAS_PX / Math.max(element.scrollHeight, 1)));
  let ranges = [];

  const canvas = await html2canvas(element, {
    scale,
    windowWidth,
    backgroundColor: '#ffffff',
    useCORS: true,
    logging: false,
    onclone: (clonedDocument, clonedElement) => {
      // Applied to the copy html2canvas draws, never to the live page.
      const style = clonedDocument.createElement('style');
      style.textContent = [
        hide.length ? `${hide.join(',')} { display: none !important; }` : '',
        unclip.length ? `${unclip.join(',')} { overflow: visible !important; max-height: none !important; }` : '',
        '* { animation: none !important; transition: none !important; }',
      ].join('\n');
      clonedDocument.head.appendChild(style);
      // An element parked offscreen is measured where it sits, so bring the copy back into the page.
      ['position', 'left', 'top'].forEach((property) => clonedElement.style.setProperty(property, property === 'position' ? 'static' : 'auto', 'important'));
      const origin = clonedElement.getBoundingClientRect().top;
      ranges = outerRanges([...clonedElement.querySelectorAll(keepTogether.join(',') || ':scope > *')], origin, scale);
    },
  });

  const doc = new jsPDF({ orientation: 'portrait', unit: 'mm', format: 'a4' });
  const contentW = PAGE_W - MARGIN_X * 2;
  const mmPerPx = contentW / canvas.width;
  const pageHeightPx = Math.floor((PAGE_H - MARGIN_TOP - MARGIN_BOTTOM) / mmPerPx);
  const slices = pageSlices(canvas.height, pageHeightPx, ranges);

  slices.forEach(([start, end], index) => {
    if (index) doc.addPage();
    const part = document.createElement('canvas');
    part.width = canvas.width;
    part.height = end - start;
    const context = part.getContext('2d');
    context.fillStyle = '#ffffff';
    context.fillRect(0, 0, part.width, part.height);
    context.drawImage(canvas, 0, start, canvas.width, end - start, 0, 0, canvas.width, end - start);
    doc.addImage(part.toDataURL('image/jpeg', 0.95), 'JPEG', MARGIN_X, MARGIN_TOP, contentW, part.height * mmPerPx);
    if (footer) {
      doc.setTextColor(130, 130, 130).setFont('helvetica', 'normal').setFontSize(8);
      doc.text(footer(index + 1, slices.length), PAGE_W / 2, PAGE_H - 6, { align: 'center' });
    }
  });
  return doc;
}

function download(blob, name) {
  const url = URL.createObjectURL(blob);
  const link = Object.assign(document.createElement('a'), { href: url, download: name });
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

export async function savePageAsPdf(element, fileName, options) {
  const doc = await buildPdfFromElement(element, options);
  download(doc.output('blob'), fileName);
}
