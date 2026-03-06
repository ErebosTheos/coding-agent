/**
 * app.js - Main Frontend Controller for Drishtikon Foundation LMS
 * Manages authentication, dynamic content loading, and visualization.
 */

import { toggleHighContrast, toggleDyslexiaFont, adjustFontSize, announceToScreenReader } from './accessibility.js';

const API_BASE = '/api/v1';
let currentUser = null;

/**
 * Wrapper for fetch that injects Authorization headers.
 */
async function apiFetch(endpoint, options = {}) {
  const token = localStorage.getItem('df_token');
  const headers = {
    'Content-Type': 'application/json',
    ...options.headers,
  };

  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }

  try {
    const response = await fetch(`${API_BASE}${endpoint}`, { ...options, headers });
    
    if (response.status === 401) {
      logout();
      return null;
    }

    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.detail || 'API request failed');
    }

    return await response.json();
  } catch (err) {
    showToast(err.message, 'error');
    throw err;
  }
}

/**
 * Handles login logic and redirects based on role.
 */
export async function login(email, password) {
  try {
    const data = await apiFetch('/auth/token', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    });

    if (data && data.access_token) {
      localStorage.setItem('df_token', data.access_token);
      currentUser = data.user;
      showToast('Welcome back!', 'success');
      
      // Redirect based on role
      const rolePath = currentUser.role.toLowerCase();
      window.location.href = `/dashboard_${rolePath}.html`;
    }
  } catch (err) {
    console.error('Login failed', err);
  }
}

/**
 * Clears local state and redirects to home.
 */
export function logout() {
  localStorage.removeItem('df_token');
  window.location.href = '/index.html';
}

/**
 * UI Notification helper
 */
export function showToast(message, type = 'success') {
  const container = document.getElementById('toast-container');
  if (!container) return;

  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.innerHTML = `
    <span class="toast-icon">${type === 'success' ? '✓' : '⚠'}</span>
    <span class="toast-msg">${message}</span>
  `;

  container.appendChild(toast);
  announceToScreenReader(`${type}: ${message}`);

  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateX(20px)';
    setTimeout(() => toast.remove(), 300);
  }, 3000);
}

/**
 * Dashboard Initialization & Data Loading
 */
export async function initDashboard() {
  const me = await apiFetch('/me');
  if (!me) return;
  
  currentUser = me;
  document.querySelectorAll('.user-name-display').forEach(el => el.textContent = me.full_name);
  
  // Determine which dashboard to load
  if (window.location.pathname.includes('student')) loadStudentData();
  else if (window.location.pathname.includes('teacher')) loadTeacherData();
  else if (window.location.pathname.includes('admin')) loadAdminData();
}

async function loadStudentData() {
  const dashboard = await apiFetch('/student/dashboard');
  renderStats(dashboard.stats);
  renderCourseList(dashboard.courses);
}

async function loadAdminData() {
  const stats = await apiFetch('/admin/stats');
  renderStats(stats);
  renderUserTable(await apiFetch('/users'));
  renderAnalyticsCharts(stats.analytics);
}

/**
 * Visualization Helpers
 */
function renderBarChart(containerId, data) {
  const container = document.getElementById(containerId);
  if (!container) return;

  container.innerHTML = '';
  container.className = 'chart-container';

  const max = Math.max(...data.map(d => d.value));
  
  data.forEach(item => {
    const height = (item.value / max) * 100;
    const bar = document.createElement('div');
    bar.className = 'chart-bar';
    bar.style.height = `${height}%`;
    bar.setAttribute('data-value', item.value);
    bar.title = `${item.label}: ${item.value}`;
    container.appendChild(bar);
  });
}

function renderDonutChart(containerId, segments) {
  const container = document.getElementById(containerId);
  if (!container) return;

  const size = 200;
  const center = size / 2;
  const radius = 80;
  const circumference = 2 * Math.PI * radius;
  
  let svg = `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" class="donut-chart">`;
  let currentOffset = 0;

  segments.forEach((seg, i) => {
    const percentage = seg.value / segments.reduce((a, b) => a + b.value, 0);
    const dashArray = `${percentage * circumference} ${circumference}`;
    const dashOffset = -currentOffset;
    
    svg += `
      <circle 
        cx="${center}" cy="${center}" r="${radius}"
        fill="transparent"
        stroke="${seg.color || 'var(--primary)'}"
        stroke-width="20"
        stroke-dasharray="${dashArray}"
        stroke-dashoffset="${dashOffset}"
        transform="rotate(-90 ${center} ${center})"
        class="donut-segment"
      />`;
    
    currentOffset += percentage * circumference;
  });

  svg += `
    <text x="50%" y="50%" text-anchor="middle" dy=".3em" fill="white" font-weight="bold">Stats</text>
    </svg>`;
  
  container.innerHTML = svg;
}

/**
 * DOM Event Bindings
 */
document.addEventListener('DOMContentLoaded', () => {
  // A11y Button Listeners
  document.getElementById('btn-high-contrast')?.addEventListener('click', toggleHighContrast);
  document.getElementById('btn-dyslexia')?.addEventListener('click', toggleDyslexiaFont);
  document.getElementById('btn-font-plus')?.addEventListener('click', () => adjustFontSize('large'));
  document.getElementById('btn-font-minus')?.addEventListener('click', () => adjustFontSize('medium'));

  // Auth Form Listeners
  const loginForm = document.getElementById('login-form');
  if (loginForm) {
    loginForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const email = e.target.email.value;
      const password = e.target.password.value;
      await login(email, password);
    });
  }

  // Global Logout
  document.getElementById('logout-link')?.addEventListener('click', (e) => {
    e.preventDefault();
    logout();
  });

  // Auto-init if on dashboard
  if (window.location.pathname.includes('dashboard')) {
    initDashboard();
  }
});