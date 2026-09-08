import { html, icon } from './ui.js';

// UID сохраняется при переименовании дашборда в Grafana.
const operationsDashboard = '/grafana/d/fastapi-observability/backend';

export function monitoring() {
  return html`
    <div class="page-title">
      <div>
        <h1>Мониторинг</h1>
        <p>Показатели работы приложения.</p>
      </div>
      <a class="button" href="#history">${icon('history')}История прогнозов</a>
    </div>
    <div class="monitoring-toolbar">
      <label class="filter-label" for="monitoring-dashboard-select">Дашборд</label>
      <div class="filter">
        <select id="monitoring-dashboard-select" aria-controls="operations-dashboard">
          <option value="operations">Работа приложения</option>
        </select>
      </div>
      <a class="button" href="${operationsDashboard}" target="_blank" rel="noopener">
        Открыть в Grafana
      </a>
      <a class="button" href="/grafana/dashboard/new" target="_blank" rel="noopener">
        ${icon('plus')}Создать дашборд
      </a>
    </div>
    <p class="section-caption">
      Изменения, сохранённые в Grafana, появятся при следующем открытии дашборда.
    </p>
    <iframe
      id="operations-dashboard"
      class="monitoring-dashboard monitoring-operations"
      title="Работа приложения"
      src="${operationsDashboard}?kiosk&refresh=30s"
    ></iframe>
  `;
}
