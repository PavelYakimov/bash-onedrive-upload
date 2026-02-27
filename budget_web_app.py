#!/usr/bin/env python3
"""Simple web app for project budget tracking with Google Sheets import/export helpers."""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterable

DB_PATH = os.environ.get("BUDGET_APP_DB", "budget-app.db")
HOST = os.environ.get("BUDGET_APP_HOST", "127.0.0.1")
PORT = int(os.environ.get("BUDGET_APP_PORT", "8000"))


@dataclass
class FlashMessage:
    level: str
    text: str


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                planned_budget REAL NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                description TEXT NOT NULL,
                expense_date TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id)
            );
            """
        )


def html_page(title: str, body: str, message: FlashMessage | None = None) -> bytes:
    msg_html = ""
    if message:
        color = "#d4edda" if message.level == "ok" else "#f8d7da"
        msg_html = f"<div style='padding:10px;background:{color};margin-bottom:12px'>{message.text}</div>"

    html = f"""<!doctype html>
<html lang='ru'>
<head>
  <meta charset='utf-8'/>
  <title>{title}</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; max-width: 1100px; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 10px; }}
    th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
    th {{ background: #f3f3f3; }}
    form {{ margin: 12px 0; padding: 12px; border: 1px solid #ddd; border-radius: 6px; }}
    input {{ margin: 4px 8px 4px 0; padding: 6px; }}
    button {{ padding: 7px 10px; }}
    .row {{ display: flex; gap: 16px; align-items: flex-end; flex-wrap: wrap; }}
  </style>
</head>
<body>
  <h1>{title}</h1>
  {msg_html}
  {body}
</body>
</html>
"""
    return html.encode("utf-8")


def parse_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise ValueError(f"Некорректное число: {value}") from exc
    if number < 0:
        raise ValueError("Значение не может быть отрицательным")
    return number


def query_projects() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT p.id, p.name, p.planned_budget, p.created_at,
                   COALESCE(SUM(e.amount), 0) AS spent,
                   p.planned_budget - COALESCE(SUM(e.amount), 0) AS remaining
            FROM projects p
            LEFT JOIN expenses e ON e.project_id = p.id
            GROUP BY p.id
            ORDER BY p.created_at DESC, p.id DESC
            """
        ).fetchall()


def query_project(project_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT p.id, p.name, p.planned_budget, p.created_at,
                   COALESCE(SUM(e.amount), 0) AS spent,
                   p.planned_budget - COALESCE(SUM(e.amount), 0) AS remaining
            FROM projects p
            LEFT JOIN expenses e ON e.project_id = p.id
            WHERE p.id = ?
            GROUP BY p.id
            """,
            (project_id,),
        ).fetchone()


def query_expenses(project_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, amount, description, expense_date FROM expenses WHERE project_id = ? ORDER BY expense_date DESC, id DESC",
            (project_id,),
        ).fetchall()


def import_rows(rows: Iterable[dict[str, str]]) -> tuple[int, int]:
    projects_added = 0
    expenses_added = 0

    with get_conn() as conn:
        for row in rows:
            name = (row.get("Project") or "").strip()
            if not name:
                continue

            budget_text = (row.get("Budget") or "").strip()
            created_at = (row.get("CreatedAt") or date.today().isoformat()).strip()
            expense_amount = (row.get("ExpenseAmount") or "").strip()
            expense_description = (row.get("ExpenseDescription") or "").strip()
            expense_date = (row.get("ExpenseDate") or date.today().isoformat()).strip()

            if budget_text:
                budget = parse_float(budget_text)
                cur = conn.execute("SELECT id FROM projects WHERE name = ?", (name,)).fetchone()
                if not cur:
                    conn.execute(
                        "INSERT INTO projects(name, planned_budget, created_at) VALUES(?,?,?)",
                        (name, budget, created_at),
                    )
                    projects_added += 1

            if expense_amount:
                amount = parse_float(expense_amount)
                project = conn.execute("SELECT id FROM projects WHERE name = ?", (name,)).fetchone()
                if not project:
                    continue
                conn.execute(
                    "INSERT INTO expenses(project_id, amount, description, expense_date) VALUES(?,?,?,?)",
                    (project[0], amount, expense_description or "Imported expense", expense_date),
                )
                expenses_added += 1

    return projects_added, expenses_added


def export_rows() -> list[dict[str, str]]:
    with get_conn() as conn:
        project_rows = conn.execute("SELECT name, planned_budget, created_at FROM projects ORDER BY id").fetchall()
        rows: list[dict[str, str]] = []
        for p in project_rows:
            rows.append(
                {
                    "Project": p["name"],
                    "Budget": f"{p['planned_budget']:.2f}",
                    "CreatedAt": p["created_at"],
                    "ExpenseAmount": "",
                    "ExpenseDescription": "",
                    "ExpenseDate": "",
                }
            )
            expense_rows = conn.execute(
                "SELECT amount, description, expense_date FROM expenses WHERE project_id=(SELECT id FROM projects WHERE name=?) ORDER BY id",
                (p["name"],),
            ).fetchall()
            for e in expense_rows:
                rows.append(
                    {
                        "Project": p["name"],
                        "Budget": "",
                        "CreatedAt": "",
                        "ExpenseAmount": f"{e['amount']:.2f}",
                        "ExpenseDescription": e["description"],
                        "ExpenseDate": e["expense_date"],
                    }
                )
        return rows


class BudgetHandler(BaseHTTPRequestHandler):
    def _parse_qs(self) -> dict[str, str]:
        parsed = urllib.parse.urlparse(self.path)
        data = urllib.parse.parse_qs(parsed.query)
        return {k: v[-1] for k, v in data.items() if v}

    def _body_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(length).decode("utf-8")
        parsed = urllib.parse.parse_qs(payload)
        return {k: v[-1] for k, v in parsed.items() if v}

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            self.render_index()
            return
        if path == "/project":
            self.render_project()
            return
        if path == "/export.csv":
            self.handle_export_csv()
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/projects":
                self.handle_add_project()
            elif path == "/expenses":
                self.handle_add_expense()
            elif path == "/import-google":
                self.handle_import_google()
            elif path == "/export-google":
                self.handle_export_google()
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        except ValueError as err:
            self._redirect(f"/?status=err&message={urllib.parse.quote(str(err))}")

    def render_index(self) -> None:
        qs = self._parse_qs()
        message = None
        if "status" in qs and "message" in qs:
            message = FlashMessage("ok" if qs["status"] == "ok" else "err", qs["message"])

        rows = query_projects()
        table_rows = "".join(
            f"<tr><td><a href='/project?id={r['id']}'>{r['name']}</a></td><td>{r['planned_budget']:.2f}</td><td>{r['spent']:.2f}</td><td>{r['remaining']:.2f}</td><td>{r['created_at']}</td></tr>"
            for r in rows
        )
        body = f"""
        <p>Простое веб-приложение для учёта бюджетов проектов.</p>
        <form method='post' action='/projects'>
          <h3>Добавить проект</h3>
          <div class='row'>
            <label>Название<br/><input name='name' required /></label>
            <label>Плановый бюджет<br/><input name='planned_budget' required /></label>
            <label>Дата создания<br/><input name='created_at' value='{date.today().isoformat()}' /></label>
            <button type='submit'>Добавить</button>
          </div>
        </form>

        <form method='post' action='/import-google'>
          <h3>Импорт из Google Sheets</h3>
          <p>Укажите CSV-ссылку опубликованного листа (File → Share → Publish to web → CSV).</p>
          <div class='row'>
            <label>CSV URL<br/><input style='min-width:700px' name='csv_url' required /></label>
            <button type='submit'>Импортировать</button>
          </div>
        </form>

        <form method='post' action='/export-google'>
          <h3>Экспорт в Google Sheets</h3>
          <p>Укажите URL вашего Google Apps Script Web App, принимающего JSON POST.</p>
          <div class='row'>
            <label>Apps Script URL<br/><input style='min-width:700px' name='apps_script_url' required /></label>
            <button type='submit'>Экспортировать</button>
          </div>
        </form>

        <p><a href='/export.csv'>Скачать CSV локально</a></p>

        <h3>Проекты</h3>
        <table>
          <thead><tr><th>Проект</th><th>Бюджет</th><th>Потрачено</th><th>Остаток</th><th>Создан</th></tr></thead>
          <tbody>{table_rows}</tbody>
        </table>
        """
        payload = html_page("Project Budget Manager", body, message)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def render_project(self) -> None:
        qs = self._parse_qs()
        project_id = int(qs.get("id", "0"))
        project = query_project(project_id)
        if not project:
            self.send_error(HTTPStatus.NOT_FOUND, "Project not found")
            return
        expenses = query_expenses(project_id)
        expense_rows = "".join(
            f"<tr><td>{e['expense_date']}</td><td>{e['amount']:.2f}</td><td>{e['description']}</td></tr>" for e in expenses
        )
        body = f"""
        <p><a href='/'>← Назад к списку</a></p>
        <h2>{project['name']}</h2>
        <p><b>Бюджет:</b> {project['planned_budget']:.2f} | <b>Потрачено:</b> {project['spent']:.2f} | <b>Остаток:</b> {project['remaining']:.2f}</p>
        <form method='post' action='/expenses'>
          <input type='hidden' name='project_id' value='{project['id']}' />
          <div class='row'>
            <label>Сумма<br/><input name='amount' required /></label>
            <label>Описание<br/><input name='description' required /></label>
            <label>Дата<br/><input name='expense_date' value='{date.today().isoformat()}' /></label>
            <button type='submit'>Добавить расход</button>
          </div>
        </form>
        <table>
          <thead><tr><th>Дата</th><th>Сумма</th><th>Описание</th></tr></thead>
          <tbody>{expense_rows}</tbody>
        </table>
        """
        payload = html_page(f"Проект: {project['name']}", body)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def handle_add_project(self) -> None:
        form = self._body_form()
        name = (form.get("name") or "").strip()
        if not name:
            raise ValueError("Название проекта обязательно")
        budget = parse_float((form.get("planned_budget") or "").strip())
        created_at = (form.get("created_at") or date.today().isoformat()).strip()

        with get_conn() as conn:
            try:
                conn.execute(
                    "INSERT INTO projects(name, planned_budget, created_at) VALUES(?,?,?)",
                    (name, budget, created_at),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Проект с таким названием уже существует") from exc

        self._redirect("/?status=ok&message=Проект+добавлен")

    def handle_add_expense(self) -> None:
        form = self._body_form()
        project_id = int(form.get("project_id", "0"))
        amount = parse_float((form.get("amount") or "").strip())
        description = (form.get("description") or "").strip()
        expense_date = (form.get("expense_date") or date.today().isoformat()).strip()
        if not description:
            raise ValueError("Описание расхода обязательно")

        with get_conn() as conn:
            project = conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
            if not project:
                raise ValueError("Проект не найден")
            conn.execute(
                "INSERT INTO expenses(project_id, amount, description, expense_date) VALUES(?,?,?,?)",
                (project_id, amount, description, expense_date),
            )

        self._redirect(f"/project?id={project_id}")

    def handle_import_google(self) -> None:
        form = self._body_form()
        csv_url = (form.get("csv_url") or "").strip()
        if not csv_url:
            raise ValueError("Нужен CSV URL")

        try:
            with urllib.request.urlopen(csv_url, timeout=20) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise ValueError(f"Не удалось скачать CSV: {exc}") from exc

        reader = csv.DictReader(io.StringIO(payload))
        projects, expenses = import_rows(reader)
        self._redirect(
            f"/?status=ok&message={urllib.parse.quote(f'Импорт завершён: проектов {projects}, расходов {expenses}') }"
        )

    def handle_export_google(self) -> None:
        form = self._body_form()
        url = (form.get("apps_script_url") or "").strip()
        if not url:
            raise ValueError("Нужен URL Apps Script")

        body = json.dumps({"rows": export_rows()}).encode("utf-8")
        req = urllib.request.Request(url=url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                status_code = response.getcode()
        except urllib.error.URLError as exc:
            raise ValueError(f"Ошибка экспорта: {exc}") from exc

        if status_code < 200 or status_code >= 300:
            raise ValueError(f"Google Script вернул код {status_code}")

        self._redirect("/?status=ok&message=Экспорт+в+Google+Sheets+завершён")

    def handle_export_csv(self) -> None:
        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=["Project", "Budget", "CreatedAt", "ExpenseAmount", "ExpenseDescription", "ExpenseDate"],
        )
        writer.writeheader()
        writer.writerows(export_rows())
        payload = output.getvalue().encode("utf-8")

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", "attachment; filename=budget-export.csv")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def run() -> None:
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), BudgetHandler)
    print(f"Budget app started: http://{HOST}:{PORT} (db: {DB_PATH})")
    server.serve_forever()


if __name__ == "__main__":
    run()
