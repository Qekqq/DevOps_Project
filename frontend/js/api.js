// Cookie сессии недоступна JavaScript. CSRF-токен хранится только в памяти.
let csrfToken = null;
let onUnauthorized = () => {};
let sessionRevision = 0;

export function configureSession(csrf, handler) {
  sessionRevision++;
  csrfToken = csrf;
  onUnauthorized = handler;
}

export async function request(path, { method = 'GET', body, signal, download = false } = {}) {
  const revision = sessionRevision;
  let response;
  try {
    response = await fetch(`/api${path}`, {
      method,
      signal,
      credentials: 'same-origin',
      cache: 'no-store',
      headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'DiabetesPredict',
        ...(csrfToken ? { 'X-CSRF-Token': csrfToken } : {}),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new Error('Не удалось подключиться к серверу. Повторите попытку.');
  }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    if (response.status === 401 && path !== '/auth/login' && revision === sessionRevision) {
      onUnauthorized();
    }
    const detail =
      typeof data.detail === 'string'
        ? data.detail
        : data.detail?.map((item) => item.msg).join('. ');
    const error = new Error(detail || 'Не удалось выполнить запрос');
    error.status = response.status;
    throw error;
  }
  if (response.status === 204) return null;
  return download ? response.blob() : response.json();
}

export function studyPath(study) {
  return `/studies/${encodeURIComponent(study.patient)}/${encodeURIComponent(study.date)}`;
}

export function historyQuery(filters, page) {
  const query = new URLSearchParams({ page, page_size: 10, feedback: filters.feedback });
  for (const model of filters.models || ['champion']) query.append('model', model);
  if (filters.patient) query.set('patient', filters.patient);
  if (filters.from) query.set('date_from', filters.from);
  if (filters.to) query.set('date_to', filters.to);
  return query;
}

export function snapshotQuery(filters) {
  const query = new URLSearchParams();
  if (filters.from) query.set('date_from', filters.from);
  if (filters.to) query.set('date_to', filters.to);
  return query;
}
