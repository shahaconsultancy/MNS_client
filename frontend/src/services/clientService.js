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

export const listClients = async () => {
  const rows = await authed('/clients/');
  rows.forEach((c) => clientCache.set(String(c.id), c));
  return rows;
};

export const createClient = async (payload) => {
  const c = await authed('/clients/', { method: 'POST', body: payload });
  clientCache.set(String(c.id), c);
  return c;
};

export const getPlan = async (id) => {
  const data = await authed(`/clients/${id}/plan/`);
  planCache.set(String(id), data);
  return data;
};

export const savePlan = async (id, payload) => {
  const data = await authed(`/clients/${id}/plan/`, { method: 'PUT', body: payload });
  planCache.set(String(id), { ...data, client: getCachedClient(id) });
  return data;
};
