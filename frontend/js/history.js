import {
  html,
  icon,
  escape,
  formatDate,
  percentage,
  outcomeBadge,
  feedbackBadge,
} from './ui.js';

export function historyView(filters) {
  return html`
    <div class="page-title">
      <div>
        <h1>История прогнозов</h1>
        <p>Сохранённые прогнозы моделей и обратная связь.</p>
      </div>
      <a href="#new" class="button primary">${icon('plus')}Новый прогноз</a>
    </div>
    <button class="button history-refresh" data-action="retry-history">
      ${icon('history')}Обновить
    </button>
    <div id="history-stats" class="stats"></div>
    <section class="card table-card">
      <form id="history-filters" class="toolbar" role="search">
        <div class="filters">
          <div class="filter search">
            <input
              name="patient"
              id="search"
              aria-label="Поиск по коду пациента"
              placeholder="Поиск по коду пациента"
              maxlength="6"
              pattern="[a-zA-Z0-9]*"
              value="${escape(filters.patient)}"
            />
          </div>
          <span class="filter-label">Период</span>
          <div class="filter">
            <input
              name="from"
              type="date"
              aria-label="Начало периода"
              value="${filters.from}"
            />
          </div>
          <span class="muted">—</span>
          <div class="filter">
            <input name="to" type="date" aria-label="Конец периода" value="${filters.to}" />
          </div>
          <div class="filter">
            <select name="feedback" aria-label="Наличие обратной связи">
              ${[
                ['all', 'Вся обратная связь'],
                ['missing', 'Не заполнена'],
                ['filled', 'Заполнена'],
              ]
                .map(
                  ([value, label]) => html`
                    <option value="${value}" ${filters.feedback === value ? 'selected' : ''}>
                      ${label}
                    </option>
                  `,
                )
                .join('')}
            </select>
          </div>
          <div id="model-filter" class="filter">${modelFilter([], filters.models)}</div>
        </div>
        <button type="button" class="button small" data-action="reset-filters">
          Сбросить
        </button>
      </form>
      <div id="history-error" class="inline-error hidden" role="alert"></div>
      <div id="history-table" aria-live="polite"></div>
    </section>
    <div class="snapshot-bar">
      <div class="head-icon">${icon('download')}</div>
      <div>
        <h3>Снимок данных для обучения</h3>
        <p>
          Исследования с обратной связью за выбранный период дат. Поиск по пациенту, модель и
          фильтр обратной связи на снимок не влияют.
        </p>
      </div>
      <button class="button" data-action="snapshot">${icon('download')}Снимок данных</button>
    </div>
  `;
}

export function historyStats(data) {
  return [
    [data.study_total, 'Всего исследований'],
    [data.filled, 'С обратной связью'],
    [data.study_total - data.filled, 'Ожидают обратную связь'],
  ]
    .map(
      ([value, label]) => html`
        <div class="card stat">
          <div class="stat-top">${label}</div>
          <div class="stat-number">${value}</div>
          <small>По выбранным фильтрам</small>
        </div>
      `,
    )
    .join('');
}

export function historyTable(data) {
  if (!data.items.length)
    return '<div class="empty-state">Прогнозы не найдены. Измените фильтры или создайте новый прогноз.</div>';
  const pages = Math.max(1, Math.ceil(data.total / data.page_size));
  return html`
    <div class="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Пациент</th>
            <th>Дата исследования ↓</th>
            <th>Модель</th>
            <th>Результат</th>
            <th>Вероятность</th>
            <th>Обратная связь</th>
            <th>Действие</th>
          </tr>
        </thead>
        <tbody>
          ${data.items
            .map(
              (row) => html`
                <tr>
                  <td>
                    <div class="patient-cell">
                      <span class="patient-square">${icon('user')}</span>
                      <strong>${escape(row.patient)}</strong>
                    </div>
                  </td>
                  <td>${formatDate(row.date)}</td>
                  <td>
                    <strong>${escape(row.model.model_name)}</strong>
                    <br />
                    <span class="muted" title="${escape(row.model.model_version)}">
                      ${escape(row.model.display_version)} /
                      ${row.model.role === 'champion' ? 'Champion' : 'Фоновая'}
                    </span>
                  </td>
                  <td>${outcomeBadge(row.model?.prediction)}</td>
                  <td><strong>${percentage(row.model?.probability)}</strong></td>
                  <td>${feedbackBadge(row.feedback)}</td>
                  <td>
                    <button
                      class="icon-btn feedback-action"
                      data-action="open-study"
                      title="Редактировать"
                      data-id="${row.id}"
                      aria-label="Открыть исследование ${escape(row.patient)} от ${formatDate(
                        row.date,
                      )}"
                    >
                      ${icon('edit')}
                    </button>
                  </td>
                </tr>
              `,
            )
            .join('')}
        </tbody>
      </table>
    </div>
    <div class="table-foot">
      <span>
        ${(data.page - 1) * data.page_size + 1}–${Math.min(
          data.page * data.page_size,
          data.total,
        )}
        из ${data.total}
      </span>
      <div class="page-buttons">
        <button
          data-action="page"
          data-page="${data.page - 1}"
          ${data.page === 1 ? 'disabled' : ''}
          aria-label="Предыдущая страница"
        >
          ‹
        </button>
        <span>${data.page} / ${pages}</span>
        <button
          data-action="page"
          data-page="${data.page + 1}"
          ${data.page === pages ? 'disabled' : ''}
          aria-label="Следующая страница"
        >
          ›
        </button>
      </div>
    </div>
  `;
}

export function modelFilter(models, selected = ['champion']) {
  const options = models.map((model) => [
    model.role === 'champion' ? 'champion' : String(model.id),
    `${model.model_name} · ${model.display_version}`,
    model.role === 'champion' ? 'Champion' : 'Фоновая',
  ]);
  const labels = options
    .filter(([value]) => selected.includes(value))
    .map(([, , role]) => role);
  return html`
    <details class="model-filter">
      <summary>Модель: ${escape(labels.join(', ') || 'Champion')}</summary>
      <div class="model-options">
        ${options
          .map(
            ([value, label]) => html`
              <label>
                <input
                  type="checkbox"
                  name="model"
                  value="${value}"
                  ${selected.includes(value) ? 'checked' : ''}
                />
                <span>${escape(label)}</span>
              </label>
            `,
          )
          .join('')}
      </div>
    </details>
  `;
}
