// The client's financial plan as a branded PDF: cover page, then the statement tables, goals, retirement,
// insurance gap check and an advisor sign-off. jsPDF loads on first use.
// Amounts read "Rs. 1,00,000" because the built-in PDF fonts have no rupee sign.
import { SOURCES, incomeTotal } from './tabs/IncomeTab.jsx';
import { CATEGORIES, expensesTotal } from './tabs/ExpensesTab.jsx';
import { INVESTMENTS, savingsTotal } from './tabs/SavingsTab.jsx';
import { CLASSES, assetsTotal, investibleAssetsTotal, personalAssetsTotal } from './tabs/AssetsTab.jsx';
import { LIABILITIES, liabilitiesTotal } from './tabs/LiabilitiesTab.jsx';
import { insuranceCalc } from './tabs/InsuranceTab.jsx';
import { goalCalc } from './tabs/GoalsTab.jsx';
import { retirementCalc } from './tabs/RetirementTab.jsx';
import { familyCover } from './tabs/FamilyTab.jsx';
import logoUrl from '../../../assets/images/logo.png';

const COMPANY = 'Money Navigating Solutions';
const TAGLINE = 'THE HUB OF FINANCIAL SERVICES';
const NAVY = [27, 42, 94];
const GOLD = [194, 144, 53];
const SAND = [240, 236, 226];
const GREY = [110, 115, 128];
const LINE = [205, 205, 205];

const PAGE = { w: 210, h: 297 };
const MARGIN = 15;
const CONTENT_TOP = 32; // below the header band
const CONTENT_BOTTOM = 278; // above the footer
const WIDTH = PAGE.w - MARGIN * 2;

const n = (v) => Number(v) || 0;
const share = (part, of) => (of > 0 ? (part / of) * 100 : 0);
const inr = (v) => `${v < 0 ? '-' : ''}Rs. ${Math.round(Math.abs(v)).toLocaleString('en-IN')}`;
const pct = (v) => `${v.toFixed(1)}%`;
const titleCase = (s) => s.replace(/\b\w/g, (c) => c.toUpperCase());
const dmy = (d) => d.toLocaleDateString('en-GB');

// How each expense group is titled in the statement.
const EXPENSE_TITLE = {
  household: 'Household Expenses', lifestyle: 'Lifestyle Expenses', dependents: 'Dependent Expenses',
  insurance: 'Insurance Premiums', loans: 'Loan EMIs',
};
const INVESTMENT_LABEL = { sip: 'Ongoing Investments (MF SIPs)', policies: 'Ongoing Investments (Policies, RDs)', other: 'Ongoing Investments (Others)' };
const LIABILITY_LABEL = {
  home: 'Home Loan - Outstanding', vehicle: 'Vehicle Loan - Outstanding', personal: 'Personal Loan',
  education: 'Education Loan', other: 'Other Loans (Gold Loan etc)', card: 'Credit Card',
};

const fileName = (name, now) => {
  const stamp = (d) => `${String(d.getFullYear()).slice(2)}${String(d.getMonth() + 1).padStart(2, '0')}${String(d.getDate()).padStart(2, '0')}`;
  const time = `${String(now.getHours()).padStart(2, '0')}${String(now.getMinutes()).padStart(2, '0')}${String(now.getSeconds()).padStart(2, '0')}`;
  const base = (name || 'Client').replace(/[\\/:*?"<>|]+/g, '').trim().replace(/\s+/g, '_').toUpperCase() || 'CLIENT';
  return `${base}_${stamp(now)}_${time}.pdf`;
};

function download(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = Object.assign(document.createElement('a'), { href: url, download: name });
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// The logo as a data URL, or null if it cannot be loaded (the PDF is then made without it).
async function loadLogo() {
  try {
    const blob = await (await fetch(logoUrl)).blob();
    return await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = reject;
      reader.readAsDataURL(blob);
    });
  } catch {
    return null;
  }
}

/** Builds the document. `logo` is a PNG data URL or null. Exported for tests; the app calls exportPdf. */
export function buildPlanPdf(state, { jsPDF, autoTable, logo = null, now = new Date() }) {
  const { info, income, expenses, savings, assetData, liabilityData, insurance, family, goals, retirement } = state;
  const clientName = info.clientName || 'Client';
  const planName = state.planName || clientName;

  const incomeAmount = incomeTotal(income);
  const expensesAmount = expensesTotal(expenses);
  const investedAmount = savingsTotal(savings);
  const assets = assetsTotal(assetData);
  const liabilities = liabilitiesTotal(liabilityData);
  const netWorth = assets - liabilities;
  const personal = personalAssetsTotal(assetData);

  const doc = new jsPDF({ orientation: 'portrait', unit: 'mm', format: 'a4' });
  const fill = (c) => doc.setFillColor(...c);
  const ink = (c) => doc.setTextColor(...c);
  const stroke = (c) => doc.setDrawColor(...c);

  // ---------------------------------------------------------------- cover
  fill(NAVY);
  doc.rect(0, 0, PAGE.w, PAGE.h, 'F');
  const box = { x: (PAGE.w - 50) / 2, y: 16, w: 50, h: 40 };
  stroke(GOLD);
  doc.setLineWidth(0.8);
  doc.line(0, box.y + box.h * 0.82, PAGE.w, box.y + box.h * 0.82);
  fill([255, 255, 255]);
  doc.roundedRect(box.x, box.y, box.w, box.h, 4, 4, 'F');
  if (logo) doc.addImage(logo, 'PNG', box.x + 5, box.y + 4, box.w - 10, (box.w - 10) * (922 / 1156), 'logo');

  ink([255, 255, 255]);
  doc.setFont('helvetica', 'bold').setFontSize(24).text(COMPANY, PAGE.w / 2, 78, { align: 'center' });
  ink(GOLD);
  doc.setFont('helvetica', 'normal').setFontSize(10.5).text(TAGLINE, PAGE.w / 2, 86, { align: 'center' });
  ink([255, 255, 255]);
  doc.setFont('helvetica', 'bold').setFontSize(36).text('Financial Plan', PAGE.w / 2, 138, { align: 'center' });

  const cover = [
    ['PREPARED FOR', clientName], ['PLAN NAME', planName], ['PLAN DATE', info.planDate || dmy(now)], ['PREPARED BY', COMPANY],
  ];
  const cb = { x: 28, y: 168, w: PAGE.w - 56, h: 12 + cover.length * 11 };
  stroke(GOLD);
  doc.setLineWidth(0.3);
  doc.roundedRect(cb.x, cb.y, cb.w, cb.h, 4, 4, 'S');
  cover.forEach(([label, value], i) => {
    const y = cb.y + 12 + i * 11;
    ink(GOLD);
    doc.setFont('helvetica', 'normal').setFontSize(10).text(label, cb.x + 8, y);
    ink([255, 255, 255]);
    doc.setFont('helvetica', 'bold').setFontSize(11).text(String(value), cb.x + cb.w - 8, y, { align: 'right' });
  });
  ink([255, 255, 255]);
  doc.setFont('helvetica', 'normal').setFontSize(8).text('Private & Confidential — prepared exclusively for the named client.', PAGE.w / 2, 285, { align: 'center' });

  // ---------------------------------------------------------------- content
  doc.addPage();
  let y = CONTENT_TOP;
  const room = (needed) => {
    if (y + needed > CONTENT_BOTTOM) { doc.addPage(); y = CONTENT_TOP; }
  };
  const heading = (text) => {
    room(34);
    ink(NAVY);
    doc.setFont('helvetica', 'bold').setFontSize(14).text(text, MARGIN, y);
    y += 3;
  };

  // cols: [{ h, w }]; rows: array of arrays; a row with `total: true` is bold on sand. Numbers are left as given text.
  const table = (title, cols, rows) => {
    // A table that fits on one page is not split across two: it moves to the next page whole.
    const height = 14 + (rows.length + 1) * 6.6;
    if (height <= CONTENT_BOTTOM - CONTENT_TOP) room(height);
    heading(title);
    autoTable(doc, {
      startY: y,
      margin: { left: MARGIN, right: MARGIN, top: CONTENT_TOP, bottom: PAGE.h - CONTENT_BOTTOM },
      head: [cols.map((c) => c.h)],
      body: rows.map((r) => (r.total ? r.cells.map((content) => ({ content, styles: { fontStyle: 'bold', textColor: NAVY, fillColor: SAND } })) : r)),
      theme: 'grid',
      styles: { font: 'helvetica', fontSize: 9, cellPadding: { top: 1.7, bottom: 1.7, left: 2.6, right: 2.6 }, textColor: [70, 70, 70], lineColor: LINE, lineWidth: 0.2, valign: 'middle' },
      headStyles: { fillColor: NAVY, textColor: [255, 255, 255], fontStyle: 'bold', lineColor: NAVY },
      columnStyles: Object.fromEntries(cols.map((c, i) => [i, { cellWidth: c.w }])),
      rowPageBreak: 'avoid',
    });
    y = doc.lastAutoTable.finalY + 9;
  };
  const sum = (w) => [WIDTH - w.reduce((a, b) => a + b, 0), ...w];
  const money4 = (first, mid) => [{ h: first, w: sum([32, 32, 26])[0] }, { h: mid[0], w: 32 }, { h: mid[1], w: 32 }, { h: mid[2], w: 26 }];
  const total = (...cells) => ({ total: true, cells });

  // Income
  table('Income', money4('Particulars', ['Monthly', 'Annual', '% Income']), [
    ...SOURCES.map((s) => [s.label, inr(n(income[s.key])), inr(n(income[s.key]) * 12), pct(share(n(income[s.key]), incomeAmount))]),
    total('Total Income', inr(incomeAmount), inr(incomeAmount * 12), incomeAmount ? '100%' : '0%'),
  ]);

  // Expenses, one table per group
  CATEGORIES.forEach((c) => {
    const title = EXPENSE_TITLE[c.key] || c.label;
    const catTotal = c.items.reduce((s, _, i) => s + n(expenses[`${c.key}-${i}`]), 0);
    table(title, money4('Particulars', ['Monthly', 'Annual', '% Income']), [
      ...c.items.map((item, i) => {
        const v = n(expenses[`${c.key}-${i}`]);
        return [item, inr(v), inr(v * 12), pct(share(v, incomeAmount))];
      }),
      total(`Total ${title}`, inr(catTotal), inr(catTotal * 12), ''),
    ]);
  });

  // Savings & investments
  const surplus = incomeAmount - expensesAmount - investedAmount;
  table('Savings & Investments', [{ h: 'Particulars', w: WIDTH - 70 }, { h: 'Monthly', w: 35 }, { h: 'Annual', w: 35 }], [
    ['Total Income', inr(incomeAmount), inr(incomeAmount * 12)],
    ['Total Expenses', inr(expensesAmount), inr(expensesAmount * 12)],
    ['Total Savings', inr(incomeAmount - expensesAmount), inr((incomeAmount - expensesAmount) * 12)],
    ...INVESTMENTS.map((x) => [INVESTMENT_LABEL[x.key] || x.label, inr(n(savings[x.key])), inr(n(savings[x.key]) * 12)]),
    ['Total Ongoing Investments', inr(investedAmount), inr(investedAmount * 12)],
    total('Surplus for New Investments', inr(surplus), inr(surplus * 12)),
  ]);

  // Assets
  table('Assets', [{ h: 'Particulars', w: WIDTH - 90 }, { h: 'Present Value', w: 40 }, { h: 'Purpose / Goal', w: 50 }], [
    ...CLASSES.flatMap((c) => c.items.map((item, i) => [
      `${titleCase(c.label)} — ${item}`, inr(n(assetData[`${c.key}-${i}`])), assetData[`${c.key}-${i}Goal`] || '',
    ])),
    total('Total Assets', inr(assets), ''),
  ]);

  // Liabilities
  table('Liabilities', [{ h: 'Particulars', w: WIDTH - 90 }, { h: 'Outstanding', w: 40 }, { h: 'Purpose', w: 50 }], [
    ...LIABILITIES.map((l) => [LIABILITY_LABEL[l.key] || l.label, inr(n(liabilityData[l.key])), liabilityData[`${l.key}Goal`] || '']),
    total('Total Liabilities', inr(liabilities), ''),
  ]);

  // Net worth
  table('Networth', [{ h: 'Networth', w: WIDTH - 50 }, { h: 'Amount', w: 50 }], [
    ['Total Assets', inr(assets)],
    ['Total Liabilities', inr(liabilities)],
    ['Networth (Including Personal Assets)', inr(netWorth)],
    ['Networth (Excluding Personal Assets)', inr(netWorth - personal)],
  ]);

  // Goals
  const goalRows = goals.map((g) => {
    const c = goalCalc(g);
    const status = c.shortfall <= 0 ? 'Funded' : c.funded >= 90 ? 'On Track' : 'Behind';
    return [g.name || 'Goal', inr(c.target), inr(c.corpus), `${Math.round(c.funded)}%`, status, inr(c.needSip)];
  });
  table('Financial Goals', [
    { h: 'Goal', w: WIDTH - 128 }, { h: 'Target (Future)', w: 30 }, { h: 'Projected', w: 30 }, { h: 'Funded', w: 18 }, { h: 'Status', w: 20 }, { h: 'Req. SIP/mo', w: 30 },
  ], goalRows.length ? goalRows : [['No goals added', '', '', '', '', '']]);

  // Retirement
  const r = retirementCalc(retirement);
  table('Retirement Plan', [{ h: 'Particulars', w: WIDTH - 60 }, { h: 'Value', w: 60 }], [
    ['Current Monthly Expense', inr(n(retirement.expense))],
    ['Years to Retirement', String(r.yearsTo)],
    ['Monthly Expense at Retirement', inr(r.expenseAtRetirement)],
    ['Corpus Required at Retirement', inr(r.required)],
    ['Existing Corpus (grown)', inr(r.grown)],
    total('Required Monthly SIP', inr(r.sip)),
  ]);

  // Insurance gap check
  const members = 1 + family.length;
  const ins = insuranceCalc({
    insurance, incomeTotal: incomeAmount, liabilitiesAmount: liabilities, investibleAmount: investibleAssetsTotal(assetData),
    members, familyLife: familyCover(family, 'lifeCover'), familyHealth: familyCover(family, 'healthCover'),
  });
  table('Insurance Gap Check', [
    { h: 'Cover', w: WIDTH - 140 }, { h: 'Recommended', w: 34 }, { h: 'Existing', w: 34 }, { h: 'Gap', w: 34 }, { h: 'Status', w: 38 },
  ], [
    ['Life Insurance', inr(ins.lifeRecommended), inr(ins.lifeExisting), inr(Math.max(0, ins.lifeRecommended - ins.lifeExisting)), ins.lifeStatus],
    ['Health Insurance', inr(ins.healthRecommended), inr(ins.healthExisting), inr(Math.max(0, ins.healthRecommended - ins.healthExisting)), ins.healthStatus],
  ]);

  // Advisor sign-off
  room(75);
  y += 4;
  stroke(GOLD);
  doc.setLineWidth(0.5);
  doc.line(MARGIN, y, PAGE.w - MARGIN, y);
  y += 11;
  ink(NAVY);
  doc.setFont('helvetica', 'bold').setFontSize(14).text('Advisor Sign-off', MARGIN, y);
  y += 8;
  ink(GREY);
  doc.setFont('helvetica', 'normal').setFontSize(9);
  const note = `This financial plan has been prepared by ${COMPANY} based on information provided by the client. Projections use assumed rates of return and inflation and are illustrative, not guaranteed. Investments are subject to market risks. Please read all scheme related documents carefully before investing.`;
  const lines = doc.splitTextToSize(note, WIDTH);
  doc.text(lines, MARGIN, y);
  y += lines.length * 4.4 + 16;
  const half = (WIDTH - 12) / 2;
  stroke(NAVY);
  doc.setLineWidth(0.4);
  [MARGIN, MARGIN + half + 12].forEach((x) => doc.line(x, y, x + half, y));
  ink(NAVY);
  doc.setFont('helvetica', 'bold').setFontSize(9);
  doc.text('Advisor Signature', MARGIN, y + 6);
  doc.text('Client Signature', MARGIN + half + 12, y + 6);
  ink(GREY);
  doc.setFont('helvetica', 'normal');
  doc.text(COMPANY, MARGIN, y + 11);
  doc.text(`Date: ${dmy(now)}`, MARGIN + half + 12, y + 11);

  // ---------------------------------------------------------------- header band and footer on every content page
  const pages = doc.getNumberOfPages();
  for (let p = 2; p <= pages; p += 1) {
    doc.setPage(p);
    fill(NAVY);
    doc.rect(0, 0, PAGE.w, 24, 'F');
    fill([255, 255, 255]);
    doc.roundedRect(MARGIN, 4.5, 15, 15, 2, 2, 'F');
    if (logo) doc.addImage(logo, 'PNG', MARGIN + 1.5, 7, 12, 12 * (922 / 1156), 'logo');
    ink([255, 255, 255]);
    doc.setFont('helvetica', 'bold').setFontSize(14).text(COMPANY, MARGIN + 20, 12);
    ink(GOLD);
    doc.setFont('helvetica', 'normal').setFontSize(9).text(`Financial Statement — ${clientName}`, MARGIN + 20, 18);
    ink([150, 150, 150]);
    doc.setFontSize(8).text(`${COMPANY}  ·  Page ${p - 1} of ${pages - 1}`, PAGE.w / 2, 290, { align: 'center' });
  }
  return doc;
}

export async function exportPdf(state) {
  const [{ jsPDF }, { default: autoTable }, logo] = await Promise.all([import('jspdf'), import('jspdf-autotable'), loadLogo()]);
  const now = new Date();
  const doc = buildPlanPdf(state, { jsPDF, autoTable, logo, now });
  download(doc.output('blob'), fileName(state.info.clientName, now));
}
