import { apiRequest } from './api.js';
import { getAccessToken, logout, refreshAccessToken } from './authService.js';

// Run an authenticated request; on an expired token refresh once, then retry.
async function authed(path, options = {}) {
  try {
    return await apiRequest(path, { ...options, token: getAccessToken() });
  } catch (err) {
    if (!/token/i.test(err.message)) throw err;
    if (await refreshAccessToken()) {
      return apiRequest(path, { ...options, token: getAccessToken() });
    }
    logout();
    window.location.replace('/login');
    throw new Error('Your session has expired. Please sign in again.');
  }
}

// In-memory caches so a plan page can paint instantly from what the list page already fetched,
// then refresh from the server in the background.
const clientCache = new Map();
const planCache = new Map();

export const getCachedClient = (id) => clientCache.get(String(id)) || planCache.get(String(id))?.client || null;
export const getCachedPlan = (id) => planCache.get(String(id)) || null;

// The client list also survives a page refresh (sessionStorage; cleared on sign-out and when the tab closes),
// so the list page can paint instantly and refresh quietly.
export const LIST_KEY = 'mns_clients_cache';
const readList = () => {
  try { return JSON.parse(sessionStorage.getItem(LIST_KEY)) || null; } catch { return null; }
};
const writeList = (rows) => {
  rows.forEach((c) => clientCache.set(String(c.id), c));
  try { sessionStorage.setItem(LIST_KEY, JSON.stringify(rows)); } catch { /* storage full or blocked: cache is optional */ }
};

export const getCachedClients = () => {
  const rows = readList();
  if (rows) rows.forEach((c) => clientCache.set(String(c.id), c));
  return rows;
};

export const listClients = async () => {
  const rows = await authed('/clients/');
  writeList(rows);
  return rows;
};

export const createClient = async (payload) => {
  const c = await authed('/clients/', { method: 'POST', body: payload });
  const list = readList();
  if (list) writeList([c, ...list.filter((r) => r.id !== c.id)]);
  else clientCache.set(String(c.id), c);
  return c;
};

// Permanently deletes the client and their whole plan on the server, then forgets every cached copy.
export const deleteClient = async (id) => {
  await authed(`/clients/${id}/`, { method: 'DELETE' });
  clientCache.delete(String(id));
  planCache.delete(String(id));
  const list = readList();
  if (list) writeList(list.filter((c) => String(c.id) !== String(id)));
};

export const getPlan = async (id) => {
  const data = await authed(`/clients/${id}/plan/`);
  planCache.set(String(id), data);
  return data;
};

export const savePlan = async (id, payload) => {
  const data = await authed(`/clients/${id}/plan/`, { method: 'PUT', body: payload });
  planCache.set(String(id), data);
  const list = readList();
  if (data.client && list) {
    // Keep the remembered client list in step with what was just saved, so the card never shows old figures.
    writeList(list.map((c) => (c.id === data.client.id ? data.client : c)));
  }
  return data;
};
