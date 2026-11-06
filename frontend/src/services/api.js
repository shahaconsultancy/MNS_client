import axios from 'axios';

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000/api';

const http = axios.create({
  baseURL: API_BASE_URL,
  headers: { 'Content-Type': 'application/json' },
});

export async function apiRequest(path, { method = 'GET', body, token } = {}) {
  try {
    const { data } = await http.request({
      url: path,
      method,
      data: body,
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    });
    return data;
  } catch (err) {
    if (!err.response) throw new Error('Unable to reach the server. Please try again.');
    const data = err.response.data && typeof err.response.data === 'object' ? err.response.data : {};
    const fieldError = Object.values(data).flat().find((v) => typeof v === 'string');
    throw new Error(data.detail || data.message || fieldError || 'Something went wrong.');
  }
}
