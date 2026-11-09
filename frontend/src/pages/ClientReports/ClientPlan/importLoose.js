// Reads ANY workbook, not just one exported from the plan page.
// Every sheet is scanned whatever its name or layout. A label cell is matched to the closest known field
// (exact name, alias or near-match) and the number beside it - or under the nearest "Monthly / Amount /
// Value / Outstanding" heading above it - becomes the value. Goals and Family Members are picked up from
// any table whose headings look like theirs.
import { SOURCES } from './tabs/IncomeTab.jsx';
import { CATEGORIES } from './tabs/ExpensesTab.jsx';
import { INVESTMENTS } from './tabs/SavingsTab.jsx';
import { CLASSES } from './tabs/AssetsTab.jsx';
import { LIABILITIES } from './tabs/LiabilitiesTab.jsx';
import { GENDERS, RELATIONS } from './tabs/FamilyTab.jsx';

const PRIORITIES = ['High', 'Medium', 'Low'];
const STOP = new Set(['and', 'of', 'the', 'a', 'in', 'to', 'for', 'my', 'your']);
const norm = (s) => String(s).toLowerCase().replace(/&/g, ' and ').replace(/[^a-z0-9]+/g, ' ').trim();
const stem = (w) => (w.length > 3 ? w.replace(/(es|s)$/, '') : w);
const tokens = (s) => norm(s).split(' ').filter((w) => w && !STOP.has(w)).map(stem);

// One-letter typos ("Saloon"/"salon", "Tutions"/"tuitions") still count as the same word.
function sameWord(a, b) {
  if (a === b) return true;
  if (Math.min(a.length, b.length) < 5 || Math.abs(a.length - b.length) > 1) return false;
  let i = 0;
  while (i < a.length && a[i] === b[i]) i += 1;
  if (a.length === b.length) return a.slice(i + 1) === b.slice(i + 1); // one letter changed
  const [long, short] = a.length > b.length ? [a, b] : [b, a];
  return long.slice(i + 1) === short.slice(i); // one letter added / dropped
}

function similarity(cell, alias) {
  if (norm(cell) === norm(alias)) return 100;
  const a = tokens(cell);
  const b = tokens(alias);
  if (!a.length || !b.length) return 0;
  const has = (list, w) => list.some((x) => sameWord(x, w));
  const shared = b.filter((w) => has(a, w)).length;
  if (shared === b.length && a.length <= b.length + 2 && alias.length > 3) return 75;
  const jac = shared / (a.length + b.length - shared);
  return jac >= 0.6 ? Math.round(jac * 85) : 0;
}

const DOMAIN_WORDS = {
  info: ['client', 'personal', 'contact', 'detail', 'info'],
  income: ['income', 'earning', 'salary'],
  expenses: ['expense', 'spending', 'outgo', 'household', 'lifestyle', 'emi'],
  savings: ['saving', 'investment', 'sip'],
  assets: ['asset'],
  liabilities: ['liabilit', 'loan', 'debt', 'outstanding'],
  insurance: ['insurance', 'cover'],
  retirement: ['retire', 'pension'],
};

function knownFields() {
  const out = [];
  const add = (domain, path, type, aliases, group = '') => out.push({ domain, path, type, aliases, group });

  const income = {
    salary: ['salary', 'monthly salary', 'take home salary'], bonus: ['bonus'], business: ['business', 'profession income'],
    rental: ['rent income', 'rent received'], investment: ['interest income', 'dividend income'],
  };
  SOURCES.forEach((x) => add('income', ['income', x.key], 'money', [x.label, ...(income[x.key] || [])]));
  const spend = {
    'household-0': ['rent', 'rent paid'], 'household-2': ['groceries', 'food', 'groceries and food', 'food and grocery'],
    'household-5': ['electricity', 'electricity bill'], 'household-7': ['mobile bill', 'internet', 'phone bill'],
    'lifestyle-2': ['dining', 'entertainment', 'dining out'], 'lifestyle-4': ['travel', 'vacation', 'holidays'],
    'dependents-0': ['school fees', 'education fees', 'tuition'], 'loans-0': ['home loan emi'], 'loans-1': ['car loan emi'],
    'insurance-0': ['term insurance premium'], 'insurance-1': ['health insurance premium', 'mediclaim premium'],
  };
  CATEGORIES.forEach((c) => c.items.forEach((item, i) => {
    // "Others if any" is the same label in three categories - the section it sits under decides which.
    const others = /^others/i.test(item) ? ['others if any', 'others'] : [];
    add('expenses', ['expenses', `${c.key}-${i}`], 'money', [item, ...others, ...(spend[`${c.key}-${i}`] || [])], c.label);
  }));
  const savings = {
    sip: ['sip', 'monthly sip', 'mutual fund sip', 'mf sip', 'ongoing investments mf sips'],
    policies: ['recurring deposit', 'policies rds', 'ongoing investments policies rds'],
    other: ['investments others', 'ongoing investments others'],
  };
  INVESTMENTS.forEach((x) => add('savings', ['savings', x.key], 'money', [x.label, ...(savings[x.key] || [])]));
  const held = {
    'debt-0': ['fixed deposits', 'fd'], 'gold-0': ['etfs mutual funds', 'gold etf'], 'gold-1': ['gold bars coins', 'gold coins'],
    'personal-0': ['primary house', 'residential house', 'own house', 'self occupied house'],
    'personal-2': ['jewellery', 'jewellery etc', 'jewelry'],
    'debt-3': ['nsc pomis kvp rds', 'post office schemes'], 'debt-7': ['bonds debentures cds'],
  };
  CLASSES.forEach((c) => c.items.forEach((item, i) => {
    add('assets', ['assetData', `${c.key}-${i}`], 'money', [item, ...(held[`${c.key}-${i}`] || [])], c.label);
  }));
  const liab = { home: ['housing loan', 'home loan outstanding'], card: ['credit card dues', 'credit card outstanding'], other: ['other loan'] };
  LIABILITIES.forEach((x) => add('liabilities', ['liabilityData', x.key], 'money', [x.label, ...(liab[x.key] || [])]));

  add('info', ['info', 'clientName'], 'text', ['client name', 'customer name', 'name of client', 'name']);
  add('info', ['info', 'spouseName'], 'text', ['spouse name', 'spouse', 'wife name', 'husband name', 'partner name']);
  add('info', ['info', 'phone'], 'phone', ['phone', 'mobile', 'mobile number', 'phone number', 'contact number', 'contact no']);
  add('info', ['info', 'email'], 'text', ['email', 'email id', 'e mail', 'email address']);
  add('info', ['info', 'address'], 'text', ['address', 'residential address']);
  add('info', ['info', 'planDate'], 'date', ['plan date', 'date', 'date of plan']);
  add('info', ['info', 'advisorNotes'], 'text', ['advisor notes', 'notes', 'remarks']);

  add('insurance', ['insurance', 'years'], 'int', ['income replacement years', 'replacement years']);
  add('insurance', ['insurance', 'lifeCover'], 'money', ['existing life cover', 'life cover', 'life insurance cover', 'term cover']);
  add('insurance', ['insurance', 'healthCover'], 'money', ['existing health cover', 'health cover', 'health insurance cover', 'mediclaim cover']);

  [
    ['expense', 'money', ['current monthly expense', 'monthly expense']],
    ['corpus', 'money', ['existing retirement corpus', 'retirement corpus', 'current corpus']],
    ['age', 'rate', ['current age', 'age']], ['retireAge', 'rate', ['retirement age', 'retire at age']],
    ['life', 'rate', ['life expectancy']], ['inflation', 'rate', ['inflation', 'inflation rate']],
    ['before', 'rate', ['return before retirement', 'pre retirement return']],
    ['after', 'rate', ['return after retirement', 'post retirement return']],
  ].forEach(([key, type, aliases]) => add('retirement', ['retirement', key], type, aliases));
  return out;
}

const NUMERIC = /^[\s₹$()\-+]*(rs\.?\s*)?[\d,]+(\.\d+)?\s*(%)?[\s)]*$/i;
const isNumeric = (v) => typeof v === 'number' || (typeof v === 'string' && NUMERIC.test(v));
const VALUE_HEAD = /monthly|per month|amount|value|outstanding|balance|current|annual|yearly|per year|\brs\b|inr|₹/;
const filled = (r) => r.filter((c) => c !== '').length;

const RELATION_WORDS = {
  Spouse: ['wife', 'husband', 'partner'], Son: ['boy', 'child son'], Daughter: ['girl', 'child daughter'],
  Father: ['dad', 'papa'], Mother: ['mom', 'mum', 'mummy'],
};
const GOAL_COLS = [
  ['name', ['goal', 'goal name', 'name', 'objective', 'financial goal']], ['priority', ['priority']],
  ['cost', ['cost today', 'cost', 'present cost', 'target amount', 'target', 'amount', 'goal amount']],
  ['years', ['years', 'time horizon', 'horizon', 'years to goal', 'tenure']], ['inflation', ['inflation', 'inflation rate']],
  ['ret', ['expected return', 'return', 'rate of return']],
  ['savings', ['current savings', 'savings', 'existing corpus', 'current corpus']], ['sip', ['monthly sip', 'sip', 'monthly investment']],
];
const FAMILY_COLS = [
  ['name', ['name', 'member name', 'full name']], ['relation', ['relationship', 'relation']],
  ['dob', ['date of birth', 'dob', 'birth date']], ['age', ['age']], ['gender', ['gender', 'sex']],
  ['occupation', ['occupation', 'profession', 'job']], ['income', ['annual income', 'income', 'yearly income']],
  ['lifePolicy', ['life policy', 'life insurance policy']], ['lifeCover', ['life cover', 'life sum assured']],
  ['healthPolicy', ['health policy', 'health insurance policy']], ['healthCover', ['health cover', 'health sum assured']],
  ['dependent', ['dependent', 'dependant']], ['notes', ['notes', 'remarks']],
];

// Column index of each known heading in `header`, or null unless every `required` heading is present.
function columnsOf(header, specs, required) {
  const cols = {};
  const used = new Set();
  specs.forEach(([field, aliases]) => {
    const names = aliases.map(norm);
    const c = header.findIndex((h, i) => !used.has(i) && h !== '' && names.includes(norm(h)));
    if (c >= 0) { cols[field] = c; used.add(c); }
  });
  return required.every((f) => f in cols) ? cols : null;
}

export function readLoose(book, defaults, h) {
  const { plain, num, money, rate, text, isoDate, isTotal } = h;
  const fields = knownFields();
  const best = new Map();
  const tables = {};
  const groups = [...new Map(fields.filter((f) => f.group).map((f) => [`${f.domain}|${f.group}`, { domain: f.domain, name: f.group }])).values()];

  const relationOf = (v) => {
    const t = String(v).trim().toLowerCase();
    return RELATIONS.find((x) => x.toLowerCase() === t) || Object.keys(RELATION_WORDS).find((k) => RELATION_WORDS[k].includes(t)) || 'Other';
  };
  const readTables = (rows) => {
    const cell = (r, cols, f) => (f in cols && r[cols[f]] !== undefined ? r[cols[f]] : '');
    const bodyOf = (i) => {
      const list = [];
      for (let j = i + 1; j < rows.length && filled(rows[j]) > 0; j += 1) list.push(rows[j]);
      return list;
    };
    rows.forEach((header, i) => {
      if (filled(header) < 3) return;

      const fam = !tables.family && columnsOf(header, FAMILY_COLS, ['name', 'relation']);
      if (fam) {
        const list = bodyOf(i).filter((r) => String(cell(r, fam, 'name')) || String(cell(r, fam, 'relation')));
        if (list.length) {
          tables.family = list.map((r, n) => {
            const dob = isoDate(cell(r, fam, 'dob'));
            const age = cell(r, fam, 'age');
            const pick = (opts, f) => opts.find((x) => x.toLowerCase() === String(cell(r, fam, f)).toLowerCase());
            return {
              id: `${Date.now()}-${n}`, name: text(cell(r, fam, 'name')), relation: relationOf(cell(r, fam, 'relation')), dob,
              age: dob || age === '' ? '' : String(num(age)), gender: pick(GENDERS, 'gender') || '',
              occupation: text(cell(r, fam, 'occupation')), income: money(cell(r, fam, 'income')),
              lifePolicy: text(cell(r, fam, 'lifePolicy')), lifeCover: money(cell(r, fam, 'lifeCover')),
              healthPolicy: text(cell(r, fam, 'healthPolicy')), healthCover: money(cell(r, fam, 'healthCover')),
              dependent: /^(yes|y|true|1)$/i.test(String(cell(r, fam, 'dependent'))), notes: text(cell(r, fam, 'notes')),
            };
          });
          tables.familyFields = list.length * 13;
        }
        return;
      }

      const goal = !tables.goals && columnsOf(header, GOAL_COLS, ['name', 'cost']);
      if (goal) {
        const list = bodyOf(i).filter((r) => String(cell(r, goal, 'name')) && !isTotal(String(cell(r, goal, 'name'))));
        if (list.length) {
          tables.goals = list.map((r, n) => ({
            id: `${Date.now()}-g${n}`, name: text(cell(r, goal, 'name')),
            priority: PRIORITIES.find((x) => x.toLowerCase() === String(cell(r, goal, 'priority')).toLowerCase()) || 'Medium',
            cost: money(cell(r, goal, 'cost')), years: String(num(cell(r, goal, 'years'))),
            inflation: 'inflation' in goal ? rate(cell(r, goal, 'inflation')) : '6',
            ret: 'ret' in goal ? rate(cell(r, goal, 'ret')) : '12',
            savings: money(cell(r, goal, 'savings')), sip: money(cell(r, goal, 'sip')),
          }));
          tables.goalFields = list.length * 8;
        }
      }
    });
  };

  book.worksheets.forEach((ws) => {
    const rows = [];
    ws.eachRow({ includeEmpty: true }, (row) => rows.push(Array.from(row.values.slice(1), plain)));
    readTables(rows);

    let section = '';
    rows.forEach((row, i) => {
      // A title row has one distinct value (merged cells repeat it across the row).
      const distinct = [...new Set(row.filter((c) => c !== '').map(String))];
      if (distinct.length === 1 && !isNumeric(distinct[0]) && !distinct[0].startsWith('*')) section = distinct[0];
      const context = norm(`${ws.name} ${section}`);
      const sectionWords = tokens(section);
      const inSection = (group) => tokens(group).every((w) => sectionWords.some((s) => sameWord(s, w)));

      row.forEach((cellValue, j) => {
        if (typeof cellValue !== 'string' || !cellValue || isNumeric(cellValue) || isTotal(cellValue) || /^total/i.test(cellValue)) return;

        let top = null;
        fields.forEach((f) => {
          const base = Math.max(...f.aliases.map((a) => similarity(cellValue, a)));
          if (!base) return;
          let score = base + (DOMAIN_WORDS[f.domain].some((w) => context.includes(w)) ? 20 : 0);
          if (f.group) {
            if (inSection(f.group)) score += 30;
            else if (groups.some((g) => g.domain === f.domain && g.name !== f.group && inSection(g.name))) score -= 60;
          }
          if (score >= 70 && (!top || score > top.score)) top = { f, score };
        });
        if (!top) return;

        // The value is under the nearest Monthly / Amount / Value heading above, else the next filled cell to the right.
        let headCol = -1;
        let headText = '';
        for (let k = i - 1; k >= 0 && k > i - 40 && filled(rows[k]) > 0; k -= 1) {
          const hit = rows[k].findIndex((x, c) => c > j && typeof x === 'string' && VALUE_HEAD.test(x.toLowerCase()));
          if (hit >= 0 && filled(rows[k]) >= 2) { headCol = hit; headText = String(rows[k][hit]).toLowerCase(); break; }
        }
        const numeric = ['money', 'rate', 'int'].includes(top.f.type);
        const ok = (v) => v !== '' && (numeric ? isNumeric(v) : !top.f.aliases.some((a) => similarity(String(v), a) >= 70));
        let col = headCol >= 0 && ok(row[headCol]) ? headCol : -1;
        if (col < 0) col = row.findIndex((v, c) => c > j && ok(v));
        if (col < 0) return;

        let value = row[col];
        const yearly = col === headCol && /annual|yearly|per year/.test(headText) && !/month/.test(headText);
        if (yearly && ['income', 'expenses', 'savings'].includes(top.f.domain)) value = num(value) / 12;

        const key = top.f.path.join('.');
        if (best.has(key) && best.get(key).score >= top.score) return;
        best.set(key, { f: top.f, score: top.score, value });
      });
    });
  });

  const data = {};
  const count = {};
  best.forEach(({ f, value }) => {
    const convert = {
      money, rate, phone: (v) => String(v).replace(/\D/g, '').slice(-10), date: isoDate,
      int: (v) => String(num(v) || ''),
    }[f.type] || text;
    const v = convert(value);
    if (v === '') return;
    const [group, key] = f.path;
    data[group] = { ...data[group], [key]: v };
    count[group] = (count[group] || 0) + 1;
  });
  // A sheet that is only a list of names (each optionally followed by a date of birth):
  // client first, spouse second, the rest family members.
  if (!data.info?.clientName) {
    book.worksheets.forEach((ws) => {
      if (tables.names) return;
      const lines = [];
      ws.eachRow({ includeEmpty: false }, (row) => {
        const cells = Array.from(row.values.slice(1), plain).filter((c) => c !== '');
        if (cells.length) lines.push(cells);
      });
      const nameLike = (l) => typeof l[0] === 'string' && !isNumeric(l[0]) && l.slice(1).every((c) => c instanceof Date);
      if (lines.length >= 1 && lines.length <= 12 && lines.every(nameLike)) {
        tables.names = lines.map((l) => ({ name: l[0].replace(/\s+/g, ' ').trim(), dob: l[1] ? isoDate(l[1]) : '' }));
      }
    });
    if (tables.names) {
      const [client, spouse, ...rest] = tables.names;
      data.info = { clientName: client.name, ...(spouse ? { spouseName: spouse.name } : {}) };
      count.info = (count.info || 0) + 1 + (spouse ? 1 : 0);
      const ageOf = (dob) => (dob ? (Date.now() - new Date(dob).getTime()) / 31557600000 : 99);
      const member = (m, n, relation) => ({
        id: `${Date.now()}-n${n}`, name: m.name, relation, dob: m.dob, age: '', gender: '', occupation: '', income: '',
        lifePolicy: '', lifeCover: '', healthPolicy: '', healthCover: '', dependent: ageOf(m.dob) < 21, notes: '',
      });
      if (!tables.family && (spouse || rest.length)) {
        tables.family = [
          ...(spouse ? [member(spouse, 0, 'Spouse')] : []),
          ...rest.map((m, n) => member(m, n + 1, 'Other')),
        ];
        tables.familyFields = tables.family.length * 3;
      }
    }
  }
  if (data.insurance) data.insurance = { ...defaults.insurance, ...data.insurance, auto: false };
  if (data.retirement) data.retirement = { ...defaults.retirement, ...data.retirement };
  if (tables.goals) { data.goals = tables.goals; count.goals = tables.goalFields; }
  if (tables.family) { data.family = tables.family; count.family = tables.familyFields; }
  return { data, count };
}
