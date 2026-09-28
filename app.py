"""Local CSV product importer. Python 3.10+, standard library only."""

import argparse
from collections import Counter
from contextlib import closing
import csv
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import re
import secrets
import sqlite3


FIELDS = ["code", "name", "price", "stock"]
MAX_BYTES = 5 * 1024 * 1024
csv.field_size_limit(MAX_BYTES)


def preview(data):
    """Parse once, preserve physical line numbers, then flag ALL duplicates."""
    if len(data) > MAX_BYTES:
        raise ValueError("文件不能超过 5 MiB")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("请将 CSV 保存为 UTF-8 编码（支持 BOM）") from exc
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = next(reader, None)
    except csv.Error as exc:
        raise ValueError(f"表头 CSV 格式错误：{exc}") from exc
    if header != FIELDS:
        raise ValueError("表头必须严格为 code,name,price,stock，且顺序一致")

    rows = []
    while True:
        start = reader.line_num + 1
        try:
            raw = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            rows.append({"line": start, "end_line": reader.line_num,
                         "values": dict.fromkeys(FIELDS, ""),
                         "errors": [{"field": "CSV", "reason": str(exc)}]})
            continue
        values = dict(zip(FIELDS, (value.strip() for value in raw)))
        values = {field: values.get(field, "") for field in FIELDS}
        errors = []
        if len(raw) != 4:
            errors.append({"field": "CSV", "reason": f"应有 4 列，实际 {len(raw)} 列"})
        for field in ("code", "name"):
            if not values[field]:
                errors.append({"field": field, "reason": "不能为空"})
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", values["price"]):
            errors.append({"field": "price", "reason": "须为非负数，最多两位小数（如 0、12.50）"})
        if not re.fullmatch(r"[0-9]+", values["stock"]):
            errors.append({"field": "stock", "reason": "须为非负整数"})
        rows.append({"line": start, "end_line": reader.line_num,
                     "values": values, "errors": errors})

    counts = Counter(row["values"]["code"] for row in rows if row["values"]["code"])
    for row in rows:
        code = row["values"]["code"]
        if code and counts[code] > 1:
            row["errors"].append({"field": "code", "reason": f"文件内编码重复，共 {counts[code]} 行"})
    valid = sum(not row["errors"] for row in rows)
    return {"rows": rows, "total": len(rows), "valid": valid, "invalid": len(rows) - valid}


def init_db(path):
    with closing(sqlite3.connect(path)) as db, db:
        # Store numeric text exactly: no float rounding or SQLite integer overflow.
        db.execute("""CREATE TABLE IF NOT EXISTS products (
            code TEXT PRIMARY KEY NOT NULL,
            name TEXT NOT NULL,
            price TEXT NOT NULL,
            stock TEXT NOT NULL
        )""")


def import_csv(path, data):
    result = preview(data)  # Never trust validation supplied by the browser.
    inserted = updated = unchanged = 0
    with closing(sqlite3.connect(path, timeout=10)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        for row in result["rows"]:
            if row["errors"]:
                continue
            value = row["values"]
            product = (value["name"], format(Decimal(value["price"]), ".2f"),
                       value["stock"].lstrip("0") or "0")
            old = db.execute("SELECT name, price, stock FROM products WHERE code = ?",
                             (value["code"],)).fetchone()
            if old == product:
                unchanged += 1
                continue
            inserted += old is None
            updated += old is not None
            db.execute("""INSERT INTO products (code, name, price, stock) VALUES (?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                name=excluded.name, price=excluded.price, stock=excluded.stock""",
                       (value["code"], *product))
        total = db.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    return {"inserted": inserted, "updated": updated, "unchanged": unchanged,
            "skipped": result["invalid"], "total_products": total}


PAGE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>商品 CSV 导入</title>
<style>
body{font:16px/1.6 system-ui,sans-serif;margin:0;background:#f5f6f8;color:#202b3a}
main{max-width:1150px;margin:40px auto;padding:24px;background:white;border-radius:12px}
h1{margin:0}p{color:#526174}button,input{font:inherit}button{padding:8px 18px;border:0;
border-radius:6px;background:#245ac7;color:white;cursor:pointer}button:disabled{opacity:.5;cursor:default}
input{max-width:100%}.actions{display:flex;gap:16px;align-items:center;flex-wrap:wrap}
.scroll{overflow:auto}table{width:100%;border-collapse:collapse;margin-top:20px}
th,td{text-align:left;border-bottom:1px solid #dde2e8;padding:10px;white-space:pre-wrap;
overflow-wrap:anywhere;min-width:65px}th{background:#edf1f7}.bad{background:#fff2f1}
.field-error{color:#a42020;font-weight:600}#status{white-space:pre-wrap;color:#233f68}
caption{text-align:left;font-weight:600}small{display:block;color:#526174}
</style></head><body><main>
<h1>商品 CSV 导入</h1>
<p>选择文件后自动预览，确认后仅导入校验通过的记录。已有编码更新，新编码新增。</p>
<p>表头：<code>code,name,price,stock</code> · UTF-8（可带 BOM）· 最大 5 MiB</p>
<div class="actions"><label for="file">CSV 文件</label>
<input id="file" type="file" accept=".csv,text/csv">
<button id="import" disabled>导入有效记录</button></div>
<p id="status" role="status" aria-live="polite">请选择文件。</p>
<small>行号为原文件物理行号，包含表头；跨行名称显示起止行号。错误行不会导入。</small>
<div class="scroll"><table><caption id="summary">文件预览</caption>
<thead><tr><th scope="col">原始行号</th><th scope="col">code</th><th scope="col">name</th>
<th scope="col">price</th><th scope="col">stock</th><th scope="col">校验结果</th></tr></thead>
<tbody id="rows"></tbody></table></div>
</main><script>
const picker = document.querySelector('#file'), button = document.querySelector('#import');
const status = document.querySelector('#status'), rows = document.querySelector('#rows');
let selected = null;
async function send(path, file) {
  const response = await fetch(path, {method:'POST', headers:{
    'Content-Type':'application/octet-stream', 'X-Import-Token':'__TOKEN__'}, body:file});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '请求失败');
  return result;
}
function render(result) {
  rows.replaceChildren();
  document.querySelector('#summary').textContent =
    `共 ${result.total} 条记录 · 有效 ${result.valid} 条 · 错误 ${result.invalid} 条`;
  const fragment = document.createDocumentFragment();
  for (const row of result.rows) {
    const tr = document.createElement('tr');
    if (row.errors.length) tr.className = 'bad';
    const line = row.line === row.end_line ? String(row.line) : `${row.line}–${row.end_line}`;
    const cells = [line, ...['code','name','price','stock'].map(k=>row.values[k]),
      row.errors.map(e=>`${e.field}：${e.reason}`).join('\n') || '通过'];
    cells.forEach((value, index) => {
      const td = document.createElement('td');
      td.textContent = value;
      if (row.errors.some(e=>e.field === ['','code','name','price','stock'][index]))
        td.className = 'field-error';
      tr.append(td);
    });
    fragment.append(tr);
  }
  rows.append(fragment);
}
picker.addEventListener('change', async () => {
  selected = null; button.disabled = true; rows.replaceChildren();
  document.querySelector('#summary').textContent = '文件预览';
  const file = picker.files[0];
  if (!file) {status.textContent = '请选择文件。'; return;}
  if (file.size > 5*1024*1024) {status.textContent = '文件不能超过 5 MiB'; return;}
  picker.disabled = true; status.textContent = '正在解析并校验…';
  try {
    const result = await send('/api/preview', file);
    render(result); selected = file; button.disabled = result.valid === 0;
    status.textContent = result.valid ? '预览完成，可导入有效记录。' : '没有可导入的有效记录。';
  } catch (error) {status.textContent = error.message;}
  finally {picker.disabled = false;}
});
button.addEventListener('click', async () => {
  if (!selected) return;
  button.disabled = true; picker.disabled = true; status.textContent = '正在导入…';
  try {
    const r = await send('/api/import', selected);
    status.textContent = `导入完成：新增 ${r.inserted}，更新 ${r.updated}，未变化 ${r.unchanged}，`+
      `跳过错误 ${r.skipped}。数据库共有 ${r.total_products} 个商品。`;
  } catch (error) {status.textContent = error.message;}
  finally {button.disabled = false; picker.disabled = false;}
});
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, body, content_type="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else json.dumps(
            body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self.reply(200, PAGE.replace("__TOKEN__", self.server.token), "text/html; charset=utf-8")
        else:
            self.reply(404, {"error": "页面不存在"})

    def do_POST(self):
        if self.path not in ("/api/preview", "/api/import"):
            self.reply(404, {"error": "接口不存在"})
            return
        if not secrets.compare_digest(self.headers.get("X-Import-Token", "").encode("utf-8"),
                                      self.server.token.encode("ascii")):
            self.reply(403, {"error": "请刷新页面后重试"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_BYTES:
                raise ValueError("请选择非空且不超过 5 MiB 的 CSV 文件")
            self.connection.settimeout(30)
            data = self.rfile.read(size)
            if len(data) != size:
                raise ValueError("文件上传不完整，请重试")
            result = preview(data) if self.path == "/api/preview" else import_csv(self.server.db, data)
            self.reply(200, result)
        except ValueError as exc:
            self.reply(400, {"error": str(exc)})
        except sqlite3.Error:
            self.reply(500, {"error": "数据库写入失败，本次事务已回滚，请检查磁盘或稍后重试"})
        except TimeoutError:
            self.reply(408, {"error": "上传超时，请重试"})


def make_server(db, port):
    init_db(db)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.db = db
    server.token = secrets.token_hex(32)
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", type=Path, default=Path(__file__).with_name("products.db"))
    args = parser.parse_args()
    with make_server(args.db, args.port) as server:
        print(f"Open http://127.0.0.1:{server.server_port}  |  Database: {args.db.resolve()}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
