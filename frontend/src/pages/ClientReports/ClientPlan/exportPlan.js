// Writes the report built by reportData.js to an Excel workbook. The PDF is written by exportPlanPdf.js. Libraries load on first use.
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

// The PDF has its own layout and file.
export { exportPdf } from './exportPlanPdf.js';
