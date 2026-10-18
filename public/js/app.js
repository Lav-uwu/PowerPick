/** PowerPick progressive enhancements. Core forms and navigation also work without JS. */
(() => {
  'use strict';

  const toast = document.getElementById('toast');
  let toastTimer;

  /** Announce cart feedback without inserting untrusted text as HTML. */
  function showToast(message, basketLink = false) {
    if (!toast) return;
    clearTimeout(toastTimer);
    toast.replaceChildren(document.createTextNode(message));
    if (basketLink) {
      const link = document.createElement('a');
      link.href = '/cart';
      link.textContent = 'View basket';
      toast.append(link);
    }
    toast.hidden = false;
    toastTimer = setTimeout(() => { toast.hidden = true; }, 5500);
  }

  /** Enhance add-to-basket forms; delegation also covers freshly filtered cards. */
  document.addEventListener('submit', async (event) => {
    const form = event.target;
    if (form.matches('[data-confirm]') && !window.confirm(form.dataset.confirm)) {
      event.preventDefault();
      return;
    }
    if (!form.matches('[data-add-cart]')) return;
    event.preventDefault();
    const button = form.querySelector('button');
    if (button.disabled) return;
    button.disabled = true;
    try {
      const response = await fetch(form.action, {
        method: 'POST', body: new FormData(form), headers: { Accept: 'application/json' }
      });
      if (!(response.headers.get('content-type') || '').includes('application/json')) {
        throw new Error('Your session may have expired. Refresh this page and try again.');
      }
      const data = await response.json();
      showToast(data.message, response.ok);
      if (response.ok) {
        document.querySelectorAll('[data-cart-count]').forEach((node) => { node.textContent = data.count; });
        document.querySelectorAll('.basket-link').forEach((node) => {
          node.setAttribute('aria-label', `Basket, ${data.count} ${data.count === 1 ? 'item' : 'items'}`);
        });
      }
    } catch (error) {
      showToast(error.message || 'The item could not be added. Please try again.');
    } finally {
      button.disabled = false;
    }
  });

  /** Keep filtering fast and shareable while retaining native GET form semantics. */
  const filters = document.getElementById('catalog-filters');
  const results = document.getElementById('catalog-results');
  if (filters && results) {
    let controller;
    let searchTimer;
    async function refreshCatalog() {
      controller?.abort();
      controller = new AbortController();
      const activeController = controller;
      const params = new URLSearchParams(new FormData(filters));
      for (const [key, value] of [...params]) if (!value) params.delete(key);
      const displayUrl = new URL('/', location.origin);
      displayUrl.search = params.toString();
      displayUrl.hash = 'catalog';
      params.set('partial', '1');
      results.setAttribute('aria-busy', 'true');
      try {
        const response = await fetch(`/?${params}`, { signal: activeController.signal });
        if (!response.ok) throw new Error('Search is unavailable. Please try again.');
        // Only same-origin, server-rendered templates enter this result region.
        results.innerHTML = await response.text();
        history.replaceState(null, '', displayUrl);
      } catch (error) {
        if (error.name !== 'AbortError') showToast(error.message);
      } finally {
        if (!activeController.signal.aborted) results.removeAttribute('aria-busy');
      }
    }
    filters.addEventListener('submit', (event) => { event.preventDefault(); refreshCatalog(); });
    document.addEventListener('change', (event) => {
      if (event.target.form === filters && event.target.type !== 'search') refreshCatalog();
    });
    document.getElementById('search').addEventListener('input', () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(refreshCatalog, 260);
    });
    const details = filters.querySelector('details');
    if (window.matchMedia('(max-width:800px)').matches && details) details.open = false;
  }

  /** Password visibility is a local display change and never reads credentials remotely. */
  document.querySelectorAll('[data-toggle-password]').forEach((button) => {
    button.addEventListener('click', () => {
      const input = document.getElementById(button.getAttribute('aria-controls'));
      const reveal = input.type === 'password';
      input.type = reveal ? 'text' : 'password';
      button.textContent = reveal ? 'Hide' : 'Show';
      button.setAttribute('aria-label', reveal ? 'Hide password' : 'Show password');
    });
  });
})();
