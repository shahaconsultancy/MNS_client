import { apiRequest } from './api.js';

const TOKEN_KEY = 'mns_access_token';
const REFRESH_KEY = 'mns_refresh_token';

export async function login({ email, password, remember }) {
  const data = await apiRequest('/auth/login/', {
    method: 'POST',
    body: { email: email.trim().toLowerCase(), password },
  });

  // "Keep me signed in" -> localStorage, otherwise cleared when the tab closes.
  const storage = remember ? localStorage : sessionStorage;
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(REFRESH_KEY);
  sessionStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(REFRESH_KEY);
  storage.setItem(TOKEN_KEY, data.access);
  storage.setItem(REFRESH_KEY, data.refresh);
  saveUserEmail(data.user?.email || email.trim().toLowerCase(), remember);
  return data;
}

export function getAccessToken() {
  return localStorage.getItem(TOKEN_KEY) || sessionStorage.getItem(TOKEN_KEY);
}

export function logout() {
  [localStorage, sessionStorage].forEach((s) => {
    s.removeItem(TOKEN_KEY);
    s.removeItem(REFRESH_KEY);
    s.removeItem(EMAIL_KEY);
  });
}

const EMAIL_KEY = 'mns_user_email';

export function saveUserEmail(email, remember) {
  localStorage.removeItem(EMAIL_KEY);
  sessionStorage.removeItem(EMAIL_KEY);
  (remember ? localStorage : sessionStorage).setItem(EMAIL_KEY, email);
}

export function getUserEmail() {
  return localStorage.getItem(EMAIL_KEY) || sessionStorage.getItem(EMAIL_KEY) || '';
}

// Exchange the refresh token for a new access token. Returns false if the session is over.
export async function refreshAccessToken() {
  const storage = [localStorage, sessionStorage].find((s) => s.getItem(REFRESH_KEY));
  if (!storage) return false;
  try {
    const data = await apiRequest('/auth/refresh/', {
      method: 'POST',
      body: { refresh: storage.getItem(REFRESH_KEY) },
    });
    storage.setItem(TOKEN_KEY, data.access);
    if (data.refresh) storage.setItem(REFRESH_KEY, data.refresh);
    return true;
  } catch {
    return false;
  }
}

// Forgot password: the secret key plus the account's email set a new password (no sign-in needed).
export function resetPassword({ email, key, newPassword, confirmPassword }) {
  return apiRequest('/auth/reset-password/', {
    method: 'POST',
    body: { email: email.trim().toLowerCase(), key, new_password: newPassword, confirm_password: confirmPassword },
  });
}
