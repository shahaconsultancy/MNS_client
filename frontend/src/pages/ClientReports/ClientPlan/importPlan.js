// Reads a workbook written by exportPlan.js and turns it back into plan page state.
// Sheets are found by tab name and rows / columns by their label, so layout changes in Excel don't matter.
// Totals, percentages, calculated figures and the Summary sheet are ignored - the site works those out itself.
import { TABS } from './tabs.js';
import { SOURCES } from './tabs/IncomeTab.jsx';
import { CATEGORIES } from './tabs/ExpensesTab.jsx';
import { INVESTMENTS } from './tabs/SavingsTab.jsx';
import { CLASSES } from './tabs/AssetsTab.jsx';
import { LIABILITIES } from './tabs/LiabilitiesTab.jsx';
import { GENDERS, RELATIONS } from './tabs/FamilyTab.jsx';

const PRIORITIES = ['High', 'Medium', 'Low'];
const MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'];
const sheetName = (key) => TABS.find((t) => t.key === key).label;

// ---- cell helpers
function plain(v) {
  if (v == null) return '';
  if (v instanceof Date) return v;
  if (typeof v === 'object') {
    if (v.richText) return v.richText.map((t) => t.text).join('').trim();
    if ('result' in v) return plain(v.result);
    return v.text ? String(v.text).trim() : '';
  }
  if (typeof v === 'string') return v.trim() === '—' ? '' : v.trim();
  return v;
}

const num = (v) => {
  if (typeof v === 'number') return v;
  const n = Number(String(v).replace(/[₹,%\s]|Rs\.?/gi, '').replace(/,/g, ''));
  return Number.isFinite(n) ? n : 0;
};
const money = (v) => (num(v) > 0 ? String(Math.round(num(v))) : '');
const rate = (v) => String(num(v));
const text = (v) => (v instanceof Date ? '' : String(v));

// "2026-09-01", "1 Sept 2026" or a real Excel date -> "2026-09-01" ('' when unreadable).
function isoDate(v) {
  const pad = (x) => String(x).padStart(2, '0');
  if (v instanceof Date) return `${v.getUTCFullYear()}-${pad(v.getUTCMonth() + 1)}-${pad(v.getUTCDate())}`;
  const s = String(v);
  if (/^\d{4}-\d{2}-\d{2}/.test(s)) return s.slice(0, 10);
  const m = s.match(/^(\d{1,2})\s+([A-Za-z]{3})[A-Za-z]*\s+(\d{4})$/);
  const month = m ? MONTHS.indexOf(m[2].toLowerCase()) : -1;
  return month < 0 ? '' : `${m[3]}-${pad(month + 1)}-${pad(Number(m[1]))}`;
}

// Every titled table of a sheet: a one-cell title row, a header row, then rows up to the next blank row.
function blocksOf(ws) {
  const rows = [];
  ws.eachRow({ includeEmpty: true }, (row) => rows.push(Array.from(row.values.slice(1), plain)));
  const filled = (r) => r.filter((c) => c !== '').length;

  const blocks = [];
  for (let i = 0; i < rows.length - 1; i += 1) {
    if (filled(rows[i]) === 1 && rows[i][0] !== '' && filled(rows[i + 1]) >= 2) {
      const head = rows[i + 1].map((h) => String(h).toLowerCase());
      const body = [];
      let j = i + 2;
      while (j < rows.length && filled(rows[j]) > 0) { body.push(rows[j]); j += 1; }
      blocks.push({ title: String(rows[i][0]), head, body });
      i = j;
    }
  }
  return blocks;
}

// A row's value under the header `name` (case-insensitive).
const at = (block, row, name) => {
  const i = block.head.indexOf(name.toLowerCase());
  return i < 0 || row[i] === undefined ? '' : row[i];
};

const isTotal = (label) => /\btotal\b/i.test(label) || /^no .* added$/i.test(label);

export async function readPlanFile(file, defaults) {
  const { default: ExcelJS } = await import('exceljs');
  const book = new ExcelJS.Workbook();
  try {
    await book.xlsx.load(await file.arrayBuffer());
  } catch {
    throw new Error('Could not read this file. Choose the .xlsx file exported from this plan page.');
  }

  const data = {};
  const tabs = [];
  let fields = 0;
  let unmatched = 0;
  const found = (key, value) => { if (value !== '') fields += 1; return value; };

  const sheet = (key) => {
    const ws = book.getWorksheet(sheetName(key));
    if (ws) tabs.push(sheetName(key));
    return ws ? blocksOf(ws) : null;
  };
  const sourceName = book.worksheets[0]?.getCell('A1').value;

  // Rows of one block matched to a list of known labels; the callback receives (matchIndex, row).
  const match = (block, labels, each, labelCol) => {
    block.body.forEach((row) => {
      const label = String(at(block, row, labelCol));
      const i = labels.indexOf(label);
      if (i >= 0) each(i, row);
      else if (label && !isTotal(label)) unmatched += 1;
    });
  };

  // Client Info
  const info = sheet('client-info');
  if (info?.[0]) {
    const fieldOf = {
      'Client name': 'clientName', 'Spouse name': 'spouseName', Phone: 'phone', Email: 'email',
      Address: 'address', 'Plan date': 'planDate', 'Advisor notes': 'advisorNotes',
    };
    data.info = {};
    info[0].body.forEach((row) => {
      const field = fieldOf[row[0]];
      const v = row[1] ?? '';
      if (!field) return;
      if (field === 'phone') data.info.phone = found(field, String(v).replace(/\D/g, '').slice(-10));
      else if (field === 'planDate') data.info.planDate = found(field, isoDate(v));
      else data.info[field] = found(field, text(v));
    });
  }

  // Income / Savings: one table of label + monthly amount
  const flat = (key, list) => {
    const blocks = sheet(key);
    if (!blocks?.[0]) return null;
    const out = {};
    match(blocks[0], list.map((x) => x.label), (i, row) => {
      out[list[i].key] = found(list[i].key, money(at(blocks[0], row, 'Monthly')));
    }, blocks[0].head[0]);
    return out;
  };
  const income = flat('income', SOURCES);
  if (income) data.income = income;
  const savings = flat('savings', INVESTMENTS);
  if (savings) data.savings = savings;

  // Expenses: one table per category
  const expenses = sheet('expenses');
  if (expenses) {
    data.expenses = {};
    expenses.forEach((block) => {
      const cat = CATEGORIES.find((c) => c.label === block.title);
      if (!cat) return;
      match(block, cat.items, (i, row) => {
        data.expenses[`${cat.key}-${i}`] = found('x', money(at(block, row, 'Monthly')));
      }, 'Expense');
    });
  }

  // Assets
  const assets = sheet('assets');
  if (assets?.[0]) {
    data.assetData = {};
    const block = assets[0];
    block.body.forEach((row) => {
      const cls = CLASSES.find((c) => c.label === at(block, row, 'Asset class'));
      const i = cls ? cls.items.indexOf(String(at(block, row, 'Asset'))) : -1;
      if (i < 0) {
        if (String(at(block, row, 'Asset class')) && !isTotal(String(at(block, row, 'Asset class')))) unmatched += 1;
        return;
      }
      const key = `${cls.key}-${i}`;
      data.assetData[key] = found(key, money(at(block, row, 'Value')));
      const purpose = text(at(block, row, 'Purpose / goal'));
      if (purpose) data.assetData[`${key}Goal`] = found(key, purpose);
    });
  }

  // Liabilities & Net Worth (only the Liabilities table is input)
  const liabilities = sheet('liabilities');
  const liabilityBlock = liabilities?.find((b) => b.title === 'Liabilities');
  if (liabilityBlock) {
    data.liabilityData = {};
    match(liabilityBlock, LIABILITIES.map((l) => l.label), (i, row) => {
      const { key } = LIABILITIES[i];
      data.liabilityData[key] = found(key, money(at(liabilityBlock, row, 'Outstanding')));
      const purpose = text(at(liabilityBlock, row, 'Purpose / goal'));
      if (purpose) data.liabilityData[`${key}Goal`] = found(key, purpose);
    }, 'Liability');
  }

  // Insurance: the Inputs table only; the cover check is calculated
  const insurance = sheet('insurance')?.find((b) => b.title === 'Inputs');
  if (insurance) {
    const auto = insurance.body.some((r) => r[0] === 'Cover taken from Family Members tab' && String(r[1]).toLowerCase() === 'yes');
    const value = (label) => insurance.body.find((r) => r[0] === label)?.[1] ?? '';
    data.insurance = {
      ...defaults.insurance,
      years: found('years', String(num(value('Income replacement years')) || defaults.insurance.years)),
      lifeCover: auto ? '' : found('life', money(value('Existing life cover'))),
      healthCover: auto ? '' : found('health', money(value('Existing health cover'))),
      auto,
    };
  }

  // Goals
  const goals = sheet('goals')?.[0];
  if (goals) {
    data.goals = goals.body
      .filter((row) => PRIORITIES.includes(String(at(goals, row, 'Priority'))))
      .map((row, i) => {
        fields += 8;
        return {
          id: `${Date.now()}-g${i}`,
          name: text(at(goals, row, 'Goal')),
          priority: String(at(goals, row, 'Priority')),
          cost: money(at(goals, row, 'Cost today')),
          years: String(num(at(goals, row, 'Years'))),
          inflation: rate(at(goals, row, 'Inflation')),
          ret: rate(at(goals, row, 'Expected return')),
          savings: money(at(goals, row, 'Current savings')),
          sip: money(at(goals, row, 'Monthly SIP')),
        };
      });
  }

  // Retirement: the Inputs table only
  const retirement = sheet('retirement')?.find((b) => b.title === 'Inputs');
  if (retirement) {
    const fieldOf = {
      'Current monthly expense': ['expense', money], 'Existing retirement corpus': ['corpus', money],
      'Current age': ['age', rate], 'Retirement age': ['retireAge', rate], 'Life expectancy': ['life', rate],
      Inflation: ['inflation', rate], 'Return before retirement': ['before', rate], 'Return after retirement': ['after', rate],
    };
    data.retirement = { ...defaults.retirement };
    retirement.body.forEach((row) => {
      const spec = fieldOf[row[0]];
      if (spec) data.retirement[spec[0]] = found(spec[0], spec[1](row[1] ?? ''));
    });
  }

  // Family Members
  const family = sheet('family')?.[0];
  if (family) {
    data.family = family.body
      .filter((row) => RELATIONS.includes(String(at(family, row, 'Relationship'))))
      .map((row, i) => {
        fields += 13;
        const dob = isoDate(at(family, row, 'Date of birth'));
        const gender = String(at(family, row, 'Gender'));
        return {
          id: `${Date.now()}-${i}`,
          name: text(at(family, row, 'Name')),
          relation: String(at(family, row, 'Relationship')),
          dob,
          age: dob ? '' : String(at(family, row, 'Age') === '' ? '' : num(at(family, row, 'Age'))),
          gender: GENDERS.includes(gender) ? gender : '',
          occupation: text(at(family, row, 'Occupation')),
          income: money(at(family, row, 'Annual income')),
          lifePolicy: text(at(family, row, 'Life policy')),
          lifeCover: money(at(family, row, 'Life cover')),
          healthPolicy: text(at(family, row, 'Health policy')),
          healthCover: money(at(family, row, 'Health cover')),
          dependent: String(at(family, row, 'Dependent')).toLowerCase() === 'yes',
          notes: text(at(family, row, 'Notes')),
        };
      });
  }

  if (!tabs.length) {
    throw new Error('No plan sheets found. Import the .xlsx file exported from this plan page.');
  }
  return { data, tabs, fields, unmatched, sourceName: typeof sourceName === 'string' ? sourceName : '' };
}
