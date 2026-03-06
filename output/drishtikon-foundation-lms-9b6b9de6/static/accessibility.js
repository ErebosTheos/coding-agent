/**
 * accessibility.js - Core A11y enhancements for Drishtikon Foundation LMS
 * Handles theme persistence, font scaling, and screen reader announcements.
 */

const A11Y_STORAGE_KEY = 'df_a11y_prefs';

const defaultPrefs = {
  highContrast: false,
  dyslexiaFont: false,
  fontSize: 'medium', // small, medium, large, x-large
};

let currentPrefs = JSON.parse(localStorage.getItem(A11Y_STORAGE_KEY)) || { ...defaultPrefs };

/**
 * Persists preferences to localStorage and applies them to the document root.
 */
function applyAndSavePrefs() {
  const root = document.documentElement;
  
  // Theme
  if (currentPrefs.highContrast) {
    root.setAttribute('data-theme', 'high-contrast');
  } else {
    root.removeAttribute('data-theme');
  }

  // Font Type
  if (currentPrefs.dyslexiaFont) {
    root.setAttribute('data-font', 'dyslexia');
  } else {
    root.removeAttribute('data-font');
  }

  // Font Size
  root.setAttribute('data-fontsize', currentPrefs.fontSize);

  localStorage.setItem(A11Y_STORAGE_KEY, JSON.stringify(currentPrefs));
}

/**
 * Toggles the high contrast mode (WCAG 2.1 compliance).
 */
export function toggleHighContrast() {
  currentPrefs.highContrast = !currentPrefs.highContrast;
  applyAndSavePrefs();
  const state = currentPrefs.highContrast ? 'enabled' : 'disabled';
  announceToScreenReader(`High contrast mode ${state}`);
}

/**
 * Toggles the OpenDyslexic-inspired font style.
 */
export function toggleDyslexiaFont() {
  currentPrefs.dyslexiaFont = !currentPrefs.dyslexiaFont;
  applyAndSavePrefs();
  const state = currentPrefs.dyslexiaFont ? 'enabled' : 'disabled';
  announceToScreenReader(`Dyslexia friendly font ${state}`);
}

/**
 * Adjusts the root font size based on user selection.
 * @param {string} size - 'small' | 'medium' | 'large' | 'xlarge'
 */
export function adjustFontSize(size) {
  currentPrefs.fontSize = size;
  applyAndSavePrefs();
  announceToScreenReader(`Font size adjusted to ${size}`);
}

/**
 * Sends a message to the hidden ARIA live region for screen readers.
 * @param {string} message - The message to announce.
 */
export function announceToScreenReader(message) {
  const announcer = document.getElementById('announcer');
  if (announcer) {
    announcer.textContent = message;
    // Clear after a delay to allow re-announcement of same message
    setTimeout(() => {
      announcer.textContent = '';
    }, 3000);
  }
}

// Initialize on load
document.addEventListener('DOMContentLoaded', () => {
  applyAndSavePrefs();
});