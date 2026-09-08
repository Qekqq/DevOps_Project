import { html, icon, escape, fields, formatDate, percentage, outcomeBadge } from './ui.js';

export function predictionRows(predictions) {
  return predictions
    .map(
      (model) => html`
        <tr>
          <td>
            <strong>${escape(model.model_name)}</strong>
            <br />
            <span class="muted model-version" title="${escape(model.model_version)}">
              ${escape(model.display_version || model.model_version)} /
              ${model.role === 'champion' ? 'Champion' : 'Фоновая'}
            </span>
          </td>
          <td>${outcomeBadge(model.prediction)}</td>
          <td>${percentage(model.probability)}</td>
        </tr>
      `,
    )
    .join('');
}

// Одна карточка и один формат данных для нового прогноза и истории.
export function studyCard(study, role) {
  const editable = role === 'admin' && study.can_feedback;
  return html`
    <div class="modal-head">
      <h2 id="modal-title">Исследование ${escape(study.patient)}</h2>
      <button class="icon-btn" data-action="close" aria-label="Закрыть карточку">
        ${icon('close')}
      </button>
    </div>
    <div class="modal-body">
      <span class="badge gray">${formatDate(study.date)}</span>
      <dl class="detail-grid">
        ${fields
          .map(
            ([key, label, unit, min, max, step]) => html`
              <div>
                <dt>${label}</dt>
                <dd>
                  ${editable
                    ? html`
                        <input
                          class="study-edit-input"
                          form="feedback-form"
                          type="number"
                          id="edit-${key}"
                          name="${key}"
                          value="${study.features[key]}"
                          min="${min}"
                          max="${max}"
                          step="${step}"
                          required
                          aria-label="${label}"
                          aria-describedby="edit-${key}-range edit-${key}-error"
                        />
                        <small id="edit-${key}-range" class="muted">
                          ${key === 'diabetes_pedigree_function' ? 'Больше 0' : 'От ' + min} до
                          ${max} ${unit}
                        </small>
                        <small
                          id="edit-${key}-error"
                          class="field-error"
                          aria-live="polite"
                        ></small>
                      `
                    : html`
                        ${escape(study.features[key])} ${unit}
                      `}
                </dd>
              </div>
            `,
          )
          .join('')}
      </dl>
      <div class="divider"></div>
      <div class="model-mini-head">Прогнозы моделей</div>
      <div class="table-scroll">
        <table>
          <tbody id="detail-predictions">${predictionRows(study.predictions)}</tbody>
        </table>
      </div>
      <p id="detail-status" class="section-caption" role="status"></p>
      <button class="button small hidden" data-action="refresh-card" id="detail-retry">
        Обновить результаты
      </button>
      <form id="feedback-form">
        <fieldset class="feedback-fields" ${editable ? '' : 'disabled'}>
          <legend class="feedback-label">Фактический исход исследования</legend>
          <div class="feedback-choice">
            <label class="outcome-choice">
              <input
                type="radio"
                name="outcome"
                value="1"
                ${editable && study.feedback === 1 ? 'checked' : ''}
              />
              Подтвержден
            </label>
            <label class="outcome-choice">
              <input
                type="radio"
                name="outcome"
                value="0"
                ${editable && study.feedback === 0 ? 'checked' : ''}
              />
              Не подтвержден
            </label>
          </div>
        </fieldset>
        <div id="feedback-error" class="inline-error hidden" role="alert"></div>
        <div class="modal-actions">
          <button type="button" class="button" data-action="close">Закрыть</button>
          ${editable ? '<button type="submit" class="button primary">Сохранить</button>' : ''}
        </div>
      </form>
    </div>
  `;
}
