const icons = {
  plus: '<path d="M12 5v14M5 12h14"/>',
  history: '<path d="M3 11a9 9 0 1 1 2.8 7M3 4v7h7M12 7v5l3 2"/>',
  chart: '<path d="M4 4v16h16M8 15l4-5 4 2 4-7"/>',
  grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  user: '<circle cx="12" cy="8" r="3"/><path d="M5 21v-3a7 7 0 0 1 14 0v3"/>',
  logout: '<path d="M9 4H4v16h5M10 12h11m-4-4 4 4-4 4"/>',
  arrow: '<path d="M4 12h16m-6-6 6 6-6 6"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/>',
  shield: '<path d="M12 3 4 6v6c0 5 8 9 8 9s8-4 8-9V6l-8-3Z"/><path d="m8 12 3 3 5-6"/>',
  file: '<path d="M14 3H5v18h14V8l-5-5ZM14 3v5h5M8 12h8M8 16h6"/>',
  edit: '<path d="m15 4 5 5M4 20l5-1L21 7l-5-5L4 14v6Z"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  eye: '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
};
export const icon = (name) => html`
  <svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.6"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    ${icons[name] || icons.file}
  </svg>
`;
export const escape = (value) =>
  String(value).replace(
    /[&<>"']/g,
    (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char],
  );
export const brand = (showIcon = true) => html`
  <div class="brand">
    ${showIcon
      ? '<img src="assets/donut.png" alt="Синий пончик — символ Diabetes Predict">'
      : ''}
    <div>
      Diabetes
      <span>Predict</span>
    </div>
  </div>
`;
export const today = () => new Date().toLocaleDateString('en-CA');
export const formatDate = (value) => new Date(value + 'T12:00:00').toLocaleDateString('ru-RU');
export const fields = [
  ['pregnancies', 'Количество беременностей', '', 0, 20, 1],
  ['insulin', 'Уровень инсулина', 'мкЕд/мл', 0, 1000, 'any'],
  ['glucose', 'Уровень глюкозы', 'мг/дл', 0, 600, 'any'],
  ['bmi', 'Индекс массы тела', 'кг/м²', 0, 100, 'any'],
  ['blood_pressure', 'Артериальное давление', 'мм рт. ст.', 0, 200, 'any'],
  ['diabetes_pedigree_function', 'Наследственный фактор', '', 0, 3, 'any'],
  ['skin_thickness', 'Толщина кожной складки', 'мм', 0, 110, 'any'],
  ['age', 'Возраст', 'лет', 1, 120, 1],
];
export const sample = {
  pregnancies: 6,
  glucose: 148,
  blood_pressure: 72,
  skin_thickness: 35,
  insulin: 0,
  bmi: 33.6,
  diabetes_pedigree_function: 0.627,
  age: 50,
};

// Шаблоны содержат доверенную разметку; все значения из API нужно экранировать.
export const html = String.raw;
export function percentage(value) {
  return value == null
    ? '—'
    : (value * 100).toLocaleString('ru-RU', { maximumFractionDigits: 1 }) + '%';
}
export function outcomeBadge(value) {
  if (value == null) return '<span class="badge gray">Ожидание результата</span>';
  return value === 1
    ? '<span class="badge red">Положительный</span>'
    : '<span class="badge green">Отрицательный</span>';
}
export function feedbackBadge(value) {
  if (value == null) return '<span class="badge gray">Не заполнена</span>';
  return html`
    <span class="badge ${value ? 'red' : 'green'}">
      ${value ? 'Подтвержден' : 'Не подтвержден'}
    </span>
  `;
}
