import { html, icon, escape, brand, today, fields, outcomeBadge } from './ui.js';
export function login() {
  return html`
    <main class="login">
      <section class="login-story">
        ${brand(false)}
        <div class="login-art">
          <img src="assets/donut.png" alt="Дружелюбный синий пончик" />
          <h1>
            Данные сегодня.
            <br />
            Забота о завтра.
          </h1>
          <p>
            Введите основные показатели пациента — приложение проанализирует данные и
            сформирует прогноз вероятности диабета с помощью машинного обучения.
          </p>
        </div>
        <footer><span>*реализовано в рамках проекта курса Devops ДПО ИТМО</span></footer>
      </section>
      <section class="login-panel">
        <form class="login-form" id="login-form">
          <p>
            Войдите в рабочее пространство
            <br />
            Diabetes Predict.
          </p>
          <label for="username">Пользователь</label>
          <input
            id="username"
            name="username"
            placeholder="Имя пользователя"
            autocomplete="username"
            required
            maxlength="100"
          />
          <label for="password">Пароль</label>
          <div class="password-wrap">
            <input
              id="password"
              name="password"
              type="password"
              placeholder="Введите пароль"
              autocomplete="current-password"
              maxlength="128"
              required
            />
            <button
              type="button"
              class="icon-btn"
              id="show-password"
              aria-label="Показать пароль"
            >
              ${icon('eye')}
            </button>
          </div>
          <button class="button primary" type="submit">
            Войти в систему ${icon('arrow')}
          </button>
          <div id="login-error" class="inline-error hidden" role="alert"></div>
        </form>
      </section>
    </main>
  `;
}

export function field([key, label, unit, min, max, step]) {
  return html`
    <div class="field">
      <label for="${key}">
        ${label}
        <span class="required">*</span>
      </label>
      <div class="input-unit">
        <input
          type="number"
          id="${key}"
          name="${key}"
          placeholder="${key === 'diabetes_pedigree_function' ? '0.000' : '0'}"
          min="${min}"
          max="${max}"
          step="${step}"
          required
          inputmode="decimal"
          aria-describedby="${key}-range ${key}-error"
        />
        ${unit
          ? html`
              <span>${unit}</span>
            `
          : ''}
      </div>
      <small id="${key}-range">
        ${key === 'diabetes_pedigree_function'
          ? 'Больше 0'
          : 'От ' + min.toLocaleString('ru-RU')}
        до ${max.toLocaleString('ru-RU')}${step === 1 ? ', целое число' : ''}
      </small>
      <small id="${key}-error" class="field-error" aria-live="polite"></small>
    </div>
  `;
}

export function newStudy(state) {
  return html`
    <div class="page-title">
      <div>
        <h1>Новый прогноз</h1>
        <p>Введите показатели пациента, чтобы получить прогноз.</p>
      </div>
      <a href="#history" class="button">${icon('history')}История прогнозов</a>
    </div>
    <div class="study-layout">
      <section class="card">
        <div class="card-head">
          <div class="head-icon">${icon('file')}</div>
          <div>
            <h2>Данные исследования</h2>
            <p>Все поля обязательны для заполнения.</p>
            <p class="missing-measurements">
              Для глюкозы, артериального давления, толщины кожной складки, инсулина и индекса
              массы тела значение 0 означает отсутствующий замер. Модель заменит его медианой,
              рассчитанной на обучающих данных.
            </p>
          </div>
        </div>
        <form id="study-form" class="form-body">
          <div class="fields">
            <div class="field">
              <label for="patient">
                Код пациента
                <span class="required">*</span>
              </label>
              <input
                id="patient"
                name="patient"
                placeholder="PAT001"
                pattern="[A-Za-z]{3}[0-9]{3}"
                maxlength="6"
                required
                autocomplete="off"
                style="text-transform:uppercase"
              />
              <small>3 латинские буквы и 3 цифры</small>
            </div>
            <div class="field">
              <label for="date">
                Дата исследования
                <span class="required">*</span>
              </label>
              <input id="date" name="date" type="date" value="${today()}" required />
              <small>Одно исследование в день</small>
            </div>
          </div>
          <div class="divider"></div>
          <div class="form-section">
            Медицинские показатели
            <span>Введите исходные значения</span>
          </div>
          <div class="fields">${fields.map(field).join('')}</div>
          <div id="study-error" class="inline-error hidden" role="alert"></div>
          <div class="form-foot">
            <button class="button primary" id="predict-button" type="submit">
              Получить прогноз
            </button>
            <button
              class="button outline-yellow"
              type="button"
              data-action="fill"
              title="Подставить демонстрационные показатели"
            >
              Пример
            </button>
            <button class="button outline-red" type="button" data-action="clear-form">
              Очистить
            </button>
          </div>
        </form>
      </section>
      <aside class="result-column">
        <section class="card result-card" aria-live="polite" id="result-region">
          ${resultCard(state)}
        </section>
      </aside>
    </div>
  `;
}

export function resultCard(state) {
  const r = state.result;
  return html`
    <div class="card-head"><h2>Результат прогноза</h2></div>
    ${r
      ? html`
          <div class="result-value">
            <div class="ring" style="--value:${r.probability}">
              <strong>
                ${r.probability.toLocaleString('ru-RU')}
                <small>%</small>
              </strong>
            </div>
            ${outcomeBadge(r.prediction)}
            <p>
              Вероятность положительного класса
              <br />
              Исследование · ${escape(r.patient)}
            </p>
          </div>
        `
      : html`
          <div class="result-empty">
            <img src="assets/donut.png" alt="Синий пончик ждёт данные" />
            <h3>Здесь появится результат</h3>
            <p>
              Заполните данные исследования
              <br />
              и нажмите «Получить прогноз».
            </p>
          </div>
        `}${r
      ? '<button class="button primary study-detail-button" data-action="open-result">Открыть карточку исследования</button>'
      : ''}
    ${r?.cached
      ? '<p class="repeat-notice" role="status"><span class="repeat-mark" aria-hidden="true">!</span><span>Этот прогноз уже выполнялся. Показан сохранённый результат; повторная запись в истории не создаётся.</span></p>'
      : ''}
    <div class="result-footer">
      <span>Статус</span>
      <strong class="${r ? 'status-ready' : 'status-waiting'}">
        ${r ? 'Прогноз получен' : 'Ожидание данных'}
      </strong>
    </div>
  `;
}
