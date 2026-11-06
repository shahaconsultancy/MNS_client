// The 11 steps of a client plan, in order. Add each tab's component here as it is built.
import ClientInfoTab from './tabs/ClientInfoTab.jsx';
import IncomeTab from './tabs/IncomeTab.jsx';
import ExpensesTab from './tabs/ExpensesTab.jsx';
import SavingsTab from './tabs/SavingsTab.jsx';
import AssetsTab from './tabs/AssetsTab.jsx';
import LiabilitiesTab from './tabs/LiabilitiesTab.jsx';
import InsuranceTab from './tabs/InsuranceTab.jsx';
import GoalsTab from './tabs/GoalsTab.jsx';
import RetirementTab from './tabs/RetirementTab.jsx';
import FamilyTab from './tabs/FamilyTab.jsx';
import SummaryTab from './tabs/SummaryTab.jsx';

export const TABS = [
  { key: 'client-info', label: 'Client Info', Component: ClientInfoTab },
  { key: 'income', label: 'Income', Component: IncomeTab },
  { key: 'expenses', label: 'Expenses', Component: ExpensesTab },
  { key: 'savings', label: 'Savings & Investments', Component: SavingsTab },
  { key: 'assets', label: 'Assets', Component: AssetsTab },
  { key: 'liabilities', label: 'Liabilities & Net Worth', Component: LiabilitiesTab },
  { key: 'insurance', label: 'Insurance', Component: InsuranceTab },
  { key: 'goals', label: 'Goals', Component: GoalsTab },
  { key: 'retirement', label: 'Retirement', Component: RetirementTab },
  { key: 'family', label: 'Family Members', Component: FamilyTab },
  { key: 'summary', label: 'Summary', Component: SummaryTab },
];
