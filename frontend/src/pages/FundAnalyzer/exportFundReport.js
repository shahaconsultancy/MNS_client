// Saves the current Fund Analyzer view as a PDF. The PDF libraries load on first use.

const NAVY = [11, 31, 61];
const GOLD = [194, 144, 53];
const SOURCE = { official_amc: 'Official AMC', charlie_alpha: 'Third-party API', mixed: 'Official AMC + Third-party API' };

// Helvetica in jsPDF has no rupee sign, arrows or typographic minus, so text is kept to plain characters.
const plain = (text) => String(text ?? '').replace(/₹/g, 'Rs. ').replace(/[−–—]/g, '-').replace(/↔/g, '<->').replace(/[·•]/g, '|').replace(/[‘’]/g, "'").replace(/[“”]/g, '"').replace(/[^\x20-\x7E]/g, '');
const dash = '-';
const pct = (n, digits = 2) => (n == null ? dash : `${(n * 100).toFixed(digits)}%`);
const signed = (n) => (n == null ? dash : `${n > 0 ? '+' : ''}${(n * 100).toFixed(2)}%`);
const points = (n) => (n == null ? dash : `${Number(n).toFixed(2)}%`);
const rupees = (n) => (n == null ? dash : `Rs. ${Number(n).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 4 })}`);
const crore = (lakhs) => (lakhs == null ? dash : `Rs. ${Number(lakhs / 100).toLocaleString('en-IN', { maximumFractionDigits: 0 })} Cr`);
const month = (value) => (value ? new Date(`${value}-01T00:00:00Z`).toLocaleString('en-GB', { month: 'long', year: 'numeric', timeZone: 'UTC' }) : dash);
const shortName = (name) => plain(name).split(' - ')[0];

function download(blob, name) {
  const url = URL.createObjectURL(blob);
  const link = Object.assign(document.createElement('a'), { href: url, download: name });
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

export async function exportFundReport({ funds, overlap, overlapLoading, fundMetadata, fundamentalsLoading, source, asOfDate, riskFreeRate }) {
  const [{ jsPDF }, { default: autoTable }] = await Promise.all([import('jspdf'), import('jspdf-autotable')]);
  const doc = new jsPDF({ orientation: 'landscape', unit: 'mm', format: 'a4' });
  const page = { w: doc.internal.pageSize.getWidth(), h: doc.internal.pageSize.getHeight() };
  const margin = 12;
  const now = new Date();
  const generated = now.toLocaleString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
  let y = 36;

  doc.setFillColor(...NAVY).rect(0, 0, page.w, 26, 'F');
  doc.setTextColor(255).setFont('helvetica', 'bold').setFontSize(18).text('Fund Analyzer Report', margin, 12);
  doc.setFont('helvetica', 'normal').setFontSize(10).text(plain(`Generated ${generated}  |  NAV data: ${source || 'AMFI'} as of ${asOfDate || dash}  |  ${funds.length} funds`), margin, 20);

  const section = (title) => {
    if (y > page.h - 40) { doc.addPage(); y = margin + 4; }
    doc.setTextColor(...NAVY).setFont('helvetica', 'bold').setFontSize(13).text(title, margin, y);
    doc.setDrawColor(...GOLD).setLineWidth(0.6).line(margin, y + 2, margin + 24, y + 2);
    y += 6;
  };
  const note = (text) => {
    if (y > page.h - 20) { doc.addPage(); y = margin + 4; }
    const lines = doc.setTextColor(90).setFont('helvetica', 'normal').setFontSize(8.5).splitTextToSize(plain(text), page.w - margin * 2);
    doc.text(lines, margin, y + 3);
    y += lines.length * 4 + 3;
  };
  const table = (head, body, right = []) => {
    autoTable(doc, {
      startY: y,
      margin: { left: margin, right: margin, bottom: 16 },
      head: [head],
      body: body.map((row) => row.map(plain)),
      styles: { font: 'helvetica', fontSize: 8.5, cellPadding: 2, textColor: 40, lineColor: [225, 221, 210], lineWidth: 0.1 },
      headStyles: { fillColor: NAVY, textColor: 255, fontStyle: 'bold' },
      columnStyles: Object.fromEntries(right.map((index) => [index, { halign: 'right' }])),
    });
    y = doc.lastAutoTable.finalY + 8;
  };

  section('Selected funds');
  table(['#', 'Scheme', 'Code', 'AMC', 'Category'], funds.map((fund, index) => [String(index + 1), fund.scheme_name, fund.scheme_code, fund.amc || dash, fund.category || dash]));

  section('Returns and risk');
  table(
    ['Fund', 'NAV', 'NAV date', '1M', '3M', '6M', '1Y', '3Y CAGR', '5Y CAGR', 'Volatility (3Y)', 'Sharpe', 'Max DD (3Y)', 'TER'],
    funds.map((fund) => {
      const m = fund.metrics || {};
      const ter = fundMetadata[fund.scheme_code]?.ter ?? fund.ter;
      return [shortName(fund.scheme_name), rupees(fund.nav), fund.nav_date || dash, signed(m.r1m), signed(m.r3m), signed(m.r6m), signed(m.r1y), signed(m.r3y), signed(m.r5y), pct(m.volatility), m.sharpe == null ? dash : m.sharpe.toFixed(2), signed(m.maxDrawdown), ter == null ? dash : `${ter.toFixed(2)}%`];
    }),
    [1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
  );
  note(`Returns above 1 year are annualised (CAGR). Volatility uses 3 years of daily NAV. Sharpe = (3Y CAGR - ${(riskFreeRate * 100).toFixed(2)}% risk-free rate) / 3Y volatility. TER is the latest AMFI-disclosed total.`);

  section('Holdings overlap');
  if (overlapLoading) {
    note('Overlap was still being calculated when this report was saved.');
  } else if (!overlap?.pairs?.length) {
    note(overlap?.highest_overlap?.message || 'Portfolio holdings unavailable for a matching reporting month.');
  } else {
    const high = overlap.highest_overlap || {};
    note(`Highest overlap: ${points(high.percentage)} between ${shortName(high.fund_a_name)} and ${shortName(high.fund_b_name)}, portfolio ${month(high.portfolio_month)}. Pairs compared: ${overlap.compared_pairs ?? overlap.pairs.length} of ${overlap.total_pairs ?? overlap.pairs.length}.${overlap.pairs_different_months ? ' Pairs use different reporting months.' : ''}`);
    const pairs = [...overlap.pairs].sort((a, b) => b.overlap_pct - a.overlap_pct);
    table(['Fund A', 'Fund B', 'Portfolio month', 'Overlap', 'Common securities', 'Holdings source'],
      pairs.map((pair) => [shortName(pair.left.scheme_name), shortName(pair.right.scheme_name), month(pair.portfolio_month), points(pair.overlap_pct), String(pair.common_count ?? pair.common_holdings?.length ?? dash), SOURCE[pair.holdings_source] || 'Official AMC']),
      [3, 4]);
    const names = Object.fromEntries(funds.map((fund) => [fund.scheme_code, shortName(fund.scheme_name)]));
    const missing = (overlap.pair_reasons || []).map((gap) => `${names[gap.left] || gap.left} / ${names[gap.right] || gap.right}: ${gap.message}`);
    if (missing.length) note(`Not compared: ${missing.join('; ')}.`);
  }
  note('Overlap is the sum, over securities both funds hold (matched by ISIN), of the smaller of the two weights, each as a percentage of the fund\'s net assets.');

  const withMeta = funds.filter((fund) => fundMetadata[fund.scheme_code]);
  section('Fundamentals');
  if (!withMeta.length) {
    note(fundamentalsLoading ? 'Fundamentals were still loading when this report was saved.' : 'Fundamentals are not available for these funds.');
  } else {
    table(['Fund', 'AUM', 'Expense ratio', 'Riskometer', 'Benchmark', 'Portfolio date', 'Holdings source'],
      withMeta.map((fund) => {
        const meta = fundMetadata[fund.scheme_code];
        return [shortName(fund.scheme_name), crore(meta.aum), meta.ter == null ? dash : `${meta.ter.toFixed(2)}%`, meta.riskometer?.level || 'Not available', meta.benchmark || dash, meta.holdings_as_of || dash, SOURCE[meta.holdings_source] || (meta.holdings?.length ? 'Official AMC' : dash)];
      }),
      [1, 2]);
    withMeta.forEach((fund) => {
      const holdings = (fundMetadata[fund.scheme_code].holdings || []).slice(0, 10);
      if (!holdings.length) return;
      if (y > page.h - 50) { doc.addPage(); y = margin + 4; }
      doc.setTextColor(60).setFont('helvetica', 'bold').setFontSize(10).text(plain(`Top holdings: ${shortName(fund.scheme_name)}`), margin, y);
      y += 2;
      table(['#', 'Holding', 'Type', 'Weight'], holdings.map((holding, index) => [String(index + 1), holding.name, holding.instrument_type || holding.sector || dash, `${holding.weight.toFixed(2)}%`]), [3]);
    });
  }

  const pages = doc.getNumberOfPages();
  for (let i = 1; i <= pages; i += 1) {
    doc.setPage(i);
    doc.setTextColor(120).setFont('helvetica', 'normal').setFontSize(7.5);
    doc.text('Mutual fund investments are subject to market risks; read all scheme-related documents carefully. Past performance may or may not be sustained in future.', margin, page.h - 6);
    doc.text(`Page ${i} of ${pages}`, page.w - margin, page.h - 6, { align: 'right' });
  }

  const stamp = now.toISOString().slice(0, 10);
  download(doc.output('blob'), `Fund Analyzer Report - ${stamp}.pdf`);
}
