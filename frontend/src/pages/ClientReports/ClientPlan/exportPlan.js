// Writes the report built by reportData.js to an Excel workbook or a PDF. Both libraries load on first use.
import { buildReport } from './reportData.js';

const NAVY = '0B1F3D';
const SAND = 'F3EFE6';

// { v, fmt } of a cell: a number takes its column's format unless the cell carries its own.
function resolve(cell, colFmt) {
  const own = cell !== null && typeof cell === 'object';
  const v = own ? cell.v : cell;
  const fmt = own ? cell.fmt : typeof v === 'number' ? colFmt : 'text';
  return { v, fmt: typeof v === 'number' ? fmt : 'text' };
}

const grouped = (v) => Math.round(Math.abs(v)).toLocaleString('en-IN');

// Text for the PDF. Helvetica has no rupee sign, so amounts read "Rs. 1,00,000".
function pdfText(cell, colFmt) {
  const { v, fmt } = resolve(cell, colFmt);
  if (typeof v !== 'number') return v == null ? '' : String(v);
  if (fmt === 'inr') return `${v < 0 ? '-' : ''}Rs. ${grouped(v)}`;
  if (fmt === 'pct') return `${v.toFixed(1)}%`;
  return String(v);
}

const fileName = (name, ext) => `${name.replace(/[\\/:*?"<>|]+/g, '').trim() || 'Client'} - Financial Plan.${ext}`;

function download(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = Object.assign(document.createElement('a'), { href: url, download: name });
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

const EXCEL_FORMAT = {
  inr: '[>=10000000]"₹"##\\,##\\,##\\,##0;[>=100000]"₹"##\\,##\\,##0;"₹"#,##0',
  pct: '0.0"%"',
  num: '#,##0',
};

export async function exportExcel(state) {
  const { default: ExcelJS } = await import('exceljs');
  const report = buildReport(state);
  const book = new ExcelJS.Workbook();
  book.creator = 'MNS Financial Services';
  book.created = new Date();

  const border = { top: { style: 'thin', color: { argb: 'FFB9B3A5' } } };

  report.sheets.forEach((sheet) => {
    const ws = book.addWorksheet(sheet.name, { views: [{ showGridLines: false }] });
    const widths = [];
    const fit = (i, text) => { widths[i] = Math.min(Math.max(widths[i] || 12, String(text).length + 3), 46); };

    ws.addRow([report.title]).font = { bold: true, size: 16, color: { argb: `FF${NAVY}` } };
    ws.addRow([`${sheet.name} · ${report.subtitle}`]).font = { color: { argb: 'FF6B7385' } };

    sheet.blocks.forEach((block) => {
      ws.addRow([]);
      ws.addRow([block.title]).font = { bold: true, size: 12, color: { argb: `FF${NAVY}` } };

      const head = ws.addRow(block.cols.map((c) => c.h));
      head.eachCell((cell, i) => {
        cell.font = { bold: true, color: { argb: 'FFFFFFFF' } };
        cell.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: `FF${NAVY}` } };
        cell.alignment = { vertical: 'middle', horizontal: block.cols[i - 1].fmt === 'text' ? 'left' : 'right', wrapText: true };
        fit(i - 1, block.cols[i - 1].h);
      });

      block.rows.forEach((r) => {
        const row = ws.addRow(r.cells.map((cell, i) => resolve(cell, block.cols[i]?.fmt).v));
        row.eachCell({ includeEmpty: true }, (cell, i) => {
          const { v, fmt } = resolve(r.cells[i - 1], block.cols[i - 1]?.fmt);
          if (fmt !== 'text') cell.numFmt = EXCEL_FORMAT[fmt];
          cell.alignment = { vertical: 'top', horizontal: typeof v === 'number' ? 'right' : 'left', wrapText: typeof v === 'string' };
          if (r.total) {
            cell.font = { bold: true };
            cell.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: `FF${SAND}` } };
            cell.border = border;
          }
          if (typeof v === 'string') fit(i - 1, v.length > 60 ? 'x'.repeat(46) : v);
        });
      });
    });

    widths.forEach((w, i) => { ws.getColumn(i + 1).width = w; });
  });

  const buffer = await book.xlsx.writeBuffer();
  download(
    new Blob([buffer], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' }),
    fileName(report.title, 'xlsx'),
  );
}

export async function exportPdf(state) {
  const [{ jsPDF }, { default: autoTable }] = await Promise.all([import('jspdf'), import('jspdf-autotable')]);
  const report = buildReport(state);
  const doc = new jsPDF({ orientation: 'landscape', unit: 'mm', format: 'a4' });
  const page = { w: doc.internal.pageSize.getWidth(), h: doc.internal.pageSize.getHeight() };
  const margin = 12;
  const navy = [11, 31, 61];
  let y = 0;

  // Cover band on the first page.
  doc.setFillColor(...navy);
  doc.rect(0, 0, page.w, 26, 'F');
  doc.setTextColor(255);
  doc.setFont('helvetica', 'bold').setFontSize(18).text(report.title, margin, 12);
  doc.setFont('helvetica', 'normal').setFontSize(10).text(report.subtitle, margin, 20);
  y = 36;

  report.sheets.forEach((sheet) => {
    if (y > page.h - 50) { doc.addPage(); y = margin + 4; }
    doc.setTextColor(...navy).setFont('helvetica', 'bold').setFontSize(14).text(sheet.name, margin, y);
    doc.setDrawColor(194, 144, 53).setLineWidth(0.6).line(margin, y + 2, margin + 24, y + 2);
    y += 8;

    sheet.blocks.forEach((block) => {
      if (y > page.h - 40) { doc.addPage(); y = margin + 4; }
      doc.setTextColor(60).setFont('helvetica', 'bold').setFontSize(10).text(block.title, margin, y);

      autoTable(doc, {
        startY: y + 2,
        margin: { left: margin, right: margin, bottom: 16 },
        head: [block.cols.map((c) => c.h)],
        body: block.rows.map((r) => r.cells.map((cell, i) => {
          const text = pdfText(cell, block.cols[i]?.fmt);
          return { content: text, styles: { halign: resolve(cell, block.cols[i]?.fmt).fmt === 'text' ? 'left' : 'right', ...(r.total ? { fontStyle: 'bold', fillColor: [243, 239, 230] } : {}) } };
        })),
        styles: { font: 'helvetica', fontSize: 8.5, cellPadding: 2, textColor: 40, lineColor: [225, 221, 210], lineWidth: 0.1 },
        headStyles: { fillColor: navy, textColor: 255, fontStyle: 'bold' },
        didParseCell: (d) => {
          if (d.section === 'head') d.cell.styles.halign = block.cols[d.column.index].fmt === 'text' ? 'left' : 'right';
        },
      });
      y = doc.lastAutoTable.finalY + 8;
    });
    y += 2;
  });

  const pages = doc.getNumberOfPages();
  for (let i = 1; i <= pages; i += 1) {
    doc.setPage(i);
    doc.setTextColor(120).setFont('helvetica', 'normal').setFontSize(8);
    doc.text(`${report.title} · ${report.subtitle}`, margin, page.h - 6);
    doc.text(`Page ${i} of ${pages}`, page.w - margin, page.h - 6, { align: 'right' });
  }

  download(doc.output('blob'), fileName(report.title, 'pdf'));
}
