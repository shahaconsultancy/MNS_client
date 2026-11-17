// Saves part of the page as a PDF that looks like the page itself, without the print dialog.
// The element is drawn with html2canvas (already installed as jsPDF's optional dependency), cut into A4 pages
// without splitting a card where it can be avoided and never through a row or a line of text, and downloaded.
// Both libraries load on first use. The text in the PDF is part of a picture, so it cannot be selected or searched.

const PAGE_W = 210; // A4 portrait, mm
const PAGE_H = 297;
const MARGIN_X = 8;
const MARGIN_TOP = 8;
const MARGIN_BOTTOM = 12; // room for the page number
const MAX_CANVAS_PX = 14000; // browsers refuse canvases much taller than this

// Whatever must never be cut in half, wherever it appears: rows, list and table rows, pictures, and every element
// that holds text directly (a line or paragraph of text). New sections are covered without being listed here.
const UNBREAKABLE = 'tr, li, img, svg, canvas, [class*="__row"], [class*="__cell"], [class*="__item"], [class*="__stat"]';
// Smallest share of a page a slice keeps when its end moves up, so a page is never left almost empty.
const MIN_FILL_KEEP = 0.75; // a card moves whole to the next page only if this page stays three-quarters full;
// otherwise it continues on the next page, split between rows (never through a row or a line of text)
const MIN_FILL_ATOM = 0.5; // for not cutting a row or a line of text: the end moves up at most half a page
// Only short elements count as rows or lines of text. Taller ones are sections, which may be split between rows.
const MAX_ATOM_SHARE = 1 / 3;
// Neighbouring rows meet at fractional pixels. Within this many pixels an edge counts as touching, not cut,
// so a moved page end never slips into the row above and drags the break upward row after row.
const EDGE_PX = 2;
const cuts = (r, y) => r.top < y - EDGE_PX && r.bottom > y + EDGE_PX;

function rangesOf(elements, originTop, scale) {
  return elements
    .map((el) => {
      const r = el.getBoundingClientRect();
      return { top: (r.top - originTop) * scale, bottom: (r.bottom - originTop) * scale };
    })
    .filter((r) => r.bottom > r.top)
    .sort((a, b) => a.top - b.top);
}

function holdsText(el) {
  for (const node of el.childNodes) {
    if (node.nodeType === 3 && node.textContent.trim()) return true;
  }
  return false;
}

/**
 * Pages as [startPx, endPx] slices of a canvas `height` px tall.
 *  1. keep: blocks (cards) are kept whole when they fit on one page. Of the blocks a page end would cut, the
 *     outermost one that fits moves the end up to its top, if the page stays at least three-quarters full.
 *  2. atoms: rows and lines of text are never cut. While the end falls inside one, it moves up to the top of
 *     the highest one it cuts. Only a single atom taller than a page (a huge picture) can still be cut.
 */
export function pageSlices(height, pageHeight, keep = [], atoms = []) {
  const slices = [];
  let start = 0;
  while (start < height) {
    let end = Math.min(start + pageHeight, height);
    if (end < height) {
      const fits = keep.filter((r) => cuts(r, end) && r.bottom - r.top <= pageHeight && r.top - start >= pageHeight * MIN_FILL_KEEP);
      if (fits.length) end = Math.floor(Math.min(...fits.map((r) => r.top)));
      for (let guard = 0; guard < 50; guard += 1) {
        const cut = atoms.filter((r) => cuts(r, end) && r.bottom - r.top <= pageHeight * MAX_ATOM_SHARE && r.top - start >= pageHeight * MIN_FILL_ATOM);
        if (!cut.length) break;
        end = Math.floor(Math.min(...cut.map((r) => r.top)));
      }
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
  let keep = [];
  let atoms = [];

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
      keep = rangesOf([...clonedElement.querySelectorAll(keepTogether.join(',') || ':scope > *')], origin, scale);
      const visible = [...clonedElement.querySelectorAll('*')].filter((el) => el.getClientRects().length);
      atoms = rangesOf(visible.filter((el) => el.matches(UNBREAKABLE) || holdsText(el)), origin, scale);
    },
  });

  const doc = new jsPDF({ orientation: 'portrait', unit: 'mm', format: 'a4' });
  const contentW = PAGE_W - MARGIN_X * 2;
  const mmPerPx = contentW / canvas.width;
  const pageHeightPx = Math.floor((PAGE_H - MARGIN_TOP - MARGIN_BOTTOM) / mmPerPx);
  const slices = pageSlices(canvas.height, pageHeightPx, keep, atoms);

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
