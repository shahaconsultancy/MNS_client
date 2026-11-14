import { useCallback, useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { Plus, Search, User, ArrowRight, Trash2 } from 'lucide-react';
import NewClientModal from '../../components/clients/NewClientModal.jsx';
import { deleteClient, getCachedClients, listClients } from '../../services/clientService.js';
import { alertSuccess, confirmDeleteClient } from '../../services/alerts.js';
import './ClientReportsPage.css';

const FILTERS = [
  { key: 'all', label: 'All clients' },
  { key: 'surplus', label: 'In surplus' },
  { key: 'deficit', label: 'In deficit' },
];

const inr = (n) => `${n < 0 ? '-' : ''}₹${Math.abs(n).toLocaleString('en-IN')}`;
const crore = (n) => `₹${(n / 10000000).toFixed(2)} Cr`;
const formatDate = (iso) => {
  const d = new Date(iso);
  return `${d.getDate()}/${d.getMonth() + 1}/${d.getFullYear()}`;
};
const initials = (name) => name.split(' ').slice(0, 2).map((w) => w[0]).join('').toUpperCase();
const titleCase = (v) => v.toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());

// Map an API record to the shape the cards render.
const toCard = (c) => ({
  id: c.id,
  name: `${c.title.toUpperCase()} ${c.full_name.toUpperCase()}`,
  fullName: titleCase(c.full_name),
  initials: initials(c.full_name),
  netWorth: Number(c.net_worth),
  surplus: Number(c.monthly_surplus),
  members: c.family_members,
  updated: c.updated_at,
  advisor: c.advisor_name,
  isNew: Date.now() - new Date(c.created_at) < 7 * 24 * 3600 * 1000,
});

const status = (c) => (c.surplus > 0 ? 'surplus' : c.surplus < 0 ? 'deficit' : 'balanced');
const STATUS_LABEL = { surplus: 'Surplus', deficit: 'Deficit', balanced: 'Balanced' };

export default function ClientReportsPage() {
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState('all');
  const [sort, setSort] = useState('recent');
  // Show the last known list immediately; the fresh list replaces it as soon as it arrives.
  const [cached] = useState(getCachedClients);
  const [clients, setClients] = useState(() => (cached || []).map(toCard));
  const [loading, setLoading] = useState(!cached);
  const [loadError, setLoadError] = useState('');
  const [showModal, setShowModal] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();
  // Set by the plan page after a successful Save. Cleared once shown, so a refresh does not show it again.
  useEffect(() => {
    if (!location.state?.saved) return;
    navigate(location.pathname, { replace: true, state: null });
    alertSuccess('Your data saved successfully', location.state.clientName ? `${location.state.clientName}'s plan has been updated.` : '');
  }, [location.state, location.pathname, navigate]);

  useEffect(() => {
    listClients()
      .then((rows) => setClients(rows.map(toCard)))
      .catch((err) => setLoadError(err.message))
      .finally(() => setLoading(false));
  }, []);

  const closeModal = useCallback(() => setShowModal(false), []);
  const askDelete = async (card) => {
    const deleted = await confirmDeleteClient(card.name, () => deleteClient(card.id));
    if (!deleted) return;
    setClients((prev) => prev.filter((c) => c.id !== card.id));
    alertSuccess('Client deleted successfully', `${card.name} and all their financial information have been removed.`);
  };
  const handleCreated = (client) => {
    setClients((prev) => [toCard(client), ...prev]);
    setShowModal(false);
    alertSuccess('Client added successfully', `${client.title} ${client.full_name}'s plan has been created.`);
  };

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    const list = clients.filter((c) => {
      if (q && !`${c.name} ${c.fullName}`.toLowerCase().includes(q)) return false;
      return filter === 'all' || status(c) === filter;
    });
    const sorters = {
      recent: (a, b) => new Date(b.updated) - new Date(a.updated),
      name: (a, b) => a.name.localeCompare(b.name),
      networth: (a, b) => b.netWorth - a.netWorth,
    };
    return [...list].sort(sorters[sort]);
  }, [clients, query, filter, sort]);

  const totals = useMemo(() => ({
    count: clients.length,
    netWorth: clients.reduce((s, c) => s + c.netWorth, 0),
    surplus: clients.reduce((s, c) => s + c.surplus, 0),
    negative: clients.filter((c) => c.surplus < 0).length,
  }), [clients]);

  return (
    <div className="reports">
      <header className="reports__header">
        <div>
          <p className="reports__eyebrow">CLIENT REPORTS</p>
          <h1>Client Reports</h1>
          <p className="reports__sub">Build and manage financial statements for each family or client.</p>
        </div>
        <button type="button" className="reports__new" onClick={() => setShowModal(true)}><Plus size={16} /> New Client Plan</button>
      </header>

      <section className="reports__stats">
        <div className="stat"><span>TOTAL CLIENTS</span><strong>{totals.count}</strong></div>
        <div className="stat"><span>COMBINED NET WORTH</span><strong>{crore(totals.netWorth)}</strong></div>
        <div className="stat"><span>MONTHLY SURPLUS</span><strong>{inr(totals.surplus)}</strong></div>
        <div className="stat"><span>NEED ATTENTION</span><strong>{totals.negative} negative</strong></div>
      </section>

      <section className="reports__toolbar">
        <label className="reports__search">
          <Search size={15} />
          <input
            type="text"
            placeholder="Search client plans…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
        <div className="reports__filters">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              type="button"
              className={`chip${filter === f.key ? ' chip--active' : ''}`}
              onClick={() => setFilter(f.key)}
            >
              {f.label}
            </button>
          ))}
        </div>
        <label className="reports__sort">
          Sort by
          <select value={sort} onChange={(e) => setSort(e.target.value)}>
            <option value="recent">Recently updated</option>
            <option value="name">Name (A–Z)</option>
            <option value="networth">Net worth</option>
          </select>
        </label>
      </section>

      <p className="reports__count">Showing {visible.length} of {clients.length} clients</p>

      {loading && <p className="reports__state">Loading clients…</p>}
      {loadError && <p className="reports__state reports__state--error" role="alert">{loadError}</p>}
      {!loading && !loadError && clients.length === 0 && (
        <p className="reports__state">No client plans yet. Click "New Client Plan" to add your first one.</p>
      )}

      <div className="reports__grid">
        {visible.map((c) => {
          const st = status(c);
          return (
            // The whole card opens the report. The Open report button stays for keyboard and screen-reader users.
            <article key={c.id} className="client client--link" onClick={() => navigate(`/client-reports/${c.id}`)}>
              <div className="client__top">
                <span className="client__avatar">{c.initials}</span>
                <div className="client__id">
                  <h3>{c.name}{c.isNew && <em className="client__new">NEW</em>}</h3>
                  <span>{c.fullName}</span>
                </div>
                <span className={`badge badge--${st}`}>{STATUS_LABEL[st]}</span>
                <button type="button" className="client__delete" aria-label={`Delete ${c.name}`} title="Delete client" onClick={(e) => { e.stopPropagation(); askDelete(c); }}>
                  <Trash2 size={15} />
                </button>
              </div>

              <div className="client__figures">
                <div><span>NET WORTH</span><strong>{inr(c.netWorth)}</strong></div>
                <div>
                  <span>MONTHLY SURPLUS</span>
                  <strong className={`amount--${st}`}>{inr(c.surplus)}</strong>
                </div>
              </div>

              <div className="client__meta">
                <span><User size={12} /> {c.members} family members</span>
                <span>Updated {formatDate(c.updated)}</span>
              </div>
              <p className="client__advisor">Advisor: {c.advisor}</p>

              <button type="button" className="client__open" onClick={(e) => { e.stopPropagation(); navigate(`/client-reports/${c.id}`); }}>
                Open report <ArrowRight size={14} />
              </button>
            </article>
          );
        })}
      </div>

      {showModal && <NewClientModal onClose={closeModal} onCreated={handleCreated} />}
    </div>
  );
}
