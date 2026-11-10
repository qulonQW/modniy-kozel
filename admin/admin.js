// Admin panel for products. Vanilla JS, served by the backend at /admin/.
//
// Auth: every request carries Telegram.WebApp.initData in X-Telegram-Init-Data;
// the server verifies it. Nothing is persisted in browser storage.
// Safety: all data is rendered with textContent / createElement only (no HTML
// string parsing anywhere; tests/test_admin_page.py enforces this).

(() => {
  'use strict';

  const API = '/api/admin';
  const PAGE_SIZE = 50;
  const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
  const IMAGE_TYPES = ['image/jpeg', 'image/png', 'image/webp'];

  const tg = window.Telegram && window.Telegram.WebApp;
  const initData = (tg && tg.initData) || '';

  const STATUS_LABELS = { draft: 'Чернетка', published: 'Опубліковано', hidden: 'Приховано' };
  const REASON_LABELS = {
    caption_not_first: 'Підпис був не на першому фото альбому — перевірте, чи всі фото від цього товару',
    no_drop_price: 'У пості немає дроп-ціни — вкажіть ціни вручну',
    multiple_drop_prices: 'У пості кілька дроп-цін — вкажіть ціни вручну',
    price_out_of_range: 'Підозріла ціна постачальника — перевірте',
    price_unlabeled: 'У пості є ціна без позначки',
    category_unknown: 'Категорію не визначено (тимчасово стоїть «Одяг»)',
    generic_title: 'Назва без бренду чи моделі — уточніть назву',
    title_missing: 'У пості немає назви',
    sizes_missing: 'Розміри не знайдено',
    sizes_unparsed: 'Розміри не вдалося розпізнати',
    photo_rejected: 'Частину фото відхилено під час імпорту',
    possible_duplicate: 'Можливий дубль: такі самі фото є в іншого товару (список нижче)',
  };

  // ---------------------------------------------------------------- helpers

  const $ = (id) => document.getElementById(id);

  /** createElement with properties; string children become text nodes. */
  function h(tag, props, ...children) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value === undefined || value === null || value === false) continue;
      if (key === 'className') el.className = value;
      else if (key === 'dataset') Object.assign(el.dataset, value);
      else if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
      else if (key in el && typeof value !== 'string') el[key] = value;
      else el.setAttribute(key, value === true ? '' : String(value));
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  const formatPrice = (value) => `${String(value).replace(/\B(?=(\d{3})+$)/g, ' ')} грн`;

  let toastTimer = null;
  function toast(message, isError = false) {
    const el = $('toast');
    el.textContent = message;
    el.className = isError ? 'toast toast-error' : 'toast';
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, isError ? 6000 : 2500);
  }

  function confirmAction(message) {
    return new Promise((resolve) => {
      if (tg && tg.showConfirm && initData) tg.showConfirm(message, resolve);
      else resolve(window.confirm(message));
    });
  }

  // -------------------------------------------------------------------- API

  class ApiError extends Error {
    constructor(status, detail) {
      super(detail || `HTTP ${status}`);
      this.status = status;
    }
  }

  async function api(method, path, body) {
    const headers = { 'X-Telegram-Init-Data': initData };
    const options = { method, headers, cache: 'no-store' };
    if (body instanceof FormData) options.body = body;
    else if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(API + path, options);
    } catch {
      throw new ApiError(0, 'network');
    }
    if (res.status === 204) return null;
    const data = await res.json().catch(() => null);
    if (!res.ok) throw new ApiError(res.status, data && typeof data.detail === 'string' ? data.detail : '');
    return data;
  }

  function errorText(err) {
    switch (err.status) {
      case 0: return 'Немає зв’язку з сервером. Перевірте інтернет і спробуйте ще раз.';
      case 401: return 'Сесія Telegram недійсна або застаріла (понад 1 годину). Закрийте адмінку й відкрийте знову з бота.';
      case 403: return 'Немає доступу: ваш Telegram-акаунт не в списку адміністраторів.';
      case 404: return 'Не знайдено — можливо, товар уже видалено.';
      case 409:
        if (err.message.startsWith('publish_blocked')) {
          return 'Не можна опублікувати: товар потребує перевірки або ціна для покупця 0. Виправте поля або позначте перевіреним.';
        }
        return 'Конфлікт: ' + err.message;
      case 411:
      case 413: return 'Файл завеликий (максимум 5 МБ).';
      case 415: return 'Дозволені лише фото JPG, PNG або WebP.';
      case 422: return 'Перевірте поля: щось заповнено неправильно (назва 1–120 символів, ціни ≥ 0, до 20 розмірів).';
      case 429: return 'Забагато запитів. Зачекайте хвилину.';
      default: return 'Помилка сервера. Спробуйте ще раз.';
    }
  }

  async function run(action, button) {
    if (button) button.disabled = true;
    try {
      return await action();
    } catch (err) {
      if (!(err instanceof ApiError)) console.error(err);
      toast(err instanceof ApiError ? errorText(err) : 'Щось пішло не так.', true);
      return undefined;
    } finally {
      if (button) button.disabled = false;
    }
  }

  // ------------------------------------------------------------ list view

  const state = {
    items: [],
    total: 0,
    selected: new Set(),
    filters: { search: '', status: '', category: '', needsReview: false, duplicates: false },
    current: null, // product being edited (null = new)
    listScroll: 0,
  };

  let listRequest = 0;

  function listQuery(offset) {
    const params = new URLSearchParams({ limit: PAGE_SIZE, offset });
    const f = state.filters;
    if (f.search) params.set('search', f.search);
    if (f.status) params.set('status', f.status);
    if (f.category) params.set('category', f.category);
    if (f.needsReview) params.set('needs_review', 'true');
    if (f.duplicates) params.set('duplicates', 'true');
    return `/products?${params}`;
  }

  async function loadList(append = false) {
    const requestId = ++listRequest;
    const offset = append ? state.items.length : 0;
    const data = await run(() => api('GET', listQuery(offset)), $('load-more'));
    if (!data || requestId !== listRequest) return; // a newer filter won
    state.items = append ? state.items.concat(data.items) : data.items;
    state.total = data.total;
    if (!append) state.selected.clear();
    renderList();
  }

  function statusBadge(status) {
    return h('span', { className: `status status-${status}` }, STATUS_LABELS[status] || status);
  }

  function productRow(p) {
    const checkbox = h('input', {
      type: 'checkbox',
      checked: state.selected.has(p.id),
      'aria-label': 'Вибрати',
      onclick: (e) => e.stopPropagation(),
      onchange: (e) => {
        if (e.target.checked) state.selected.add(p.id);
        else state.selected.delete(p.id);
        renderBulkbar();
      },
    });
    const cover = p.images[0];
    const thumb = cover
      ? h('img', { className: 'thumb', src: cover.url, alt: '', loading: 'lazy' })
      : h('div', { className: 'thumb' });
    const meta = h('div', { className: 'row-meta' },
      h('span', { className: p.price_retail > 0 ? '' : 'price-zero' }, formatPrice(p.price_retail)),
      statusBadge(p.status),
      h('span', null, p.category),
      p.needs_review ? h('span', { className: 'flag' }, '⚠ перевірка') : null,
      p.duplicate_group ? h('span', { className: 'flag flag-dup' }, p.duplicate_group === p.id ? '⧉ є дублі' : '⧉ дубль?') : null,
      p.images.length ? h('span', null, `${p.images.length} фото`) : h('span', { className: 'price-zero' }, 'без фото'),
    );
    return h('li', { className: 'product-row', onclick: () => openEditor(p.id) },
      checkbox, thumb, h('div', { className: 'row-body' }, h('div', { className: 'row-title' }, p.title), meta));
  }

  function renderList() {
    const list = $('product-list');
    list.replaceChildren(...state.items.map(productRow));
    if (!state.items.length) list.append(h('li', { className: 'empty' }, 'Нічого не знайдено.'));
    $('summary').textContent = `Знайдено: ${state.total}` +
      (state.items.length < state.total ? ` · показано ${state.items.length}` : '');
    $('load-more').hidden = state.items.length >= state.total;
    renderBulkbar();
  }

  function renderBulkbar() {
    const count = state.selected.size;
    $('bulkbar').hidden = count === 0;
    $('bulk-count').textContent = `Вибрано: ${count}`;
  }

  async function bulkStatus(status, button) {
    const ids = [...state.selected];
    const result = await run(() => api('POST', '/products/bulk-status', { ids, status }), button);
    if (!result) return;
    if (result.blocked.length) {
      toast(`Оновлено: ${result.updated}. Не опубліковано ${result.blocked.length} — потребують перевірки або ціна 0.`, true);
    } else {
      toast(`Оновлено: ${result.updated}`);
    }
    await loadList();
  }

  // ------------------------------------------------------------ edit view

  const form = $('edit-form');

  function showView(name) {
    const editing = name === 'edit';
    if (editing) state.listScroll = window.scrollY;
    $('list-view').hidden = editing;
    $('edit-view').hidden = !editing;
    if (tg && initData && tg.BackButton) {
      if (editing) tg.BackButton.show();
      else tg.BackButton.hide();
    }
    window.scrollTo(0, editing ? 0 : state.listScroll);
  }

  async function openEditor(id) {
    if (id) {
      const product = await run(() => api('GET', `/products/${encodeURIComponent(id)}`));
      if (!product) return;
      state.current = product;
    } else {
      state.current = null;
    }
    renderEditor();
    showView('edit');
  }

  function fillForm(p) {
    const f = form.elements;
    f.title.value = p ? p.title : '';
    f.description.value = p ? p.description : '';
    f.category.value = p ? p.category : 'Одяг';
    f.status.value = p ? p.status : 'draft';
    f.price_retail.value = p ? p.price_retail : '';
    f.price_supplier.value = p && p.price_supplier !== null ? p.price_supplier : '';
    f.sizes.value = p ? p.sizes.join(', ') : '';
    f.in_stock.checked = p ? p.in_stock : true;
    f.popular.checked = p ? p.popular : false;
  }

  function renderEditor() {
    const p = state.current;
    fillForm(p);
    $('form-error').hidden = true;
    $('edit-title').textContent = p ? 'Редагування' : 'Новий товар';

    const reasons = p && p.needs_review ? p.review_reasons : [];
    $('review-box').hidden = !(p && p.needs_review);
    $('review-reasons').replaceChildren(
      ...(reasons.length ? reasons : ['']).map((r) => h('li', null, REASON_LABELS[r] || r || 'Перевірте дані товару')),
    );

    renderDuplicates();
    $('images-section').hidden = !p;
    $('danger-zone').hidden = !p;
    renderImages();
    renderPriceHint();
    $('edit-meta').textContent = p
      ? `ID ${p.id} · змінено ${new Date(p.updated_at).toLocaleString('uk-UA')}` +
        (p.source_message_id ? ` · пост постачальника №${p.source_message_id}` : '')
      : '';
  }

  function renderDuplicates() {
    const members = state.current ? state.current.duplicates : [];
    $('duplicate-box').hidden = members.length === 0;
    $('duplicate-list').replaceChildren(...members.map((m) => h('li', null,
      h('button', { type: 'button', className: 'duplicate-item', onclick: () => openEditor(m.id) },
        m.image_url ? h('img', { className: 'thumb', src: m.image_url, alt: '', loading: 'lazy' }) : h('div', { className: 'thumb' }),
        h('div', { className: 'row-body' },
          h('div', { className: 'row-title' }, m.title),
          h('div', { className: 'row-meta' },
            statusBadge(m.status),
            m.is_main ? h('span', { className: 'flag flag-dup' }, 'перший') : null,
            h('span', null, `${m.image_count} фото`),
            h('span', { className: 'muted' }, `ID ${m.id}`))),
      ))));
  }

  function renderPriceHint() {
    const p = state.current;
    const retail = Number(form.elements.price_retail.value);
    const supplier = Number(form.elements.price_supplier.value);
    const parts = [];
    if (retail > 0 && supplier > 0) {
      const markup = Math.round((retail / supplier - 1) * 100);
      parts.push(`Націнка ${markup}% (${formatPrice(retail - supplier)})`);
    }
    if (p && p.price_reference) parts.push(`постачальник радить «купити собі» за ${formatPrice(p.price_reference)}`);
    $('price-hint').textContent = parts.join(' · ');
    $('price-hint').hidden = parts.length === 0;
  }

  function parseSizes(text) {
    return text.split(/[,;\n]+/).map((s) => s.trim()).filter(Boolean);
  }

  function intOrNull(value) {
    return value.trim() === '' ? null : Number(value);
  }

  function readForm() {
    const f = form.elements;
    return {
      title: f.title.value.trim(),
      description: f.description.value.trim(),
      category: f.category.value,
      status: f.status.value,
      price_retail: intOrNull(f.price_retail.value),
      price_supplier: intOrNull(f.price_supplier.value),
      sizes: parseSizes(f.sizes.value),
      in_stock: f.in_stock.checked,
      popular: f.popular.checked,
    };
  }

  function validate(values) {
    if (!values.title || values.title.length > 120) return 'Назва: від 1 до 120 символів.';
    if (values.description.length > 2000) return 'Опис: до 2000 символів.';
    for (const key of ['price_retail', 'price_supplier']) {
      const v = values[key];
      if (key === 'price_retail' && v === null) return 'Вкажіть ціну для покупця.';
      if (v !== null && (!Number.isInteger(v) || v < 0)) return 'Ціни — цілі числа від 0.';
    }
    if (values.sizes.length > 20) return 'Не більше 20 розмірів.';
    if (values.sizes.some((s) => s.length > 10)) return 'Кожен розмір — до 10 символів.';
    if (values.status === 'published' && !(values.price_retail > 0)) return 'Щоб опублікувати, вкажіть ціну для покупця більше 0.';
    return null;
  }

  /** Only changed fields: a status-only change must not count as a review. */
  function diff(product, values) {
    const changes = {};
    for (const [key, value] of Object.entries(values)) {
      const before = product[key];
      const same = Array.isArray(value) ? JSON.stringify(value) === JSON.stringify(before) : value === before;
      if (!same) changes[key] = value;
    }
    return changes;
  }

  async function save(event) {
    event.preventDefault();
    const values = readForm();
    const problem = validate(values);
    const errorEl = $('form-error');
    errorEl.hidden = !problem;
    errorEl.textContent = problem || '';
    if (problem) return;

    const p = state.current;
    if (p) {
      const changes = diff(p, values);
      if (!Object.keys(changes).length) {
        toast('Змін немає');
        return;
      }
      const updated = await run(() => api('PATCH', `/products/${encodeURIComponent(p.id)}`, changes), $('save'));
      if (!updated) return;
      state.current = updated;
      toast('Збережено');
    } else {
      const created = await run(() => api('POST', '/products', values), $('save'));
      if (!created) return;
      state.current = created;
      toast('Товар створено. Тепер можна додати фото.');
    }
    renderEditor();
    refreshListItem(state.current);
  }

  function refreshListItem(product) {
    const index = state.items.findIndex((i) => i.id === product.id);
    if (index >= 0) state.items[index] = product;
  }

  async function confirmReview(event) {
    const p = state.current;
    const updated = await run(
      () => api('PATCH', `/products/${encodeURIComponent(p.id)}`, { needs_review: false }),
      event.currentTarget,
    );
    if (!updated) return;
    state.current = updated;
    refreshListItem(updated);
    renderEditor();
    toast('Позначено як перевірене');
  }

  async function deleteProduct(event) {
    const p = state.current;
    if (!(await confirmAction('Видалити товар назавжди разом із фото?'))) return;
    const ok = await run(() => api('DELETE', `/products/${encodeURIComponent(p.id)}`), event.currentTarget);
    if (ok === undefined) return;
    state.items = state.items.filter((i) => i.id !== p.id);
    state.total = Math.max(0, state.total - 1);
    state.selected.delete(p.id);
    toast('Товар видалено');
    backToList();
  }

  // ---------------------------------------------------------------- images

  function renderImages() {
    const p = state.current;
    const images = p ? p.images : [];
    $('image-grid').replaceChildren(...images.map((img, index) => h('li', { className: 'image-cell' },
      h('img', { src: img.url, alt: `Фото ${index + 1}`, loading: 'lazy' }),
      index === 0 ? h('span', { className: 'cover' }, 'обкладинка') : null,
      h('div', { className: 'image-actions' },
        h('button', { type: 'button', 'aria-label': 'Ліворуч', disabled: index === 0, onclick: (e) => moveImage(index, -1, e) }, '←'),
        h('button', { type: 'button', 'aria-label': 'Праворуч', disabled: index === images.length - 1, onclick: (e) => moveImage(index, 1, e) }, '→'),
        h('button', { type: 'button', 'aria-label': 'Видалити фото', onclick: (e) => deleteImage(img.id, e) }, '✕'),
      ),
    )));
  }

  async function updateImages(request, button) {
    const updated = await run(request, button);
    if (!updated) return;
    state.current = updated;
    refreshListItem(updated);
    renderImages();
  }

  function moveImage(index, delta, event) {
    const ids = state.current.images.map((i) => i.id);
    [ids[index], ids[index + delta]] = [ids[index + delta], ids[index]];
    const path = `/products/${encodeURIComponent(state.current.id)}/images/order`;
    return updateImages(() => api('PUT', path, { image_ids: ids }), event.currentTarget);
  }

  async function deleteImage(imageId, event) {
    const button = event.currentTarget;
    if (!(await confirmAction('Видалити це фото?'))) return;
    const path = `/products/${encodeURIComponent(state.current.id)}/images/${imageId}`;
    await updateImages(() => api('DELETE', path), button);
  }

  async function uploadFiles(event) {
    const input = event.target;
    const files = [...input.files];
    input.value = '';
    for (const file of files) {
      if (!IMAGE_TYPES.includes(file.type)) {
        toast(`«${file.name}»: дозволені лише JPG, PNG або WebP.`, true);
        continue;
      }
      if (file.size > MAX_IMAGE_BYTES) {
        toast(`«${file.name}»: файл більший за 5 МБ.`, true);
        continue;
      }
      const body = new FormData();
      body.append('file', file);
      const path = `/products/${encodeURIComponent(state.current.id)}/images`;
      await updateImages(() => api('POST', path, body));
    }
    if (files.length) toast('Фото завантажено');
  }

  // ------------------------------------------------------------ navigation

  function backToList() {
    showView('list');
    renderList();
  }

  function debounce(fn, ms) {
    let timer;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), ms);
    };
  }

  function init() {
    if (tg && initData) {
      tg.ready();
      tg.expand();
      document.documentElement.classList.add('in-telegram');
      tg.BackButton.onClick(backToList);
    } else {
      const notice = $('env-notice');
      notice.textContent = 'Відкрито поза Telegram — працює лише в режимі розробки (DEV_ADMIN_ID).';
      notice.hidden = false;
    }

    $('search').addEventListener('input', debounce((e) => {
      state.filters.search = e.target.value.trim();
      loadList();
    }, 300));
    $('filter-status').addEventListener('change', (e) => { state.filters.status = e.target.value; loadList(); });
    $('filter-category').addEventListener('change', (e) => { state.filters.category = e.target.value; loadList(); });
    $('filter-review').addEventListener('click', (e) => {
      state.filters.needsReview = !state.filters.needsReview;
      e.currentTarget.setAttribute('aria-pressed', String(state.filters.needsReview));
      loadList();
    });
    $('filter-duplicates').addEventListener('click', (e) => {
      state.filters.duplicates = !state.filters.duplicates;
      e.currentTarget.setAttribute('aria-pressed', String(state.filters.duplicates));
      loadList();
    });
    $('load-more').addEventListener('click', () => loadList(true));
    $('new-product').addEventListener('click', () => openEditor(null));
    $('bulk-publish').addEventListener('click', (e) => bulkStatus('published', e.currentTarget));
    $('bulk-hide').addEventListener('click', (e) => bulkStatus('hidden', e.currentTarget));
    $('bulk-clear').addEventListener('click', () => { state.selected.clear(); renderList(); });

    $('back').addEventListener('click', backToList);
    form.addEventListener('submit', save);
    form.elements.price_retail.addEventListener('input', renderPriceHint);
    form.elements.price_supplier.addEventListener('input', renderPriceHint);
    $('confirm-review').addEventListener('click', confirmReview);
    $('delete-product').addEventListener('click', deleteProduct);
    $('upload').addEventListener('change', uploadFiles);

    loadList();
  }

  init();
})();
