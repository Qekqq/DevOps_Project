import { html, icon, escape, fields } from './ui.js';
import { request } from './api.js';

const names = { accuracy: 'Accuracy', recall: 'Recall', precision: 'Precision', f1: 'F1',
  specificity: 'Специфичность', npv: 'NPV', roc_auc: 'ROC AUC', log_loss: 'Log loss' };
const colors = ['#829bd8', '#69aaa0', '#d3a16d', '#ac8dca'];
const descriptions = {
  accuracy: 'Доля верных прогнозов среди исследований с исходом.',
  recall: 'Доля выявленных положительных исходов: TP / (TP + FN).',
  precision: 'Доля подтверждённых положительных исходов среди положительных прогнозов.',
  f1: 'Баланс Precision и Recall: 2TP / (2TP + FP + FN).',
  specificity: 'Доля верно распознанных отрицательных исходов: TN / (TN + FP).',
  npv: 'Доля отрицательных исходов среди отрицательных прогнозов: TN / (TN + FN).',
  roc_auc: 'Показатель того, насколько хорошо вероятности разделяют положительный и отрицательный классы.',
  log_loss: 'Ошибка вероятностей: уверенные неверные прогнозы штрафуются сильнее.',
};
const fmt = (n, digits = 3) => n == null ? '—' : Number(n).toLocaleString('ru-RU', { maximumFractionDigits: digits });
const percent = n => n == null ? '—' : `${fmt(n * 100, 1)}%`;
const featureName = name => fields.find(f => f[0] === name)?.[1] || name;
const displayDate = value => value.split('-').reverse().join('.');
function inclusiveEnd(value) {
  const date = new Date(`${value}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() - 1);
  return date.toISOString().slice(0, 10);
}
function tip(label, text) {
  return `<span class="ml-tip" tabindex="0" aria-label="${escape(`${label}. ${text}`)}">${escape(label)}<span class="ml-tooltip" role="tooltip">${escape(text)}</span></span>`;
}

// Screening baselines, not clinical acceptance thresholds or Evidently test defaults.
// Expected confusion proportions for a random classifier matching class prevalence.
function assessment(key, point) {
  const value = point.metrics[key];
  if (value == null || !Number.isFinite(value)) return { bad: false, missing: true };
  const p = point.evaluated ? point.actual_positive / point.evaluated : 0;
  if (!(p > 0 && p < 1)) return { bad: false };
  const baseline = { accuracy: Math.max(p, 1 - p), recall: p, precision: p, f1: p,
    specificity: 1 - p, npv: 1 - p, roc_auc: 0.5, log_loss: -p * Math.log(p) - (1 - p) * Math.log(1 - p) }[key];
  const bad = key === 'log_loss' ? value > baseline + 1e-12 : key === 'roc_auc' ? value <= baseline : value < baseline - 1e-12;
  return { bad };
}

export function monitoring() {
  return html`
    <div class="page-title"><div><h1>Мониторинг</h1>
      <p>Работа сервисов, качество моделей и входных данных.</p></div>
      <a class="button" href="#history">${icon('history')}История прогнозов</a></div>
    <div class="monitoring-toolbar" role="group" aria-label="Раздел мониторинга">
      <button class="button primary" data-monitoring-section="models" aria-pressed="true">Метрики моделей</button>
      <button class="button" data-monitoring-section="operations" aria-pressed="false">Работа приложения</button>
    </div>
    <section id="model-monitoring" aria-label="Метрики моделей">
      <form id="model-report-filters" class="ml-filters">
        <label>Версия модели<select name="model_version"><option value="">Загрузка…</option></select></label>
        <label>Временная шкала<select name="time_axis">
          <option value="prediction_time">Время запроса прогноза (UTC)</option>
          <option value="study_date">Дата исследования</option></select></label>
        <label>С даты<input type="date" name="date_from"></label>
        <label>По дату<input type="date" name="date_to"></label>
        <label>Интервал<select name="resolution"><option value="day">День</option>
          <option value="week">Неделя</option><option value="month" selected>Месяц</option></select></label>
        <button class="button primary" type="submit">Рассчитать</button>
        <button class="button" type="button" id="ml-all-history">Вся история</button>
      </form>
      <p id="model-report-status" role="status" aria-live="polite">Загружаем историю…</p>
      <div id="model-report-content"></div>
    </section>
    <section id="operations-monitoring" aria-label="Работа приложения" hidden>
      <div class="monitoring-toolbar">
        <select id="monitoring-dashboard-select" aria-label="Выбрать дашборд"></select>
        <a id="monitoring-open" class="button monitoring-grafana-link" href="/grafana/" target="_blank" rel="noopener"
          title="Открыть в Grafana" aria-label="Открыть в Grafana">
          <img src="/assets/grafana.svg" alt="" width="26" height="26">
        </a></div>
      <p id="monitoring-status" role="status"></p>
      <iframe id="operations-dashboard" class="monitoring-dashboard monitoring-operations" title="Работа приложения" hidden></iframe>
    </section>`;
}

// Local SVG: no CDN, raw patient records or separate chart server.
function chart(title, points, series, { maximum = 1, threshold = null } = {}) {
  const values = points.flatMap(p => series.map(s => s.value(p))).filter(v => v != null && Number.isFinite(v));
  const max = Math.max(maximum, ...values, threshold || 0);
  const x = i => 52 + (points.length === 1 ? 420 : i * 840 / (points.length - 1));
  const y = v => 190 - v / max * 160;
  const ticks = [0, max / 2, max].map(v => `<line x1="52" x2="892" y1="${y(v)}" y2="${y(v)}" class="ml-grid"/>
    <text x="42" y="${y(v) + 4}" text-anchor="end">${fmt(v, 2)}</text>`).join('');
  const lines = series.map((s, si) => {
    let previous = null;
    return points.map((p, i) => {
      const v = s.value(p);
      if (v == null || !Number.isFinite(v)) { previous = null; return ''; }
      const line = previous == null ? '' : `<line x1="${x(previous.i)}" y1="${y(previous.v)}" x2="${x(i)}" y2="${y(v)}" stroke="${colors[si]}" stroke-width="2"/>`;
      previous = { i, v };
      const alert = s.key ? assessment(s.key, p).bad : threshold != null && v >= threshold;
      const label = `${p.start}: ${s.name} ${fmt(v)}, прогнозов ${p.samples}, исходов ${p.evaluated}${alert ? ', требует проверки' : ''}`;
      return `${line}<circle cx="${x(i)}" cy="${y(v)}" r="4" fill="${colors[si]}" tabindex="0"
        ${alert ? 'stroke="#a63e4b" stroke-width="2"' : ''}
        role="button" data-point="${i}" aria-label="${escape(label)}"><title>${escape(label)}</title></circle>`;
    }).join('');
  }).join('');
  const labelCount = Math.min(6, points.length);
  const labelIndices = Array.from({ length: labelCount }, (_, i) => labelCount === 1 ? 0 : Math.round(i * (points.length - 1) / (labelCount - 1)));
  const labels = labelIndices.map(i => `<text x="${x(i)}" y="220" text-anchor="middle">${displayDate(points[i].start)}</text>`).join('');
  return `<article class="ml-panel"><h3>${escape(title)}</h3>
    <div class="ml-legend">${series.map((s, i) => `<span><i style="background:${colors[i]}"></i>${s.description ? tip(s.name, s.description) : escape(s.name)}</span>`).join('')}</div>
    ${values.length ? `<svg class="ml-chart" viewBox="0 0 940 236" role="group" aria-label="${escape(title)}">${ticks}
      ${threshold == null ? '' : `<line x1="52" x2="892" y1="${y(threshold)}" y2="${y(threshold)}" stroke="#c84747" stroke-dasharray="5 5"/>`}
      ${lines}${labels}</svg>` : '<p class="ml-empty">Недостаточно данных для расчёта.</p>'}</article>`;
}

function details(point, report) {
  const m = point.confusion;
  return `<article class="ml-panel"><h3>${point === report.summary ? 'Весь выбранный период' : `Интервал с ${displayDate(point.start)} по ${displayDate(inclusiveEnd(point.end_exclusive))} (включительно)`}</h3>
    <p>Все прогнозы: <b>${point.samples}</b> · С исходом: <b>${point.evaluated}</b> · Без исхода:
      <b>${point.missing_outcomes}</b> · Фактический класс 1: <b>${point.actual_positive}</b></p>
    ${point.small_sample ? '<p class="ml-notice">Мало наблюдений: метрики могут сильно меняться от одного исхода.</p>' : ''}
    <div class="ml-metrics">${Object.entries(names).map(([key, name]) => {
      const state = assessment(key, point);
      return `<div class="${state.bad ? 'ml-metric-bad' : state.missing ? 'ml-metric-missing' : ''}">${tip(name, descriptions[key])}<strong>${fmt(point.metrics[key])}</strong>
        ${state.bad ? '<small>Требует проверки</small>' : state.missing ? '<small>Недостаточно данных</small>' : ''}</div>`;
    }).join('')}</div>
    <details><summary>Матрица ошибок</summary>
      <table class="ml-table"><thead><tr><th>Фактический исход</th><th>Прогноз 0</th><th>Прогноз 1</th></tr></thead><tbody>
      <tr><th>0 — отрицательный</th><td>TN: ${m.tn}</td><td>FP: ${m.fp}</td></tr>
      <tr><th>1 — положительный</th><td>FN: ${m.fn}</td><td>TP: ${m.tp}</td></tr></tbody></table>
    </details>
    <div class="ml-table-wrap"><table class="ml-table"><caption>Качество данных и drift относительно train выбранной версии</caption>
      <thead><tr><th>Показатель</th><th>Пропуски</th><th>Доля</th><th>${tip('PSI', 'Изменение распределения признака относительно обучающей выборки.')}</th><th>Наблюдений / train</th><th>Состояние</th></tr></thead>
      <tbody>${Object.entries(point.drift).map(([name, d]) => `<tr><td>${escape(featureName(name))}</td><td>${point.missing[name]}</td>
        <td>${point.samples ? percent(point.missing[name] / point.samples) : '—'}</td><td class="${d.detected ? 'ml-alert-cell' : ''}">${fmt(d.psi)}</td>
        <td>${d.samples} / ${d.reference_samples}</td><td class="${d.psi == null || d.detected ? 'ml-alert-text' : ''}">${d.psi == null ? 'Недостаточно данных' : d.detected ? 'Обнаружено смещение' : 'Смещение не обнаружено'}</td></tr>`).join('')}</tbody></table></div></article>`;
}

function renderReport(root, report) {
  const points = report.points;
  root.innerHTML = `<div class="ml-summary-bar"><h2>${escape(report.model_version)} · ${escape(report.date_from)} — ${escape(report.date_to)}</h2>
    <button type="button" class="button" id="ml-export">Скачать CSV</button></div>
    <p class="section-caption">${escape(report.engine)} · Эталон: train, ${report.reference_samples} строк.
      Исключено исследований из датасета этой версии: ${report.excluded_training}.
      Качество модели — по известным исходам; качество данных и drift — по всем оставшимся прогнозам.</p>
    ${report.reference_error ? `<p class="ml-notice">${escape(report.reference_error)}</p>` : ''}
    ${details(report.summary, report)}
    <div class="ml-panel"><label>Метрика на графике <select id="ml-quality"><option value="overview">Recall, Precision, F1, Accuracy</option>
      ${Object.entries(names).map(([key, name]) => `<option value="${key}">${name}</option>`).join('')}</select></label></div>
    <div id="ml-quality-chart"></div>
    ${chart('Полнота обратной связи', points, [
      { name: 'Все прогнозы', value: p => p.samples },
      { name: 'С исходом', value: p => p.evaluated },
      { name: 'Без исхода', value: p => p.missing_outcomes }])}
    ${chart('Доля положительного класса', points, [
      { name: 'Все прогнозы', value: p => p.predicted_positive_rate },
      { name: 'С исходом', value: p => p.actual_positive_rate }])}
    <div class="ml-panel"><label>Признак для графика drift <select id="ml-feature">${fields.map(f => `<option value="${f[0]}">${escape(f[1])}</option>`).join('')}</select></label></div>
    <div id="ml-drift-chart"></div>
    ${chart('Качество данных во времени', points, [{ name: 'Доля пропущенных значений', value: p => p.missing_rate }])}
    <article class="ml-panel"><label>Подробности интервала <select id="ml-point"><option value="">Выберите интервал или точку графика</option>
      ${points.map((p, i) => `<option value="${i}">${escape(p.start)} — ${p.samples} прогнозов, ${p.evaluated} исходов</option>`).join('')}</select></label></article>
    <div id="ml-point-details"></div>`;
  const feature = root.querySelector('#ml-feature');
  const quality = root.querySelector('#ml-quality');
  const drawQuality = () => {
    const keys = quality.value === 'overview' ? ['recall', 'precision', 'f1', 'accuracy'] : [quality.value];
    root.querySelector('#ml-quality-chart').innerHTML = chart('Качество модели во времени', points,
      keys.map(key => ({ key, name: names[key], description: descriptions[key], value: p => p.metrics[key] })));
  };
  quality.addEventListener('change', drawQuality); drawQuality();
  const drawDrift = () => {
    root.querySelector('#ml-drift-chart').innerHTML = chart('Drift входных данных (PSI)', points,
      [{ name: featureName(feature.value), description: 'PSI показывает изменение распределения признака относительно обучающей выборки.', value: p => p.drift[feature.value].psi }],
      { maximum: 0.5, threshold: report.psi_threshold });
  };
  feature.value = 'glucose'; feature.addEventListener('change', drawDrift); drawDrift();
  const pointSelect = root.querySelector('#ml-point');
  const showPoint = () => {
    root.querySelector('#ml-point-details').innerHTML = pointSelect.value === '' ? '' : details(points[Number(pointSelect.value)], report);
  };
  pointSelect.addEventListener('change', showPoint);
  const activatePoint = event => {
    const point = event.target.closest('[data-point]');
    if (!point || (event.type === 'keydown' && !['Enter', ' '].includes(event.key))) return;
    event.preventDefault(); pointSelect.value = point.dataset.point; showPoint();
    root.querySelector('#ml-point-details').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  };
  root.onclick = activatePoint; root.onkeydown = activatePoint;
  root.querySelector('#ml-export').addEventListener('click', () => {
    const featureKeys = Object.keys(report.summary.drift);
    const rows = [['model_version', 'time_axis', 'timezone', 'start', 'end_exclusive', 'samples', 'evaluated', 'missing_outcomes', 'actual_positive', ...Object.keys(names), ...featureKeys.map(k => `${k}_psi`), ...featureKeys.map(k => `${k}_missing`)]];
    points.forEach(p => rows.push([report.model_version, report.time_axis, report.timezone, p.start, p.end_exclusive, p.samples, p.evaluated, p.missing_outcomes, p.actual_positive,
      ...Object.keys(names).map(k => p.metrics[k] ?? ''), ...featureKeys.map(k => p.drift[k].psi ?? ''), ...featureKeys.map(k => p.missing[k])]));
    const cell = value => `"${String(value).replace(/^[=+@-]/, "'$&").replaceAll('"', '""')}"`;
    const url = URL.createObjectURL(new Blob(['\ufeff', rows.map(r => r.map(cell).join(',')).join('\r\n')], { type: 'text/csv;charset=utf-8' }));
    const a = document.createElement('a'); a.href = url; a.download = 'model-monitoring.csv'; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
}

export async function loadDashboards() {
  const form = document.querySelector('#model-report-filters');
  const content = document.querySelector('#model-report-content');
  const status = document.querySelector('#model-report-status');
  if (!form) return;
  let busy = false;
  const calculate = async () => {
    if (busy) return;
    busy = true;
    const query = new URLSearchParams(new FormData(form));
    for (const [key, value] of [...query]) if (!value) query.delete(key);
    form.querySelectorAll('input,select,button').forEach(el => { el.disabled = true; });
    status.textContent = 'Рассчитываем по актуальным данным…'; content.replaceChildren();
    try {
      const report = await request(`/monitoring/model-report?${query}`);
      if (!form.isConnected) return;
      form.elements.model_version.replaceChildren(...report.available_models.map(m => new Option(`${m.name} / ${m.version} (${m.role})`, m.version)));
      form.elements.model_version.value = report.model_version;
      form.elements.date_from.value = report.date_from; form.elements.date_to.value = report.date_to;
      renderReport(content, report);
      status.textContent = report.summary.samples ? 'Расчёт завершён. Нажмите на точку графика для подробностей интервала.' : 'В выбранном периоде нет подходящих прогнозов.';
    } catch (error) { if (status.isConnected) status.textContent = error.message; }
    finally {
      busy = false;
      if (form.isConnected) form.querySelectorAll('input,select,button').forEach(el => { el.disabled = false; });
    }
  };
  form.addEventListener('submit', e => { e.preventDefault(); calculate(); });
  document.querySelector('#ml-all-history').addEventListener('click', () => {
    form.elements.date_from.value = ''; form.elements.date_to.value = ''; calculate();
  });
  ['time_axis', 'model_version'].forEach(name => form.elements[name].addEventListener('change', () => {
    form.elements.date_from.value = ''; form.elements.date_to.value = ''; calculate();
  }));
  let operationsLoaded = false;
  document.querySelectorAll('[data-monitoring-section]').forEach(button => button.addEventListener('click', () => {
    const operations = button.dataset.monitoringSection === 'operations';
    document.querySelector('#model-monitoring').hidden = operations;
    document.querySelector('#operations-monitoring').hidden = !operations;
    document.querySelectorAll('[data-monitoring-section]').forEach(b => {
      const active = b === button; b.classList.toggle('primary', active); b.setAttribute('aria-pressed', String(active));
    });
    if (operations && !operationsLoaded) { operationsLoaded = true; loadOperations(); }
  }));
  await calculate();
}

async function loadOperations() {
  const select = document.querySelector('#monitoring-dashboard-select');
  const frame = document.querySelector('#operations-dashboard');
  const status = document.querySelector('#monitoring-status');
  try {
    const response = await fetch('/grafana/api/search?type=dash-db&limit=1000', { credentials: 'same-origin', cache: 'no-store' });
    if (!response.ok) throw new Error('Не удалось загрузить Grafana.');
    const dashboards = (await response.json()).filter(d => typeof d.title === 'string' && /^[a-zA-Z0-9_-]{1,128}$/.test(d.uid));
    if (!select.isConnected) return;
    select.replaceChildren(...dashboards.map(d => new Option(d.title, d.uid)));
    if (!dashboards.length) {
      select.add(new Option('Дашбордов пока нет', ''));
      select.disabled = true;
    }
    const show = () => {
      if (!select.value) return;
      const url = `/grafana/d/${encodeURIComponent(select.value)}?from=now-30d&to=now`;
      document.querySelector('#monitoring-open').href = url;
      frame.src = `${url}&kiosk&hideLogo=true`; frame.hidden = false;
    };
    select.addEventListener('change', show);
    if (dashboards.some(d => d.uid === 'fastapi-observability')) select.value = 'fastapi-observability';
    status.textContent = dashboards.length ? 'Сохранённые дашборды Grafana. Метрики качества моделей доступны в соседнем разделе.' : 'Сохранённых дашбордов пока нет.';
    show();
  } catch (error) { if (status.isConnected) status.textContent = error.message; }
}
