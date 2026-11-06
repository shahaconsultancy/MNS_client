import axios from 'axios';
import { getAccessToken, logout, refreshAccessToken } from './authService.js';

const axiosClient = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000/api',
});

// Attach the JWT to every request.
axiosClient.interceptors.request.use((config) => {
  const token = getAccessToken();
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

// On a 401 refresh the token once and retry; otherwise replace error.message with something readable.
axiosClient.interceptors.response.use(
  (response) => response,
  async (error) => {
    if (axios.isCancel(error)) return Promise.reject(error);

    const original = error.config;
    if (error.response?.status === 401 && !original.retried) {
      original.retried = true;
      if (await refreshAccessToken()) return axiosClient(original);
      logout();
      window.location.replace('/login');
      error.message = 'Your session has expired. Please sign in again.';
      return Promise.reject(error);
    }

    if (!error.response) {
      error.message = 'Unable to reach the server. Please try again.';
    } else {
      const data = error.response.data || {};
      const fieldError = Object.values(data).flat().find((v) => typeof v === 'string');
      error.message = data.detail || fieldError || 'Something went wrong.';
    }
    return Promise.reject(error);
  },
);

export default axiosClient;
