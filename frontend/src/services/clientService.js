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

export const listClients = () => authed('/clients/');

export const createClient = (payload) => authed('/clients/', { method: 'POST', body: payload });

export const getPlan = (id) => authed(`/clients/${id}/plan/`);

export const savePlan = (id, payload) => authed(`/clients/${id}/plan/`, { method: 'PUT', body: payload });
