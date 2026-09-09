import {
  request,
  configureSession,
  studyPath,
  historyQuery,
  snapshotQuery,
} from './js/api.js';
import { html, icon, escape, brand, fields, sample } from './js/ui.js';
import { login, newStudy, resultCard } from './js/views.js';
import {
  historyView,
  historyStats,
  historyTable,
  modelFilter,
  snapshotDialog,
} from './js/history.js';
import { studyCard, predictionRows } from './js/study-card.js';
import { monitoring } from './js/monitoring.js';

const app = document.querySelector('#app');
const titles = {
  new: 'Новый прогноз',
  history: 'История прогнозов',
  monitoring: 'Мониторинг',
};
const defaultFilters = () => ({
  patient: '',
  from: '',
  to: '',
  feedback: 'all',
  models: ['champion'],
});
const state = {
  user: null,
  route: 'new',
  result: null,
  draft: {},
  filters: defaultFilters(),
  history: null,
  selected: null,
};
let generation = 0;
let predictionVersion = 0;
let connectionTimer;
let navigation = 0;
let cardVersion = 0;
let sessionTimer;
let searchTimer;
let toastTimer;
let historyRequest;

function toast(message) {
  const element = document.querySelector('#toast');
  element.textContent = message;
  element.classList.add('visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove('visible'), 4000);
}

function showError(selector, error) {
  const element = document.querySelector(selector);
  if (!element) return;
  element.textContent = error.message;
  element.classList.remove('hidden');
}

function endSession() {
  generation++;
  predictionVersion++;
  clearTimeout(connectionTimer);
  navigation++;
  cardVersion++;
  historyRequest?.abort();
  clearTimeout(sessionTimer);
  clearTimeout(searchTimer);
  Object.assign(state, {
    user: null,
    result: null,
    draft: {},
    selected: null,
    route: 'new',
    history: null,
    filters: defaultFilters(),
  });
  configureSession(null, endSession);
  render();
}

function applySession(session) {
  state.user = session.user;
  configureSession(session.csrf_token, endSession);
  clearTimeout(sessionTimer);
  sessionTimer = setTimeout(() => {
    endSession();
    toast('Сессия истекла. Войдите снова.');
  }, session.expires_in * 1000);
}

function shell(content) {
  const admin = state.user.role === 'admin';
  return html`
    <aside class="sidebar" id="sidebar">
      ${brand(false)}
      <div class="eyebrow nav-group">Рабочее пространство</div>
      <nav class="nav" aria-label="Основная навигация">
        ${Object.entries(titles)
          .filter(([route]) => admin || route === 'new')
          .map(
            ([route, title]) => html`
              <a
                href="#${route}"
                class="${state.route === route ? 'active' : ''}"
                ${state.route === route ? 'aria-current="page"' : ''}
              >
                ${icon(
                  { new: 'plus', history: 'history', monitoring: 'chart' }[route],
                )}${title}
              </a>
            `,
          )
          .join('')}
      </nav>
      <div class="sidebar-bottom">
        <div class="profile">
          <div class="avatar">${admin ? 'АД' : 'ПЛ'}</div>
          <div>
            <strong>${escape(state.user.username)}</strong>
            <small>${admin ? 'Администратор' : 'Пользователь'}</small>
          </div>
          <button class="icon-btn" data-action="logout" aria-label="Выйти">
            ${icon('logout')}
          </button>
        </div>
      </div>
    </aside>
    <main class="main">
      <header class="topbar">
        <button class="icon-btn mobile-menu" data-action="menu" aria-label="Открыть меню">
          ${icon('menu')}
        </button>
        <div class="breadcrumb">
          ${icon('grid')}
          <span>Рабочее пространство</span>
          <span>/</span>
          <strong>${titles[state.route]}</strong>
        </div>
        <div class="top-meta">
          <span class="connection-status" id="connection-status" role="status">
            Проверяем подключение…
          </span>
        </div>
      </header>
      <div class="content">${content}</div>
    </main>
    <dialog class="modal" id="detail-modal" aria-labelledby="modal-title"></dialog>
    ${admin ? snapshotDialog() : ''}
  `;
}

function render() {
  document.title = `${state.user ? titles[state.route] : 'Вход'} · Diabetes Predict`;
  if (!state.user) {
    app.innerHTML = login(state);
    return;
  }
  if (state.user.role !== 'admin') state.route = 'new';
  const content =
    state.route === 'new'
      ? newStudy(state)
      : state.route === 'history'
        ? historyView(state.filters)
        : monitoring();
  app.innerHTML = shell(content);
  checkConnection();
  if (state.user.role !== 'admin')
    app.querySelectorAll('a[href="#history"]').forEach((link) => link.remove());
  if (state.route === 'new') {
    const form = document.querySelector('#study-form');
    for (const [key, value] of Object.entries(state.draft)) {
      if (form.elements[key]) form.elements[key].value = value;
    }
  }
  document.querySelector('#detail-modal')?.addEventListener('close', () => {
    cardVersion++;
    state.selected = null;
  });
}

async function navigate(route) {
  if (!state.user) return;
  const version = ++navigation,
    currentGeneration = generation;
  try {
    const session = await request('/auth/me');
    if (version !== navigation || currentGeneration !== generation) return;
    applySession(session);
    state.route =
      route in titles && (route === 'new' || state.user.role === 'admin') ? route : 'new';
    cardVersion++;
    state.selected = null;
    historyRequest?.abort();
    window.history.replaceState(null, '', '#' + state.route);
    render();
    if (state.route === 'history') await loadHistory(1);
  } catch (error) {
    toast(error.message);
  }
}

async function loadHistory(page = 1) {
  historyRequest?.abort();
  historyRequest = new AbortController();
  const controller = historyRequest;
  const table = document.querySelector('#history-table');
  if (!table) return;
  const errorBox = document.querySelector('#history-error');
  errorBox.classList.add('hidden');
  if (state.filters.from && state.filters.to && state.filters.from > state.filters.to) {
    showError('#history-error', new Error('Начало периода не может быть позже окончания'));
    return;
  }
  table.innerHTML = '<div class="empty-state">Загружаем прогнозы…</div>';
  try {
    const data = await request('/studies?' + historyQuery(state.filters, page), {
      signal: controller.signal,
    });
    if (controller.signal.aborted || !table.isConnected) return;
    state.history = data;
    table.innerHTML = historyTable(data);
    document.querySelector('#history-stats').innerHTML = historyStats(data);
    const filter = document.querySelector('#model-filter');
    const wasOpen = filter.querySelector('details')?.open;
    filter.innerHTML = modelFilter(data.models, state.filters.models);
    filter.querySelector('details').open = Boolean(wasOpen);
  } catch (error) {
    if (error.name === 'AbortError' || !table.isConnected) return;
    table.innerHTML =
      '<div class="empty-state"><button class="button" data-action="retry-history">Повторить загрузку</button></div>';
    showError('#history-error', error);
  }
}

async function openStudy(reference) {
  if (!reference) return;
  const version = ++cardVersion,
    currentGeneration = generation;
  try {
    const study = await request(studyPath(reference));
    if (version !== cardVersion || currentGeneration !== generation) return;
    state.selected = study;
    const dialog = document.querySelector('#detail-modal');
    dialog.innerHTML = studyCard(study, state.user.role);
    dialog.showModal();
    refreshCard(version);
  } catch (error) {
    if (currentGeneration === generation) toast(error.message);
  }
}

async function refreshCard(version = ++cardVersion) {
  const study = state.selected,
    dialog = document.querySelector('#detail-modal');
  if (!study || !dialog?.open) return;
  const status = dialog.querySelector('#detail-status');
  const retry = dialog.querySelector('#detail-retry');
  retry.classList.add('hidden');
  for (let attempt = 0; attempt < 15; attempt++) {
    if (version !== cardVersion || !dialog.open) return;
    try {
      const updated = await request(studyPath(study));
      if (version !== cardVersion || !dialog.open) return;
      dialog.querySelector('#detail-predictions').innerHTML = predictionRows(
        updated.predictions,
      );
      if (!updated.predictions.some((model) => model.status === 'pending')) {
        status.textContent = '';
        return;
      }
      status.textContent = 'Ожидаем результаты моделей. Карточка обновляется автоматически.';
    } catch (error) {
      if (version !== cardVersion) return;
      status.textContent = error.message;
      retry.classList.remove('hidden');
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
  if (version === cardVersion && dialog.open) {
    status.textContent = 'Не все результаты готовы. Обновите карточку позже.';
    retry.classList.remove('hidden');
  }
}

async function submitLogin(form) {
  const button = form.querySelector('[type="submit"]');
  button.disabled = true;
  try {
    const session = await request('/auth/login', {
      method: 'POST',
      body: Object.fromEntries(new FormData(form)),
    });
    generation++;
    applySession(session);
    form.reset();
    state.route = 'new';
    window.history.replaceState(null, '', '#new');
    render();
  } catch (error) {
    showError('#login-error', error);
  } finally {
    button.disabled = false;
  }
}

async function submitPrediction(form) {
  const version = ++predictionVersion;
  const values = Object.fromEntries(new FormData(form));
  state.draft = { ...values };
  const features = Object.fromEntries(fields.map(([key]) => [key, Number(values[key])]));
  const button = form.querySelector('[type="submit"]');
  const currentGeneration = generation;
  button.disabled = true;
  button.textContent = 'Выполняется прогноз…';
  document.querySelector('#study-error').classList.add('hidden');
  try {
    const result = await request('/predict', {
      method: 'POST',
      body: {
        patient_code: values.patient.toUpperCase(),
        study_date: values.date,
        ...features,
      },
    });
    if (currentGeneration !== generation || version !== predictionVersion) return;
    state.result = {
      patient: values.patient.toUpperCase(),
      date: values.date,
      prediction: result.prediction,
      cached: result.cached,
      probability: Math.round(result.probability * 1000) / 10,
    };
    const region = document.querySelector('#result-region');
    if (region) region.innerHTML = resultCard(state);
  } catch (error) {
    if (currentGeneration === generation) showError('#study-error', error);
  } finally {
    button.disabled = false;
    button.textContent = 'Получить прогноз';
  }
}

async function submitFeedback(form) {
  if (state.user?.role !== 'admin' || !state.selected?.can_feedback) return;
  const reference = state.selected,
    version = cardVersion;
  const button = form.querySelector('[type="submit"]');
  button.disabled = true;
  try {
    const values = new FormData(form);
    const identity = { patient_code: reference.patient, study_date: reference.date };
    const features = Object.fromEntries(fields.map(([key]) => [key, Number(values.get(key))]));
    const outcome = values.get('outcome');
    button.textContent = 'Сохраняем…';
    const saved = await request(studyPath(reference), {
      method: 'PUT',
      body: {
        input: { ...identity, ...features },
        original: { ...identity, ...reference.features },
        true_label: outcome === null ? null : Number(outcome),
        original_feedback: reference.feedback,
      },
    });
    if (version !== cardVersion) return;
    document.querySelector('#detail-modal').close();
    const messages = [];
    if (saved.recalculated) messages.push('Прогнозы пересчитаны');
    if (saved.feedback_changed) messages.push('Обратная связь сохранена');
    toast(messages.length ? messages.join('. ') : 'Нет изменений для сохранения');
    if (
      state.result?.patient === reference.patient &&
      state.result?.date === reference.date &&
      saved.recalculated
    ) {
      state.result = {
        patient: reference.patient,
        date: reference.date,
        prediction: saved.champion.prediction,
        probability: Math.round(saved.champion.probability * 1000) / 10,
        cached: false,
      };
      state.draft = { patient: reference.patient, date: reference.date, ...features };
      const inputForm = document.querySelector('#study-form');
      if (inputForm)
        for (const [key, value] of Object.entries(features))
          inputForm.elements[key].value = value;
      const region = document.querySelector('#result-region');
      if (region) region.innerHTML = resultCard(state);
    }
    if (state.route === 'history') await loadHistory(state.history?.page || 1);
  } catch (error) {
    if (version === cardVersion) showError('#feedback-error', error);
  } finally {
    button.disabled = false;
    button.textContent = 'Сохранить';
  }
}

function openSnapshot() {
  const dialog = document.querySelector('#snapshot-modal');
  const form = dialog.querySelector('form');
  form.reset();
  form.elements.name.setCustomValidity('');
  dialog.querySelector('#snapshot-error').classList.add('hidden');
  dialog.dataset.query = snapshotQuery(state.filters).toString();
  dialog.querySelector('#snapshot-period').textContent =
    `Исследования с обратной связью. Период: ${state.filters.from || 'с начала истории'} — ${state.filters.to || 'по последнюю дату'}.`;
  dialog.oncancel = (event) => {
    if (form.dataset.saving) event.preventDefault();
  };
  dialog.showModal();
}

async function downloadSnapshot(form) {
  if (form.dataset.saving) return;
  const input = form.elements.name;
  const name = input.value.trim();
  const invalid = !name || name.length > 100 || /[<>:"/\\|?*\x00-\x1f\x7f]/.test(name);
  input.setCustomValidity(invalid ? 'Введите название от 1 до 100 символов без <>:"/\\|?* и переносов строк.' : '');
  if (!form.reportValidity()) return;
  const dialog = form.closest('dialog');
  const button = form.querySelector('[type="submit"]');
  const controls = dialog.querySelectorAll('button, input');
  form.dataset.saving = 'true';
  controls.forEach((control) => { control.disabled = true; });
  button.textContent = 'Создаём снимок…';
  dialog.querySelector('#snapshot-error').classList.add('hidden');
  const currentGeneration = generation;
  try {
    const blob = await request('/studies/snapshot?' + dialog.dataset.query, {
      method: 'POST',
      body: { name },
      download: true,
    });
    if (currentGeneration !== generation) return;
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `snapshot-${name}.csv`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    dialog.close();
    toast('Снимок сохранён и зарегистрирован для обучения');
  } catch (error) {
    if (dialog.isConnected) showError('#snapshot-error', error);
  } finally {
    delete form.dataset.saving;
    controls.forEach((control) => { control.disabled = false; });
    button.textContent = 'Создать и скачать';
  }
}

const actions = {
  'clear-form': () => {
    predictionVersion++;
    state.draft = {};
    state.result = null;
    render();
    document.querySelector('#patient').focus();
  },
  menu: () => document.querySelector('#sidebar').classList.toggle('open'),
  close: () => document.querySelector('#detail-modal').close(),
  'open-result': () => openStudy(state.result),
  'open-study': (button) =>
    openStudy(state.history?.items.find((row) => row.id === Number(button.dataset.id))),
  'refresh-card': () => refreshCard(),
  page: (button) => loadHistory(Number(button.dataset.page)),
  'retry-history': () => loadHistory(state.history?.page || 1),
  snapshot: openSnapshot,
  'close-snapshot': () => document.querySelector('#snapshot-modal').close(),
  'reset-filters': () => {
    state.filters = defaultFilters();
    render();
    loadHistory(1);
  },
  fill: () => {
    const form = document.querySelector('#study-form');
    form.elements.patient.value = 'DEM001';
    for (const [key, value] of Object.entries(sample)) form.elements[key].value = value;
    state.draft = Object.fromEntries(new FormData(form));
    form.querySelectorAll('input').forEach(validateField);
  },
  logout: async (button) => {
    button.disabled = true;
    try {
      await request('/auth/logout', { method: 'POST' });
      endSession();
    } catch {
      toast('Не удалось завершить сессию. Повторите выход после восстановления соединения.');
    } finally {
      button.disabled = false;
    }
  },
};

app.addEventListener('click', (event) => {
  const button = event.target.closest('[data-action]');
  if (button) actions[button.dataset.action]?.(button);
  if (event.target.closest('#show-password')) {
    const input = document.querySelector('#password');
    input.type = input.type === 'password' ? 'text' : 'password';
    document
      .querySelector('#show-password')
      .setAttribute(
        'aria-label',
        input.type === 'password' ? 'Показать пароль' : 'Скрыть пароль',
      );
  }
});

app.addEventListener('submit', (event) => {
  event.preventDefault();
  const handlers = {
    'login-form': submitLogin,
    'study-form': submitPrediction,
    'feedback-form': submitFeedback,
    'snapshot-form': downloadSnapshot,
  };
  if (event.target.id === 'history-filters') {
    clearTimeout(searchTimer);
    loadHistory(1);
    return;
  }
  handlers[event.target.id]?.(event.target);
});

app.addEventListener('input', (event) => {
  if (event.target.id === 'snapshot-name') event.target.setCustomValidity('');
  if (event.target.form?.id === 'feedback-form' && event.target.type === 'number')
    validateField(event.target);
  if (event.target.form?.id === 'study-form') {
    state.draft = Object.fromEntries(new FormData(event.target.form));
    validateField(event.target);
  }
  if (event.target.form?.id !== 'history-filters' || event.target.name === 'model') return;
  state.filters = readFilters(event.target.form);
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadHistory(1), 300);
});

app.addEventListener('focusout', (event) => {
  if (event.target.id === 'patient') validateField(event.target, true);
});

app.addEventListener(
  'invalid',
  (event) => {
    if (event.target.form?.id === 'study-form') validateField(event.target, true);
  },
  true,
);

app.addEventListener('change', (event) => {
  if (event.target.form?.id === 'history-filters') {
    state.filters = readFilters(event.target.form);
    clearTimeout(searchTimer);
    loadHistory(1);
  }
});
window.addEventListener('hashchange', () => navigate(location.hash.slice(1)));

async function start() {
  app.innerHTML = '<main class="login-panel"><p role="status">Проверяем вход…</p></main>';
  try {
    applySession(await request('/auth/me'));
    await navigate(location.hash.slice(1) || 'new');
  } catch (error) {
    render();
    if (error.status !== 401) toast(error.message);
  }
}
configureSession(null, endSession);
start();

function validateField(input, complete = false) {
  if (input.name === 'patient') {
    const value = input.value;
    const validPrefix = /^(?:[A-Za-z]{0,3}|[A-Za-z]{3}[0-9]{0,3})$/.test(value);
    const validCode = /^[A-Za-z]{3}[0-9]{3}$/.test(value);
    const message =
      'Формат кода пациента - сначала строго 3 латинские буквы, затем 3 цифры, без пробелов.';
    // Полный формат обязателен при отправке; правильный незавершённый ввод не ругаем.
    input.setCustomValidity(value === '' || validCode ? '' : message);
    const invalid = complete === true ? !validCode : !validPrefix;
    input.setAttribute('aria-invalid', String(invalid));
    document.getElementById('patient-error').textContent = invalid ? message : '';
    return;
  }
  if (input.name === 'diabetes_pedigree_function') {
    input.setCustomValidity(
      input.value !== '' && Number(input.value) <= 0
        ? 'Значение должно быть больше 0 и не больше 3'
        : '',
    );
  }
  const target = document.getElementById(input.id + '-error');
  if (!target) return;
  const invalid = !input.validity.valid;
  input.setAttribute('aria-invalid', String(invalid));
  target.textContent = invalid ? input.validationMessage : '';
}

async function checkConnection() {
  clearTimeout(connectionTimer);
  const element = document.querySelector('#connection-status');
  if (!state.user || !element) return;
  try {
    const health = await request('/health', { signal: AbortSignal.timeout(5000) });
    if (health.status !== 'ok') throw new Error('Unavailable');
    if (!element.isConnected) return;
    element.classList.remove('offline');
    element.textContent = '● Подключено к API';
  } catch {
    if (!element.isConnected) return;
    element.classList.add('offline');
    element.textContent = '● API недоступен';
  }
  if (state.user && element.isConnected) connectionTimer = setTimeout(checkConnection, 30000);
}

function readFilters(form) {
  const values = new FormData(form);
  const models = values.getAll('model');
  return {
    patient: values.get('patient'),
    from: values.get('from'),
    to: values.get('to'),
    feedback: values.get('feedback'),
    models: models.length ? models : ['champion'],
  };
}
