"""Create an offline PDF from Trivy JSON and the recorded applicability decisions.

Reporting never grants a release exception and never downloads advisory content.
Install requirements-security.txt only on the reporting / development machine.
"""

import argparse
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from scripts.vulnerability_review import blocks, matching_review

LABELS = {
    "blocked": "Блокирует выпуск",
    "not_affected": "Не затрагивает проверенную сборку",
    "no_fix": "Исправление не указано; требует разбора",
    "below_gate": "Ниже порога блокировки; требует разбора",
    "incomplete": "Оценка не завершена",
}
ORDER = {name: index for index, name in enumerate(LABELS)}
SEVERITY_ORDER = {
    name: index
    for index, name in enumerate(("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"))
}


def read_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def collect(root):
    """Retain every package occurrence, including partial scanner results."""
    directories = sorted(
        {
            p.parent
            for pattern in ("report.json", "scan-status.json")
            for p in root.rglob(pattern)
        }
    )
    images, findings = [], []
    for directory in directories:
        report = read_json(directory / "report.json")
        status = read_json(directory / "scan-status.json")
        complete = (
            status.get("state") == "complete"
            and status.get("image_id") == report.get("ResolvedImageId")
            and bool(status.get("image_id"))
            and isinstance(report.get("Results"), list)
        )
        image = {
            "name": status.get("image")
            or report.get("RequestedImage")
            or directory.name,
            "digest": report.get("ResolvedImageId", "Не определён"),
            "complete": complete,
            "time": status.get("completed_at")
            or status.get("started_at", "Не указано"),
            "scanner": status.get("scanner", "Не указан"),
            "error": status.get("error", ""),
            "source": str(directory.relative_to(root) / "report.json"),
        }
        images.append(image)
        reviews = report.get("ApplicabilityReviews", []) if complete else []
        for result in report.get("Results", []):
            for finding in result.get("Vulnerabilities", []) or []:
                review = matching_review(result, finding, reviews)
                if not complete:
                    decision = "incomplete"
                elif review:
                    decision = "not_affected"
                elif blocks(result, finding, reviews):
                    decision = "blocked"
                elif not finding.get("FixedVersion"):
                    decision = "no_fix"
                else:
                    decision = "below_gate"
                findings.append(
                    {
                        "image": image,
                        "target": result.get("Target", ""),
                        "finding": finding,
                        "review": review,
                        "decision": decision,
                    }
                )
    findings.sort(
        key=lambda row: (
            ORDER[row["decision"]],
            SEVERITY_ORDER.get(row["finding"].get("Severity"), 4),
            row["finding"].get("VulnerabilityID", ""),
            row["finding"].get("PkgName", ""),
        )
    )
    return images, findings


def group_findings(findings):
    """Describe a CVE once when identical evidence applies to several packages."""
    groups = {}
    for row in findings:
        finding = row["finding"]
        key = tuple(
            finding.get(field, "")
            for field in (
                "VulnerabilityID",
                "Severity",
                "Title",
                "Description",
                "PrimaryURL",
            )
        ) + (row["decision"], json.dumps(row["review"], sort_keys=True))
        groups.setdefault(key, []).append(row)
    return list(groups.values())


def verdict(images, findings, scan_outcome):
    if not images or not all(image["complete"] for image in images):
        return "Проверка не завершена. Разрешение на выпуск не подтверждено."
    if any(row["decision"] == "blocked" for row in findings):
        return "Обнаружены находки, блокирующие выпуск проверяемых образов."
    if scan_outcome != "success":
        return (
            "Не все этапы сканирования успешны. Разрешение на выпуск не подтверждено."
        )
    return "Блокирующих находок нет по действующей политике."


def register_fonts():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    candidates = [
        (
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ),
        (
            Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/arial.ttf",
            Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/arialbd.ttf",
        ),
    ]
    for regular, bold in candidates:
        if regular.is_file() and bold.is_file():
            pdfmetrics.registerFont(TTFont("Report", str(regular)))
            pdfmetrics.registerFont(TTFont("ReportBold", str(bold)))
            pdfmetrics.registerFontFamily("Report", normal="Report", bold="ReportBold")
            return
    raise RuntimeError(
        "Install fonts-dejavu-core to render Cyrillic in the security PDF"
    )


def text(value):
    # Advisory text is untrusted: no active ReportLab markup or control characters.
    value = "".join(char for char in str(value) if char >= " " or char in "\n\t")
    return escape(value).replace("\n", "<br/>")


def write_pdf(root, output, *, scan_outcome="unknown", commit="", run_url=""):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    images, findings = collect(root)
    register_fonts()
    ink, muted = colors.HexColor("#18304c"), colors.HexColor("#627389")
    body = ParagraphStyle(
        "body",
        fontName="Report",
        fontSize=9,
        leading=13,
        textColor=ink,
        spaceAfter=5,
        splitLongWords=True,
    )
    small = ParagraphStyle(
        "small", parent=body, fontSize=7.5, leading=10, textColor=muted
    )
    heading = ParagraphStyle(
        "heading",
        parent=body,
        fontName="ReportBold",
        fontSize=13,
        leading=17,
        spaceBefore=13,
        spaceAfter=7,
        keepWithNext=True,
    )
    title = ParagraphStyle("title", parent=heading, fontSize=22, leading=27)
    story = []

    def add(value, style=body):
        story.append(Paragraph(text(value), style))

    def field(label, value, style=body):
        story.append(Paragraph(f"<b>{text(label)}</b> {text(value)}", style))

    add("Отчёт об уязвимостях", title)
    add("DiabetesPredict | Проверка контейнерных образов", small)
    add(
        "Сформирован: " + datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC"),
        small,
    )
    if commit:
        add("Коммит проверяющего workflow: " + commit, small)
    if run_url:
        add("Запуск: " + run_url, small)
    add(verdict(images, findings, scan_outcome), heading)
    add(
        "Отчёт относится только к перечисленным образам и состоянию базы сканера "
        "на момент проверки. Отсутствие блокировки не означает отсутствия уязвимостей. "
        "Работающие контейнеры эта проверка не останавливает."
    )
    add("Правило выпуска", heading)
    add(
        "HIGH и CRITICAL с указанным исправлением блокируют публикацию, кроме "
        "проверенных оценок «не затрагивает». Такая оценка действует только для "
        "конкретных CVE, пакета, версии, пути и SHA-256 бинарного файла до даты "
        "пересмотра. Неизученные находки не считаются неприменимыми."
    )
    add(
        "Решения в PDF зафиксированы на момент сканирования. Сам PDF не выдаёт "
        "разрешение на выпуск и не продлевает срок исключений.",
        small,
    )
    counts = Counter(row["decision"] for row in findings)
    rows = [[Paragraph("<b>Результат</b>", body), Paragraph("<b>Находок</b>", body)]]
    for key, label in LABELS.items():
        rows.append([Paragraph(text(label), body), Paragraph(str(counts[key]), body)])
    table = Table(rows, colWidths=[145 * mm, 25 * mm], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eaf0fc")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.white]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.append(table)
    add(
        f"Всего: {len(findings)} находок в {len(images)} образах. "
        "Одна CVE в разных пакетах или образах учитывается отдельно.",
        small,
    )
    add("Проверенные образы", heading)
    if not images:
        add("Результаты сканирования отсутствуют. Причина доступна в журнале workflow.")
    for index, image in enumerate(images, 1):
        field(f"{index}.", image["name"])
        field("Image ID:", image["digest"], small)
        field("Состояние:", "Завершено" if image["complete"] else "Не завершено")
        field("Время:", image["time"], small)
        field("Сканер:", image["scanner"], small)
        field("Исходный JSON:", image["source"], small)
        if image["error"]:
            field("Ошибка:", image["error"] + ". Подробности в журнале workflow.")
        story.append(Spacer(1, 3 * mm))

    add("Описание находок", heading)
    if not findings:
        add("Находок в доступных результатах нет. Состояние проверки указано выше.")
    for index, group in enumerate(group_findings(findings), 1):
        row = group[0]
        finding, review = row["finding"], row["review"]
        add(
            f"{index}. {finding.get('VulnerabilityID', 'Без идентификатора')} | "
            f"{finding.get('Severity', 'UNKNOWN')}",
            heading,
        )
        field("Решение:", LABELS[row["decision"]])
        for affected in group:
            package = affected["finding"]
            image_number = images.index(affected["image"]) + 1
            field(
                package.get("PkgName", "Не указан") + ":",
                package.get("InstalledVersion", "Не указана")
                + " → "
                + (package.get("FixedVersion") or "исправление не указано"),
            )
            add(f"Образ {image_number} | {affected['target']}", small)
        field(
            "Суть (описание источника):", finding.get("Title") or "Заголовок не указан"
        )
        add(
            finding.get("Description")
            or "Подробное описание отсутствует в базе сканера."
        )
        if review:
            field(
                "Почему не затрагивает:",
                review.get("reason") or review["justification"],
            )
            field("Доказательства в репозитории:", review["evidence"])
            field(
                "Проверено / пересмотр до:",
                review["reviewed"] + " / " + review["expires"],
            )
            field("SHA-256 бинарного файла:", review["binary_sha256"], small)
        elif row["decision"] == "blocked":
            add(
                "Нужно установить исправление либо документировать и проверить "
                "неприменимость этой находки к конкретной сборке перед выпуском."
            )
        else:
            add(
                "Отдельного подтверждения неприменимости нет. Требуется оценка "
                "пути выполнения и доступности уязвимой функции в приложении.",
                small,
            )
        source = finding.get("PrimaryURL", "")
        if source.startswith(("https://", "http://")):
            # Plain text URL also remains useful in printed copies.
            field("Источник:", source, small)

    def page(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#dbe3ee"))
        canvas.line(20 * mm, 17 * mm, A4[0] - 20 * mm, 17 * mm)
        canvas.setFont("Report", 7)
        canvas.setFillColor(muted)
        canvas.drawString(
            20 * mm, 12 * mm, "DiabetesPredict • Отчёт Trivy и оценка применимости"
        )
        canvas.drawRightString(A4[0] - 20 * mm, 12 * mm, str(document.page))
        canvas.restoreState()

    output.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(
        str(output),
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=23 * mm,
        title="Отчёт об уязвимостях DiabetesPredict",
        author="DiabetesPredict",
    ).build(story, onFirstPage=page, onLaterPages=page)
    return {"images": len(images), "findings": len(findings), "decisions": dict(counts)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scan-outcome", default="unknown")
    parser.add_argument("--commit", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--run-url", default="")
    args = parser.parse_args()
    print(
        json.dumps(
            write_pdf(
                args.input,
                args.output,
                scan_outcome=args.scan_outcome,
                commit=args.commit,
                run_url=args.run_url,
            )
        )
    )


if __name__ == "__main__":
    main()
