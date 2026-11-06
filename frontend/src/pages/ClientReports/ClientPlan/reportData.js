// Turns the plan page state into the sheets / tables written by the Excel and PDF exports.
// One sheet per plan tab, named like the tab. A block is a titled table; a column's `fmt` is text | inr | pct | num.
import { TABS } from './tabs.js';
import { SOURCES, incomeTotal } from './tabs/IncomeTab.jsx';
import { CATEGORIES, expensesTotal } from './tabs/ExpensesTab.jsx';
import { INVESTMENTS, savingsTotal } from './tabs/SavingsTab.jsx';
import { CLASSES, assetsTotal, investibleAssetsTotal, personalAssetsTotal } from './tabs/AssetsTab.jsx';
import { LIABILITIES, liabilitiesTotal } from './tabs/LiabilitiesTab.jsx';
import { insuranceCalc } from './tabs/InsuranceTab.jsx';
import { goalCalc } from './tabs/GoalsTab.jsx';
import { retirementCalc } from './tabs/RetirementTab.jsx';
import { ageOf, familyCover } from './tabs/FamilyTab.jsx';

const n = (v) => Number(v) || 0;
const share = (part, of) => (of > 0 ? (part / of) * 100 : 0);
const row = (...cells) => ({ cells });
const total = (...cells) => ({ cells, total: true });
const col = (h, fmt = 'text') => ({ h, fmt });
// A cell whose format differs from its column's.
const inr = (v) => ({ v, fmt: 'inr' });
const pct = (v) => ({ v, fmt: 'pct' });
const pair = (title, rows) => ({ title, cols: [col('Field'), col('Value', 'text')], rows });

const txt = (v) => (v === '' || v == null ? '—' : v);
const date = (v) => (v ? new Date(v).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' }) : '—');

export function buildReport(s) {
  const { info, income, expenses, savings, assetData, liabilityData, insurance, family, goals, retirement } = s;
  const incomeAmount = incomeTotal(income);
  const expensesAmount = expensesTotal(expenses);
  const investedAmount = savingsTotal(savings);
  const surplus = incomeAmount - expensesAmount - investedAmount;
  const assets = assetsTotal(assetData);
  const liabilities = liabilitiesTotal(liabilityData);
  const netWorth = assets - liabilities;

  // ---- Client Info
  const clientInfo = [pair('Personal & contact', [
    row('Client name', txt(info.clientName)),
    row('Spouse name', txt(info.spouseName)),
    row('Phone', info.phone ? `+91 ${info.phone}` : '—'),
    row('Email', txt(info.email)),
    row('Address', txt(info.address)),
    row('Plan date', date(info.planDate)),
    row('Advisor notes', txt(info.advisorNotes)),
  ])];

  // ---- Income
  const incomeBlocks = [{
    title: 'Monthly income',
    cols: [col('Source'), col('Monthly', 'inr'), col('Yearly', 'inr'), col('% of income', 'pct')],
    rows: [
      ...SOURCES.map((x) => row(x.label, n(income[x.key]), n(income[x.key]) * 12, share(n(income[x.key]), incomeAmount))),
      total('Total income', incomeAmount, incomeAmount * 12, incomeAmount ? 100 : 0),
    ],
  }];

  // ---- Expenses
  const catTotal = (c) => c.items.reduce((sum, _, i) => sum + n(expenses[`${c.key}-${i}`]), 0);
  const expenseBlocks = CATEGORIES.map((c) => ({
    title: c.label,
    cols: [col('Expense'), col('Monthly', 'inr'), col('% of expenses', 'pct')],
    rows: [
      ...c.items.map((item, i) => row(item, n(expenses[`${c.key}-${i}`]), share(n(expenses[`${c.key}-${i}`]), expensesAmount))),
      total(`${c.label} total`, catTotal(c), share(catTotal(c), expensesAmount)),
    ],
  }));
  expenseBlocks.push({
    title: 'Expense summary',
    cols: [col('Category'), col('Monthly', 'inr'), col('% of income', 'pct')],
    rows: [
      ...CATEGORIES.map((c) => row(c.label, catTotal(c), share(catTotal(c), incomeAmount))),
      total('Total expenses', expensesAmount, share(expensesAmount, incomeAmount)),
    ],
  });

  // ---- Savings & Investments
  const savingsBlocks = [{
    title: 'Monthly investments',
    cols: [col('Investment'), col('Monthly', 'inr'), col('Yearly', 'inr'), col('% of investments', 'pct')],
    rows: [
      ...INVESTMENTS.map((x) => row(x.label, n(savings[x.key]), n(savings[x.key]) * 12, share(n(savings[x.key]), investedAmount))),
      total('Total investments', investedAmount, investedAmount * 12, investedAmount ? 100 : 0),
    ],
  }];

  // ---- Assets
  const assetBlocks = [{
    title: 'Assets by class',
    cols: [col('Asset class'), col('Asset'), col('Value', 'inr'), col('Purpose / goal'), col('% of assets', 'pct')],
    rows: [
      ...CLASSES.flatMap((c) => {
        const classTotal = c.items.reduce((sum, _, i) => sum + n(assetData[`${c.key}-${i}`]), 0);
        return [
          ...c.items.map((item, i) => row(
            c.label, item, n(assetData[`${c.key}-${i}`]), txt(assetData[`${c.key}-${i}Goal`]),
            share(n(assetData[`${c.key}-${i}`]), assets),
          )),
          total(`${c.label} total`, '', classTotal, '', share(classTotal, assets)),
        ];
      }),
      total('Total assets', '', assets, '', assets ? 100 : 0),
    ],
  }];

  // ---- Liabilities & Net Worth
  const personal = personalAssetsTotal(assetData);
  const liabilityBlocks = [
    {
      title: 'Liabilities',
      cols: [col('Liability'), col('Outstanding', 'inr'), col('Purpose / goal'), col('% of liabilities', 'pct')],
      rows: [
        ...LIABILITIES.map((l) => row(l.label, n(liabilityData[l.key]), txt(liabilityData[`${l.key}Goal`]), share(n(liabilityData[l.key]), liabilities))),
        total('Total liabilities', liabilities, '', liabilities ? 100 : 0),
      ],
    },
    {
      title: 'Net worth',
      cols: [col('Item'), col('Amount', 'inr')],
      rows: [
        row('Total assets', assets),
        row('Total liabilities', liabilities),
        total('Net worth', netWorth),
        row('Personal assets (home, vehicles, jewellery)', personal),
        total('Net worth excluding personal assets', netWorth - personal),
      ],
    },
  ];

  // ---- Insurance
  const members = 1 + family.length;
  const ins = insuranceCalc({
    insurance, incomeTotal: incomeAmount, liabilitiesAmount: liabilities, investibleAmount: investibleAssetsTotal(assetData),
    members, familyLife: familyCover(family, 'lifeCover'), familyHealth: familyCover(family, 'healthCover'),
  });
  const gap = (rec, have) => (have >= rec ? ['Surplus', have - rec] : ['Gap', rec - have]);
  const insuranceBlocks = [
    {
      title: 'Inputs',
      cols: [col('Field'), col('Value', 'num')],
      rows: [
        row('Income replacement years', ins.years),
        row('Existing life cover', inr(ins.lifeExisting)),
        row('Existing health cover', inr(ins.healthExisting)),
        row('Cover taken from Family Members tab', insurance.auto ? 'Yes' : 'No'),
      ],
    },
    {
      title: 'Cover check',
      cols: [col('Cover'), col('Recommended', 'inr'), col('Existing', 'inr'), col('Status'), col('Surplus / gap', 'inr')],
      rows: [
        row('Life insurance', ins.lifeRecommended, ins.lifeExisting, ins.lifeStatus, gap(ins.lifeRecommended, ins.lifeExisting)[1]),
        row('Health insurance', ins.healthRecommended, ins.healthExisting, ins.healthStatus, gap(ins.healthRecommended, ins.healthExisting)[1]),
      ],
    },
    {
      title: 'How the recommendation is worked out',
      cols: [col('Item'), col('Amount', 'inr')],
      rows: [
        row(`Annual income × ${ins.years} years`, ins.annualIncome * ins.years),
        row('+ Outstanding liabilities', liabilities),
        row('− Investible assets (liquid, debt, equity, gold)', investibleAssetsTotal(assetData)),
        total('Recommended life cover', ins.lifeRecommended),
        row(`Family floater for ${members} member${members === 1 ? '' : 's'}`, ins.floater),
        row('Floor: 50% of annual income', ins.floor),
        total('Recommended health cover', ins.healthRecommended),
      ],
    },
  ];

  // ---- Goals
  const calcs = goals.map(goalCalc);
  const goalBlocks = [{
    title: 'Goals',
    cols: [
      col('Goal'), col('Priority'), col('Cost today', 'inr'), col('Years', 'num'), col('Inflation', 'pct'), col('Expected return', 'pct'),
      col('Current savings', 'inr'), col('Monthly SIP', 'inr'), col('Future target', 'inr'), col('Projected corpus', 'inr'),
      col('Funded', 'pct'), col('Surplus / shortfall', 'inr'), col('SIP to fully fund', 'inr'),
    ],
    rows: goals.length
      ? [
          ...goals.map((g, i) => row(
            g.name, g.priority, n(g.cost), n(g.years), n(g.inflation), n(g.ret), n(g.savings), n(g.sip),
            calcs[i].target, calcs[i].corpus, calcs[i].funded, calcs[i].corpus - calcs[i].target, calcs[i].needSip,
          )),
          total(
            'Total', '', goals.reduce((a, g) => a + n(g.cost), 0), '', '', '', goals.reduce((a, g) => a + n(g.savings), 0),
            goals.reduce((a, g) => a + n(g.sip), 0), calcs.reduce((a, c) => a + c.target, 0), calcs.reduce((a, c) => a + c.corpus, 0),
            '', '', '',
          ),
        ]
      : [row('No goals added', '', '', '', '', '', '', '', '', '', '', '', '')],
  }];

  // ---- Retirement
  const r = retirementCalc(retirement);
  const retirementBlocks = [
    {
      title: 'Inputs',
      cols: [col('Field'), col('Value', 'num')],
      rows: [
        row('Current monthly expense', inr(n(retirement.expense))),
        row('Existing retirement corpus', inr(n(retirement.corpus))),
        row('Current age', n(retirement.age)),
        row('Retirement age', n(retirement.retireAge)),
        row('Life expectancy', n(retirement.life)),
        row('Inflation', pct(n(retirement.inflation))),
        row('Return before retirement', pct(n(retirement.before))),
        row('Return after retirement', pct(n(retirement.after))),
      ],
    },
    {
      title: 'Result',
      cols: [col('Item'), col('Amount', 'inr')],
      rows: [
        row(`Monthly expense at retirement (after ${r.yearsTo} yrs)`, r.expenseAtRetirement),
        row(`Corpus required (${r.yearsIn} yrs of retirement)`, r.required),
        row('Existing corpus grown to retirement', r.grown),
        row('Gap to be funded', r.gap),
        total(`Required monthly SIP (${r.months} months)`, r.sip),
      ],
    },
  ];

  // ---- Family Members
  const familyBlocks = [{
    title: `Family members (${family.length})`,
    cols: [
      col('Name'), col('Relationship'), col('Date of birth'), col('Age', 'num'), col('Gender'), col('Occupation'),
      col('Annual income', 'inr'), col('Life policy'), col('Life cover', 'inr'), col('Health policy'), col('Health cover', 'inr'),
      col('Dependent'), col('Notes'),
    ],
    rows: family.length
      ? [
          ...family.map((m) => row(
            txt(m.name), m.relation, date(m.dob), m.dob ? n(ageOf(m.dob)) : m.age === '' ? '—' : n(m.age), txt(m.gender), txt(m.occupation),
            n(m.income), txt(m.lifePolicy), n(m.lifeCover), txt(m.healthPolicy), n(m.healthCover), m.dependent ? 'Yes' : 'No', txt(m.notes),
          )),
          total('Total', '', '', '', '', '', family.reduce((a, m) => a + n(m.income), 0), '', familyCover(family, 'lifeCover'), '', familyCover(family, 'healthCover'), '', ''),
        ]
      : [row('No family members added', '', '', '', '', '', '', '', '', '', '', '', '')],
  }];

  // ---- Summary
  const summaryBlocks = [
    {
      title: 'Monthly cash flow',
      cols: [col('Item'), col('Monthly', 'inr'), col('Yearly', 'inr')],
      rows: [
        row('Income', incomeAmount, incomeAmount * 12),
        row('Expenses', expensesAmount, expensesAmount * 12),
        row('Savings & investments', investedAmount, investedAmount * 12),
        total('Monthly surplus', surplus, surplus * 12),
      ],
    },
    {
      title: 'Balance sheet',
      cols: [col('Item'), col('Amount', 'inr')],
      rows: [
        row('Total assets', assets),
        row('Total liabilities', liabilities),
        total('Net worth', netWorth),
        row('Net worth excluding personal assets', netWorth - personal),
      ],
    },
    {
      title: 'Asset allocation',
      cols: [col('Asset class'), col('Value', 'inr'), col('% of assets', 'pct')],
      rows: CLASSES.map((c) => {
        const v = c.items.reduce((sum, _, i) => sum + n(assetData[`${c.key}-${i}`]), 0);
        return row(c.label, v, share(v, assets));
      }),
    },
    {
      title: 'Spending by category',
      cols: [col('Category'), col('Monthly', 'inr'), col('% of expenses', 'pct')],
      rows: CATEGORIES.map((c) => row(c.label, catTotal(c), share(catTotal(c), expensesAmount))),
    },
    {
      title: 'Plan highlights',
      cols: [col('Item'), col('Value', 'text')],
      rows: [
        row('Family members', String(family.length)),
        row('Goals', String(goals.length)),
        row('Life cover', `${ins.lifeStatus}`),
        row('Health cover', `${ins.healthStatus}`),
        row('Retirement corpus required', inr(r.required)),
      ],
    },
  ];
  const blocksOf = {
    'client-info': clientInfo, income: incomeBlocks, expenses: expenseBlocks, savings: savingsBlocks, assets: assetBlocks,
    liabilities: liabilityBlocks, insurance: insuranceBlocks, goals: goalBlocks, retirement: retirementBlocks,
    family: familyBlocks, summary: summaryBlocks,
  };

  return {
    title: info.clientName || 'Client',
    subtitle: `Financial plan · ${date(info.planDate)}`,
    sheets: TABS.map((t) => ({ name: t.label, blocks: blocksOf[t.key] })),
  };
}
